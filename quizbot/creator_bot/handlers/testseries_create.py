"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.

Professional Test Series creation workflow (``/newseries``).

Additive feature: a button-driven, mobile-friendly wizard that collects a
complete test-series setup (title, subject, numbering, candidate boxes,
branding, watermark, settings, marks, identity) into a structured
:class:`TestSeriesConfig`, shows a final preview, and generates the PDF
through the EXISTING pipeline (:func:`reports._build_testseries_payload`
+ :func:`reports._generate_pdf_via_api`). The ``/testseries`` QID flow and
the direct-file flow are untouched; the PDF API contract is extended only
additively (optional ``series_setup``); no second polling client (wired
through the existing creator bridge).

UX rule: buttons for every Yes/No or predefined choice; free text only
where the user must actually type something; uploads only for images/files.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from io import BytesIO

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from quizbot.database import QuizRepository, get_db
from quizbot.shared import config
from quizbot.shared.utils import is_premium_user

from .. import state as creator_state
from ..ratelimit import ratelimit
from .reports import _TSR_LOCK, _build_testseries_payload, _generate_pdf_via_api
from .testseries_file import MAX_FILE_BYTES, md_escape, process_testseries_upload

logger = logging.getLogger(__name__)

CB_PREFIX = "tsc_"
MAX_IMAGE_BYTES = 5 * 1024 * 1024

SUBJECT_CHOICES = (
    "Geography", "History", "Polity", "Economy",
    "Environment", "Sci-Tech", "Art & Culture", "Intl. Relations",
)

# step -> short callback code (codes carry no underscores so that
# `tsc_<code>_<uid>_<value>` always parses unambiguously).
CB_CODE = {
    "intake": "in",
    "subject": "sj",
    "test_number": "tn",
    "booklet_series": "bs",
    "booklet_number": "bn",
    "cand_name": "cn",
    "cand_roll": "cr",
    "cand_regid": "cg",
    "cand_batch": "cb",
    "cand_date": "cd",
    "cand_candsig": "cs",
    "cand_evalsig": "ce",
    "logo": "lg",
    "watermark": "wm",
    "tagline": "tg",
    "answer_key": "ak",
    "solutions": "so",
    "visuals": "vi",
    "marks_correct": "mc",
    "marks_negative": "mn",
    "paper": "pp",
    "duration": "du",
    "test_code": "tc",
    "preview": "pv",
    "edit_menu": "em",
}
CB_STEP = {code: step for step, code in CB_CODE.items()}

CANDIDATE_FIELDS = (
    ("cand_name", "cand_name", "Candidate Name"),
    ("cand_roll", "cand_roll", "Roll Number"),
    ("cand_regid", "cand_regid", "Registration / Candidate ID"),
    ("cand_batch", "cand_batch", "Batch"),
    ("cand_date", "cand_date", "Date"),
    ("cand_candsig", "cand_candsig", "Candidate Signature"),
    ("cand_evalsig", "cand_evalsig", "Evaluator / Invigilator Signature"),
)

EDIT_SECTIONS = (
    ("title", "Title"),
    ("subject", "Subject"),
    ("numbers", "Test / Booklet No."),
    ("candidate", "Candidate fields"),
    ("branding", "Branding"),
    ("settings", "Test settings"),
    ("marks", "Marks"),
    ("identity", "Paper / Duration / Code"),
)
SECTION_START = {
    "title": "title",
    "subject": "subject",
    "numbers": "test_number",
    "candidate": "cand_name",
    "branding": "institute",
    "settings": "answer_key",
    "marks": "marks_correct",
    "identity": "paper",
}
SECTION_ENDS = {
    "title": {"title"},
    "subject": {"subject"},
    "numbers": {"booklet_number", "booklet_number_input"},
    "candidate": {"cand_evalsig"},
    "branding": {"tagline"},
    "settings": {"visuals"},
    "marks": {"marks_negative", "marks_negative_input"},
    "identity": {"test_code"},
}

LIMITS = {
    "title": 100, "subject": 60, "test_number": 20, "booklet_series": 10,
    "booklet_number": 20, "institute_name": 80, "watermark_text": 60,
    "tagline": 120, "paper": 40, "duration": 40, "test_code": 30,
}


# ─── Structured configuration ───────────────────────────────────────
@dataclass
class TestSeriesConfig:
    """Complete, validated setup for one test-series paper.

    Scalar fields only (images travel separately as user-scoped session
    bytes, never in this object), so :meth:`to_dict` is always JSON-safe.
    Derived fields (``total_questions``, ``max_marks``) are computed, never
    typed by the user.
    """

    title: str = ""
    subject: str = ""
    test_number_mode: str = "auto"  # auto | manual
    test_number: str = ""
    booklet_series: str = "A"
    booklet_number_mode: str = "auto"  # auto | manual
    booklet_number: str = ""
    cand_name: bool = False
    cand_roll: bool = False
    cand_regid: bool = False
    cand_batch: bool = False
    cand_date: bool = False
    cand_candsig: bool = False
    cand_evalsig: bool = False
    institute_name: str = ""
    logo_present: bool = False
    logo_bytes: int = 0
    watermark_mode: str = "none"  # none | text | image | both
    watermark_text: str = ""
    wm_image_present: bool = False
    wm_image_bytes: int = 0
    tagline: str = ""
    answer_key: bool = True
    solutions: bool = True
    visuals: str = "auto"  # auto | yes | no
    marks_correct: float = 2.0
    marks_negative: float = -0.66
    paper: str = ""
    duration: str = ""
    test_code: str = ""
    total_questions: int = 0
    max_marks: float = 0.0

    def enabled_candidate_fields(self) -> list[str]:
        return [label for _step, attr, label in CANDIDATE_FIELDS
                if getattr(self, attr)]

    def validate(self) -> list[str]:
        """Return a list of user-facing problems (empty == valid)."""
        problems: list[str] = []
        if not self.title.strip():
            problems.append("Title is missing.")
        elif len(self.title) > LIMITS["title"]:
            problems.append("Title is too long.")
        if len(self.subject) > LIMITS["subject"]:
            problems.append("Subject is too long.")
        if self.test_number_mode not in ("auto", "manual"):
            problems.append("Test number mode is invalid.")
        if self.test_number_mode == "manual" and not self.test_number.strip():
            problems.append("Manual test number is missing.")
        if not self.booklet_series.strip():
            problems.append("Booklet series is missing.")
        if self.booklet_number_mode not in ("auto", "manual"):
            problems.append("Booklet number mode is invalid.")
        if self.booklet_number_mode == "manual" and not self.booklet_number.strip():
            problems.append("Manual booklet number is missing.")
        if not self.institute_name.strip():
            problems.append("Institute name is missing.")
        if self.watermark_mode not in ("none", "text", "image", "both"):
            problems.append("Watermark choice is invalid.")
        if self.watermark_mode in ("text", "both") and not self.watermark_text.strip():
            problems.append("Watermark text is missing.")
        if self.watermark_mode in ("image", "both") and not self.wm_image_present:
            problems.append("Watermark image is missing.")
        if self.visuals not in ("auto", "yes", "no"):
            problems.append("Visual-aids choice is invalid.")
        if not (0 < self.marks_correct <= 100):
            problems.append("Correct-answer marks must be between 0 and 100.")
        if not (-100 <= self.marks_negative <= 0):
            problems.append("Negative marks must be between -100 and 0.")
        if self.total_questions <= 0:
            problems.append("No questions attached.")
        return problems

    def to_dict(self) -> dict:
        data = asdict(self)
        data["candidate_fields"] = self.enabled_candidate_fields()
        return data


def _derive_totals(quizzes: list[dict], marks_correct: float) -> tuple[int, float]:
    """Count usable (with-options) questions; max marks = total × correct."""
    total = 0
    for quiz in quizzes:
        for q in quiz.get("questions", []):
            if [o for o in q.get("options", []) if o]:
                total += 1
    return total, round(total * float(marks_correct), 2)


_CONFIG_FIELDS = {f.name for f in TestSeriesConfig.__dataclass_fields__.values()}


def build_config(session: dict) -> TestSeriesConfig:
    """Assemble + validate the config from a wizard session."""
    cfg = TestSeriesConfig(**{k: v for k, v in session.get("config", {}).items()
                              if k in _CONFIG_FIELDS})
    assets = session.get("assets", {})
    logo = assets.get("logo")
    cfg.logo_present = bool(logo)
    cfg.logo_bytes = len(logo) if logo else 0
    wm_image = assets.get("wm_image")
    cfg.wm_image_present = bool(wm_image)
    cfg.wm_image_bytes = len(wm_image) if wm_image else 0
    total, maximum = _derive_totals(session.get("quizzes", []), cfg.marks_correct)
    cfg.total_questions = total
    cfg.max_marks = maximum
    return cfg


def _b64_or_none(data: bytes | None) -> str | None:
    if not data:
        return None
    return base64.b64encode(bytes(data)).decode("ascii")


def build_series_setup(cfg: TestSeriesConfig, assets: dict) -> dict:
    """Map the wizard config + session images to the PDF `series_setup`.

    Pure helper (JSON-safe): automatic numbers with no typed value map to
    "" (the renderer omits those rows); images travel as base64 and stay
    job-scoped on the service (never written to public paths).
    """
    assets = assets or {}
    test_no = cfg.test_number if cfg.test_number_mode == "manual" else ""
    booklet_no = cfg.booklet_number if cfg.booklet_number_mode == "manual" else ""
    return {
        "subject": cfg.subject,
        "test_number": test_no,
        "booklet_series": cfg.booklet_series,
        "booklet_number": booklet_no,
        "paper": cfg.paper,
        "test_code": cfg.test_code,
        "duration": cfg.duration,
        "marks_correct": cfg.marks_correct,
        "marks_negative": cfg.marks_negative,
        "candidate_fields": cfg.enabled_candidate_fields(),
        "institute_name": cfg.institute_name,
        "logo_b64": _b64_or_none(assets.get("logo")),
        "watermark_mode": cfg.watermark_mode,
        "watermark_text": cfg.watermark_text,
        "wm_image_b64": _b64_or_none(assets.get("wm_image")),
        "answer_key": cfg.answer_key,
        "solutions": cfg.solutions,
        "visuals": cfg.visuals,
    }


# ─── Session helpers ────────────────────────────────────────────────
def _fresh_session() -> dict:
    return {"step": "intake", "config": {}, "assets": {}, "quizzes": [],
            "source": None, "edit_return": False, "edit_section": None,
            "ts": time.time()}


def _cancel_kb(uid: int, step: str) -> InlineKeyboardMarkup:
    code = CB_CODE.get(step, "xx")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel",
                             callback_data=f"{CB_PREFIX}{code}_{uid}_cancel"),
    ]])


def _cb(uid: int, step: str, label: str, value: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        label, callback_data=f"{CB_PREFIX}{CB_CODE[step]}_{uid}_{value}")


def _yn_kb(uid: int, step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_cb(uid, step, "Yes", "1"), _cb(uid, step, "No", "0")],
        [InlineKeyboardButton(
            "❌ Cancel",
            callback_data=f"{CB_PREFIX}{CB_CODE[step]}_{uid}_cancel")],
    ])


# ─── Step prompts (pure text + keyboards) ───────────────────────────
def _prompt(step: str, uid: int, session: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render the prompt for `step`. Never raises for known steps."""
    cancel = _cancel_kb(uid, step)
    if step == "intake":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "intake", "📄 Send question file", "file"),
             _cb(uid, "intake", "🔢 Quiz IDs", "qids")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}in_{uid}_cancel")],
        ])
        return ("**New Test Series** 📝\n\nWhere are your questions?",
                kb)
    if step == "intake_file":
        return ("📄 Send the question file (`.txt`, `.md` or `.pdf`) — "
                "same formats as /testseries.\n\nOr /cancel to stop.", cancel)
    if step == "intake_qids":
        return ("🔢 Send your quiz IDs separated by spaces "
                "(e.g. `GGN123 GGN456`).\n\nOnly quizzes you created can be "
                "used. Or /cancel to stop.", cancel)
    if step == "title":
        return ("**1 · Test Series Title**\n\nSend the title "
                "(e.g. `UPSC Mock 7`).", cancel)
    if step == "subject":
        rows = []
        for i in range(0, len(SUBJECT_CHOICES), 2):
            rows.append([_cb(uid, "subject", SUBJECT_CHOICES[i], f"s{i}"),
                         _cb(uid, "subject", SUBJECT_CHOICES[i + 1], f"s{i + 1}")])
        rows.append([InlineKeyboardButton(
            "❌ Cancel", callback_data=f"{CB_PREFIX}sj_{uid}_cancel")])
        return ("**2 · Subject**\n\nPick one — or just type your own subject.",
                InlineKeyboardMarkup(rows))
    if step == "test_number":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "test_number", "Automatic", "auto"),
             _cb(uid, "test_number", "Manual", "manual")],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"{CB_PREFIX}tn_{uid}_cancel")],
        ])
        return ("**3 · Test Number**\n\nAutomatic lets the system assign it; "
                "Manual lets you type it.", kb)
    if step == "test_number_input":
        return ("Type the test number (e.g. `3`).", cancel)
    if step == "booklet_series":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "booklet_series", "A", "A"),
             _cb(uid, "booklet_series", "B", "B"),
             _cb(uid, "booklet_series", "C", "C"),
             _cb(uid, "booklet_series", "D", "D")],
            [_cb(uid, "booklet_series", "Custom", "custom")],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"{CB_PREFIX}bs_{uid}_cancel")],
        ])
        return ("**4 · Booklet Series** (paper set)", kb)
    if step == "booklet_series_input":
        return ("Type the custom booklet series (e.g. `E`).", cancel)
    if step == "booklet_number":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "booklet_number", "Automatic", "auto"),
             _cb(uid, "booklet_number", "Manual", "manual")],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"{CB_PREFIX}bn_{uid}_cancel")],
        ])
        return ("**5 · Booklet Number**", kb)
    if step == "booklet_number_input":
        return ("Type the booklet number (e.g. `12`).", cancel)
    for cand_step, _attr, label in CANDIDATE_FIELDS:
        if step == cand_step:
            return (f"**6 · {label}**\n\nInclude a blank **{label}** box "
                    f"in the paper?", _yn_kb(uid, step))
    if step == "institute":
        return ("**7 · Institute Name**\n\nSend the institute name for the "
                "cover (e.g. `Journey for LBSNAA`).", cancel)
    if step == "logo":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "logo", "Upload Logo", "upload"),
             _cb(uid, "logo", "Skip", "skip")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}lg_{uid}_cancel")],
        ])
        return ("**Logo** — upload a logo image, or skip.", kb)
    if step == "logo_upload":
        return ("🖼️ Send the logo as a photo (or image file).", cancel)
    if step == "watermark":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "watermark", "Text", "text"),
             _cb(uid, "watermark", "Image", "image")],
            [_cb(uid, "watermark", "Both", "both"),
             _cb(uid, "watermark", "Skip", "skip")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}wm_{uid}_cancel")],
        ])
        return ("**Watermark** — pick one. (Logo and watermark images are "
                "separate uploads.)", kb)
    if step == "wm_text":
        return ("Send the watermark text (shown faintly on each page).",
                cancel)
    if step == "wm_image":
        return ("🖼️ Send the watermark image as a photo (or image file).",
                cancel)
    if step == "tagline":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "tagline", "Skip tagline", "skip")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}tg_{uid}_cancel")],
        ])
        return ("**Tagline** — send the cover tagline shown under the title, "
                "or skip.", kb)
    if step == "answer_key":
        return ("**8 · Answer Key**\n\nInclude an answer key?",
                _yn_kb(uid, step))
    if step == "solutions":
        return ("**Detailed Solutions**\n\nInclude detailed solutions?",
                _yn_kb(uid, step))
    if step == "visuals":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "visuals", "Automatic", "auto"),
             _cb(uid, "visuals", "Yes", "yes"),
             _cb(uid, "visuals", "No", "no")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}vi_{uid}_cancel")],
        ])
        return ("**Visual Aids** (maps / diagrams in solutions)\n\n"
                "Automatic = the smart engine decides per question.",
                kb)
    if step == "marks_correct":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "marks_correct", "+2", "p2"),
             _cb(uid, "marks_correct", "+1", "p1"),
             _cb(uid, "marks_correct", "Custom", "custom")],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"{CB_PREFIX}mc_{uid}_cancel")],
        ])
        return ("**9 · Correct-answer marks** (default +2)", kb)
    if step == "marks_correct_input":
        return ("Type the marks for a correct answer (e.g. `4`).", cancel)
    if step == "marks_negative":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "marks_negative", "-0.66", "n066"),
             _cb(uid, "marks_negative", "-0.33", "n033")],
            [_cb(uid, "marks_negative", "0", "n0"),
             _cb(uid, "marks_negative", "Custom", "custom")],
            [InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"{CB_PREFIX}mn_{uid}_cancel")],
        ])
        return ("**Negative marking** (default −0.66)", kb)
    if step == "marks_negative_input":
        return ("Type the negative marks as a number ≤ 0 (e.g. `-0.25`).",
                cancel)
    if step == "paper":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "paper", "Skip", "skip")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}pp_{uid}_cancel")],
        ])
        return ("**10 · Paper** (optional, e.g. `Paper I`) — send it or skip.",
                kb)
    if step == "duration":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "duration", "Skip", "skip")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}du_{uid}_cancel")],
        ])
        return ("**Duration** (optional, e.g. `2 hours`) — send it or skip.",
                kb)
    if step == "test_code":
        kb = InlineKeyboardMarkup([
            [_cb(uid, "test_code", "Skip", "skip")],
            [InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}tc_{uid}_cancel")],
        ])
        return ("**Test Code** (optional, e.g. `JFL-GEO-01-2027`) — send it "
                "or skip.", kb)
    if step == "edit_menu":
        rows = []
        items = list(EDIT_SECTIONS)
        for i in range(0, len(items), 2):
            row = [_cb(uid, "edit_menu", items[i][1], items[i][0])]
            if i + 1 < len(items):
                row.append(_cb(uid, "edit_menu", items[i + 1][1],
                               items[i + 1][0]))
            rows.append(row)
        rows.append([
            _cb(uid, "edit_menu", "◀️ Back", "back"),
            InlineKeyboardButton(
                "❌ Cancel", callback_data=f"{CB_PREFIX}em_{uid}_cancel"),
        ])
        return ("**Edit setup** — what do you want to change?",
                InlineKeyboardMarkup(rows))
    if step == "preview":
        cfg = build_config(session)
        kb = InlineKeyboardMarkup([
            [_cb(uid, "preview", "✅ Generate PDF", "generate")],
            [_cb(uid, "preview", "✏️ Edit", "edit"),
             InlineKeyboardButton(
                 "❌ Cancel", callback_data=f"{CB_PREFIX}pv_{uid}_cancel")],
        ])
        return (_render_preview(cfg), kb)
    return ("⚠️ Unknown step. Send /cancel and start again with /newseries.",
            cancel)


def _render_preview(cfg: TestSeriesConfig) -> str:
    """Compact pre-generation summary (user text escaped)."""
    test_no = cfg.test_number if cfg.test_number_mode == "manual" else "Automatic"
    booklet_no = cfg.booklet_number if cfg.booklet_number_mode == "manual" else "Auto"
    cand = cfg.enabled_candidate_fields()
    return (
        "**11 · Review your Test Series**\n\n"
        f"Title: **{md_escape(cfg.title)}**\n"
        f"Subject: {md_escape(cfg.subject) or '—'}\n"
        f"Test No.: {md_escape(test_no)} • "
        f"Booklet: {md_escape(cfg.booklet_series)}-{md_escape(booklet_no)}\n"
        f"Questions: **{cfg.total_questions}** • "
        f"Max marks: **{cfg.max_marks:g}**\n"
        f"Marks: +{cfg.marks_correct:g} / {cfg.marks_negative:g}\n"
        f"Key: {'Yes' if cfg.answer_key else 'No'} • "
        f"Solutions: {'Yes' if cfg.solutions else 'No'} • "
        f"Visuals: {cfg.visuals.title()}\n"
        f"Candidate boxes: {md_escape(', '.join(cand)) if cand else 'None'}\n"
        f"Institute: {md_escape(cfg.institute_name)}\n"
        f"Logo: {'Uploaded' if cfg.logo_present else '—'} • "
        f"Watermark: {cfg.watermark_mode.title()}\n"
        f"Tagline: {md_escape(cfg.tagline) or '—'}\n"
        f"Paper: {md_escape(cfg.paper) or '—'} • "
        f"Duration: {md_escape(cfg.duration) or '—'}\n"
        f"Code: {md_escape(cfg.test_code) or '—'}"
    )


async def _send_step(c: Client, target, uid: int) -> None:
    """Render the session's current step: edit the tapped message for
    callback targets, reply fresh for message targets."""
    sess = creator_state.testseries_create.get(uid) or {}
    text, markup = _prompt(sess.get("step", ""), uid, sess)
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.reply(text, reply_markup=markup)


def _advance(session: dict) -> None:
    """Move the session one step forward (branch-aware). Edit mode
    returns to the preview at the edited section's end."""
    step = session.get("step")
    cfg = session.get("config", {})
    nxt = _STATIC_NEXT.get(step, step)
    if step == "test_number":
        nxt = "test_number_input" if cfg.get("test_number_mode") == "manual" else "booklet_series"
    elif step == "booklet_series":
        nxt = "booklet_series_input" if cfg.get("booklet_series") == "custom" else "booklet_number"
    elif step == "booklet_number":
        nxt = "booklet_number_input" if cfg.get("booklet_number_mode") == "manual" else "cand_name"
    elif step == "logo":
        nxt = "logo_upload" if cfg.get("logo_choice") == "upload" else "watermark"
    elif step == "watermark":
        mode = cfg.get("watermark_mode", "none")
        nxt = {"text": "wm_text", "image": "wm_image", "both": "wm_text"}.get(mode, "tagline")
    elif step == "wm_text":
        nxt = "wm_image" if cfg.get("watermark_mode") == "both" else "tagline"
    elif step == "marks_correct":
        nxt = "marks_correct_input" if cfg.get("marks_correct") == "custom" else "marks_negative"
    elif step == "marks_negative":
        nxt = "marks_negative_input" if cfg.get("marks_negative") == "custom" else "paper"
    session["step"] = nxt
    if session.get("edit_return"):
        ends = SECTION_ENDS.get(session.get("edit_section") or "", set())
        if step in ends:
            session["step"] = "preview"
            session["edit_return"] = False
            session["edit_section"] = None


_STATIC_NEXT = {
    "intake_file": "title",
    "intake_qids": "title",
    "title": "subject",
    "subject": "test_number",
    "test_number_input": "booklet_series",
    "booklet_series_input": "booklet_number",
    "booklet_number_input": "cand_name",
    "cand_name": "cand_roll",
    "cand_roll": "cand_regid",
    "cand_regid": "cand_batch",
    "cand_batch": "cand_date",
    "cand_date": "cand_candsig",
    "cand_candsig": "cand_evalsig",
    "cand_evalsig": "institute",
    "institute": "logo",
    "logo_upload": "watermark",
    "wm_image": "tagline",
    "tagline": "answer_key",
    "answer_key": "solutions",
    "solutions": "visuals",
    "visuals": "marks_correct",
    "marks_correct_input": "marks_negative",
    "marks_negative_input": "paper",
    "paper": "duration",
    "duration": "test_code",
    "test_code": "preview",
}


# ─── Input validation (pure) ────────────────────────────────────────
def _clean_text(value: str, limit: int) -> str | None:
    """Collapse whitespace; None when empty or over the limit."""
    cleaned = " ".join((value or "").split())
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned


def _parse_marks(value: str, *, positive: bool) -> float | None:
    try:
        number = float((value or "").strip().replace(",", "."))
    except ValueError:
        return None
    if positive and not (0 < number <= 100):
        return None
    if not positive and not (-100 <= number <= 0):
        return None
    return round(number, 2)


def _looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:4] == b"\x89PNG":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


# ─── Question intake ───────────────────────────────────────────────
async def _fetch_quizzes(uid: int, ids: list[str]) -> tuple[list, list, list]:
    """Fetch owned, non-empty quizzes (mirrors /testseries semantics)."""
    repo = QuizRepository(get_db())
    quizzes, failed, not_owner = [], [], []
    for qid in ids:
        quiz = await repo.get(qid)
        if not quiz or not quiz.get("questions"):
            failed.append(qid)
            continue
        if quiz.get("creator_id") != uid:
            not_owner.append(qid)
            continue
        quizzes.append(quiz)
    return quizzes, failed, not_owner


def _quiz_ids_from_text(text: str) -> list[str]:
    return [tok.strip() for tok in (text or "").replace(",", " ").split()
            if tok.strip()][:20]


async def _store_quizzes(uid: int, quizzes: list[dict], source: str) -> None:
    sess = creator_state.testseries_create.get(uid)
    if sess is None:
        return
    sess["quizzes"] = quizzes
    sess["source"] = source
    sess["step"] = "title"
    if (sess.get("config") or {}).get("title"):
        sess["step"] = "subject"  # title prefilled via /newseries title=...


# ─── Command entry ──────────────────────────────────────────────────
@ratelimit("default")
async def newseries_cmd(c: Client, m: Message) -> None:
    """/newseries [QID...] [title=...] -- start the professional setup."""
    uid = m.from_user.id
    if not config.PDF_API_BASE:
        await m.reply(
            "PDF generation is not configured on this bot (no PDF_API_BASE set). "
            "Ask the bot operator to configure a PDF microservice."
        )
        return
    if not await is_premium_user(uid):
        await m.reply("**Premium feature** -- the test-series studio is for premium users only. Use /pay.")
        return
    if uid in creator_state.testseries_upload or uid in creator_state.quiz_creation:
        await m.reply("⚠️ Finish or /cancel your current setup first, then send /newseries again.")
        return
    if uid in creator_state.testseries_create:
        await m.reply("Starting a fresh setup (previous draft discarded).")
    creator_state.testseries_create[uid] = _fresh_session()

    raw = m.text.split(maxsplit=1)[1].strip() if len(m.text.split(maxsplit=1)) > 1 else ""
    quiz_ids = [tok for tok in raw.split()
                if not tok.lower().startswith(("mode=", "title="))]
    title_tok = next((tok.split("=", 1)[1].replace("_", " ")
                      for tok in raw.split() if tok.lower().startswith("title=")), "")
    if title_tok:
        cleaned = _clean_text(title_tok, LIMITS["title"])
        if cleaned:
            creator_state.testseries_create[uid]["config"]["title"] = cleaned
    if not quiz_ids:
        await _send_step(c, m, uid)
        return
    quizzes, failed, not_owner = await _fetch_quizzes(uid, quiz_ids)
    if not quizzes:
        creator_state.testseries_create.pop(uid, None)
        if not_owner:
            await m.reply(f"You are not the creator of: `{', '.join(not_owner)}`")
        else:
            await m.reply("No valid quizzes found. Check the IDs and try again.")
        return
    notes = []
    if failed:
        notes.append(f"Not found/empty: `{', '.join(failed)}`")
    if not_owner:
        notes.append(f"Not created by you: `{', '.join(not_owner)}`")
    await _store_quizzes(uid, quizzes, "qids")
    total, _ = _derive_totals(quizzes, 2.0)
    prefix = f"✅ {len(quizzes)} quiz(es), **{total}** questions loaded."
    if notes:
        prefix += "\nSkipped — " + " • ".join(notes)
    await m.reply(prefix)
    await _send_step(c, m, uid)


# ─── Callback router ────────────────────────────────────────────────
async def creation_cb(c: Client, cb: CallbackQuery) -> None:
    """Inline callbacks for the /newseries wizard (`tsc_<code>_<uid>_<v>`)."""
    parts = (cb.data or "").split("_")
    if len(parts) < 4 or parts[0] != "tsc":
        await cb.answer("Invalid button.", show_alert=True)
        return
    code, uid_s, value = parts[1], parts[2], "_".join(parts[3:])
    try:
        uid = int(uid_s)
    except ValueError:
        await cb.answer("Invalid session.", show_alert=True)
        return
    if cb.from_user.id != uid:
        await cb.answer("❌ This setup belongs to another user.", show_alert=True)
        return
    sess = creator_state.testseries_create.get(uid)
    if sess is None:
        await cb.answer("⚠️ Session expired. Start again with /newseries.", show_alert=True)
        return
    if value == "cancel":
        creator_state.testseries_create.pop(uid, None)
        await cb.answer("Cancelled.")
        try:
            await cb.message.edit_text("❌ Test-series setup cancelled.")
        except Exception:
            pass
        return
    step = CB_STEP.get(code)
    if step is None:
        await cb.answer("Invalid button.", show_alert=True)
        return
    if sess.get("step") != step:
        await cb.answer("This button is outdated — please use the latest prompt.", show_alert=True)
        return
    handled = await _apply_choice(c, cb, uid, sess, step, value)
    if handled:
        await cb.answer()
        await _send_step(c, cb, uid)


async def _apply_choice(c: Client, cb: CallbackQuery, uid: int,
                        sess: dict, step: str, value: str) -> bool:
    """Apply one button tap. Returns True when the wizard advanced."""
    cfg = sess.setdefault("config", {})

    async def reject(text: str) -> bool:
        await cb.answer(text, show_alert=True)
        return False

    if step == "intake":
        if value not in ("file", "qids"):
            return await reject("Invalid choice.")
        sess["step"] = "intake_file" if value == "file" else "intake_qids"
        return True
    if step == "subject":
        if not re.fullmatch(r"s[0-7]", value or ""):
            return await reject("Invalid choice.")
        cfg["subject"] = SUBJECT_CHOICES[int(value[1:])]
        _advance(sess)
        return True
    if step == "test_number":
        if value not in ("auto", "manual"):
            return await reject("Invalid choice.")
        cfg["test_number_mode"] = value
        if value == "auto":
            cfg["test_number"] = ""
        _advance(sess)
        return True
    if step == "booklet_series":
        if value not in ("A", "B", "C", "D", "custom"):
            return await reject("Invalid choice.")
        cfg["booklet_series"] = value
        _advance(sess)
        return True
    if step == "booklet_number":
        if value not in ("auto", "manual"):
            return await reject("Invalid choice.")
        cfg["booklet_number_mode"] = value
        if value == "auto":
            cfg["booklet_number"] = ""
        _advance(sess)
        return True
    if step.startswith("cand_"):
        if value not in ("0", "1"):
            return await reject("Invalid choice.")
        cfg[step] = (value == "1")
        _advance(sess)
        return True
    if step == "logo":
        if value not in ("upload", "skip"):
            return await reject("Invalid choice.")
        cfg["logo_choice"] = value
        if value == "skip":
            sess.get("assets", {})["logo"] = None
        _advance(sess)
        return True
    if step == "watermark":
        if value not in ("text", "image", "both", "skip"):
            return await reject("Invalid choice.")
        cfg["watermark_mode"] = "none" if value == "skip" else value
        if value == "skip":
            cfg["watermark_text"] = ""
            sess.get("assets", {})["wm_image"] = None
        if value == "text":
            sess.get("assets", {})["wm_image"] = None
        if value == "image":
            cfg["watermark_text"] = ""
        _advance(sess)
        return True
    if step in ("tagline", "paper", "duration", "test_code"):
        if value != "skip":
            return await reject("Invalid choice.")
        cfg[step] = ""
        _advance(sess)
        return True
    if step in ("answer_key", "solutions"):
        if value not in ("0", "1"):
            return await reject("Invalid choice.")
        cfg[step] = (value == "1")
        _advance(sess)
        return True
    if step == "visuals":
        if value not in ("auto", "yes", "no"):
            return await reject("Invalid choice.")
        cfg["visuals"] = value
        _advance(sess)
        return True
    if step == "marks_correct":
        if value not in ("p2", "p1", "custom"):
            return await reject("Invalid choice.")
        cfg["marks_correct"] = {"p2": 2.0, "p1": 1.0}.get(value, "custom")
        _advance(sess)
        return True
    if step == "marks_negative":
        if value not in ("n066", "n033", "n0", "custom"):
            return await reject("Invalid choice.")
        cfg["marks_negative"] = {"n066": -0.66, "n033": -0.33,
                                 "n0": 0.0}.get(value, "custom")
        _advance(sess)
        return True
    if step == "preview":
        if value == "edit":
            sess["step"] = "edit_menu"
            return True
        if value == "generate":
            await cb.answer()  # ack now; generation renders its own outcome
            await _do_generate(c, cb, uid)
            return False
        return await reject("Invalid choice.")
    if step == "edit_menu":
        if value == "back":
            sess["step"] = "preview"
            return True
        if value not in SECTION_START:
            return await reject("Invalid choice.")
        sess["edit_return"] = True
        sess["edit_section"] = value
        sess["step"] = SECTION_START[value]
        return True
    return await reject("This step needs a typed reply, not a button.")


# ─── Text / photo / document inputs ─────────────────────────────────
TEXT_STEPS = {
    "title": ("title", LIMITS["title"]),
    "subject": ("subject", LIMITS["subject"]),
    "test_number_input": ("test_number", LIMITS["test_number"]),
    "booklet_series_input": ("booklet_series", LIMITS["booklet_series"]),
    "booklet_number_input": ("booklet_number", LIMITS["booklet_number"]),
    "institute": ("institute_name", LIMITS["institute_name"]),
    "wm_text": ("watermark_text", LIMITS["watermark_text"]),
    "tagline": ("tagline", LIMITS["tagline"]),
    "paper": ("paper", LIMITS["paper"]),
    "duration": ("duration", LIMITS["duration"]),
    "test_code": ("test_code", LIMITS["test_code"]),
}
BUTTON_ONLY_STEPS = {
    "intake", "test_number", "booklet_series", "booklet_number",
    "cand_name", "cand_roll", "cand_regid", "cand_batch", "cand_date",
    "cand_candsig", "cand_evalsig", "logo", "watermark", "answer_key",
    "solutions", "visuals", "marks_correct", "marks_negative", "preview",
    "edit_menu",
}
IMAGE_STEPS = {"logo_upload", "wm_image"}


async def handle_create_text(c: Client, m: Message) -> None:
    """Route free text inside the wizard by current step."""
    uid = m.from_user.id
    sess = creator_state.testseries_create.get(uid)
    if sess is None:
        return
    step = sess.get("step")
    cfg = sess.setdefault("config", {})

    if step == "intake_qids":
        ids = _quiz_ids_from_text(m.text or "")
        if not ids:
            await m.reply("Send at least one quiz ID (e.g. `GGN123 GGN456`).")
            return
        quizzes, failed, not_owner = await _fetch_quizzes(uid, ids)
        if not quizzes:
            await m.reply("No valid quizzes found. Check the IDs and try again (or /cancel).")
            return
        await _store_quizzes(uid, quizzes, "qids")
        total, _ = _derive_totals(quizzes, 2.0)
        loaded = f"✅ {len(quizzes)} quiz(es), **{total}** questions loaded."
        notes = []
        if failed:
            notes.append(f"Not found/empty: `{', '.join(failed)}`")
        if not_owner:
            notes.append(f"Not created by you: `{', '.join(not_owner)}`")
        if notes:
            loaded += "\nSkipped — " + " • ".join(notes)
        await m.reply(loaded)
        await _send_step(c, m, uid)
        return
    if step in ("marks_correct_input", "marks_negative_input"):
        positive = step == "marks_correct_input"
        number = _parse_marks(m.text or "", positive=positive)
        if number is None:
            hint = "a number between 0 and 100 (e.g. `4`)" if positive \
                else "a number between −100 and 0 (e.g. `-0.25`)"
            await m.reply(f"⚠️ Please send {hint}.")
            return
        cfg["marks_correct" if positive else "marks_negative"] = number
        _advance(sess)
        await _send_step(c, m, uid)
        return
    if step in TEXT_STEPS:
        field, limit = TEXT_STEPS[step]
        cleaned = _clean_text(m.text or "", limit)
        if cleaned is None:
            await m.reply(f"⚠️ Please send a non-empty value (max {limit} characters).")
            return
        cfg[field] = cleaned
        _advance(sess)
        await _send_step(c, m, uid)
        return
    if step in IMAGE_STEPS:
        await m.reply("🖼️ Please send an image as a photo (or /cancel).")
        return
    if step == "intake_file":
        await m.reply("📄 Please send the question file as a document (or /cancel).")
        return
    if step in BUTTON_ONLY_STEPS:
        await m.reply("👆 Please tap one of the buttons above (or /cancel).")
        return
    creator_state.testseries_create.pop(uid, None)
    await m.reply("⚠️ This setup got out of sync. Send /newseries to start fresh.")


async def handle_create_photo(c: Client, m: Message) -> None:
    """Route photo uploads (logo / watermark image steps only)."""
    uid = m.from_user.id
    sess = creator_state.testseries_create.get(uid)
    if sess is None:
        return
    step = sess.get("step")
    if step not in IMAGE_STEPS:
        if step in TEXT_STEPS or step in ("marks_correct_input",
                                          "marks_negative_input",
                                          "intake_qids"):
            await m.reply("✏️ This step needs a typed reply — please send text (or /cancel).")
        elif step in BUTTON_ONLY_STEPS:
            await m.reply("👆 Please tap one of the buttons above (or /cancel).")
        else:
            await m.reply("📄 Please send the question file as a document (or /cancel).")
        return
    photos = m.photo
    photo = photos[-1] if isinstance(photos, list) else photos
    file_id = getattr(photo, "file_id", None)
    if not file_id:
        await m.reply("⚠️ Could not read that photo. Please try again.")
        return
    await _store_image(c, m, uid, sess, step, file_id)


async def handle_create_document(c: Client, m: Message) -> None:
    """Route documents: the MCQ file at intake, images at image steps."""
    uid = m.from_user.id
    sess = creator_state.testseries_create.get(uid)
    if sess is None:
        return
    step = sess.get("step")
    doc = m.document
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if step in IMAGE_STEPS:
        if not mime.startswith("image/"):
            await m.reply("🖼️ That is not an image. Send a photo or image file (or /cancel).")
            return
        await _store_image(c, m, uid, sess, step, doc.file_id)
        return
    if step != "intake_file":
        if step in TEXT_STEPS or step in ("marks_correct_input",
                                          "marks_negative_input",
                                          "intake_qids"):
            await m.reply("✏️ This step needs a typed reply — please send text (or /cancel).")
        else:
            await m.reply("👆 Please tap one of the buttons above (or /cancel).")
        return
    filename = (getattr(doc, "file_name", "") or "").strip()
    if not filename.lower().endswith((".txt", ".md", ".markdown", ".pdf")) \
            and "pdf" not in mime and "text" not in mime:
        await m.reply("⚠️ Please send a `.txt`, `.md` or `.pdf` question file (or /cancel).")
        return
    size = getattr(doc, "file_size", 0) or 0
    if size > MAX_FILE_BYTES:
        await m.reply(f"⚠️ File too large ({size / 1048576:.1f} MB). Maximum is "
                      f"{MAX_FILE_BYTES // 1048576} MB. Send a smaller file or /cancel.")
        return
    status = await m.reply("⏳ Reading file...")
    try:
        buf = await c.download_media(doc.file_id, in_memory=True)
        buf.seek(0)
        content = buf.read()
    except Exception as exc:
        logger.exception("newseries file download failed")
        await status.edit_text(f"❌ Could not download that file: {md_escape(exc)}\nPlease try again or /cancel.")
        return
    if len(content) > MAX_FILE_BYTES or not content:
        await status.edit_text("⚠️ That file is empty or over 15 MB. Send a valid file or /cancel.")
        return
    result = await asyncio.to_thread(process_testseries_upload, content, filename or "upload.txt")
    if result.error or not result.ok:
        detail = result.error or f"{len(result.problems)} problem(s), first: {result.problems[0]}"
        await status.edit_text(f"❌ {md_escape(detail)}\n\nFix the file and send it again, or /cancel.")
        return
    pseudo = {"quiz_name": filename or "file",
              "questions": [q.to_payload() for q in result.questions]}
    await _store_quizzes(uid, [pseudo], "file")
    await status.edit_text(f"✅ File parsed: **{len(result.questions)}** questions, "
                           f"**0** problems.")
    await _send_step(c, m, uid)


async def _store_image(c: Client, m: Message, uid: int, sess: dict,
                       step: str, file_id: str) -> None:
    """Download + validate a logo/watermark image into session assets."""
    status = await m.reply("⏳ Receiving image...")
    try:
        buf = await c.download_media(file_id, in_memory=True)
        buf.seek(0)
        data = buf.read()
    except Exception as exc:
        logger.exception("newseries image download failed")
        await status.edit_text(f"❌ Could not download that image: {md_escape(exc)}\nPlease try again or /cancel.")
        return
    if len(data) > MAX_IMAGE_BYTES:
        await status.edit_text(f"⚠️ Image too large ({len(data) / 1048576:.1f} MB). "
                               "Maximum is 5 MB. Send a smaller image or /cancel.")
        return
    if not _looks_like_image(data):
        await status.edit_text("⚠️ That file is not a PNG/JPEG/WebP image. Send an image or /cancel.")
        return
    sess.setdefault("assets", {})["logo" if step == "logo_upload" else "wm_image"] = bytes(data)
    sess["config"]["logo_choice"] = sess["config"].get("logo_choice", "upload")
    _advance(sess)
    await status.edit_text(f"✅ Image received ({len(data) // 1024 + 1} KB).")
    await _send_step(c, m, uid)


# ─── Generation ─────────────────────────────────────────────────────
def build_create_caption(cfg: TestSeriesConfig, source: str) -> str:
    lines = [f"**{md_escape(cfg.title)}**", ""]
    bits = []
    if cfg.subject:
        bits.append(f"Subject: {md_escape(cfg.subject)}")
    test_no = cfg.test_number if cfg.test_number_mode == "manual" else "Auto"
    booklet_no = cfg.booklet_number if cfg.booklet_number_mode == "manual" else "Auto"
    bits.append(f"Test: {md_escape(test_no)}")
    bits.append(f"Booklet: {md_escape(cfg.booklet_series)}-{md_escape(booklet_no)}")
    lines.append(" | ".join(bits))
    lines.append(f"Questions: {cfg.total_questions} | Marks: +{cfg.marks_correct:g} / "
                 f"{cfg.marks_negative:g} | Max: {cfg.max_marks:g}")
    lines.append(f"From: {'file' if source == 'file' else 'saved quizzes'}")
    return "\n".join(lines)


def build_create_filename(cfg: TestSeriesConfig) -> str:
    slug = re.sub(r"[^\w\-]+", "_", cfg.title).strip("_")[:40] or "Paper"
    return f"TestSeries_{slug}_{cfg.total_questions}Q.pdf"


async def _do_generate(c: Client, target, uid: int) -> None:
    """Final preview → existing PDF pipeline → send → cleanup session."""
    sess = creator_state.testseries_create.get(uid)
    if sess is None:
        return
    message = target.message if hasattr(target, "message") else target
    if _TSR_LOCK.locked():
        # Session is kept on preview: the user can retry Generate shortly.
        await message.reply(
            "**System busy** — another test series PDF is being generated. "
            "Tap ✅ Generate PDF again shortly.")
        return
    cfg = build_config(sess)
    problems = cfg.validate()
    if problems:
        await message.reply("⚠️ Setup incomplete:\n• " + "\n• ".join(problems))
        return
    sess["step"] = "generating"
    status = await message.reply(
        f"Generating PDF...\n\nQuestions: **{cfg.total_questions}**\n"
        f"Title: **{md_escape(cfg.title)}**")
    tagline = cfg.tagline.strip() or None
    try:
        pdf_bytes = await _generate_pdf_via_api(
            sess["quizzes"], "keyonly", cfg.title,
            tagline=tagline, institute_name=cfg.institute_name.strip() or None,
            series_setup=build_series_setup(cfg, sess.get("assets", {})))
    except Exception as exc:
        logger.exception("newseries PDF generation failed")
        sess["step"] = "preview"
        await status.edit_text(f"PDF generation failed: {md_escape(exc)}\n\n"
                               "Your setup is kept — tap ✅ Generate PDF to retry.")
        return
    await status.edit_text("Uploading PDF...")
    out_name = build_create_filename(cfg)
    buf = BytesIO(pdf_bytes)
    buf.name = out_name
    caption = build_create_caption(cfg, sess.get("source") or "qids")
    try:
        if hasattr(target, "message") and hasattr(target, "data"):
            await target.message.reply_document(document=buf, file_name=out_name,
                                                caption=caption)
        else:
            await target.reply_document(document=buf, file_name=out_name,
                                        caption=caption)
        await status.delete()
    except Exception as exc:
        logger.exception("Failed to send newseries PDF")
        sess["step"] = "preview"
        await status.edit_text(f"Failed to send file: {md_escape(exc)}\n\n"
                               "Your setup is kept — tap ✅ Generate PDF to retry.")
        return
    creator_state.testseries_create.pop(uid, None)


# ─── Legacy Pyrogram registration (standalone creator bot) ──────────
def _in_create_filter():
    async def _check(_: Client, __: Message, m: Message) -> bool:
        user = m.from_user
        return bool(user) and user.id in creator_state.testseries_create
    return filters.create(_check)


def register(app: Client) -> None:
    app.on_message(filters.command("newseries") & filters.private)(newseries_cmd)
    app.on_callback_query(filters.regex(r"^tsc_[a-z]+_\d+_.+$"))(creation_cb)
    app.on_message(filters.document & filters.private & _in_create_filter())(handle_create_document)
    app.on_message(filters.photo & filters.private & _in_create_filter())(handle_create_photo)
    app.on_message(filters.text & filters.private & _in_create_filter())(handle_create_text)
