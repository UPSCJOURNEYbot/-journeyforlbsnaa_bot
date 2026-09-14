"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.

Phase 1 — question-file import surface.

* ``.txt`` / ``.md`` / ``.markdown`` and extracted PDF text / OCR output all
  flow through the ONE canonical parser in :mod:`quizbot.creator_bot.parsing`
  (pasted text uses the same path). JSON answers the structured schema.
* Every rejected block is returned with a structured reason so the caller
  can report processed/skipped honestly — a bad block never aborts an
  import and nothing ever reports a false success.
* Filenames are used for extension dispatch only and never touch the
  filesystem. Uploads/fetches are size-capped; the SSRF guard protects
  public-link fetches.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from quizbot.shared.utils.netguard import assert_public_http_url
from .. import parsing
from ..parsing import (
    RE_ANSWER_KEY_ASSOCIATION,
    RE_ANSWER_OUT_OF_RANGE,
    RE_CONFLICTING_ANSWERS,
    RE_INSUFFICIENT_OPTIONS,
    RE_MISSING_ANSWER,
    RE_MULTIPLE_CORRECT_UNSUPPORTED,
    filter_words,
    parse_question_block,
    parse_question_document,
    strip_source_noise,
)

logger = logging.getLogger(__name__)

# Backwards-compatible alias for callers/tests importing it from this module.
_assert_public_http_url = assert_public_http_url

# Human-readable explanations for structured reject codes.
REASON_MESSAGES = {
    parsing.RE_EMPTY: "empty document",
    parsing.RE_MISSING_QUESTION: "missing question text",
    parsing.RE_INSUFFICIENT_OPTIONS: "fewer than two valid options",
    parsing.RE_TOO_MANY_OPTIONS: "more than 10 options",
    parsing.RE_MISSING_ANSWER: "no answer marked",
    parsing.RE_UNRECOGNIZED_ANSWER: "answer line did not name an option",
    parsing.RE_ANSWER_OUT_OF_RANGE: "answer points outside the options",
    parsing.RE_CONFLICTING_ANSWERS: "the ✅ marker and Answer: line disagree",
    parsing.RE_MULTIPLE_CORRECT_UNSUPPORTED: "multiple correct options marked (single-answer quizzes only)",
    parsing.RE_ANSWER_KEY_ASSOCIATION: "answer key could not be matched to a question",
    parsing.RE_MALFORMED_ANSWER_KEY: "answer-key section was unreadable",
    "malformed_json_question": "malformed JSON question object",
    "ocr_unavailable": "image/OCR processing unavailable",
}


def _reason_text(code: str) -> str:
    return REASON_MESSAGES.get(code, code.replace("_", " "))


def _skipped_to_dicts(skipped) -> list[dict]:
    return [{"reason": s.reason, "detail": s.detail or "",
             "ordinal": s.ordinal, "snippet": s.snippet or ""}
            for s in skipped]


def _summarize_skipped(skipped: list[dict], limit: int = 8) -> str:
    """One human-readable tally of skipped blocks for feedback."""
    if not skipped:
        return ""
    counts: dict[str, int] = {}
    for s in skipped:
        code = s.get("reason", "unknown")
        counts[code] = counts.get(code, 0) + 1
    parts = [f"{n} × {_reason_text(code)}" for code, n in
             sorted(counts.items(), key=lambda kv: -kv[1])][:limit]
    return "; ".join(parts)


def _finalize(q: dict, remove_words: list[str]) -> dict:
    """Apply remove-words + source-noise cleanup to one parsed question."""
    q_text = q["question"]
    opts = list(q.get("options", []))
    exp = q.get("explanation")
    if remove_words:
        q_text = filter_words(q_text, remove_words)
        opts = [filter_words(o, remove_words) for o in opts]
        if exp:
            exp = filter_words(exp, remove_words)
    q_text = strip_source_noise(q_text)
    opts = [strip_source_noise(o) for o in opts]
    if exp:
        exp = strip_source_noise(exp)
    out = dict(q)
    out["question"] = q_text
    out["options"] = opts
    out["explanation"] = exp
    return out


def _process_txt(content: str, remove_words: list[str], out_questions: list[dict],
                 skipped_out: Optional[list] = None) -> int:
    """Parse a .txt upload through the canonical parser, preserving the
    legacy `RT:`/`ID:`/`<ggn>...</ggn>` structured-field style for
    reply_text/file_id metadata."""
    ggn_blocks: list[str] = []

    def _replace_ggn(match) -> str:
        placeholder = f"GGN_PLACEHOLDER_{len(ggn_blocks)}"
        ggn_blocks.append(match.group(1))
        return f"RT: <ggn>{placeholder}</ggn>"

    protected = re.sub(r"<ggn>(.*?)</ggn>", _replace_ggn, content, flags=re.DOTALL)
    blocks = protected.strip().split("\n\n")

    unstructured: list[str] = []
    processed = 0

    for block in blocks:
        if not block.strip():
            continue
        lines = block.strip().split("\n")
        has_structured = any(ln.strip().startswith(("RT:", "ID:")) for ln in lines)
        if not has_structured:
            unstructured.append(block.strip())
            continue

        reply_text: Optional[str] = None
        file_id: Optional[str] = None
        core_lines: list[str] = []
        for ln in lines:
            stripped = ln.strip()
            if stripped.startswith("RT:"):
                rt_content = stripped[3:].strip()
                ph = re.search(r"<ggn>GGN_PLACEHOLDER_(\d+)</ggn>", rt_content)
                if ph:
                    idx = int(ph.group(1))
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
            if skipped_out is not None:
                reason = (RE_MULTIPLE_CORRECT_UNSUPPORTED if parsed
                          else RE_MISSING_ANSWER)
                skipped_out.append({
                    "reason": reason,
                    "detail": "legacy structured block could not be parsed as a "
                              "single-answer MCQ",
                    "ordinal": None,
                    "snippet": " ".join(core_lines)[:80]})
            continue
        item = _finalize(parsed, remove_words)
        if not item["question"] or len(item["options"]) < 2:
            continue
        item["reply_text"] = reply_text
        item["file_id"] = file_id
        out_questions.append(item)
        processed += 1

    if unstructured:
        report = parse_question_document("\n\n".join(unstructured))
        for q in report.questions:
            item = _finalize(q, remove_words)
            if item["question"] and len(item["options"]) >= 2:
                item.setdefault("reply_text", None)
                item.setdefault("file_id", None)
                out_questions.append(item)
                processed += 1
        if skipped_out is not None:
            skipped_out.extend(_skipped_to_dicts(report.skipped))
    return processed


def _process_json(raw: dict, remove_words: list[str], out_questions: list[dict],
                  skipped_out: Optional[list] = None) -> int:
    """Parse the simple JSON schema:
    `{"questions": [{"question_text", "options": [{"id","text"}],
    "correct_option_id", "explanation"?, "reference_text"?, "file_id"?}]}`
    """
    questions = raw.get("questions")
    if not isinstance(questions, list):
        raise ValueError("'questions' should be an array.")

    processed = 0
    for position, q in enumerate(questions, 1):
        try:
            if not isinstance(q, dict):
                continue
            if not all(k in q for k in
                       ("question_text", "options", "correct_option_id")):
                if skipped_out is not None:
                    skipped_out.append({"reason": "malformed_json_question",
                                        "detail": "missing question_text/options/"
                                                  "correct_option_id",
                                        "ordinal": position, "snippet": ""})
                continue
            question_text = q["question_text"]
            if remove_words:
                question_text = filter_words(question_text, remove_words)
            question_text = strip_source_noise(question_text)

            options_data = q["options"]
            if not isinstance(options_data, list) or len(options_data) < 2:
                if skipped_out is not None:
                    skipped_out.append({
                        "reason": RE_INSUFFICIENT_OPTIONS,
                        "detail": "JSON question has fewer than two options",
                        "ordinal": position,
                        "snippet": str(question_text)[:80]})
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
                if skipped_out is not None:
                    skipped_out.append({
                        "reason": RE_INSUFFICIENT_OPTIONS,
                        "detail": "no usable option text after filtering",
                        "ordinal": position,
                        "snippet": str(question_text)[:80]})
                continue

            correct_id = q["correct_option_id"]
            if correct_id not in id_to_index:
                if skipped_out is not None:
                    skipped_out.append({
                        "reason": RE_ANSWER_OUT_OF_RANGE,
                        "detail": f"correct_option_id {correct_id!r} not among "
                                  f"option ids {sorted(id_to_index)}",
                        "ordinal": position,
                        "snippet": str(question_text)[:80]})
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

            item = {
                "question": question_text, "options": options,
                "correct_option_id": correct_index, "explanation": explanation,
                "reply_text": reply_text, "file_id": q.get("file_id"),
            }
            # Phase F: structured explanation companions. Option notes bind
            # to POSITIONAL indices; carry the companion only when the
            # option list survived 1:1. Prose-only companions are safe
            # regardless.
            detail = q.get("explanation_detail")
            if (isinstance(detail, dict)
                    and len(options) == len(options_data)
                    and isinstance(detail.get("options"), (list, dict))):
                item["explanation_detail"] = detail
            elif isinstance(detail, dict) and not isinstance(
                    detail.get("options"), (list, dict)):
                item["explanation_detail"] = detail
            out_questions.append(item)
            processed += 1
        except Exception:
            logger.debug("Skipped malformed question in JSON import",
                         exc_info=True)
            if skipped_out is not None:
                skipped_out.append({"reason": "malformed_json_question",
                                    "detail": "could not parse this JSON object",
                                    "ordinal": position, "snippet": ""})
            continue
    return processed


def _process_text_content(text: str, remove_words: list[str],
                          out_questions: list[dict],
                          skipped_out: Optional[list] = None) -> int:
    """Parse plain text/Markdown/Markdown text via the canonical parser."""
    local_skipped: list[dict] = [] if skipped_out is None else skipped_out
    if "RT:" in text or "<ggn>" in text:
        return _process_txt(text, remove_words, out_questions, local_skipped)
    before = len(out_questions)
    report = parse_question_document(text)
    for q in report.questions:
        item = _finalize(q, remove_words)
        if item["question"] and len(item["options"]) >= 2:
            item.setdefault("reply_text", None)
            item.setdefault("file_id", None)
            out_questions.append(item)
    local_skipped.extend(_skipped_to_dicts(report.skipped))
    return len(out_questions) - before


def ocr_pdf_text(content: bytes, pages: int, max_pages: int = 30) -> str:
    """Render PDF pages to images and OCR them with Tesseract (English+Hindi).

    Shared helper used by the quiz-creation import and the test-series
    file flow. Raises whatever the imaging/OCR stack raises; callers turn
    that into a user-facing message. Capped at `max_pages`.
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


def _process_pdf(content: bytes, remove_words: list[str], out_questions: list[dict],
                 skipped_out: Optional[list] = None) -> tuple[int, int]:
    """Extract text from a PDF and parse it via the canonical parser.

    Text-based PDFs are parsed directly (packed questions, separate
    answer-key sections). If nothing parses, pages are rendered and
    OCR'd (capped at 30 pages). Raises ``RuntimeError`` with an
    actionable message when no usable question can be detected.
    Returns ``(processed, pages)``.
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

    local_skipped: list[dict] = []
    count = _process_text_content(text, remove_words, out_questions, local_skipped)
    if count:
        if skipped_out is not None:
            skipped_out.extend(local_skipped)
        return count, pages

    # Scanned/image-only PDF fallback (only when normal extraction found
    # nothing that looks like a text-layer question paper).
    ocr_error: Optional[Exception] = None
    ocr_skipped: list[dict] = []
    ocr_attempted = False
    text_rich_questions = (
        len(text.strip()) > 500
        and re.search(r"^\s*Q\.?\s*\d{1,3}(?!\d)", text, flags=re.M | re.I))
    if not text_rich_questions:
        ocr_attempted = True
        try:
            ocr_text = ocr_pdf_text(content, pages)
            count = _process_text_content(
                ocr_text, remove_words, out_questions, ocr_skipped)
        except Exception as exc:
            ocr_error = exc
            logger.warning("PDF OCR fallback unavailable/failed: %s", exc)
        if count:
            if skipped_out is not None:
                skipped_out.extend(ocr_skipped)
            return count, pages

    # When OCR actually ran, its block analysis reflects the document we
    # attempted last; never merge both skip lists (that double-reported
    # the same question in the final error message).
    effective_skipped = ocr_skipped if ocr_attempted and ocr_skipped \
        else local_skipped
    if skipped_out is not None:
        skipped_out.extend(effective_skipped)
    # De-duplicate identical (ordinal, reason) rows.
    if skipped_out is not None:
        seen = set()
        deduped = []
        for s in skipped_out:
            key = (s.get("ordinal"), s.get("reason"), s.get("detail"))
            if key not in seen:
                seen.add(key)
                deduped.append(s)
        skipped_out[:] = deduped
    reasons = "; ".join(
        f"{s.get('ordinal') or '?'}: {_reason_text(s.get('reason', ''))}"
        for s in effective_skipped[:5]) or "no answer markers found"
    if text_rich_questions:
        raise RuntimeError(
            f"No usable questions with answer keys detected in this PDF "
            f"({pages} page(s)). Problems: {reasons}. Quiz-report PDFs mark "
            "the correct answer visually only (colour), so their questions "
            "cannot be re-imported with an answer key. Use /testseries "
            "<QUIZ_ID> to generate the test-series PDF from the original "
            "quiz, or send a text-based question file (.txt / .md / "
            ".markdown / PDF) where the correct option is marked with ✅, "
            "an 'Answer:' line, or a separate answer-key section.")
    detail = f" (OCR also failed: {ocr_error})" if ocr_error else ""
    raise RuntimeError(
        f"No usable questions detected in this PDF ({pages} page(s)){detail}. "
        f"Problems: {reasons}. Quiz-report PDFs mark the correct answer "
        "visually only (colour), so their questions cannot be re-imported "
        "with an answer key. Use /testseries <QUIZ_ID> to generate the "
        "test-series PDF from the original quiz, or send a text-based "
        "question file (.txt / .md / .markdown / PDF) where the correct "
        "option is marked with ✅, an 'Answer:' line, or a separate "
        "answer-key section.")


def _process_image(content: bytes, remove_words: list[str], out_questions: list[dict],
                   skipped_out: Optional[list] = None) -> int:
    """OCR an image when Tesseract is installed, then parse the OCR text."""
    try:
        from io import BytesIO
        from PIL import Image
        import pytesseract
        image = Image.open(BytesIO(content))
        text = pytesseract.image_to_string(image, lang="eng+hin")
    except Exception as exc:
        raise RuntimeError(
            "Image OCR unavailable. Install Pillow, pytesseract and the "
            "Tesseract OCR package (with Hindi language data).") from exc
    return _process_text_content(text, remove_words, out_questions, skipped_out)


def _extract_html_text(html: str) -> str:
    """Extract visible text from public AI/share pages (no JS)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


_PUBLIC_URL_MAX_BYTES = 25 * 1024 * 1024
_UPLOAD_MAX_BYTES = 25 * 1024 * 1024


async def process_public_url(
    url: str, out_questions: list[dict], remove_words: list[str]
) -> tuple[Optional[int], Optional[str], Optional[dict]]:
    """Fetch a public text/AI share URL and parse its visible content.

    The fetched host is SSRF-validated (public IPs only) for the original
    URL and every redirect hop, and the response is size-capped. JSON
    responses use the structured JSON flow; everything else goes through
    the canonical text parser. Returns ``(count, error, report)``.
    """
    import aiohttp
    from urllib.parse import urljoin, urlparse

    try:
        _assert_public_http_url(url)
    except ValueError as exc:
        return None, f"❌ {str(exc).capitalize()}", None
    data = b""
    ctype = ""
    skipped: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=25, sock_read=20)
        headers = {"User-Agent": "Mozilla/5.0 (Quizbot Question Importer)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            current = url
            for _hop in range(6):
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            return None, (
                                f"❌ Link redirected without a target "
                                f"(HTTP {resp.status})."), None
                        current = urljoin(current, location)
                        try:
                            _assert_public_http_url(current)
                        except ValueError as exc:
                            return None, f"❌ {str(exc).capitalize()}", None
                        continue
                    if resp.status >= 400:
                        return None, f"❌ Link could not be opened (HTTP {resp.status}).", None
                    ctype = (resp.headers.get("content-type") or "").lower()
                    chunks = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > _PUBLIC_URL_MAX_BYTES:
                            return None, "❌ The linked page is too large to import (25 MB limit).", None
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    break
            else:
                return None, "❌ The link redirected too many times.", None

        if "pdf" in ctype or url.lower().split("?", 1)[0].endswith(".pdf"):
            count, _pages = _process_pdf(
                data, remove_words, out_questions, skipped)
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="ignore")
            if "html" in ctype or "<html" in text[:1000].lower():
                text = _extract_html_text(text)
            stripped = text.lstrip()
            json_payload = None
            if "json" in ctype or stripped[:1] in ("{", "["):
                try:
                    json_payload = json.loads(stripped)
                except ValueError:
                    json_payload = None
            if json_payload is not None:
                if isinstance(json_payload, list):
                    json_payload = {"questions": json_payload}
                if (not isinstance(json_payload, dict)
                        or not isinstance(json_payload.get("questions"), list)):
                    return None, ("❌ Invalid JSON quiz link: expected an "
                                  "object with a 'questions' array."), None
                count = _process_json(
                    json_payload, remove_words, out_questions, skipped)
            else:
                count = _process_text_content(
                    text, remove_words, out_questions, skipped)
        if not count:
            summary = "; ".join(
                _reason_text(s.get("reason", "")) for s in skipped[:3])
            tail = f" Rejected blocks: {summary}." if summary else ""
            return 0, ("❌ Link opened, but no valid MCQ questions were found."
                       f"{tail} The AI share page may require login or "
                       "JavaScript rendering."), {"skipped": skipped}
        return count, None, {"skipped": skipped}
    except Exception as exc:
        # Log only scheme/host/path -- never the raw URL, which may carry
        # a signed token or credentials in its query string.
        try:
            p = urlparse(url)
            safe_target = f"{p.scheme}://{p.netloc}{p.path}"
        except Exception:
            safe_target = "<unparseable url>"
        logger.exception("Public URL import failed for %s", safe_target)
        return None, f"⚠️ Could not import this link: {exc}", None


def process_uploaded_file(
    content: bytes, filename: str, out_questions: list[dict],
    remove_words: list[str]
) -> tuple[Optional[int], Optional[str], Optional[dict]]:
    """Import .txt/.md/.markdown/.pdf documents and common image files.

    The /create dispatcher accepts exactly the four text document
    extensions (images reach the OCR flow; JSON content is supported via
    pasted text or public links rather than attachments). The internal
    .json branch here also serves public-link responses.

    Returns ``(count, error, report)``; ``report.skipped`` carries the
    structured per-block reject list so callers never claim a false
    success. Extension matching is case-insensitive.
    """
    lower = (filename or "").lower().split("?", 1)[0]
    skipped: list[dict] = []
    kind = lower.rsplit(".", 1)[-1] if "." in lower else "text"
    if len(content) > _UPLOAD_MAX_BYTES:
        return None, ("❌ File too large to import safely (25 MB limit). "
                      "Split it into smaller question files."), None
    try:
        count: Optional[int]
        if lower.endswith(".json"):
            text = content.decode("utf-8")
            data = json.loads(text)
            if not isinstance(data, dict) or "questions" not in data:
                return None, ("❌ Invalid JSON format. Expected an object "
                              "with a 'questions' array."), None
            count = _process_json(data, remove_words, out_questions, skipped)
        elif lower.endswith((".txt", ".md", ".markdown")):
            text = content.decode("utf-8")
            count = _process_text_content(
                text, remove_words, out_questions, skipped)
        elif lower.endswith(".pdf"):
            count, _ = _process_pdf(
                content, remove_words, out_questions, skipped)
        elif lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp",
                             ".tif", ".tiff")):
            count = _process_image(
                content, remove_words, out_questions, skipped)
        else:
            return None, ("❌ Supported file sources: TXT, MD, MARKDOWN, "
                          "PDF, PNG/JPG/WEBP/BMP/TIFF images, pasted text or "
                          "JSON, and public links."), None
        report = {"kind": kind, "processed": count,
                  "skipped": skipped,
                  "skipped_summary": _summarize_skipped(skipped)}
        if count == 0:
            summary = _summarize_skipped(skipped) or "no recognizable questions"
            return 0, None, {**report, "error_summary": summary}
        return count, None, report
    except UnicodeDecodeError:
        return None, "❌ Text file is not valid UTF-8.", None
    except json.JSONDecodeError as exc:
        return None, f"❌ Invalid JSON format: {exc}", None
    except RuntimeError as exc:
        # Actionable import errors (PDF detection / OCR) go straight to the
        # user without a traceback; they are logged at debug above.
        return None, f"⚠️ {exc}", {"kind": kind, "skipped": skipped}
    except Exception as exc:
        logger.exception("process_uploaded_file failed for %s", filename)
        return None, f"⚠️ Error processing file: {exc}", {"kind": kind, "skipped": skipped}
