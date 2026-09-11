"""Text helpers for the visual engine: sanitising, wrapping, labels.

All user-facing visual text goes through :func:`clean_text`, which reuses
the PDF renderer's sanitiser so labels can only contain glyphs the
vendored Hind fonts can actually render (Hindi/English Unicode support).

Drawing helpers are duck-typed on a minimal ``pdf`` protocol
(``set_font`` / ``get_string_width`` / ``set_xy`` / ``cell`` /
``set_text_color``) so layouts stay unit-testable with a recording fake
as well as with a real fpdf2 document.
"""

from __future__ import annotations

import re
from typing import Any

# Must match the family registered in pdf_service.render._Doc ("hind").
FONT_FAMILY = "hind"

TITLE_SIZE = 10.0
LABEL_SIZE = 8.5
SMALL_SIZE = 7.5
TINY_SIZE = 6.5
MIN_LABEL_SIZE = 6.0

# Line height factor for stacked label lines (mm per point).
LINE_FACTOR = 0.5

_WS_RE = re.compile(r"\s+")
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def has_devanagari(text: str) -> bool:
    """True when `text` contains any Devanagari character."""
    return bool(_DEVANAGARI_RE.search(text or ""))


def clean_text(text: object, limit: int = 200) -> str:
    """Sanitise arbitrary text for visual output (single string, no count)."""
    from ..render import sanitize_text

    cleaned, _dropped = sanitize_text(text, limit)
    return cleaned


def clean_label(text: object, limit: int = 80) -> str:
    """Sanitise to a single-line label (whitespace collapsed)."""
    return _WS_RE.sub(" ", clean_text(text, limit)).strip()


def fit_size(pdf: Any, text: str, max_width: float, start: float,
             minimum: float = MIN_LABEL_SIZE, bold: bool = False) -> float:
    """Largest font size in [minimum, start] whose width fits `max_width`.

    Deterministic: steps down in 0.5pt increments.
    """
    if max_width <= 0:
        return minimum
    size = float(start)
    style = "B" if bold else ""
    while size > minimum + 1e-9:
        pdf.set_font(FONT_FAMILY, style, size)
        if pdf.get_string_width(text) <= max_width:
            return size
        size = round(size - 0.5, 2)
    pdf.set_font(FONT_FAMILY, style, minimum)
    return minimum


def wrap_lines(pdf: Any, text: str, max_width: float, size: float,
               style: str = "", max_lines: int = 3) -> list[str]:
    """Greedy word-wrap `text` to lines fitting `max_width` at `size`.

    Overflow is truncated with an ellipsis on the last allowed line.
    Returns at least one line (possibly "").
    """
    pdf.set_font(FONT_FAMILY, style, size)
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if pdf.get_string_width(trial) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    else:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # Ellipsis when content was cut: words remain or a line was dropped.
    used_words = sum(len(line.split()) for line in lines)
    if used_words < len(words) and lines:
        last = lines[-1]
        while last and pdf.get_string_width(last + "…") > max_width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last + "…" if last else "…"
        if pdf.get_string_width(lines[-1]) > max_width:
            lines[-1] = ""
    return lines or [""]


def put_label(pdf: Any, x: float, y: float, width: float, text: str,
              size: float, bold: bool = False, align: str = "C",
              color: tuple[int, int, int] = (0, 0, 0),
              max_lines: int = 2) -> float:
    """Draw a wrapped label; returns the y-coordinate below the text."""
    if not text:
        return y
    pdf.set_text_color(*color)
    style = "B" if bold else ""
    lines = wrap_lines(pdf, text, width, size, style, max_lines)
    line_h = size * LINE_FACTOR
    for i, line in enumerate(lines):
        pdf.set_xy(x, y + i * line_h)
        pdf.cell(width, line_h, line, align=align)
    return y + len(lines) * line_h


# Short human-readable badges per visual type (rendered on the container).
BADGES = {
    "location_map": "Locator Map",
    "historical_map": "Historical Map",
    "regional_map": "Region Map",
    "process": "Process",
    "flowchart": "Flowchart",
    "concept_map": "Concept Map",
    "mind_map": "Mind Map",
    "timeline": "Timeline",
    "comparison": "Comparison",
    "cause_effect": "Cause & Effect",
    "cycle": "Cycle",
    "labelled_diagram": "Diagram",
    "classification": "Classification",
    "infographic": "Quick Revision",
}


def badge_label(visual_type: str) -> str:
    """Short badge text for a visual type id (fallback: title-cased id)."""
    return BADGES.get(visual_type, visual_type.replace("_", " ").title())
