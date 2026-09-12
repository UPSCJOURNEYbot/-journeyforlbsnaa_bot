"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from ..parsing import filter_words, parse_question_block, strip_source_noise

logger = logging.getLogger(__name__)


def _process_txt(content: str, remove_words: list[str], out_questions: list[dict]) -> int:
    """Parse a .txt upload using the same block-parsing engine as pasted
    text, plus the legacy `RT:`/`ID:`/`<ggn>...</ggn>` structured-field
    style for reply_text/file_id metadata."""
    ggn_blocks: list[str] = []

    def _replace_ggn(match) -> str:
        placeholder = f"GGN_PLACEHOLDER_{len(ggn_blocks)}"
        ggn_blocks.append(match.group(1))
        return f"RT: <ggn>{placeholder}</ggn>"

    import re

    protected = re.sub(r"<ggn>(.*?)</ggn>", _replace_ggn, content, flags=re.DOTALL)

    blocks = protected.strip().split("\n\n")
    processed = 0

    for block in blocks:
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        has_structured = any(ln.strip().startswith(("RT:", "ID:")) for ln in lines)

        reply_text: Optional[str] = None
        file_id: Optional[str] = None
        core_lines = lines

        if has_structured:
            reply_text, file_id, core_lines = None, None, []
            for ln in lines:
                stripped = ln.strip()
                if stripped.startswith("RT:"):
                    rt_content = stripped[3:].strip()
                    ph_match = re.search(r"<ggn>GGN_PLACEHOLDER_(\d+)</ggn>", rt_content)
                    if ph_match:
                        idx = int(ph_match.group(1))
                        reply_text = ggn_blocks[idx] if idx < len(ggn_blocks) else rt_content
                    else:
                        reply_text = rt_content
                    if remove_words:
                        reply_text = filter_words(reply_text, remove_words)
                    reply_text = strip_source_noise(reply_text)
                elif stripped.startswith("ID:"):
                    file_id = strip_source_noise(stripped[3:].strip())
                else:
                    core_lines.append(ln)

        parsed = parse_question_block("\n".join(core_lines))
        if not parsed or isinstance(parsed["correct_option_id"], list):
            continue

        q_text = parsed["question"]
        opts = parsed["options"]
        exp = parsed.get("explanation")
        if remove_words:
            q_text = filter_words(q_text, remove_words)
            opts = [filter_words(o, remove_words) for o in opts]
            if exp:
                exp = filter_words(exp, remove_words)
        q_text = strip_source_noise(q_text)
        opts = [strip_source_noise(o) for o in opts]
        if exp:
            exp = strip_source_noise(exp)

        if not q_text or len(opts) < 2:
            continue

        out_questions.append(
            {
                "question": q_text, "options": opts,
                "correct_option_id": parsed["correct_option_id"],
                "explanation": exp, "reply_text": reply_text, "file_id": file_id,
            }
        )
        processed += 1

    return processed


def _process_json(raw: dict, remove_words: list[str], out_questions: list[dict]) -> int:
    """Parse the simple JSON schema:
    `{"questions": [{"question_text", "options": [{"id","text"}],
    "correct_option_id", "explanation"?, "reference_text"?, "file_id"?}]}`
    """
    questions = raw.get("questions")
    if not isinstance(questions, list):
        raise ValueError("'questions' should be an array.")

    processed = 0
    for q in questions:
        try:
            if not isinstance(q, dict):
                continue
            if not all(k in q for k in ("question_text", "options", "correct_option_id")):
                continue
            question_text = q["question_text"]
            if remove_words:
                question_text = filter_words(question_text, remove_words)
            question_text = strip_source_noise(question_text)

            options_data = q["options"]
            if not isinstance(options_data, list) or len(options_data) < 2:
                continue

            options: list[str] = []
            id_to_index: dict = {}
            for opt in options_data:
                if not isinstance(opt, dict) or "id" not in opt or "text" not in opt:
                    continue
                text = opt["text"]
                if remove_words:
                    text = filter_words(text, remove_words)
                text = strip_source_noise(text)
                if not text:
                    continue
                id_to_index[opt["id"]] = len(options)
                options.append(text)
            if len(options) < 2:
                continue

            correct_id = q["correct_option_id"]
            if correct_id not in id_to_index:
                continue
            correct_index = id_to_index[correct_id]

            explanation = q.get("explanation")
            if explanation and remove_words:
                explanation = filter_words(explanation, remove_words)
            if explanation:
                explanation = strip_source_noise(explanation)

            reply_text = q.get("reference_text")
            if reply_text and remove_words:
                reply_text = filter_words(reply_text, remove_words)
            if reply_text:
                reply_text = strip_source_noise(reply_text)

            out_questions.append(
                {
                    "question": question_text, "options": options,
                    "correct_option_id": correct_index, "explanation": explanation,
                    "reply_text": reply_text, "file_id": q.get("file_id"),
                }
            )
            processed += 1
        except Exception:
            logger.debug("Skipped malformed question in JSON import", exc_info=True)
            continue
    return processed



def _process_text_content(text: str, remove_words: list[str], out_questions: list[dict]) -> int:
    """Parse plain text/Markdown using the same tolerant MCQ parser."""
    return _process_txt(text, remove_words, out_questions)


_Q_BADGE_START_RE = re.compile(r"^\s*Q\.?\s*(\d{1,3})(?!\d)[\s.:)\-–]*(.*)$")
_NUM_START_RE = re.compile(r"^\s*(\d{1,3})\s*[.):]\s*(.*)$")
_OPT_LINE_RE = re.compile(r"^[A-Da-d]\s*[\).:-]\s*\S|^\d{1,3}\s*[\).:-]\s*\S")


def _segment_question_blocks(text: str) -> str:
    """Re-chunk continuous extracted PDF text into blank-line-separated
    question blocks so report-style PDFs (questions packed together with no
    blank line between them) can be parsed.

    A question starts on a line beginning with ``Q12`` / ``Q12.`` (quiz-report
    layout) -- or, when no such badge exists, with ``12.`` / ``12)`` provided
    the following lines look like option lines (this prevents numbered option
    lists from being mistaken for questions). Lines before the first question
    start (report header, leaderboard, section banners) are dropped and the
    leading question number is stripped from the question text. Returns the
    input unchanged when no question starts are found.
    """
    lines = text.splitlines()

    q_starts = [i for i, ln in enumerate(lines) if _Q_BADGE_START_RE.match(ln)]
    if q_starts:
        starts, first_re = q_starts, _Q_BADGE_START_RE
    else:
        def _looks_like_question(i: int) -> bool:
            seen = 0
            for j in range(i + 1, len(lines)):
                st = lines[j].strip()
                if not st:
                    continue
                if _OPT_LINE_RE.match(st):
                    return True
                seen += 1
                if seen >= 6:
                    return False
            return False

        starts = [i for i, ln in enumerate(lines) if _NUM_START_RE.match(ln) and _looks_like_question(i)]
        if not starts:
            return text
        first_re = _NUM_START_RE

    start_set = set(starts)
    blocks: list[str] = []
    current: list[str] = []
    for i, ln in enumerate(lines):
        if i in start_set:
            if current:
                blocks.append("\n".join(current))
            m = first_re.match(ln)
            rest = (m.group(2) or "").strip()
            current = [rest] if rest else []
        elif i >= starts[0]:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))
    blocks = [b for b in blocks if b.strip()]
    if not blocks:
        return text
    return "\n\n".join(blocks)


def ocr_pdf_text(content: bytes, pages: int, max_pages: int = 30) -> str:
    """Render PDF pages to images and OCR them with Tesseract (English+Hindi).

    Shared helper used by the quiz-creation import and the test-series
    file flow. Raises whatever the imaging/OCR stack raises; callers turn
    that into a user-facing message. Capped at `max_pages` pages so the
    bot stays responsive on huge scans.
    """
    from io import BytesIO

    import fitz  # PyMuPDF
    from PIL import Image
    import pytesseract

    doc = fitz.open(stream=content, filetype="pdf")
    capped = min(pages, max_pages)
    try:
        ocr_pages: list[str] = []
        for idx in range(capped):
            pix = doc.load_page(idx).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(BytesIO(pix.tobytes("png")))
            ocr_pages.append(pytesseract.image_to_string(image, lang="eng+hin"))
    finally:
        doc.close()
    if pages > capped:
        logger.warning("PDF OCR capped at %d of %d pages", capped, pages)
    return "\n\n".join(ocr_pages)


def _process_pdf(content: bytes, remove_words: list[str], out_questions: list[dict]) -> tuple[int, int]:
    """Extract text from a PDF and parse it.

    Text-based PDFs are parsed directly, using question-block segmentation
    for report-style layouts that pack questions together without blank
    lines.  If nothing parses, pages are rendered and OCR'd with Tesseract
    (capped to 30 pages so the bot stays responsive).  Raises
    ``RuntimeError`` with an actionable message when no usable question can
    be detected, so the caller can tell the user exactly what to do instead
    of silently "processing" forever.  Returns ``(processed, pages)``.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for PDF import") from exc

    doc = fitz.open(stream=content, filetype="pdf")
    try:
        pages = len(doc)
        text = "\n\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()

    count = _process_text_content(_segment_question_blocks(text), remove_words, out_questions)
    if not count:
        count = _process_text_content(text, remove_words, out_questions)
    if count:
        return count, pages

    if len(text.strip()) > 500 and re.search(r"^\s*Q\.?\s*\d{1,3}(?!\d)", text, flags=re.M | re.I):
        # A real text layer with recognizable question starts exists; OCR
        # cannot recover answer information that is not present in the text.
        # Fail fast with an actionable message instead of OCR-ing the
        # (potentially large) document pointlessly.
        raise RuntimeError(
            f"No usable questions with answer keys detected in this PDF ({pages} page(s)). "
            "Quiz-report PDFs mark the correct answer visually only (colour), so their "
            "questions cannot be re-imported with an answer key. Use /testseries <QUIZ_ID> "
            "to generate the test-series PDF from the original quiz, or send a text-based "
            "question file (.txt / .json / PDF) where the correct option is marked with "
            "✅ or given as an 'Answer:' line."
        )

    # Scanned/image-only PDF fallback: OCR each page (capped) when normal
    # extraction found no parseable MCQs. Keeps normal PDFs fast while
    # supporting image-based study material.
    ocr_error: Optional[Exception] = None
    try:
        ocr_text = ocr_pdf_text(content, pages)
        count = _process_text_content(_segment_question_blocks(ocr_text), remove_words, out_questions)
        if not count:
            count = _process_text_content(ocr_text, remove_words, out_questions)
    except Exception as exc:
        ocr_error = exc
        logger.warning("PDF OCR fallback unavailable/failed: %s", exc)
    if count:
        return count, pages

    detail = f" (OCR also failed: {ocr_error})" if ocr_error else ""
    raise RuntimeError(
        f"No usable questions detected in this PDF ({pages} page(s)){detail}. "
        "Quiz-report PDFs mark the correct answer visually only (colour), so their "
        "questions cannot be re-imported with an answer key. Use /testseries <QUIZ_ID> "
        "to generate the test-series PDF from the original quiz, or send a text-based "
        "question file (.txt / .json / PDF) where the correct option is marked with "
        "✅ or given as an 'Answer:' line."
    )


def _process_image(content: bytes, remove_words: list[str], out_questions: list[dict]) -> int:
    """OCR an image when Tesseract is installed, then parse the OCR text."""
    try:
        from PIL import Image
        import pytesseract
        from io import BytesIO
        image = Image.open(BytesIO(content))
        text = pytesseract.image_to_string(image, lang="eng+hin")
    except Exception as exc:
        raise RuntimeError(
            "Image OCR unavailable. Install Pillow, pytesseract and the Tesseract OCR package."
        ) from exc
    return _process_text_content(text, remove_words, out_questions)


def _extract_html_text(html: str) -> str:
    """Extract useful text from public AI/share pages without executing JS."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


async def process_public_url(
    url: str, out_questions: list[dict], remove_words: list[str]
) -> tuple[Optional[int], Optional[str]]:
    """Fetch a public text/AI share URL and parse its visible content.

    Works for public pages that expose their content in HTML. Dynamic/private
    pages may intentionally return no question text; the caller gets a clear
    error instead of silently accepting an empty import.
    """
    import aiohttp
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "❌ Please send a valid public http/https link."
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {"User-Agent": "Mozilla/5.0 (Quizbot Question Importer)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True, max_redirects=5) as resp:
                if resp.status >= 400:
                    return None, f"❌ Link could not be opened (HTTP {resp.status})."
                ctype = (resp.headers.get("content-type") or "").lower()
                data = await resp.read()
        if "pdf" in ctype or url.lower().split("?", 1)[0].endswith(".pdf"):
            count, _ = _process_pdf(data, remove_words, out_questions)
        else:
            encoding = "utf-8"
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="ignore")
            if "html" in ctype or "<html" in text[:1000].lower():
                text = _extract_html_text(text)
            count = _process_text_content(text, remove_words, out_questions)
        if not count:
            return 0, "❌ Link opened, but no valid MCQ questions were found. The AI share page may require login or JavaScript rendering."
        return count, None
    except Exception as exc:
        logger.exception("Public URL import failed: %s", url)
        return None, f"⚠️ Could not import this link: {exc}"

def process_uploaded_file(
    content: bytes, filename: str, out_questions: list[dict], remove_words: list[str]
) -> tuple[Optional[int], Optional[str]]:
    """Import .txt/.md/.markdown/.json/.pdf and common image files."""
    lower = (filename or "").lower().split("?", 1)[0]
    try:
        if lower.endswith(".json"):
            text = content.decode("utf-8")
            data = json.loads(text)
            if not isinstance(data, dict) or "questions" not in data:
                return None, "❌ Invalid JSON format. Expected an object with a 'questions' array."
            count = _process_json(data, remove_words, out_questions)
        elif lower.endswith((".txt", ".md", ".markdown")):
            text = content.decode("utf-8")
            count = _process_text_content(text, remove_words, out_questions)
        elif lower.endswith(".pdf"):
            count, _ = _process_pdf(content, remove_words, out_questions)
        elif lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")):
            count = _process_image(content, remove_words, out_questions)
        else:
            return None, "❌ Supported question sources: TXT, MD, JSON, PDF, PNG/JPG/WEBP/BMP/TIFF, plain text, and public links."
        return count, None
    except UnicodeDecodeError:
        return None, "❌ Text file is not valid UTF-8."
    except json.JSONDecodeError as exc:
        return None, f"❌ Invalid JSON format: {exc}"
    except Exception as exc:
        logger.exception("process_uploaded_file failed for %s", filename)
        return None, f"⚠️ Error processing file: {exc}"

