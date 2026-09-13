"""Phase F: question + explanation enhancement (pure, shared layer).

This module is deliberately **framework-free** (standard library only) so it
can be reused by every question surface in the codebase -- runner polls and
post-answer messages, the Mini App, the WeasyPrint/PDF reports, the PDF
microservice payload, analytics snapshots and the AI importer -- without
duplicating logic or importing Telegram/DB code.

Safety contract (non-negotiable, see Phase F brief):

* The answer key (``correct_option_id``), options, scoring and question
  identity are NEVER inspected for rewriting and never modified. This module
  only reads them to validate *optional companion explanation content*.
* Nothing is ever fabricated. All output is composed exclusively from text
  the creator/importer/AI already supplied; when an explanation is missing
  the output is empty -- never invented.
* Legacy questions (a single plain ``explanation`` string, or no
  explanation at all) are byte-for-byte identical on every output channel:
  structured sections are purely additive and opt-in via the optional
  ``explanation_detail`` companion field.
* All user-supplied text is length-capped here and HTML-escaped by the
  rendering helpers, so malformed markup, Hindi/Hinglish Unicode and
  Telegram special characters can never break a message or a report.

Optional structured shape (``question["explanation_detail"]``)::

    {
        "why":      "why the marked option is correct",      # optional
        "concept":  "the core concept/distinction",         # optional
        "takeaway": "one-line exam takeaway",               # optional
        "options":  [{"index": 0, "note": "..."}],          # optional
    }

Only present, validated sections are stored/rendered. Option notes are bound
to option *indices* (letters are derived at render time after shuffling), so
a note can never point at the wrong displayed option.
"""

from __future__ import annotations

import html
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Field name / size constants
# ---------------------------------------------------------------------------

DETAIL_KEY = "explanation_detail"

_WHY_KEYS = ("why", "why_correct", "correct_why", "why_this", "reason")
_CONCEPT_KEYS = ("concept", "core_concept", "key_concept", "concept_note")
_TAKEAWAY_KEYS = ("takeaway", "key_takeaway", "remember", "exam_takeaway", "tip")
_OPTION_KEYS = (
    "options", "option_notes", "option_explanations", "distractors",
    "distractor_explanations", "why_wrong",
)
_NOTE_KEYS = ("note", "text", "explanation", "why", "reason")

# Defensive caps for the OPTIONAL structured sections: real explanations
# are far below these; they only stop a pathological document from blowing
# up a Telegram message, snapshot or PDF. The legacy ``explanation`` string
# itself is never capped at this layer (byte-preserved; each presentation
# surface enforces its own limit instead).
WHY_MAX = 1200
CONCEPT_MAX = 800
TAKEAWAY_MAX = 500
OPTION_NOTE_MAX = 300
MAX_OPTION_NOTES = 10

# One Telegram message carries at most 4096 characters; reserve room for
# the bold header/emoji and per-chunk continuation markers.
TELEGRAM_CHUNK_LIMIT = 3500
# The PDF microservice contract caps the explanation field at 12000 chars
# (pydantic) and its renderer sanitises at 10000 chars (with its own marker).
# Stay just below 10000 so OUR line-boundary truncation is final and the
# service never re-slices a composed explanation mid-line.
PDF_SERVICE_EXPLANATION_MAX = 9950

_OPTION_LETTERS = "ABCDEFGHIJ"
_BLANK_LINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Low-level sanitisation
# ---------------------------------------------------------------------------

def _clean_text(value: Any, limit: int) -> Optional[str]:
    """Coerce to a stripped single string (CRLF normalised, blank-line runs
    collapsed), capped at ``limit`` on a word boundary. None/empty -> None.
    Content itself is never rewritten, only bounded."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANK_LINES.sub("\n\n", text).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 1)]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit // 2:
        cut = cut[:space].rstrip()
    return cut + "\u2026"


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        token = value.strip()
        if token.isdigit():
            return int(token)
        if len(token) == 1 and token.upper() in _OPTION_LETTERS:
            return _OPTION_LETTERS.index(token.upper())
    return None


def normalize_option_notes(
    value: Any, option_count: Optional[int] = None,
    note_limit: int = OPTION_NOTE_MAX,
) -> list[dict]:
    """Validate option-indexed notes into ``[{"index", "note"}]``.

    Accepts a list (strings at their positional index, or dicts with an
    index/letter and a note) or a mapping (``{"0": "...", "A": "..."}``).
    Notes with missing/non-integer/out-of-range indices are dropped (never
    re-pointed), there is at most one note per index and the result is sorted
    by index and capped. No option text is ever altered.
    """
    raw_items: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        raw_items = list(value.items())
    elif isinstance(value, (list, tuple)):
        for pos, item in enumerate(value):
            if isinstance(item, dict):
                idx = None
                for k in ("index", "option", "option_index", "i", "id"):
                    if k in item:
                        idx = item[k]
                        break
                note = None
                for k in _NOTE_KEYS:
                    if item.get(k) is not None:
                        note = item[k]
                        break
                raw_items.append((idx, note))
            elif isinstance(item, str):
                raw_items.append((pos, item))
            elif item is None:
                continue
    notes: dict[int, str] = {}
    upper = option_count if isinstance(option_count, int) else len(_OPTION_LETTERS)
    for raw_idx, raw_note in raw_items:
        idx = _as_int(raw_idx)
        # Prose sections accept strings only: a number/list/dict rendered
        # via str() would surface Python noise as fabricated explanation.
        note = _clean_text(raw_note, note_limit) \
            if isinstance(raw_note, str) else None
        if idx is None or note is None:
            continue
        if idx < 0 or idx >= upper:
            continue
        notes.setdefault(idx, note)
    return [
        {"index": idx, "note": notes[idx]}
        for idx in sorted(notes)[:MAX_OPTION_NOTES]
    ]


def _first_present(src: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in src and src[key] is not None:
            return src[key]
    return None


def normalize_explanation_detail(
    value: Any, option_count: Optional[int] = None,
    note_limit: int = OPTION_NOTE_MAX,
) -> Optional[dict]:
    """Return a sanitised ``explanation_detail`` dict, or None.

    Unknown keys are ignored; every section passes through the same content
    preserving text cleaner and bounds; an entirely empty/garbage value
    normalises to None so no empty structure is ever stored.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    def _prose_w(keys: tuple[str, ...], limit: int) -> Optional[str]:
        found = _first_present(value, keys)
        return _clean_text(found, limit) if isinstance(found, str) else None
    why = _prose_w(_WHY_KEYS, WHY_MAX)
    concept = _prose_w(_CONCEPT_KEYS, CONCEPT_MAX)
    takeaway = _prose_w(_TAKEAWAY_KEYS, TAKEAWAY_MAX)
    if why:
        out["why"] = why
    if concept:
        out["concept"] = concept
    notes = normalize_option_notes(
        _first_present(value, tuple(_OPTION_KEYS)), option_count,
        note_limit=note_limit,
    )
    if notes:
        out["options"] = notes
    if takeaway:
        out["takeaway"] = takeaway
    return out or None


def normalize_question_explanation(
    question: Any, option_count: Optional[int] = None
) -> Any:
    """Return a copy of a question with sanitised explanation companions.

    * ``explanation`` stays a plain string (trimmed only; never rewritten);
      a structured object placed there by an importer is moved into
      ``explanation_detail`` (its ``why`` becomes the short explanation);
    * ``explanation_detail`` is validated and dropped entirely when empty.
    Questions/options/answer key/analytics/media are copied through
    untouched. Non-dict input is returned unchanged.
    """
    if not isinstance(question, dict):
        return question
    q = dict(question)
    if option_count is None:
        opts = q.get("options")
        option_count = len(opts) if isinstance(opts, list) else None

    raw_explanation = q.get("explanation")
    detail_source = q.get(DETAIL_KEY)

    if isinstance(raw_explanation, dict):
        # Structured object supplied in the legacy slot: split it apart.
        why = raw_explanation.get("why") or raw_explanation.get("concept")
        detail_source = raw_explanation
        raw_explanation = why if isinstance(why, str) else None
        q["explanation"] = raw_explanation if raw_explanation else None

    elif isinstance(raw_explanation, str):
        # Stored explanation text is preserved byte-for-byte (whitespace
        # included); presentation layers strip/cap at their own boundaries.
        q["explanation"] = raw_explanation
    elif raw_explanation is None:
        # Preserve explicit None / absent key as-is (do not inject keys).
        if "explanation" in q:
            q["explanation"] = None
    else:
        # Numbers/booleans/lists are not valid explanations; drop, never
        # stringify unrelated objects into a fake explanation.
        q["explanation"] = None

    detail = normalize_explanation_detail(detail_source, option_count)
    if detail is not None:
        q[DETAIL_KEY] = detail
    else:
        q.pop(DETAIL_KEY, None)
    return q


# ---------------------------------------------------------------------------
# Content accessors
# ---------------------------------------------------------------------------

def get_detail(question: Optional[dict]) -> dict:
    if not isinstance(question, dict):
        return {}
    detail = question.get(DETAIL_KEY)
    return detail if isinstance(detail, dict) else {}


def get_main_explanation(question: Optional[dict]) -> Optional[str]:
    """The canonical explanation string (exact stored text)."""
    if not isinstance(question, dict):
        return None
    raw = question.get("explanation")
    return raw if isinstance(raw, str) and raw.strip() else None


def short_explanation(question: Optional[dict], limit: int = 190) -> Optional[str]:
    """A short correct-answer explanation for Telegram's native poll field
    (hard 200-char cap). Prefers the canonical string, then the structured
    ``why``; truncated on a sentence boundary. Pure presentation -- the
    stored explanation is never touched."""
    text = get_main_explanation(question)
    if not text:
        text = get_detail(question).get("why")
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[: limit - 1]
    sentence = max(window.rfind(". "), window.rfind("। "), window.rfind("\n"))
    if sentence > limit // 2:
        return window[: sentence + 1].rstrip() + "\u2026"
    space = window.rfind(" ")
    if space > limit // 2:
        return window[:space].rstrip() + "\u2026"
    return window.rstrip() + "\u2026"


def poll_explanation(question: Optional[dict]) -> Optional[str]:
    """Source text for the native Telegram poll explanation field.

    Keeps the canonical stored explanation verbatim (the existing
    prepare_poll_data trimming is unchanged); falls back to the structured
    "why" section only when a question has structured detail but no plain
    explanation string. The caller still enforces the 200-char poll cap.
    """
    main = get_main_explanation(question)
    if main:
        return main
    why = get_detail(question).get("why")
    return why.strip() if isinstance(why, str) and why.strip() else None


# ---------------------------------------------------------------------------
# Block model / composition
# ---------------------------------------------------------------------------

def _display_index(canonical_index: int,
                   display_order: Optional[list[int]]) -> int:
    """Map a canonical option index to the letter position the player
    actually saw. ``display_order[display_position]`` holds the canonical
    index (same convention as the quiz engine's answer mapping). Without a
    permutation the canonical index IS the display index. A note whose
    option vanished from the permutation keeps its canonical index."""
    if not display_order:
        return canonical_index
    try:
        return list(display_order).index(canonical_index)
    except ValueError:
        return canonical_index


def explanation_blocks(
    question: Optional[dict], display_order: Optional[list[int]] = None
) -> list[dict]:
    """Decompose a question's explanation content into ordered, validated
    blocks: ``[{kind, text}]`` with ``kind`` in
    main/why/concept/options/takeaway. Options blocks carry ``notes`` as
    ``[{"index", "note"}]`` where ``index`` is the DISPLAY position the
    player saw (canonical when no ``display_order`` is supplied). Returns
    [] when there is nothing to show."""
    if not isinstance(question, dict):
        return []
    main = get_main_explanation(question)
    detail = get_detail(question)
    # Coerce defensively: renderers may read question documents that never
    # passed through a write chokepoint (legacy restores/imports), so never
    # trust that stored sections are clean strings. Prose accepts strings
    # only -- never str() a number/list/dict into explanation-looking text.
    def _prose(key: str, limit: int) -> Optional[str]:
        value = detail.get(key)
        return _clean_text(value, limit) if isinstance(value, str) else None
    why = _prose("why", WHY_MAX)
    concept = _prose("concept", CONCEPT_MAX)
    takeaway = _prose("takeaway", TAKEAWAY_MAX)
    blocks: list[dict] = []
    if main:
        blocks.append({"kind": "main", "text": main})
    if why and why != (main or "").strip():
        blocks.append({"kind": "why", "text": why})
    if concept:
        blocks.append({"kind": "concept", "text": concept})
    notes = normalize_option_notes(
        detail.get("options"),
        len(question["options"]) if isinstance(question.get("options"), list)
        else None,
    )
    if notes:
        # Re-base the canonical note indices onto the shuffled display the
        # player actually voted on, so "A) why it's wrong" never points at a
        # different option than the poll did.
        display_notes = [
            {"index": _display_index(n["index"], display_order),
             "note": n["note"]}
            for n in notes
        ]
        display_notes.sort(key=lambda n: n["index"])
        blocks.append({"kind": "options", "notes": display_notes})
    if takeaway:
        blocks.append({"kind": "takeaway", "text": takeaway})
    return blocks


def option_letter(index: int) -> str:
    return _OPTION_LETTERS[index] if 0 <= index < len(_OPTION_LETTERS) else str(index + 1)


# ---- plain-text rendering (Mini App, PDF microservice, interactive HTML
# report which renders pre-wrapped text) ----------------------------------

_TEXT_LABELS = {
    "why": "Why the correct answer is right:",
    "concept": "Core concept:",
    "takeaway": "Takeaway:",
    "options": "Option notes:",
}


def render_plain_text(
    question: Optional[dict], *, max_len: Optional[int] = None,
    display_order: Optional[list[int]] = None,
) -> str:
    """Compose a single plain-text explanation.

    A legacy question with only an ``explanation`` string returns that
    string verbatim; structured companions append clearly labelled
    sections. ``max_len`` (if given) bounds the result on a line boundary.
    """
    blocks = explanation_blocks(question, display_order)
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0]["kind"] == "main":
        text = blocks[0]["text"]
    else:
        parts: list[str] = []
        for block in blocks:
            kind = block["kind"]
            if kind == "main":
                parts.append(block["text"])
            elif kind == "options":
                # Blank-line separated: Markdown renderers (Mini App) turn
                # single newlines into spaces, while pre-wrap/PDF surfaces
                # simply show a little extra vertical breathing room.
                lines = [_TEXT_LABELS["options"]]
                lines.extend(
                    f"{option_letter(n['index'])}. {n['note']}"
                    for n in block["notes"]
                )
                parts.append("\n\n".join(lines))
            else:
                parts.append(f"{_TEXT_LABELS[kind]} {block['text']}")
        text = "\n\n".join(parts)
    if max_len and len(text) > max_len:
        window = text[: max_len - 1]
        boundary = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        if boundary > max_len // 2:
            text = window[:boundary].rstrip() + "\u2026"
        else:
            text = window.rstrip() + "\u2026"
    return text


# ---- Telegram HTML rendering (safe, chunked) ------------------------------

_HTML_LABELS = {
    "why": "Why the correct answer is right",
    "concept": "Core concept",
    "takeaway": "Takeaway",
    "options": "Option notes",
}
_HEADER = "\U0001f4a1 <b>Explanation:</b>"


# Tags Telegram's HTML parser actually understands. Anything else that
# *looks* like a tag ("<because>", "<3", "a<b") must be escaped, otherwise
# send_message(ParseMode.HTML) 400s and the explanation is lost.
_TG_ALLOWED_TAG = re.compile(
    r"</?\s*(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|"
    r"blockquote|br)\b(?:[^<>]*)/?>",
    re.IGNORECASE,
)


def html_message_safe(markup: str) -> bool:
    """True when every ``<...>`` in *markup* is a Telegram-supported tag."""
    residual = _TG_ALLOWED_TAG.sub("", markup)
    return "<" not in residual and ">" not in residual


def _html_paragraph(text: str) -> str:
    """Safe Telegram HTML for one paragraph: prefer the existing
    important-word bolding, but if the 'authored markup' pass-through would
    leave an unsupported tag behind, escape the original instead."""
    from quizbot.shared.bold_words import format_bold_html
    rendered, formatted = format_bold_html(text)
    if formatted or html_message_safe(rendered):
        return rendered
    return html.escape(text, quote=False)


def _bold_paragraphs(text: str) -> str:
    """HTML for one authored plain-text section, with multi-paragraph line
    breaks preserved. Never emits unparseable HTML (see _html_paragraph)."""
    return "\n".join(_html_paragraph(p) if p.strip() else ""
                     for p in text.split("\n"))


def _block_html_lines(block: dict) -> list[str]:
    kind = block["kind"]
    if kind == "main":
        return _bold_paragraphs(block["text"]).split("\n")
    if kind == "options":
        lines = [f"<b>{_HTML_LABELS['options']}</b>"]
        for n in block["notes"]:
            letter = option_letter(n["index"])
            lines.append(
                f"<b>{html.escape(letter, quote=False)}.</b> "
                f"{html.escape(n['note'], quote=False)}"
            )
        return lines
    label = _HTML_LABELS[kind]
    icon = "\U0001f4cc" if kind == "takeaway" else "\u2728"
    body = _bold_paragraphs(block["text"])
    return [f"{icon} <b>{html.escape(label, quote=False)}</b>", body]


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    """Pack rendered lines into chunks <= limit chars, splitting an over-long
    single line on whitespace. Opening tags and closing tags always live on
    the same line (we only wrap whole lines), so chunks stay well-formed."""
    chunks: list[str] = []
    current = ""

    def _safe_cut(p: str) -> int:
        """Whitespace cut <= limit that never slices through an HTML tag
        (an ``<a href=...>`` can contain spaces)."""
        cut = p.rfind(" ", 0, limit)
        if cut <= 0:
            cut = min(limit, len(p))
        head = p[:cut]
        last_open, last_close = head.rfind("<"), head.rfind(">")
        if last_open > last_close:
            # A tag starts in the head and does not finish there.
            close = p.find(">", last_open)
            space_after = p.find(" ", close + 1) if close != -1 else -1
            if close != -1 and space_after != -1 and space_after <= limit:
                return space_after
            before = p.rfind(" ", 0, last_open)
            if before > 0:
                return before
        return cut

    for line in lines:
        piece = line
        while len(piece) > limit:
            cut = _safe_cut(piece)
            if cut <= 0:
                cut = min(limit, len(piece))
            head, piece = piece[:cut].rstrip(), piece[cut:].lstrip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        candidate = piece if not current else current + "\n" + piece
        if len(candidate) > limit:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def render_telegram_messages(
    question: Optional[dict], *,
    display_order: Optional[list[int]] = None,
) -> list[str]:
    """Render zero or more Telegram-HTML message bodies for a question's
    explanation, each safely under the message limit. Returns [] when there
    is no explanation at all (caller then sends nothing, as before).

    Legacy, single-string explanations render exactly as they did pre-Phase
    F (same header, same bolding) -- except content past 4000 chars is now
    continued in labelled follow-up messages instead of being silently cut.
    """
    blocks = explanation_blocks(question, display_order)
    if not blocks:
        return []
    lines: list[str] = []
    for i, block in enumerate(blocks):
        if i == 0 and block["kind"] == "main":
            lines.extend(_block_html_lines(block))
        else:
            if lines:
                lines.append("")
            lines.extend(_block_html_lines(block))

    body_limit = TELEGRAM_CHUNK_LIMIT - len(_HEADER) - 20
    chunks = _chunk_lines(lines, body_limit)
    messages: list[str] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        head = _HEADER if i == 0 else "\U0001f4a1 <b>Explanation (cont. %d/%d):</b>" % (
            i + 1, total)
        messages.append(f"{head}\n\n{chunk}")
    return messages


def is_authored_rich(question: Optional[dict]) -> bool:
    """Whether ANY explanation text already carries rich/math markup that
    should take the sendRichMessage path (mirrors the pre-Phase-F check on
    the single explanation string, extended to structured sections)."""
    from quizbot.shared.rich_quiz import _is_rich
    if _is_rich(get_main_explanation(question)):
        return True
    detail = get_detail(question)
    for key in ("why", "concept", "takeaway"):
        value = detail.get(key)
        if isinstance(value, str) and _is_rich(value):
            return True
    raw_notes = detail.get("options")
    if isinstance(raw_notes, list):
        for n in raw_notes:
            if isinstance(n, dict) and isinstance(n.get("note"), str) \
                    and _is_rich(n["note"]):
                return True
    return False


def render_rich_markdown(
    question: Optional[dict], *,
    display_order: Optional[list[int]] = None,
) -> str:
    """Compose authored-rich explanation content for sendRichMessage.
    Markdown-style labels wrap structured sections; legacy content is
    returned verbatim (its own markup preserved)."""
    blocks = explanation_blocks(question, display_order)
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0]["kind"] == "main":
        return blocks[0]["text"]
    parts: list[str] = []
    for block in blocks:
        kind = block["kind"]
        if kind == "main":
            parts.append(block["text"])
        elif kind == "options":
            lines = [f"**{_HTML_LABELS['options']}**"]
            lines.extend(
                f"**{option_letter(n['index'])}.** {n['note']}"
                for n in block["notes"]
            )
            parts.append("\n".join(lines))
        elif kind == "takeaway":
            parts.append(f"📌 **{_HTML_LABELS[kind]}**\n{block['text']}")
        else:
            parts.append(f"**{_HTML_LABELS[kind]}**\n{block['text']}")
    return "\n\n---\n\n".join(parts)


# ---- HTML report rendering (WeasyPrint; values escaped) -------------------

def render_report_html(
    question: Optional[dict], *,
    esc=lambda text: html.escape(text, quote=False),
    display_order: Optional[list[int]] = None,
) -> str:
    """Render the explanation box inner HTML for an HTML/PDF report.

    ``esc`` escapes (and may post-process, e.g. math-to-MathML) each text
    fragment. With the default escaper a legacy plain explanation renders
    exactly as ``html.escape(text, quote=False)`` (the previous WeasyPrint
    behaviour up to its own escaper); structured sections append labelled,
    escaped blocks.
    """
    blocks = explanation_blocks(question, display_order)
    if not blocks:
        return ""

    def e(text: str) -> str:
        return esc(text)

    parts: list[str] = []
    for block in blocks:
        kind = block["kind"]
        if kind in ("main", "why", "concept", "takeaway"):
            if kind == "main":
                parts.append(e(block["text"]))
            else:
                parts.append(
                    f"<strong>{e(_HTML_LABELS[kind])}:</strong> "
                    f"{e(block['text'])}"
                )
        else:
            rows = [f"<strong>{e(_HTML_LABELS['options'])}:</strong>"]
            for n in block["notes"]:
                rows.append(f"{e(option_letter(n['index']))}. {e(n['note'])}")
            parts.append("<br/>".join(rows))
    return '<br/><br/>'.join(parts)


# ---------------------------------------------------------------------------
# Read-only question health diagnostics (never mutate, never decide scoring)
# ---------------------------------------------------------------------------

def question_health_flags(question: Any) -> list[str]:
    """Return stable diagnostic codes for a question document.

    This is a read-only linter used by tests/importers; it NEVER changes the
    answer key and scoring code must not consult it. It exists so a
    contradictory/malformed document can be reported honestly instead of
    being guessed at.
    """
    flags: list[str] = []
    if not isinstance(question, dict):
        return ["not_an_object"]
    stem = question.get("question")
    if not isinstance(stem, str) or not stem.strip():
        flags.append("missing_question")
    options = question.get("options")
    if not isinstance(options, list) or len(options) < 2:
        flags.append("options_missing")
    else:
        if any(not isinstance(o, str) or not o.strip() for o in options):
            flags.append("empty_option")
        seen = set()
        for o in options:
            key = o.strip().casefold() if isinstance(o, str) else object()
            if key in seen:
                flags.append("duplicate_options")
                break
            seen.add(key)
        correct = question.get("correct_option_id")
        ids = correct if isinstance(correct, list) else [correct]
        if not ids:
            flags.append("answer_missing")
        for cid in ids:
            if isinstance(cid, bool) or not isinstance(cid, int) or not (0 <= cid < len(options)):
                flags.append("answer_out_of_range")
                break
    explanation = question.get("explanation")
    if explanation is not None and not isinstance(explanation, str):
        flags.append("explanation_not_text")
    elif isinstance(explanation, str) and not explanation.strip():
        flags.append("explanation_empty")
    detail = question.get(DETAIL_KEY)
    if detail is not None:
        if not isinstance(detail, dict):
            flags.append("detail_not_object")
        else:
            normalized = normalize_explanation_detail(
                detail, len(options) if isinstance(options, list) else None)
            if detail and normalized is None:
                flags.append("detail_unparseable")
            elif isinstance(options, list):
                raw_notes = detail.get("options")
                raw_count = len(raw_notes) if isinstance(raw_notes, (list, dict)) else 0
                kept = len(normalized.get("options", [])) if normalized else 0
                if raw_count and not kept:
                    flags.append("detail_option_index_out_of_range")
    return flags
