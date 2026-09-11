"""End-to-end tests for the self-hosted Test Series PDF microservice.

Spins up the real FastAPI app on 127.0.0.1 (ephemeral port) and verifies, for
10 / 55 / 100 / 200 / 300 questions x inline+keyonly modes:

  API accepts -> job created -> progress works -> done -> download is a valid,
  non-empty, openable PDF whose TEXT contains every question, option, answer,
  explanation and the Hindi/Unicode content -- plus layout sanity (no blank
  pages, no replacement chars, no stacked words) and job cleanup.

Also covers invalid payloads, unknown/traversal job ids, premature download,
stale-job reaping and a concurrent-request smoke test.
"""

from __future__ import annotations

import json
import random
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
import fitz  # noqa: E402  (PyMuPDF; already a project dependency)

import uvicorn  # noqa: E402

import pdf_service.app as appmod  # noqa: E402
from pdf_service.render import answer_letters  # noqa: E402

SIZES = (10, 55, 100, 200, 300)
POLL_DEADLINE = 300

_HINDI_BITS = [
    "भारत की राजधानी", "प्रश्न संख्या", "ज्ञानी व्यक्ति", "क्षेत्रीय भाषा",
    "त्रिशूल धारण", "श्रृंखला टूटना", "उत्तर प्रदेश", "लब्धप्रतिष्ठ",
]


def make_questions(n: int, seed: int) -> list[dict]:
    """Deterministic mixed English/Hindi question set with unique markers."""
    rng = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        hi = _HINDI_BITS[(i - 1) % len(_HINDI_BITS)]
        out.append({
            "question": (f"Q{i}-STEM-MARKER: What is {i} x {i + 1}? "
                         f"यह {hi} प्रश्न संख्या {i} है।"),
            "options": [f"Q{i}-OPT-{label}-MARKER {hi} {i * (k + 1)}"
                        for k, label in enumerate("ABCD")],
            "correct_option_id": ([0, 2] if i % 7 == 0 else (i % 4)),
            "explanation": ("" if i % 11 == 0
                            else f"Q{i}-EXPL-MARKER because {i} reasons. "
                                 f"व्याख्या {hi}।"),
        })
    assert all(0 <= (c if isinstance(c, int) else 0) < 4
               or all(0 <= x < 4 for x in c)
               for c in [q["correct_option_id"] for q in out])
    return out


class _LiveServer(unittest.TestCase):
    base_url: str = ""
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None
    _tmpdir: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="pdfsvc-test-")
        appmod.JOBS_DIR = Path(cls._tmpdir.name)  # isolate job files
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(appmod.app, host="127.0.0.1", port=port,
                                log_level="error", access_log=False)
        cls._server = uvicorn.Server(config)
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        cls.base_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base_url + "/healthz",
                                            timeout=5) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("Test PDF server did not start.")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._server is not None:
            cls._server.should_exit = True
        if cls._thread is not None:
            cls._thread.join(timeout=15)
        if cls._tmpdir is not None:
            cls._tmpdir.cleanup()
        super().tearDownClass()

    # -- HTTP helpers -------------------------------------------------
    def _get(self, path: str, timeout: int = 20):
        with urllib.request.urlopen(self.base_url + path,
                                    timeout=timeout) as r:
            return r.status, r.read()

    def _post(self, path: str, obj: dict, timeout: int = 60):
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())

    def _generate_and_download(self, questions: list[dict], mode: str,
                               title: str, quiz_names: list[str]) -> bytes:
        status, job = self._post("/api/generate", {
            "questions_json": questions,
            "institute_name": "Quiz Creator",
            "tagline": "Test Series",
            "exam_title": title,
            "solution_display": "inline" if mode == "inline" else "end",
            "quiz_names": quiz_names,
            "async": True,
        })
        self.assertEqual(status, 200, job)
        self.assertTrue(job.get("progress_url", "").startswith("/api/"))
        self.assertTrue(job.get("download_url", "").startswith("/api/"))
        deadline = time.time() + POLL_DEADLINE
        seen_progress = False
        while time.time() < deadline:
            s, body = self._get(job["progress_url"])
            self.assertEqual(s, 200, body)
            st = json.loads(body.decode())
            if st.get("progress", 0) > 0:
                seen_progress = True
            if st.get("status") == "done":
                break
            self.assertNotEqual(st.get("status"), "error",
                                st.get("error"))
            time.sleep(1.0)
        else:
            self.fail(f"Job {job['job_id']} never reached done.")
        self.assertTrue(seen_progress, "progress endpoint never advanced")
        s, pdf = self._get(job["download_url"], timeout=60)
        self.assertEqual(s, 200)
        self.assertTrue(pdf[:4] == b"%PDF", pdf[:20])
        self.assertGreater(len(pdf), 1000, "PDF suspiciously small")
        return pdf


class PdfContentCases(_LiveServer):
    """Full content verification per size x mode (10 API renders)."""

    def _check_pdf(self, pdf: bytes, questions: list[dict], mode: str,
                   title: str, quiz_names: list[str]) -> None:
        n = len(questions)
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            self.assertGreaterEqual(doc.page_count, 1)
            pages_text = [p.get_text() for p in doc]
            full = "\n".join(pages_text)
            # 1. branding / titles / sources
            self.assertIn("Journey for लबासना", full)
            self.assertIn("Test Series", full)
            self.assertIn(title, full)
            for name in quiz_names:
                self.assertIn(name, full)
            # 2. every question number + stem marker present
            for i in range(1, n + 1):
                self.assertIn(f"Q{i}.", full, f"missing Q{i}")
                self.assertIn(f"Q{i}-STEM-MARKER", full)
            # 3. options present (first/middle/last sampled fully)
            for i in (1, n // 2 or 1, n):
                for label in "ABCD":
                    self.assertIn(f"Q{i}-OPT-{label}-MARKER", full)
            # 4. answers preserved
            if mode == "inline":
                self.assertEqual(full.count("Answer:"), n,
                                 "inline must show one Answer per question")
                for i in (1, n // 2 or 1, n):
                    letters = answer_letters(
                        questions[i - 1]["correct_option_id"], 4)
                    self.assertIn(f"Answer: {letters}", full)
            else:
                self.assertIn("Answer Key", full)
                self.assertIn("Detailed Solutions", full)
                key_region = full.split("Answer Key", 1)[1].split(
                    "Detailed Solutions", 1)[0]
                for i in range(1, n + 1):
                    letters = answer_letters(
                        questions[i - 1]["correct_option_id"], 4)
                    self.assertIn(f"Q{i}", key_region)
                    self.assertIn(letters, key_region,
                                  f"key letters missing for Q{i}")
            # 5. explanations preserved (skipping intentionally empty ones)
            for i in range(1, n + 1):
                if questions[i - 1]["explanation"]:
                    self.assertIn(f"Q{i}-EXPL-MARKER", full)
            # 6. Hindi/Unicode preserved incl. conjuncts
            for snippet in ("प्रश्न संख्या", "ज्ञानी", "क्षेत्र", "उत्तर",
                            "व्याख्या", "श्रृंखला"):
                self.assertIn(snippet, full, snippet)
            # 7. layout sanity: no blank pages, no U+FFFD, sane word volume
            for idx, text in enumerate(pages_text):
                self.assertTrue(text.strip(), f"page {idx} is blank")
            self.assertNotIn("�", full)
            words = sum(len(t.split()) for t in pages_text)
            self.assertGreater(words, n * 8, "word volume too low")
            # 8. no stacked/overlapping words on sampled pages
            for page in list(doc)[:3]:
                self._assert_no_overlap(page)
        finally:
            doc.close()

    @staticmethod
    def _assert_no_overlap(page) -> None:
        words = page.get_text("words")  # x0,y0,x1,y1,word,...
        for i in range(len(words)):
            ax0, ay0, ax1, ay1 = words[i][:4]
            area_a = max(0.0, (ax1 - ax0) * (ay1 - ay0))
            if area_a <= 0:
                continue
            for j in range(i + 1, len(words)):
                bx0, by0, bx1, by1 = words[j][:4]
                ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
                iy = max(0.0, min(ay1, by1) - max(ay0, by0))
                inter = ix * iy
                area_b = max(0.0, (bx1 - bx0) * (by1 - by0))
                if area_b <= 0:
                    continue
                if inter / min(area_a, area_b) > 0.7:
                    raise AssertionError(
                        f"overlapping words on page {page.number}: "
                        f"{words[i][4]!r} vs {words[j][4]!r}")

    def _run_case(self, n: int, mode: str) -> None:
        questions = make_questions(n, seed=1000 + n)
        quiz_names = ["ALPHA-QUIZ", "BETA-QUIZ-लबासना"] if n > 10 else ["SOLO"]
        title = f"Stress {n}Q {mode} परीक्षा शीर्षक"
        started = time.time()
        pdf = self._generate_and_download(questions, mode, title, quiz_names)
        elapsed = time.time() - started
        print(f"\n  [{n}Q/{mode}] {len(pdf)} bytes in {elapsed:.1f}s", flush=True)
        # Bot polls for 180s max: every render must finish far inside that.
        self.assertLess(elapsed, 170, "render too slow for bot poll budget")
        self._check_pdf(pdf, questions, mode, title, quiz_names)

    def test_10_inline(self): self._run_case(10, "inline")
    def test_10_keyonly(self): self._run_case(10, "keyonly")
    def test_55_inline(self): self._run_case(55, "inline")
    def test_55_keyonly(self): self._run_case(55, "keyonly")
    def test_100_inline(self): self._run_case(100, "inline")
    def test_100_keyonly(self): self._run_case(100, "keyonly")
    def test_200_inline(self): self._run_case(200, "inline")
    def test_200_keyonly(self): self._run_case(200, "keyonly")
    def test_300_inline(self): self._run_case(300, "inline")
    def test_300_keyonly(self): self._run_case(300, "keyonly")


class PdfApiEdgeCases(_LiveServer):
    """Invalid payloads, unknown jobs, traversal, premature download."""

    def _post_status(self, obj: dict) -> int:
        try:
            status, _ = self._post("/api/generate", obj)
            return status
        except urllib.error.HTTPError as e:
            return e.code

    def _good_question(self) -> dict:
        return {"question": "Q?", "options": ["A", "B"],
                "correct_option_id": 0, "explanation": ""}

    def _base_payload(self, **over) -> dict:
        body = {"questions_json": [self._good_question()],
                "institute_name": "x", "tagline": "y",
                "exam_title": "t", "solution_display": "end",
                "quiz_names": [], "async": True}
        body.update(over)
        return body

    def test_rejects_empty_and_malformed(self):
        self.assertEqual(self._post_status({}), 422)
        self.assertEqual(
            self._post_status(self._base_payload(questions_json=[])), 422)
        self.assertEqual(
            self._post_status(self._base_payload(solution_display="nope")),
            422)
        bad_opts = self._good_question()
        bad_opts["options"] = ["only-one"]
        self.assertEqual(
            self._post_status(self._base_payload(questions_json=[bad_opts])),
            422)
        bad_idx = self._good_question()
        bad_idx["correct_option_id"] = 9
        self.assertEqual(
            self._post_status(self._base_payload(questions_json=[bad_idx])),
            422)
        bad_type = self._good_question()
        bad_type["correct_option_id"] = "B"
        self.assertEqual(
            self._post_status(self._base_payload(questions_json=[bad_type])),
            422)

    def test_rejects_too_many_questions(self):
        big = [self._good_question() for _ in range(2001)]
        self.assertEqual(
            self._post_status(self._base_payload(questions_json=big)), 422)

    def test_unknown_and_traversal_job_ids(self):
        import http.client
        for bad in ("doesnotexist123456", "..", "../x", "%2e%2e",
                    "x" * 100, "a/b", "a b"):
            for path in ("/api/progress/", "/api/download/"):
                try:
                    self._get(path + bad)
                    self.fail(f"{path + bad} unexpectedly 200")
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (404, 409),
                                  f"{path + bad} -> {e.code}")
                except http.client.InvalidURL:
                    pass  # client refuses to send it: equally safe

    def test_premature_download_is_409(self):
        from pdf_service.app import Job, manager
        job = Job(job_id="premature01", status="processing", total=5)
        manager._jobs[job.job_id] = job
        try:
            try:
                self._get("/api/download/premature01")
                self.fail("expected 409 for a processing job")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 409)
        finally:
            manager._jobs.pop(job.job_id, None)

    def test_concurrent_requests(self):
        results: dict[int, bytes] = {}
        errors: dict[int, str] = {}

        def worker(k: int) -> None:
            try:
                qs = make_questions(10, seed=5000 + k)
                results[k] = self._generate_and_download(
                    qs, "keyonly", f"Parallel {k}", [f"P{k}"])
            except Exception as exc:  # noqa: BLE001 -- collected, asserted
                errors[k] = repr(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=240)
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 3)
        for pdf in results.values():
            self.assertTrue(pdf[:4] == b"%PDF")


class PdfCleanupCases(unittest.TestCase):
    """TTL expiry, stale reaping and orphan-file sweeping (no server)."""

    def test_cleanup_reaps_and_sweeps(self):
        import pdf_service.app as appmod
        from pdf_service.app import Job, JobManager

        with tempfile.TemporaryDirectory(prefix="pdfsvc-clean-") as tmp:
            old_dir = appmod.JOBS_DIR
            appmod.JOBS_DIR = Path(tmp)
            try:
                mgr = JobManager()
                now = time.time()
                old = now - appmod.JOB_TTL_SECONDS - 10
                done = Job(job_id="a" * 32, status="done", total=3,
                           pdf_name="a" * 32 + ".pdf", created=old,
                           updated=old)
                (Path(tmp) / done.pdf_name).write_bytes(b"%PDF-fake")
                mgr._jobs[done.job_id] = done
                stale = Job(job_id="b" * 32, status="processing", total=3,
                            created=old, updated=old)
                mgr._jobs[stale.job_id] = stale
                orphan = Path(tmp) / "orphan.pdf"
                orphan.write_bytes(b"%PDF-orphan")
                ancient = old - 100
                import os
                os.utime(orphan, (ancient, ancient))

                report = mgr.cleanup_once(now=now)
                self.assertGreaterEqual(report["removed"], 2)
                self.assertEqual(report["stale_marked"], 1)
                self.assertNotIn(done.job_id, mgr._jobs)
                self.assertFalse((Path(tmp) / done.pdf_name).exists())
                self.assertFalse(orphan.exists())
                self.assertEqual(mgr._jobs[stale.job_id].status, "error")
                self.assertTrue(mgr._jobs[stale.job_id].error)
            finally:
                appmod.JOBS_DIR = old_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
