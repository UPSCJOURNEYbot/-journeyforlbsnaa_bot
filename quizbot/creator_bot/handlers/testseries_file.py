"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.

Direct MCQ file -> Test Series PDF flow (bare ``/testseries``).

Additive feature: a premium creator sends a ``.txt`` / ``.md`` / ``.pdf``
question file and gets the same book-style PDF the QID flow produces.
Nothing here modifies Workflows 1-4: generation is delegated to
:func:`reports._build_testseries_payload` and
:func:`reports._generate_pdf_via_api` with a pseudo-quiz dict, and the
``/testseries QID...`` path is byte-for-byte unchanged.

Supported file layouts (mixable per question inside one file):

* **Format A** -- headers ``Q.1.`` / ``Q26`` / ``Question 26``, options
  ``A)`` / ``a)`` / ``A.``, correct option marked with ``✅``, optional
  ``Ex:`` explanation line(s).
* **Format B** -- same headers/options, answer given as ``Answer: C`` or
  ``Answer: <option text>``, optional ``Solution:`` and
  ``Extra details:`` line(s).

Core guarantee: **no question is ever silently skipped.** Every detected
question block either becomes a validated question or produces a
per-question problem entry (``Q17 — answer not detected``); if any
problem exists the PDF is NOT generated and the user gets the report.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from io import BytesIO

from pyrogram import Client
from pyrogram.types import Message

from .. import state as creator_state
from .reports import _TSR_LOCK, _build_testseries_payload, _generate_pdf_via_api

logger = logging.getLogger(__name__)

# Telegram's Bot API caps downloads at 20 MB; stay safely under it.
MAX_FILE_BYTES = 15 * 1024 * 1024
# Mirrors the PDF microservice's own question cap (contract, PDF_SERVICE.md).
MAX_QUESTIONS_PER_FILE = 2000
SUPPORTED_EXTS = (".txt", ".md", ".markdown", ".pdf")

# How many problem lines fit in one Telegram message (4096 chars) safely.
_MAX_PROBLEM_LINES = 30

# ─── Line patterns ────────────────────────────────────────────────────
_Q_START = re.compile(
    r"^\s*(?:Q\s*\.?\s*(\d{1,4})|Question\s*(\d{1,4})|प्रश्न\s*(\d{1,4}))\s*[.)]?\s*(.*)$",
    re.IGNORECASE,
)
_OPTION = re.compile(r"^\s*([A-Za-z])\s*[).:–-]\s*(\S(?:.*)?)\s*$")
_ANSWER = re.compile(r"^\s*(?:Answers?|Ans\.?|उत्तर)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_EXPLAIN = re.compile(r"^\s*(Ex\.?|Explanation|व्याख्या)\s*:\s*(.*)$", re.IGNORECASE)
_SOLUTION = re.compile(r"^\s*(Solutions?|हल)\s*:\s*(.*)$", re.IGNORECASE)
_EXTRA = re.compile(r"^\s*Extra\s+details?\s*:\s*(.*)$", re.IGNORECASE)
_MARK = re.compile(r"[✅✔✓☑]\uFE0F?")
_LETTER_VAL = re.compile(r"^\(?([A-Ja-j])\)?\s*[.)]?\s*(.*)$")
_META = re.compile(r"^\s*[^\W\d_][^:]{0,24}:\s")
_MD_ESCAPE = re.compile(r"([\\_\*\[\`])")


def md_escape(text: str) -> str:
    """Escape user-controlled text for Telegram legacy-Markdown replies."""
    return _MD_ESCAPE.sub(r"\\\1", str(text))


def _clean_chunk(text: str) -> str:
    """Strip ✅-style marks and collapse whitespace in one parsed chunk."""
    return " ".join(_MARK.sub("", text).split())


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


# ─── Parsed model ─────────────────────────────────────────────────────
@dataclass
class ParsedQuestion:
    number: int | None
    question: str
    options: list[str]
    correct_index: int
    explanation: str = ""

    def to_payload(self) -> dict:
        return {
            "question": self.question,
            "options": list(self.options),
            "correct_option_id": self.correct_index,
            "explanation": self.explanation,
        }


@dataclass
class FileParseResult:
    ok: bool
    questions: list[ParsedQuestion] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    total_blocks: int = 0
    with_explanations: int = 0
    pages: int = 0
    ocr_used: bool = False
    is_pdf: bool = False
    error: str = ""


@dataclass
class PdfText:
    text: str
    pages: int
    ocr_used: bool


# ─── Block segmentation + parsing (pure) ──────────────────────────────
def _split_blocks(lines: list[str]) -> list[dict]:
    """Split lines into question blocks at Q-start headers.

    Text before the first header (preamble/instructions) is not a
    question and is ignored -- every block from here on is accounted
    for as either a question or a problem entry.
    """
    blocks: list[dict] = []
    current: dict | None = None
    for line in lines:
        m = _Q_START.match(line)
        if m:
            if current is not None:
                blocks.append(current)
            num = next((int(g) for g in m.groups()[:3] if g), None)
            current = {"number": num, "head": m.group(4).strip(), "body": []}
        elif current is not None:
            current["body"].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _resolve_answer(value: str, options: list[dict]) -> tuple[int | None, str]:
    """Resolve an ``Answer:`` value to an option index.

    Returns ``(index, "")`` on success or ``(None, reason)`` when the
    value matches nothing (caller turns the reason into a problem).
    """
    cleaned = _clean_chunk(value)
    if not cleaned:
        return None, "empty Answer: line"
    norm = _norm(cleaned)
    for i, opt in enumerate(options):
        if _norm(opt["text"]) == norm:
            return i, ""
    m = _LETTER_VAL.match(cleaned)
    if m:
        letter, rest = m.group(1).upper(), _norm(m.group(2))
        idx = next((i for i, o in enumerate(options) if o["letter"] == letter), None)
        if idx is not None:
            if not rest:
                return idx, ""
            opt_norm = _norm(options[idx]["text"])
            if rest == opt_norm or rest in opt_norm or opt_norm in rest:
                return idx, ""
    return None, f'answer "{cleaned}" matches no option'


def _parse_block(number: int | None, head: str, body: list[str], label: str) -> tuple[ParsedQuestion | None, list[str]]:
    """Parse one question block. Returns (question-or-None, problems).

    Invariant: a block that is not returned as a question ALWAYS yields
    at least one problem entry -- nothing is dropped silently.
    """
    problems: list[str] = []
    q_lines = [head] if head else []
    options: list[dict] = []  # {letter, text, mark}
    answers: list[str] = []
    expl: list[list] = []  # [label, lines]
    notes: list[str] = []
    cur_expl: int | None = None
    phase = "q"  # q = question text, o = options, t = tail (answers/explanations)

    def _tail_line(text: str) -> None:
        if cur_expl is not None:
            expl[cur_expl][1].append(text)
        else:
            notes.append(text)

    def _start_expl(label_text: str, rest: str) -> None:
        nonlocal cur_expl, phase
        expl.append([label_text, [rest.strip()] if rest.strip() else []])
        cur_expl = len(expl) - 1
        phase = "t"

    for raw in body:
        line = raw.strip()
        if not line:
            continue
        # Option-looking lines AFTER explanations started are explanation
        # content (e.g. "e. g. ..."), not options -- the options region
        # is closed once answers/explanations begin.
        opt = _OPTION.match(line)
        if opt is not None and not (phase == "t" and options):
            content = opt.group(2)
            options.append(
                {
                    "letter": opt.group(1).upper(),
                    "text": _clean_chunk(content),
                    "mark": bool(_MARK.search(content)),
                }
            )
            phase = "o"
            continue
        m = _ANSWER.match(line)
        if m:
            answers.append(m.group(1).strip())
            continue
        m = _EXPLAIN.match(line)
        if m:
            _start_expl("Ex", m.group(2))
            continue
        m = _SOLUTION.match(line)
        if m:
            _start_expl("Solution", m.group(2))
            continue
        m = _EXTRA.match(line)
        if m:
            _start_expl("Extra details", m.group(1))
            continue
        if phase == "q":
            q_lines.append(line)
        elif phase == "o" and _META.match(line):
            # Trailing "Label: ..." metadata (Topic:/Source:/...) is notes,
            # not option text -- close the options region.
            phase = "t"
            _tail_line(line)
        elif phase == "o":
            options[-1]["text"] += "\n" + _clean_chunk(line)
            options[-1]["mark"] = options[-1]["mark"] or bool(_MARK.search(line))
        else:
            _tail_line(line)

    question_text = "\n".join(q_lines).strip()
    if not question_text:
        problems.append(f"{label} — question text missing")
    if len(options) < 2:
        problems.append(f"{label} — only {len(options)} option(s) found (need 2–10)")
    elif len(options) > 10:
        problems.append(f"{label} — {len(options)} options found (maximum 10)")
    letters = [o["letter"] for o in options]
    if len(set(letters)) != len(letters):
        dupes = sorted({letter for letter in letters if letters.count(letter) > 1})
        problems.append(f"{label} — duplicate option letter(s): {', '.join(dupes)}")
    if len(answers) > 1:
        problems.append(f"{label} — {len(answers)} Answer: lines found (keep exactly one)")

    marks = [i for i, o in enumerate(options) if o["mark"]]
    if len(marks) > 1:
        marked = ", ".join(options[i]["letter"] for i in marks)
        problems.append(f"{label} — {len(marks)} ✅-marked options ({marked}); mark exactly one")

    resolved: int | None = None
    if len(answers) == 1:
        resolved, err = _resolve_answer(answers[0], options)
        if err:
            problems.append(f"{label} — {err}")
            resolved = None
    if len(marks) == 1:
        if resolved is not None and resolved != marks[0]:
            problems.append(f"{label} — ✅ option and Answer: disagree")
            resolved = None
        else:
            resolved = marks[0]
    if not marks and not answers:
        problems.append(f"{label} — answer not detected")

    valid = (
        resolved is not None
        and bool(question_text)
        and 2 <= len(options) <= 10
        and len(answers) <= 1
        and len(marks) <= 1
        and len(set(letters)) == len(letters)
    )
    if not valid:
        return None, problems

    parts: list[str] = []
    for expl_label, expl_lines in expl:
        text = "\n".join(expl_lines).strip()
        if text:
            parts.append(f"{expl_label}: {text}")
    if notes:
        text = "\n".join(notes).strip()
        if text:
            parts.append(f"Notes: {text}")
    return (
        ParsedQuestion(
            number=number,
            question=question_text,
            options=[o["text"] for o in options],
            correct_index=resolved,
            explanation="\n\n".join(parts),
        ),
        problems,
    )


def parse_testseries_text(text: str) -> FileParseResult:
    """Parse full file text into validated questions + problems."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = _split_blocks(lines)
    if not blocks:
        return FileParseResult(
            ok=False,
            error=(
                "No questions detected in this file. Mark each question with "
                "Q.1. / Q26 / Question 26, give 2–10 A) options, and mark the "
                "answer with ✅ or an Answer: line."
            ),
        )
    if len(blocks) > MAX_QUESTIONS_PER_FILE:
        return FileParseResult(
            ok=False,
            total_blocks=len(blocks),
            error=(
                f"File has {len(blocks)} questions; maximum "
                f"{MAX_QUESTIONS_PER_FILE} per PDF. Split the file and try again."
            ),
        )
    questions: list[ParsedQuestion] = []
    problems: list[str] = []
    seen_numbers: set[int] = set()
    for seq, block in enumerate(blocks, 1):
        num = block["number"]
        if num is None:
            label = f"#{seq}"
        elif num in seen_numbers:
            label = f"Q{num} (item #{seq})"
        else:
            label = f"Q{num}"
        if num is not None:
            seen_numbers.add(num)
        parsed, per = _parse_block(num, block["head"], block["body"], label)
        problems.extend(per)
        if parsed is not None:
            questions.append(parsed)
    return FileParseResult(
        ok=bool(questions) and not problems,
        questions=questions,
        problems=problems,
        total_blocks=len(blocks),
        with_explanations=sum(1 for q in questions if q.explanation.strip()),
    )


# ─── PDF text extraction (text layer + OCR fallback) ──────────────────
def _dedup_edge_lines(pages_text: list[str]) -> str:
    """Drop identical header/footer lines repeated across pages.

    Only short lines (<=100 chars) sitting on page edges (first/last two
    non-blank lines) on at least half the pages (min 3) are treated as
    running heads. Mid-page content -- including options that repeat
    across questions, e.g. "None of the above" -- is never touched.
    """
    if len(pages_text) < 3:
        return "\n\n".join(pages_text)
    edge_hits: dict[str, int] = {}
    for page_text in pages_text:
        nonblank = [ln.strip() for ln in page_text.split("\n")]
        nonblank = [ln for ln in nonblank if ln]
        edge = set(nonblank[:2] + nonblank[-2:])
        for line in edge:
            if line and len(line) <= 100:
                edge_hits[line] = edge_hits.get(line, 0) + 1
    threshold = max(3, len(pages_text) // 2)
    banned = {line for line, hits in edge_hits.items() if hits >= threshold}
    if not banned:
        return "\n\n".join(pages_text)
    return "\n\n".join(
        "\n".join(ln for ln in page_text.split("\n") if ln.strip() not in banned)
        for page_text in pages_text
    )


def extract_testseries_pdf_text(content: bytes) -> PdfText:
    """Extract question-file text from a PDF (text layer, else OCR).

    Scanned/image-only PDFs fall back to the project's shared Tesseract
    helper (:func:`file_import.ocr_pdf_text`, English+Hindi, 30-page cap).
    Raises RuntimeError with a user-facing message on any failure.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to read PDF files.") from exc
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise RuntimeError("Could not read this PDF (file is corrupted or not a PDF).") from exc
    try:
        pages = len(doc)
        if pages == 0:
            raise RuntimeError("This PDF has no pages.")
        layer = [page.get_text("text") for page in doc]
    finally:
        doc.close()
    text = _dedup_edge_lines(layer)
    if text.strip():
        return PdfText(text=text, pages=pages, ocr_used=False)
    try:
        from .file_import import ocr_pdf_text
        ocr_text = ocr_pdf_text(content, pages)
    except Exception as exc:
        logger.warning("testseries PDF OCR unavailable/failed: %s", exc)
        raise RuntimeError(
            "This PDF looks scanned (no readable text) and OCR is unavailable "
            "on this server. Send a text .txt/.md file instead, or ask the bot "
            "operator to install Tesseract OCR."
        ) from exc
    return PdfText(text=ocr_text, pages=pages, ocr_used=True)


def process_testseries_upload(content: bytes, filename: str) -> FileParseResult:
    """Decode/extract an uploaded file and parse it (CPU-bound, sync).

    Run via ``asyncio.to_thread`` so the single poller never freezes.
    Never raises for bad content -- failures come back as
    ``result.error`` / ``result.problems`` for the user report.
    """
    lower = (filename or "").lower()
    is_pdf = lower.endswith(".pdf") or content[:5] == b"%PDF-"
    pages, ocr_used = 0, False
    if is_pdf:
        try:
            pdf = extract_testseries_pdf_text(content)
        except RuntimeError as exc:
            return FileParseResult(ok=False, is_pdf=True, error=str(exc))
        text, pages, ocr_used = pdf.text, pdf.pages, pdf.ocr_used
    else:
        text = content.decode("utf-8-sig", errors="replace")
        if "\x00" in text:
            return FileParseResult(ok=False, error="This file does not look like readable text.")
    result = parse_testseries_text(text)
    result.pages = pages
    result.ocr_used = ocr_used
    result.is_pdf = is_pdf
    return result


# ─── Paper-settings config (pure) ─────────────────────────────────────
CONFIG_DEFAULTS = {
    "title": "Mock Test",
    "subject": "",
    "test": "",
    "exam": "",
    "marks": "",
    "neg": "",
    "mode": "keyonly",
}
_GENERATE_WORDS = {"generate", "default", "defaults", "skip", "ok", "go"}
_KEY_ALIASES = {"negative": "neg"}


def parse_file_config(text: str) -> tuple[dict | None, str | None]:
    """Parse the one-line paper settings after a successful upload.

    Strict by design: unknown keys/words are reported, never silently
    ignored, so a typo like ``titel=`` cannot misconfigure the paper.
    Returns (meta, None) or (None, error).
    """
    meta = dict(CONFIG_DEFAULTS)
    tokens = (text or "").split()
    for tok in tokens:
        if "=" in tok:
            key, _, value = tok.partition("=")
            key = _KEY_ALIASES.get(key.strip().lower(), key.strip().lower())
            if key not in meta:
                return None, (
                    f"Unknown setting '{key}'. Valid keys: "
                    "title subject test exam marks neg mode."
                )
            value = value.strip().replace("_", " ")
            if key == "mode":
                if value.lower() not in ("inline", "keyonly"):
                    return None, "mode must be inline or keyonly."
                meta[key] = value.lower()
            else:
                if not value:
                    return None, f"Empty value for '{key}'."
                meta[key] = value
        elif tok.lower() not in _GENERATE_WORDS:
            return None, (
                f"Unrecognized '{tok}'. Send key=value pairs "
                "(title=... mode=...) or 'generate' for defaults."
            )
    return meta, None


def build_file_tagline(meta: dict) -> str:
    """Cover tagline carrying subject/test/exam/marks/negative meta."""
    bits: list[str] = []
    if meta.get("subject"):
        bits.append(str(meta["subject"]))
    if meta.get("test"):
        bits.append(str(meta["test"]))
    if meta.get("exam"):
        bits.append(str(meta["exam"]))
    if meta.get("marks"):
        bits.append(f"Marks: {meta['marks']}")
    if meta.get("neg"):
        bits.append(f"Negative: {meta['neg']}")
    return " • ".join(bits) if bits else "Test Series"


def build_file_caption(meta: dict, count: int, filename: str) -> str:
    lines = [f"**{md_escape(meta['title'])}**", ""]
    details = " | ".join(
        f"{label}: {md_escape(meta[key])}"
        for key, label in (("subject", "Subject"), ("test", "Test"), ("exam", "Exam"))
        if meta.get(key)
    )
    if details:
        lines.append(details)
    stats = f"Questions: {count}"
    if meta.get("marks"):
        stats += f" | Marks: {md_escape(meta['marks'])}"
    if meta.get("neg"):
        stats += f" | Negative: {md_escape(meta['neg'])}"
    stats += f" | Mode: {meta['mode']}"
    lines.append(stats)
    lines.append(f"From file: `{md_escape(filename)}`")
    return "\n".join(lines)


def build_file_filename(meta: dict, count: int) -> str:
    slug = re.sub(r"[^\w\-]+", "_", str(meta["title"])).strip("_")[:40] or "Paper"
    return f"MockTest_{slug}_{count}Q.pdf"


# ─── User-facing reports (pure text builders) ─────────────────────────
def render_problem_report(result: FileParseResult, filename: str) -> str:
    lines = [
        f"⚠️ **Could not build the PDF from `{md_escape(filename)}`**",
        "",
        f"Blocks found: **{result.total_blocks}** • "
        f"Usable: **{len(result.questions)}** • "
        f"Problems: **{len(result.problems)}**",
        "",
    ]
    for problem in result.problems[:_MAX_PROBLEM_LINES]:
        lines.append(f"• {md_escape(problem)}")
    if len(result.problems) > _MAX_PROBLEM_LINES:
        lines.append(f"• …and {len(result.problems) - _MAX_PROBLEM_LINES} more")
    if (
        result.is_pdf
        and not result.ocr_used
        and result.problems
        and all("answer not detected" in p for p in result.problems)
    ):
        lines += [
            "",
            "Tip: quiz-report PDFs mark the correct answer visually (colour) "
            "only, so answers cannot be recovered from them. Regenerate with "
            "`/testseries <QUIZ_ID>`, or send a file where answers are marked "
            "with ✅ or an Answer: line.",
        ]
    lines += ["", "Fix the file and send it again, or /cancel. Nothing was generated."]
    text = "\n".join(lines)
    return text[:3900]


def render_config_prompt(result: FileParseResult, filename: str) -> str:
    return (
        f"✅ **File parsed: `{md_escape(filename)}`**\n\n"
        f"Questions: **{len(result.questions)}** • "
        f"With explanations: **{result.with_explanations}** • "
        f"Problems: **0**"
        + (f"\nPDF pages: **{result.pages}**" + (" (OCR read)" if result.ocr_used else "") if result.is_pdf else "")
        + "\n\nNow send the paper settings in ONE message:\n"
        "`title=SSC_Mock_1 subject=Polity test=Test_1 exam=SSC_CGL marks=100 neg=0.25 mode=keyonly`\n"
        "All keys optional — send `generate` to use defaults "
        "(title Mock Test, mode keyonly). Or /cancel."
    )


# ─── Telegram flow handlers (called via the bridge router) ────────────
async def start_upload_flow(c: Client, m: Message) -> None:
    """Bare ``/testseries`` entry: ask for the MCQ file.

    Called from :func:`reports.testseries_cmd` AFTER its PDF_API_BASE /
    busy / premium guards, so those protections are inherited unchanged.
    """
    uid = m.from_user.id
    creator_state.testseries_upload[uid] = {"step": "awaiting_file", "ts": time.time()}
    await m.reply(
        "**Mock Test PDF Generator**\n\n"
        "📄 Send me a question file — `.txt`, `.md` or `.pdf` — with options "
        "and marked answers, and I'll build the test-series PDF.\n\n"
        "Formats understood:\n"
        "• `Q.1.` / `Q26` / `Question 26` headers, `A)` options\n"
        "• Correct answer: ✅ on the option, or `Answer: C` / `Answer: <option text>`\n"
        "• Explanations: `Ex:` / `Solution:` / `Extra details:`\n\n"
        "Or generate from your saved quizzes:\n"
        "`/testseries QID1 [QID2...] [mode=inline|keyonly] [title=Your_Title]`\n\n"
        "Send /cancel to stop."
    )


async def handle_testseries_document(c: Client, m: Message) -> None:
    """Download + parse the MCQ file sent while awaiting one."""
    uid = m.from_user.id
    sess = creator_state.testseries_upload.get(uid)
    if not sess or sess.get("step") != "awaiting_file":
        return
    doc = m.document
    filename = (getattr(doc, "file_name", "") or "").strip()
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if not filename.lower().endswith(SUPPORTED_EXTS) and "pdf" not in mime and "text" not in mime:
        await m.reply("⚠️ Please send a `.txt`, `.md` or `.pdf` question file (or /cancel).")
        return
    size = getattr(doc, "file_size", 0) or 0
    if size > MAX_FILE_BYTES:
        await m.reply(
            f"⚠️ File too large ({size / 1048576:.1f} MB). Maximum is "
            f"{MAX_FILE_BYTES // 1048576} MB. Send a smaller file or /cancel."
        )
        return
    status = await m.reply("⏳ Reading file...")
    try:
        buf = await c.download_media(doc.file_id, in_memory=True)
        buf.seek(0)
        content = buf.read()
    except Exception as exc:
        logger.exception("testseries file download failed")
        await status.edit_text(f"❌ Could not download that file: {md_escape(exc)}\nPlease try again or /cancel.")
        return
    if len(content) > MAX_FILE_BYTES:
        await status.edit_text("⚠️ File too large (over 15 MB). Send a smaller file or /cancel.")
        return
    if not content:
        await status.edit_text("⚠️ That file is empty. Send a valid question file or /cancel.")
        return
    # Parsing (especially scanned PDFs) is CPU-bound: keep it off the
    # event loop so the single PTB poller never appears frozen.
    result = await asyncio.to_thread(process_testseries_upload, content, filename or "upload.txt")
    if result.error:
        await status.edit_text(f"❌ {md_escape(result.error)}\n\nFix the file and send it again, or /cancel.")
        return
    if not result.ok:
        await status.edit_text(render_problem_report(result, filename or "upload"))
        return
    sess["step"] = "awaiting_config"
    sess["questions"] = [q.to_payload() for q in result.questions]
    sess["filename"] = filename or "upload.txt"
    await status.edit_text(render_config_prompt(result, sess["filename"]))


async def handle_testseries_text(c: Client, m: Message) -> None:
    """Nudge while awaiting the file (user sent text instead)."""
    uid = m.from_user.id
    sess = creator_state.testseries_upload.get(uid)
    if not sess or sess.get("step") != "awaiting_file":
        return
    await m.reply("📄 Please send the `.txt`, `.md` or `.pdf` question file (or /cancel).")


async def handle_testseries_config(c: Client, m: Message) -> None:
    """Apply the one-line paper settings and generate the PDF."""
    uid = m.from_user.id
    sess = creator_state.testseries_upload.get(uid)
    if not sess or sess.get("step") != "awaiting_config" or not sess.get("questions"):
        return
    meta, err = parse_file_config(m.text or "")
    if err:
        await m.reply(
            f"⚠️ {md_escape(err)}\n"
            "Example: `title=SSC_Mock_1 mode=inline` — or send `generate`. (/cancel to stop)"
        )
        return
    if _TSR_LOCK.locked():
        await m.reply(
            "**System busy** — another test series PDF is being generated. "
            "Please send your settings again shortly (or /cancel)."
        )
        return
    questions = sess["questions"]
    pseudo_quiz = {"quiz_name": sess.get("filename") or "file", "questions": questions}
    async with _TSR_LOCK:
        status = await m.reply(
            f"Generating PDF...\n\nQuestions: **{len(questions)}**\nTitle: **{md_escape(meta['title'])}**"
        )
        try:
            pdf_bytes = await _generate_pdf_via_api(
                [pseudo_quiz], meta["mode"], meta["title"], tagline=build_file_tagline(meta)
            )
        except Exception as exc:
            logger.exception("testseries file-flow PDF generation failed")
            await status.edit_text(f"PDF generation failed: {md_escape(exc)}")
            return
        await status.edit_text("Uploading PDF...")
        out_name = build_file_filename(meta, len(questions))
        buf = BytesIO(pdf_bytes)
        buf.name = out_name
        caption = build_file_caption(meta, len(questions), sess.get("filename") or "file")
        try:
            await m.reply_document(document=buf, file_name=out_name, caption=caption)
            await status.delete()
        except Exception as exc:
            logger.exception("Failed to send testseries file-flow PDF")
            await status.edit_text(f"Failed to send file: {md_escape(exc)}")
            return
    creator_state.testseries_upload.pop(uid, None)
