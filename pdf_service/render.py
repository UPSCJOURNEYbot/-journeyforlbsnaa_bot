"""Professional Test Series PDF renderer (fpdf2 + HarfBuzz shaping).

Produces print-friendly A4 PDFs branded "Journey for लबासना" with correct
Devanagari shaping (via uharfbuzz) and vendored OFL Hind fonts -- no system
libraries or fonts required.

Two layouts, matching the bot contract (`solution_display`):
  - "inline": answer + explanation after every question.
  - "end"   : questions first, then an Answer Key table + Detailed Solutions.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).resolve().parent / "fonts"
FONT_REGULAR = FONTS_DIR / "Hind-Regular.ttf"
FONT_BOLD = FONTS_DIR / "Hind-Bold.ttf"

BRAND = "Journey for लबासना"

# Generous truncation guards keep any single pathological field from
# exploding layout; normal quiz content never approaches these.
MAX_QUESTION_CHARS = 5000
MAX_OPTION_CHARS = 500
MAX_EXPLANATION_CHARS = 10000
MAX_TITLE_CHARS = 300

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Text fallbacks for pictographs/symbols the PDF fonts cannot render.
# Anything else outside the fonts' coverage is dropped (counted in logs).
_EMOJI_FALLBACKS = {
    "✅": "[Correct] ", "✔️": "[Correct] ", "✔": "[Correct] ",
    "❌": "[Wrong] ", "✖️": "[Wrong] ", "✖": "[Wrong] ",
    "➡️": "-> ", "➡": "-> ", "⬅️": "<- ", "⬆️": "^ ", "⬇️": "v ",
    "⚠️": "[!] ", "⚠": "[!] ", "💡": "Tip: ", "📌": "* ", "📚": "",
    "📝": "", "🎯": "", "📊": "", "⏱️": "", "⏱": "", "⏰": "",
    "🔑": "Key: ", "❓": "? ", "❔": "? ", "⭐": "*", "🌟": "*",
    "👉": "-> ", "👈": "<- ", "🔥": "", "🎉": "", "🙏": "",
}

_cmap_cache: Optional[set[int]] = None


def _supported_codepoints() -> set[int]:
    """Union of cmap codepoints of both vendored fonts (cached)."""
    global _cmap_cache
    if _cmap_cache is not None:
        return _cmap_cache
    from fontTools.ttLib import TTFont  # fpdf2 dependency; always present

    covered: set[int] = set()
    for path in (FONT_REGULAR, FONT_BOLD):
        try:
            font = TTFont(str(path), lazy=True)
            for table in font["cmap"].tables:
                if table.isUnicode():
                    covered.update(table.cmap.keys())
        except Exception:
            logger.exception("Could not read cmap from %s", path)
    _cmap_cache = covered
    return covered


def sanitize_text(value: object, limit: int) -> tuple[str, int]:
    """Clean arbitrary quiz text for PDF output.

    Unescapes entities, normalises newlines, strips control characters,
    truncates to `limit`, and replaces/drops glyphs the fonts cannot render.
    Returns (clean_text, dropped_count).
    """
    text = html.unescape("" if value is None else str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    covered = _supported_codepoints()
    out: list[str] = []
    dropped = 0
    for ch in text:
        cp = ord(ch)
        if ch == "\n" or cp < 128 or cp in covered:
            out.append(ch)
        elif ch in _EMOJI_FALLBACKS:
            out.append(_EMOJI_FALLBACKS[ch])
        elif cp in (0x200C, 0x200D):  # ZWNJ/ZWJ shape Devanagari; keep
            out.append(ch)
        else:
            dropped += 1
    return "".join(out), dropped


def answer_letters(correct: object, option_count: int) -> str:
    """Normalise a correct_option_id (int or list) to 'B' / 'A, C' form."""
    if isinstance(correct, bool):
        return "—"
    ids = list(correct) if isinstance(correct, (list, tuple)) else [correct]
    letters = sorted({chr(65 + int(i)) for i in ids
                      if isinstance(i, (int, float)) and 0 <= int(i) < option_count})
    return ", ".join(letters) if letters else "—"


class _Doc:
    """Thin fpdf2 wrapper with brand header/footer + spacing helpers."""

    def __init__(self, exam_title: str) -> None:
        from fpdf import FPDF, XPos, YPos  # noqa: F401  (re-exported)

        self._XPos = XPos
        self._YPos = YPos
        self.exam_title = exam_title

        class _PDF(FPDF):
            pass

        pdf = _PDF(orientation="P", unit="mm", format="A4")
        pdf.set_auto_page_break(True, margin=20)
        pdf.set_margins(15, 14, 15)
        pdf.add_font("hind", "", str(FONT_REGULAR))
        pdf.add_font("hind", "B", str(FONT_BOLD))
        try:
            pdf.set_text_shaping(True)  # HarfBuzz: correct Devanagari
        except Exception:
            logger.warning("Text shaping unavailable; Devanagari may mis-shape")
        pdf.alias_nb_pages("{nb}")

        outer = self

        def _header() -> None:
            if pdf.page_no() == 1:
                return  # first page carries the full cover block instead
            pdf.set_font("hind", "B", 8)
            pdf.set_text_color(90, 90, 90)
            pdf.cell(0, 6, f"{BRAND}  •  {outer.exam_title[:60]}",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_draw_color(180, 180, 180)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(3)
            pdf.set_text_color(0, 0, 0)

        def _footer() -> None:
            pdf.set_y(-14)
            pdf.set_draw_color(180, 180, 180)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.set_font("hind", "", 8)
            pdf.set_text_color(100, 100, 100)
            pdf.cell(0, 8, f"{BRAND}  •  Page {pdf.page_no()} / {{nb}}",
                     align="C")

        pdf.header = _header  # type: ignore[method-assign]
        pdf.footer = _footer  # type: ignore[method-assign]
        self.pdf = pdf

    # -- helpers ------------------------------------------------------
    def ensure_space(self, mm: float) -> None:
        if self.pdf.get_y() + mm > self.pdf.page_break_trigger:
            self.pdf.add_page()

    def heading(self, text: str, size: int = 13) -> None:
        pdf = self.pdf
        self.ensure_space(14)
        pdf.set_font("hind", "B", size)
        pdf.set_text_color(20, 40, 90)
        pdf.multi_cell(0, 6.5, text, new_x=self._XPos.LMARGIN,
                       new_y=self._YPos.NEXT)
        pdf.set_draw_color(20, 40, 90)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 40, pdf.get_y())
        pdf.ln(3)
        pdf.set_text_color(0, 0, 0)

    def body(self, text: str, size: int = 10.5, bold: bool = False,
             color: tuple[int, int, int] = (0, 0, 0)) -> None:
        pdf = self.pdf
        pdf.set_font("hind", "B" if bold else "", size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, 5.2, text, new_x=self._XPos.LMARGIN,
                       new_y=self._YPos.NEXT)

    def filled_block(self, text: str, size: int = 10,
                     fill: tuple[int, int, int] = (243, 244, 246)) -> None:
        pdf = self.pdf
        pdf.set_font("hind", "", size)
        pdf.set_text_color(30, 30, 30)
        pdf.set_fill_color(*fill)
        x = pdf.l_margin + 4
        pdf.set_x(x)
        pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 4, 5.2, text,
                       new_x=self._XPos.LMARGIN, new_y=self._YPos.NEXT,
                       fill=True)


def _cover(doc: _Doc, *, exam_title: str, tagline: str, quiz_names: list[str],
           total: int) -> None:
    pdf = doc.pdf
    pdf.add_page()
    pdf.set_font("hind", "B", 24)
    pdf.set_text_color(20, 40, 90)
    pdf.multi_cell(0, 11, BRAND, align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.set_font("hind", "", 13)
    pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(0, 7, tagline or "Test Series", align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)
    pdf.set_draw_color(20, 40, 90)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin + 30, pdf.get_y(), pdf.w - pdf.r_margin - 30,
             pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.ln(5)
    pdf.set_font("hind", "B", 16)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 8, exam_title, align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)
    names = ", ".join(n for n in quiz_names if n) or "—"
    pdf.set_font("hind", "", 10.5)
    pdf.set_text_color(50, 50, 50)
    pdf.multi_cell(0, 5.5, f"Source quiz(zes): {names}", align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.multi_cell(
        0, 5.5,
        f"Total questions: {total}   •   "
        f"Generated: {datetime.now().strftime('%d %b %Y')}",
        align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(4)
    pdf.set_font("hind", "B", 11)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 6, "Instructions", new_x=doc._XPos.LMARGIN,
             new_y=doc._YPos.NEXT)
    pdf.set_font("hind", "", 10)
    pdf.set_text_color(40, 40, 40)
    for line in (
        "• Read every question carefully before answering.",
        "• Each question has one or more correct options as shown in the key.",
        "• There is no negative marking unless your instructor says otherwise.",
        "• Review the answer key and explanations after completing the test.",
    ):
        pdf.multi_cell(0, 5.2, line, new_x=doc._XPos.LMARGIN,
                       new_y=doc._YPos.NEXT)
    pdf.ln(2)


def _question_block(doc: _Doc, number: int, question: dict,
                    show_solution: bool) -> None:
    """Render one question (+options, +solution when inline mode)."""
    pdf = doc.pdf
    q_text, _ = sanitize_text(question.get("question", ""), MAX_QUESTION_CHARS)
    options = [sanitize_text(o, MAX_OPTION_CHARS)[0]
               for o in question.get("options", [])]
    letters = answer_letters(question.get("correct_option_id"),
                            len(options))
    explanation, _ = sanitize_text(question.get("explanation", ""),
                                   MAX_EXPLANATION_CHARS)

    doc.ensure_space(42)
    pdf.set_font("hind", "B", 11)
    pdf.set_text_color(0, 0, 0)
    prefix = f"Q{number}. "
    pdf.set_font("hind", "", 10.5)
    # Number in bold, stem in regular: write number, then flow the stem.
    pdf.set_font("hind", "B", 10.5)
    pdf.write(5.4, prefix)
    pdf.set_font("hind", "", 10.5)
    pdf.multi_cell(0, 5.4, q_text or "—",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(1)
    for idx, opt in enumerate(options):
        label = chr(65 + idx)
        pdf.set_font("hind", "", 10.5)
        x = pdf.l_margin + 6
        pdf.set_x(x)
        pdf.set_font("hind", "B", 10.5)
        pdf.write(5.2, f"{label}) ")
        pdf.set_font("hind", "", 10.5)
        # Indent wrapped lines under the option text, not the label.
        pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - 6, 5.2,
                       opt or "—", new_x=doc._XPos.LMARGIN,
                       new_y=doc._YPos.NEXT)
    if show_solution:
        pdf.ln(1)
        doc.ensure_space(24)
        pdf.set_font("hind", "B", 10.5)
        pdf.set_text_color(22, 101, 52)  # dark green, print-safe
        pdf.multi_cell(0, 5.4, f"Answer: {letters}",
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        if explanation:
            doc.filled_block(f"Explanation: {explanation}")
        pdf.ln(1)
    pdf.ln(2.5)


def _answer_key_grid(doc: _Doc, questions: list[dict]) -> None:
    """Compact multi-column answer key (flows across pages as needed)."""
    pdf = doc.pdf
    doc.heading("Answer Key", size=14)
    cols = 5
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    col_w = usable / cols
    cell_h = 7.2
    y = pdf.get_y()
    x0 = pdf.l_margin
    for i, q in enumerate(questions):
        options = q.get("options", []) or []
        letters = answer_letters(q.get("correct_option_id"), len(options))
        col = i % cols
        if col == 0 and i > 0:
            y += cell_h
        if y + cell_h > pdf.page_break_trigger:
            pdf.add_page()
            y = pdf.get_y()
        pdf.set_xy(x0 + col * col_w, y)
        pdf.set_font("hind", "", 10)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(col_w, cell_h, f"Q{i + 1} – {letters}")
    pdf.set_xy(pdf.l_margin, y + cell_h + 4)


def _detailed_solutions(doc: _Doc, questions: list[dict]) -> None:
    doc.heading("Detailed Solutions", size=14)
    for i, q in enumerate(questions, 1):
        options = q.get("options", []) or []
        letters = answer_letters(q.get("correct_option_id"), len(options))
        explanation, _ = sanitize_text(q.get("explanation", ""),
                                       MAX_EXPLANATION_CHARS)
        doc.ensure_space(26)
        pdf = doc.pdf
        pdf.set_font("hind", "B", 10.5)
        pdf.multi_cell(0, 5.4, f"Q{i}.  Answer: {letters}",
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        if explanation:
            doc.filled_block(explanation)
        else:
            pdf.set_font("hind", "", 10)
            pdf.set_text_color(120, 120, 120)
            pdf.multi_cell(0, 5.2, "No explanation provided.",
                           new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(2)


def render_testseries_pdf(
    questions: list[dict],
    *,
    exam_title: str,
    tagline: str = "Test Series",
    quiz_names: Optional[list[str]] = None,
    solution_display: str = "end",
    output_path: str | Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Render the test-series PDF. Returns {"pages", "questions", "bytes"}.

    Raises ValueError on empty/invalid input, RuntimeError on render failure.
    """
    if not questions:
        raise ValueError("No questions to render.")
    title, _ = sanitize_text(exam_title or "Mock Test", MAX_TITLE_CHARS)
    tag, _ = sanitize_text(tagline or "Test Series", 200)
    names = [sanitize_text(n, 200)[0] for n in (quiz_names or []) if n]
    inline = (solution_display or "end").lower() == "inline"

    doc = _Doc(exam_title=title or "Mock Test")
    _cover(doc, exam_title=title or "Mock Test", tagline=tag or "Test Series",
           quiz_names=names, total=len(questions))
    doc.pdf.add_page()
    doc.heading("Questions", size=14)
    total = len(questions)
    for i, q in enumerate(questions, 1):
        _question_block(doc, i, q, show_solution=inline)
        if progress_cb is not None and (i % 25 == 0 or i == total):
            progress_cb(i, total)
    if not inline:
        doc.pdf.add_page()
        _answer_key_grid(doc, questions)
        doc.pdf.ln(4)
        _detailed_solutions(doc, questions)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.pdf.output(str(out))
    except Exception as exc:
        raise RuntimeError(f"PDF rendering failed: {exc}") from exc
    size = out.stat().st_size
    if size <= 0:
        raise RuntimeError("PDF rendering produced an empty file.")
    return {"pages": doc.pdf.page_no(), "questions": total, "bytes": size}
