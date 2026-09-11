"""FastAPI front-end for the self-hosted Test Series PDF microservice.

Implements exactly the contract the Telegram bot (`reports.py`) expects:

  POST /api/generate            {"questions_json": [...], ...}
    -> 200 {"job_id": ..., "progress_url": "/api/progress/<id>",
            "download_url": "/api/download/<id>"}
  GET  /api/progress/<job_id>   -> {"status": queued|processing|done|error,
                                    "progress": 0..100, "error": ...?}
  GET  /api/download/<job_id>   -> application/pdf (200) once done
  GET  /healthz                  -> {"status": "ok"}

Safety properties: request limits, job-ID allow-listing (no path traversal),
bounded concurrency, stale-job reaping, TTL file cleanup, safe error messages.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .render import BRAND, render_testseries_pdf

logger = logging.getLogger("pdf_service")

# ---------------------------------------------------------------------------
# Limits & TTLs
# ---------------------------------------------------------------------------
MAX_QUESTIONS = 2000
MAX_OPTIONS = 10
MIN_OPTIONS = 2
MAX_BODY_BYTES = 20 * 1024 * 1024  # room for base64 logo/watermark images
MAX_ACTIVE_JOBS = 8
MAX_STORED_JOBS = 500
RENDER_WORKERS = 2
JOB_TTL_SECONDS = 3600          # done/error jobs + files are deleted after this
STALE_ACTIVE_SECONDS = 1800     # queued/processing older than this -> error
CLEANUP_INTERVAL_SECONDS = 300

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JOBS_DIR = DATA_DIR / "pdf_jobs"


# ---------------------------------------------------------------------------
# Request models (accept the bot's existing payload shape verbatim)
# ---------------------------------------------------------------------------
class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=6000)
    options: list[str] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    correct_option_id: Union[int, list[int]]
    explanation: str = Field(default="", max_length=12000)

    @field_validator("options")
    @classmethod
    def _non_empty_options(cls, values: list[str]) -> list[str]:
        cleaned = [(v or "")[:600] for v in values]
        if any(not v.strip() for v in cleaned):
            raise ValueError("options must not contain empty strings")
        return cleaned

    @field_validator("correct_option_id")
    @classmethod
    def _valid_indices(cls, value: Union[int, list[int]],
                       info) -> Union[int, list[int]]:
        data = info.data
        options = data.get("options", []) if isinstance(data, dict) else []
        count = len(options)
        ids = list(value) if isinstance(value, list) else [value]
        if not ids or not all(isinstance(i, int) and not isinstance(i, bool)
                              and 0 <= i < count for i in ids):
            raise ValueError(
                f"correct_option_id indices must be within 0..{count - 1}")
        return value


class GenerateIn(BaseModel):
    questions_json: list[QuestionIn] = Field(min_length=1,
                                             max_length=MAX_QUESTIONS)
    institute_name: str = Field(default="", max_length=200)
    tagline: str = Field(default="Test Series", max_length=200)
    exam_title: str = Field(default="Mock Test", max_length=400)
    solution_display: str = Field(default="end", max_length=20)
    quiz_names: list[str] = Field(default_factory=list, max_length=50)
    # Phase 4 M2: optional /newseries setup (identification, candidates,
    # branding, watermark, key/solutions/visuals, marks). Absent/empty
    # keeps the historical rendering untouched.
    series_setup: dict = Field(default_factory=dict)
    # NOTE: the bot also sends "async": true -- accepted and ignored
    # (generation here is always asynchronous).

    @field_validator("solution_display")
    @classmethod
    def _normalise_display(cls, value: str) -> Literal["inline", "end"]:
        low = (value or "").strip().lower()
        if low == "inline":
            return "inline"
        if low in ("end", "keyonly", "key_only"):
            return "end"
        raise ValueError("solution_display must be 'inline' or 'end'")


# ---------------------------------------------------------------------------
# Job manager (thread-safe, in-memory + one PDF file per finished job)
# ---------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    status: str = "queued"  # queued | processing | done | error
    progress: int = 0
    total: int = 0
    error: Optional[str] = None
    pdf_name: Optional[str] = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=RENDER_WORKERS,
                                        thread_name_prefix="pdf-render")

    # -- lifecycle ----------------------------------------------------
    def create(self, total: int) -> Optional[Job]:
        with self._lock:
            active = sum(1 for j in self._jobs.values()
                         if j.status in ("queued", "processing"))
            if active >= MAX_ACTIVE_JOBS:
                return None
            job = Job(job_id=uuid.uuid4().hex, total=total)
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated = time.time()

    def submit(self, job_id: str, payload: dict) -> None:
        self._pool.submit(_run_render, job_id, payload)

    def pdf_path(self, job: Job) -> Optional[Path]:
        if not job.pdf_name or not JOB_ID_RE.match(job.job_id):
            return None
        candidate = (JOBS_DIR / job.pdf_name).resolve()
        if candidate.parent != JOBS_DIR.resolve():
            return None  # defence in depth: never escape the jobs dir
        return candidate

    # -- reaping ------------------------------------------------------
    def cleanup_once(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        removed, stale_marked = 0, 0
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                age = now - job.created
                if job.status in ("done", "error") and age > JOB_TTL_SECONDS:
                    self._delete_file_locked(job)
                    del self._jobs[job_id]
                    removed += 1
                elif (job.status in ("queued", "processing")
                      and now - job.updated > STALE_ACTIVE_SECONDS):
                    job.status = "error"
                    job.error = ("Job timed out while rendering "
                                 "(stale worker). Please retry.")
                    job.updated = now
                    stale_marked += 1
            # Bound memory: drop oldest finished jobs beyond the cap.
            if len(self._jobs) > MAX_STORED_JOBS:
                finished = sorted(
                    (j for j in self._jobs.values()
                     if j.status in ("done", "error")),
                    key=lambda j: j.updated)
                for job in finished[:len(self._jobs) - MAX_STORED_JOBS]:
                    self._delete_file_locked(job)
                    del self._jobs[job.job_id]
                    removed += 1
        # Remove orphan PDFs with no job record (e.g. after a restart).
        try:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            with self._lock:
                known = {j.pdf_name for j in self._jobs.values()
                         if j.pdf_name}
            for path in JOBS_DIR.glob("*.pdf"):
                if path.name not in known:
                    try:
                        if now - path.stat().st_mtime > JOB_TTL_SECONDS:
                            path.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        pass
        except OSError:
            logger.warning("Job-dir cleanup sweep failed", exc_info=True)
        if removed or stale_marked:
            logger.info("Cleanup: removed=%d stale_marked=%d", removed,
                        stale_marked)
        return {"removed": removed, "stale_marked": stale_marked}

    def _delete_file_locked(self, job: Job) -> None:
        try:
            path = self.pdf_path(job)
            if path is not None:
                path.unlink(missing_ok=True)
        except OSError:
            pass


manager = JobManager()


def _run_render(job_id: str, payload: dict) -> None:
    started = time.time()
    try:
        manager.update(job_id, status="processing", progress=2)
        questions = payload["questions_json"]
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = JOBS_DIR / f"{job_id}.tmp.pdf"
        final_path = JOBS_DIR / f"{job_id}.pdf"

        def _progress(done: int, total: int) -> None:
            pct = 5 + int(90 * done / max(1, total))
            manager.update(job_id, progress=min(95, pct))

        stats = render_testseries_pdf(
            questions,
            exam_title=payload.get("exam_title") or "Mock Test",
            tagline=payload.get("tagline") or "Test Series",
            quiz_names=payload.get("quiz_names") or [],
            solution_display=payload.get("solution_display") or "end",
            series_setup=payload.get("series_setup") or None,
            output_path=tmp_path,
            progress_cb=_progress,
        )
        tmp_path.replace(final_path)
        manager.update(job_id, status="done", progress=100,
                       pdf_name=final_path.name)
        logger.info("Job %s done: %d questions, %d pages, %d bytes in %.1fs",
                    job_id, stats["questions"], stats["pages"],
                    stats["bytes"], time.time() - started)
    except Exception as exc:  # noqa: BLE001 -- must never kill the worker
        logger.exception("Job %s failed", job_id)
        safe = str(exc)[:300] or "PDF rendering failed."
        manager.update(job_id, status="error", error=safe)


def _cleanup_loop(stop: threading.Event) -> None:
    while not stop.wait(CLEANUP_INTERVAL_SECONDS):
        try:
            manager.cleanup_once()
        except Exception:  # noqa: BLE001 -- cleanup must never crash
            logger.exception("Cleanup sweep failed")


_cleanup_stop = threading.Event()
_cleanup_thread = threading.Thread(target=_cleanup_loop, args=(_cleanup_stop,),
                                   name="pdf-cleanup", daemon=True)
_cleanup_thread.start()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(title="Journey PDF Service", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "brand": BRAND, "version": __version__}

    @app.post("/api/generate")
    async def generate(body: GenerateIn, request: Request) -> JSONResponse:
        try:
            length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": f"Request body too large (>{MAX_BODY_BYTES} bytes)."},
                status_code=413)
        job = manager.create(total=len(body.questions_json))
        if job is None:
            return JSONResponse(
                {"error": "Server busy: too many active PDF jobs. "
                          "Please retry shortly."},
                status_code=429)
        manager.submit(job.job_id, body.model_dump())
        logger.info("Job %s queued: %d questions (%s)", job.job_id,
                    len(body.questions_json), body.solution_display)
        return JSONResponse({
            "job_id": job.job_id,
            "status": "queued",
            "progress_url": f"/api/progress/{job.job_id}",
            "download_url": f"/api/download/{job.job_id}",
        })

    @app.get("/api/progress/{job_id}")
    async def progress(job_id: str) -> JSONResponse:
        if not JOB_ID_RE.match(job_id or ""):
            return JSONResponse({"detail": "Unknown job."}, status_code=404)
        job = manager.get(job_id)
        if job is None:
            return JSONResponse({"detail": "Unknown or expired job."},
                                status_code=404)
        body: dict = {"job_id": job.job_id, "status": job.status,
                      "progress": job.progress, "total": job.total}
        if job.error:
            body["error"] = job.error
        return JSONResponse(body)

    @app.get("/api/download/{job_id}")
    async def download(job_id: str):
        if not JOB_ID_RE.match(job_id or ""):
            return JSONResponse({"detail": "Unknown job."}, status_code=404)
        job = manager.get(job_id)
        if job is None:
            return JSONResponse({"detail": "Unknown or expired job."},
                                status_code=404)
        if job.status != "done":
            return JSONResponse(
                {"detail": f"Job not ready (status={job.status}).",
                 "error": job.error},
                status_code=409)
        path = manager.pdf_path(job)
        if path is None or not path.is_file():
            return JSONResponse({"detail": "PDF file no longer available."},
                                status_code=404)
        return FileResponse(str(path), media_type="application/pdf",
                            filename=f"Journey-TestSeries-{job.job_id[:8]}.pdf")

    return app


app = create_app()
