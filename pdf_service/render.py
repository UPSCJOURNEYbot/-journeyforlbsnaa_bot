"""Professional Test Series PDF renderer (fpdf2 + HarfBuzz shaping).

Produces print-friendly A4 PDFs branded "Journey for लबासना" with correct
Devanagari shaping (via uharfbuzz) and vendored OFL Hind fonts -- no system
libraries or fonts required.

Two layouts, matching the bot contract (`solution_display`):
  - "inline": answer + explanation after every question.
  - "end"   : questions first, then an Answer Key table + Detailed Solutions.
"""

from __future__ import annotations

import base64
import html
import logging
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Must run before any FPDF is constructed: replaces fpdf2 2.8.8's
# cluster -> ToUnicode assignment so split Devanagari clusters (pre-base
# matras, reph) do not emit unmapped raw-CID glyphs inside Hindi words.
from .shape_compat import apply_harfbuzz_tounicode_fix  # noqa: E402

apply_harfbuzz_tounicode_fix()

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


# ── Phase 4 M2: professional series setup (optional, lenient) ─────────
_SETUP_IMAGE_MAX_BYTES = 6 * 1024 * 1024
_SETUP_IMAGE_MAX_PX = 800
# Pre-decode pixel cap (header-only check): a small file can still describe
# a gigapixel image, and full-decoding it would spike RAM (a 117 KB
# 6000x6000 PNG costs ~275 MB transient). 16 MP is far beyond any logo or
# watermark need (both downscale to <= 800 px) while keeping the worst
# transient under ~100 MB.
_SETUP_IMAGE_MAX_MP = 16_000_000


def _setup_str(value: object, limit: int) -> str:
    """Coerce a setup scalar to sanitised text ("" when missing/weird)."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return sanitize_text(value, limit)[0]


def _setup_float(value: object, default: float, lo: float, hi: float) -> float:
    """Coerce a setup number, falling back to `default` when unusable."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not (lo <= number <= hi):
        return default
    return number


def _setup_image(value: object, *, wash: bool = False):
    """Decode + normalise a base64 setup image.

    Returns (png_bytes, width_px, height_px) or None. Never raises: a
    missing/corrupt/oversized image is simply skipped, never fatal.
    `wash` blends the image toward white for watermark use. WebP/JPEG/PNG
    uploads all normalise to PNG; aspect ratio is always preserved.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > _SETUP_IMAGE_MAX_BYTES:
        return None
    try:
        from PIL import Image

        img = Image.open(BytesIO(raw))
        width, height = img.size  # header only: no pixels decoded yet
        if (width <= 0 or height <= 0
                or width * height > _SETUP_IMAGE_MAX_MP):
            return None
        img.load()
        img = img.convert("RGB")
        img.thumbnail((_SETUP_IMAGE_MAX_PX, _SETUP_IMAGE_MAX_PX))
        if wash:
            white = Image.new("RGB", img.size, (255, 255, 255))
            img = Image.blend(img, white, 0.85)
        buf = BytesIO()
        img.save(buf, "PNG")
        return (buf.getvalue(), img.size[0], img.size[1])
    except Exception:
        logger.warning("Series-setup image skipped (unreadable).",
                       exc_info=True)
        return None


class _SeriesSetup:
    """Lenient view over the bot's optional `series_setup` mapping.

    Unknown/mistyped values fall back to legacy behavior; a missing or
    empty mapping means plain /testseries rendering (cover, key, solutions
    and visuals exactly as before). This constructor never raises.
    """

    def __init__(self, raw: object) -> None:
        data = raw if isinstance(raw, dict) else {}
        self.active = bool(data)
        self.subject = ""
        self.test_number = ""
        self.booklet_series = ""
        self.booklet_number = ""
        self.paper = ""
        self.test_code = ""
        self.duration = ""
        self.institute_name = ""
        self.watermark_mode = "none"
        self.watermark_text = ""
        self.candidate_fields: list[str] = []
        self.answer_key = True
        self.solutions = True
        self.answer_sheet = False
        self.visuals = "auto"
        self.marks_correct = 2.0
        self.marks_negative = -0.66
        self.logo = None
        self.wm_image = None
        if not self.active:
            return
        try:
            self.subject = _setup_str(data.get("subject"), 80)
            self.test_number = _setup_str(data.get("test_number"), 20)
            self.booklet_series = _setup_str(data.get("booklet_series"), 10)
            self.booklet_number = _setup_str(data.get("booklet_number"), 20)
            self.paper = _setup_str(data.get("paper"), 40)
            self.test_code = _setup_str(data.get("test_code"), 30)
            self.duration = _setup_str(data.get("duration"), 40)
            self.institute_name = _setup_str(data.get("institute_name"), 80)
            mode = _setup_str(data.get("watermark_mode"), 10).lower()
            if mode in ("none", "text", "image", "both"):
                self.watermark_mode = mode
            self.watermark_text = _setup_str(data.get("watermark_text"), 60)
            fields = data.get("candidate_fields")
            if isinstance(fields, (list, tuple)):
                self.candidate_fields = [
                    f for f in (_setup_str(v, 60) for v in fields) if f][:7]
            ak = data.get("answer_key", True)
            self.answer_key = ak if isinstance(ak, bool) else True
            so = data.get("solutions", True)
            self.solutions = so if isinstance(so, bool) else True
            sh = data.get("answer_sheet", False)
            self.answer_sheet = sh if isinstance(sh, bool) else False
            vi = data.get("visuals", "auto")
            self.visuals = vi if vi in ("auto", "yes", "no") else "auto"
            self.marks_correct = _setup_float(data.get("marks_correct"),
                                              2.0, 0, 100)
            if self.marks_correct <= 0:
                self.marks_correct = 2.0
            self.marks_negative = _setup_float(data.get("marks_negative"),
                                               -0.66, -100, 0)
            self.logo = _setup_image(data.get("logo_b64"))
            self.wm_image = _setup_image(data.get("wm_image_b64"), wash=True)
        except Exception:
            logger.exception("Bad series_setup ignored; legacy rendering.")
            self.active = False

    @property
    def booklet_display(self) -> str:
        if self.booklet_series and self.booklet_number:
            return f"{self.booklet_series}-{self.booklet_number}"
        return self.booklet_series or self.booklet_number


def _fit_mm(pw: int, ph: int, max_w: float, max_h: float) -> tuple[float, float]:
    """Scale pixel dims into an mm box, preserving aspect ratio."""
    scale = min(max_w / max(1, pw), max_h / max(1, ph))
    return (max(1.0, pw * scale), max(1.0, ph * scale))


def _fit_text(pdf, text: str, max_w: float) -> str:
    """Ellipsise `text` to `max_w` mm in the pdf's current font."""
    if pdf.get_string_width(text) <= max_w:
        return text
    while text and pdf.get_string_width(text + "…") > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _fmt_plus(value: float) -> str:
    return "+%g" % value


def _watermark_text(pdf, text: str, cx: float, cy: float, size: int,
                    max_w: float) -> None:
    """One faint diagonal watermark line centred on (cx, cy)."""
    pdf.set_font("hind", "B", size)
    while size > 14 and pdf.get_string_width(text) > max_w:
        size -= 2
        pdf.set_font("hind", "B", size)
    pdf.set_text_color(208, 208, 208)
    with pdf.rotation(45, cx, cy):
        pdf.set_xy(cx - max_w / 2, cy - 8)
        pdf.cell(max_w, 16, text, align="C")
    pdf.set_text_color(0, 0, 0)


def _watermark_image(pdf, prepared, cx: float, cy: float, box: float) -> None:
    """One faint centred watermark image (aspect preserved)."""
    data, pw, ph = prepared
    w, h = _fit_mm(pw, ph, box, box)
    pdf.image(BytesIO(data), x=cx - w / 2, y=cy - h / 2, w=w, h=h)


def _draw_watermark(pdf, *, mode: str, text: str, image) -> None:
    """Faint per-page watermark, drawn first so it sits under content.

    Single modes centre on the page; "both" offsets text (upper diagonal)
    and image (lower centre) so the two never overlap. Cursor-neutral
    (the page header/content that follows must start at the top margin)
    and never raises.
    """
    try:
        saved = (pdf.get_x(), pdf.get_y())
    except Exception:
        saved = None
    try:
        if mode in ("text", "both") and text:
            if mode == "both":
                _watermark_text(pdf, text, 105, 80, 36, 120.0)
            else:
                _watermark_text(pdf, text, 105, 148, 46, 175.0)
        if mode in ("image", "both") and image:
            if mode == "both":
                _watermark_image(pdf, image, 105, 215, 90.0)
            else:
                _watermark_image(pdf, image, 105, 148, 130.0)
    except Exception:
        logger.exception("Watermark skipped after failure")
    finally:
        if saved is not None:
            try:
                pdf.set_xy(*saved)
            except Exception:
                pass


class _Doc:
    """Thin fpdf2 wrapper with brand header/footer + spacing helpers."""

    def __init__(self, exam_title: str, *, watermark_mode: str = "none",
                 watermark_text: str = "", watermark_image=None) -> None:
        from fpdf import FPDF, XPos, YPos  # noqa: F401  (re-exported)

        self._XPos = XPos
        self._YPos = YPos
        self.exam_title = exam_title
        self.watermark_mode = watermark_mode
        self.watermark_text = watermark_text
        self.watermark_image = watermark_image

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
            if outer.watermark_mode != "none":
                # Background on EVERY page (cover included), under content.
                _draw_watermark(pdf, mode=outer.watermark_mode,
                                text=outer.watermark_text,
                                image=outer.watermark_image)
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


def _maybe_solution_visual(doc: _Doc, question: dict,
                           visuals: str = "auto") -> None:
    """Draw an optional Phase-3 solution visual below the explanation.

    Question-first viz path (required for /newseries): the visual engine
    classifies intent from the question text alone (question requirement
    > visual capability > available data), while structural evidence may
    come from the explanation. This preserves educational value while
    ensuring question intent drives visuals (Rule-13).

    The visual engine decides whether a visual is educationally useful
    and reliable; without a decision the solution stays text-only.
    `visuals="no"` suppresses optional visuals entirely; "auto" and "yes"
    both defer to the engine (a forced visual could fabricate facts).
    This function never raises: visuals are strictly optional and can
    never break PDF generation.
    """
    if str(visuals or "auto").lower() == "no":
        return
    try:
        from pdf_service.viz import engine as viz_engine
        from pdf_service.viz import mapdraw, templates

        opts = list(question.get("options", []) or [])
        answer_id = question.get("correct_option_id", -1)
        correct = (opts[answer_id]
                   if isinstance(answer_id, int)
                   and 0 <= answer_id < len(opts) else "")
        spec = viz_engine.safe_decide_visual(
            question.get("question", ""),
            tuple(opts),
            question.get("explanation", ""),
            correct_answer=correct)
        if spec is None:
            return
        pdf = doc.pdf
        usable = pdf.w - pdf.l_margin - pdf.r_margin
        is_map = spec.visual_type in ("location_map", "regional_map")
        if is_map:
            height = mapdraw.suggest_height(spec.payload["base"], usable)
        else:
            height = templates.estimate_height_for_spec(spec, usable)
        doc.ensure_space(height + 4)
        top = pdf.get_y()
        rect = (pdf.l_margin, top, usable, height)
        if is_map:
            mapdraw.draw_map(pdf, base_id=spec.payload["base"],
                             places=spec.payload["places"], rect=rect,
                             title=spec.title,
                             routes=spec.payload.get("routes", []))
        else:
            templates.draw_diagram(pdf, spec, rect)
        pdf.set_xy(pdf.l_margin, top + height + 3)
    except Exception:
        logger.exception("Solution visual skipped after failure")


def _cover_logo(doc: _Doc, prepared) -> None:
    """Centred cover logo (aspect preserved). Never raises."""
    pdf = doc.pdf
    try:
        data, pw, ph = prepared
        w, h = _fit_mm(pw, ph, 120.0, 24.0)
        x = (pdf.w - w) / 2
        pdf.image(BytesIO(data), x=x, y=pdf.get_y(), w=w, h=h)
        pdf.set_y(pdf.get_y() + h + 3)
    except Exception:
        logger.exception("Cover logo skipped after failure")


def _has_hindi(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


def _info_grid(doc: _Doc, setup: _SeriesSetup, total: int) -> None:
    """Professional two-column grid plus boxed Duration/Marks/Date strip.

    Based on Sample (1).pdf visual grammar: clear labels, values,
    bordered boxes for DURATION/TOTAL MARKS/DATE style.
    """
    pdf = doc.pdf
    # Primary rows
    rows: list[tuple[str, str]] = []
    if setup.subject:
        rows.append(("Subject", setup.subject))
    if setup.paper:
        rows.append(("Paper", setup.paper))
    if setup.test_number:
        rows.append(("Test No", setup.test_number))
    if setup.booklet_display:
        rows.append(("Booklet", setup.booklet_display))
    if setup.test_code:
        rows.append(("Test Code", setup.test_code))
    if setup.duration:
        rows.append(("Duration", setup.duration))
    rows.append(("Total Questions", str(total)))
    rows.append(("Maximum Marks", "%g" % round(total * setup.marks_correct, 2)))
    rows.append(("Correct Marks", _fmt_plus(setup.marks_correct)))
    rows.append(("Negative Marks", "%g" % setup.marks_negative))

    usable = pdf.w - pdf.l_margin - pdf.r_margin
    half = usable / 2

    # Draw boxed strip for key metrics (Duration, Total Marks, Max Marks, Date)
    # Mimics Sample (1).pdf: DURATION | TOTAL MARKS | DATE in separate bordered boxes
    pdf.set_font("hind", "B", 9)
    pdf.set_text_color(20, 40, 90)
    # Build strip data
    strip = []
    if setup.duration:
        strip.append(("DURATION", setup.duration))
    strip.append(("TOTAL MARKS", "%g" % round(total * setup.marks_correct, 2)))
    strip.append(("TOTAL QUESTIONS", str(total)))
    if setup.test_number:
        strip.append(("TEST NO", setup.test_number))
    if setup.booklet_display:
        strip.append(("BOOKLET", setup.booklet_display))
    if setup.test_code:
        strip.append(("CODE", setup.test_code))

    if strip:
        box_w = usable / min(3, len(strip)) if len(strip) <= 3 else usable / 3
        # First row: up to 3 boxes
        doc.ensure_space(18)
        y0 = pdf.get_y()
        x0 = pdf.l_margin
        for idx, (lbl, val) in enumerate(strip[:3]):
            x = x0 + (idx % 3) * box_w
            y = y0
            pdf.set_draw_color(20, 40, 90)
            pdf.set_line_width(0.4)
            pdf.rect(x + 1, y, box_w - 2, 14, style="D")
            pdf.set_font("hind", "B", 7.5)
            pdf.set_xy(x + 1, y + 1)
            pdf.cell(box_w - 2, 4, lbl, align="C")
            pdf.set_font("hind", "", 9)
            pdf.set_xy(x + 1, y + 6)
            pdf.cell(box_w - 2, 6, _fit_text(pdf, val, box_w - 4), align="C")
        pdf.set_xy(pdf.l_margin, y0 + 16)
        # Second row if more
        if len(strip) > 3:
            y1 = pdf.get_y()
            for idx, (lbl, val) in enumerate(strip[3:6]):
                x = x0 + (idx % 3) * box_w
                y = y1
                pdf.rect(x + 1, y, box_w - 2, 14, style="D")
                pdf.set_font("hind", "B", 7.5)
                pdf.set_xy(x + 1, y + 1)
                pdf.cell(box_w - 2, 4, lbl, align="C")
                pdf.set_font("hind", "", 9)
                pdf.set_xy(x + 1, y + 6)
                pdf.cell(box_w - 2, 6, _fit_text(pdf, val, box_w - 4), align="C")
            pdf.set_xy(pdf.l_margin, y1 + 16)
        pdf.set_line_width(0.2)
        pdf.ln(2)

    # Detailed two-column grid
    pdf.set_text_color(0, 0, 0)
    for i in range(0, len(rows), 2):
        doc.ensure_space(8)
        y = pdf.get_y()
        for j, (label, value) in enumerate(rows[i:i + 2]):
            x = pdf.l_margin + j * half
            pdf.set_xy(x, y)
            pdf.set_font("hind", "B", 10)
            lab = label + ": "
            lw = pdf.get_string_width(lab) + 0.5
            pdf.cell(lw, 6, lab)
            pdf.set_font("hind", "", 10)
            pdf.cell(half - lw - 2, 6, _fit_text(pdf, value, half - lw - 2))
        pdf.set_xy(pdf.l_margin, y + 6.5)
    pdf.ln(2)


def _candidate_boxes(doc: _Doc, labels: list[str]) -> None:
    """Professional candidate-detail boxes with Roll Number shading note.

    Based on Sample (1).pdf: Candidate Name, Center/Batch, Roll Number
    with circle-shading instruction, Candidate's Signature,
    Invigilator's Signature. Preserves original English labels exactly
    for backward compatibility with existing tests, while header is
    bilingual.
    """
    pdf = doc.pdf
    doc.ensure_space(14)
    pdf.set_font("hind", "B", 11)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 6, "Candidate Details / अभ्यर्थी विवरण", new_x=doc._XPos.LMARGIN,
             new_y=doc._YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    label_w = 62.0

    has_roll = any("roll" in lbl.lower() for lbl in labels)
    for label in labels:
        doc.ensure_space(10)
        y = pdf.get_y()
        pdf.set_xy(pdf.l_margin, y)
        pdf.set_font("hind", "", 10.5)
        # Preserve original label exactly for test compatibility
        # (no truncation via _fit_text that would cut long labels)
        # Use multi_cell if needed but keep label visible
        pdf.cell(label_w, 8, label[:60])
        pdf.set_draw_color(80, 80, 80)
        pdf.rect(pdf.l_margin + label_w, y + 0.5, usable - label_w, 7.5)
        pdf.set_xy(pdf.l_margin, y + 9)

    if has_roll:
        doc.ensure_space(10)
        pdf.set_font("hind", "", 8.5)
        pdf.set_text_color(80, 80, 80)
        pdf.multi_cell(0, 4.5,
                       "ROLL NUMBER: Shade the corresponding circle for each digit on the OMR sheet. / "
                       "अनुक्रमांक: OMR शीट पर प्रत्येक अंक के लिए संबंधित गोले को भरें।",
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
    pdf.ln(1)


def _get_bilingual_instructions(setup: Optional["_SeriesSetup"], total: int) -> list[tuple[str, str]]:
    """Professional bilingual instructions EN+HI paired, competitive-exam style.

    Based on Sample (1).pdf grammar and standard UPSC/SSC conventions.
    Dynamic values (total, marks, negative, duration) injected when available.
    Includes backward-compatible exact phrasing expected by existing tests:
    - "Each question carries +X marks."
    - "Negative marking: -Y per wrong answer." / "There is no negative marking."
    """
    marks_correct = getattr(setup, "marks_correct", 2.0) if setup and getattr(setup, "active", False) else 2.0
    marks_negative = getattr(setup, "marks_negative", -0.66) if setup and getattr(setup, "active", False) else -0.66
    duration = getattr(setup, "duration", "") if setup and getattr(setup, "active", False) else ""
    total_q = total

    def fmt_marks(v: float) -> str:
        return ("%g" % v).rstrip("0").rstrip(".") if "." in ("%g" % v) else "%g" % v

    corr_str = _fmt_plus(marks_correct)  # +2, +4 etc for backward compat
    corr_plain = fmt_marks(marks_correct)
    neg_abs = abs(marks_negative)
    neg_str = fmt_marks(marks_negative)
    neg_abs_str = fmt_marks(neg_abs)

    instructions = []

    # 1 - Read carefully (backward compat)
    instructions.append((
        "Read every question carefully before answering.",
        "प्रत्येक प्रश्न को उत्तर देने से पहले ध्यान से पढ़ें।"
    ))

    # 2 - Marks per question (exact phrasing for tests)
    en2 = f"Each question carries {corr_str} marks."
    hi2 = f"प्रत्येक प्रश्न {corr_plain} अंक का है।"
    instructions.append((en2, hi2))

    # 3 - Negative marking (exact phrasing for tests)
    if marks_negative < 0:
        en3 = f"Negative marking: {neg_str} per wrong answer."
        hi3 = f"नकारात्मक अंकन: प्रत्येक गलत उत्तर पर {neg_str}।"
    else:
        en3 = "There is no negative marking."
        hi3 = "कोई नकारात्मक अंकन नहीं है।"
    instructions.append((en3, hi3))

    # 4 - question count and marking (professional, from Sample)
    if total_q:
        en4 = f"This test booklet contains {total_q} questions divided into multiple sections. Each question carries {corr_plain} mark(s), with negative marking of {neg_abs_str} for every wrong answer."
        hi4 = f"इस परीक्षा पुस्तिका में {total_q} प्रश्न हैं जो कई खंडों में विभाजित हैं। प्रत्येक प्रश्न {corr_plain} अंक का है, प्रत्येक गलत उत्तर पर {neg_abs_str} अंक की नकारात्मक अंकन होगी।"
        instructions.append((en4, hi4))

    # 5 - duration if available
    if duration:
        en5 = f"Duration of the test is {duration}. Manage your time accordingly."
        hi5 = f"परीक्षा की अवधि {duration} है। समय का उचित प्रबंधन करें।"
        instructions.append((en5, hi5))

    # 6 - no electronic devices
    instructions.append((
        "Use of calculator, mobile phone, or any electronic device is strictly prohibited.",
        "कैलकुलेटर, मोबाइल फोन या किसी भी इलेक्ट्रॉनिक उपकरण का उपयोग सख्त वर्जित है।"
    ))

    # 7 - OMR shading
    instructions.append((
        "Darken the appropriate circle on the OMR/answer sheet using a black/blue ball point pen only. Rough work should be done only on the space provided.",
        "OMR/उत्तर पत्रक पर उपयुक्त गोले को केवल काले/नीले बॉल पॉइंट पेन से भरें। रफ कार्य केवल निर्धारित स्थान पर करें।"
    ))

    # 8 - do not open until instructed
    instructions.append((
        "Do not open the booklet until instructed to do so by the invigilator.",
        "निरीक्षक के निर्देश के बिना पुस्तिका न खोलें।"
    ))

    # 9 - all compulsory
    instructions.append((
        "All questions are compulsory. Each question has four options, out of which only one is correct. Choose the correct option.",
        "सभी प्रश्न अनिवार्य हैं। प्रत्येक प्रश्न के चार विकल्प हैं, जिनमें से केवल एक सही है। सही विकल्प चुनें।"
    ))

    # 10 - review after test
    if setup and getattr(setup, "active", False):
        if setup.answer_key and setup.solutions:
            en10 = "Review the answer key and detailed solutions after completing the test for self-evaluation."
            hi10 = "स्व-मूल्यांकन के लिए परीक्षा के बाद उत्तर कुंजी और विस्तृत समाधान देखें।"
        elif setup.answer_key:
            en10 = "Review the answer key after completing the test for self-evaluation."
            hi10 = "स्व-मूल्यांकन के लिए परीक्षा के बाद उत्तर कुंजी देखें।"
        elif setup.solutions:
            en10 = "Review the detailed solutions after completing the test for self-evaluation."
            hi10 = "स्व-मूल्यांकन के लिए परीक्षा के बाद विस्तृत समाधान देखें।"
        else:
            en10 = "Evaluate your performance after completing the test."
            hi10 = "परीक्षा के बाद अपने प्रदर्शन का मूल्यांकन करें।"
    else:
        en10 = "Review the answer key and explanations after completing the test."
        hi10 = "परीक्षा के बाद उत्तर कुंजी और व्याख्या देखें।"
    instructions.append((en10, hi10))

    # 11 - legacy fallback for non-active setup
    if not (setup and getattr(setup, "active", False)):
        instructions.append((
            "There is no negative marking unless your instructor says otherwise.",
            "जब तक आपके प्रशिक्षक अन्यथा न कहें, कोई नकारात्मक अंकन नहीं है।"
        ))

    return instructions


def _render_bilingual_instructions(doc: "_Doc", setup: Optional["_SeriesSetup"], total: int) -> None:
    """Render paired EN+HI instructions with numbering, professional style."""
    pdf = doc.pdf
    doc.ensure_space(20)
    pdf.set_font("hind", "B", 12)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 7, "Instructions to Candidates / परीक्षार्थियों के लिए निर्देश",
             new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(1)
    pdf.set_text_color(40, 40, 40)
    instructions = _get_bilingual_instructions(setup, total)
    for idx, (en, hi) in enumerate(instructions, 1):
        doc.ensure_space(14)
        # Number
        pdf.set_font("hind", "B", 10)
        pdf.set_xy(pdf.l_margin, pdf.get_y())
        num_w = pdf.get_string_width(f"{idx}. ") + 1
        pdf.cell(num_w, 5.2, f"{idx}. ")
        # English
        pdf.set_font("hind", "", 10)
        x_after_num = pdf.l_margin + num_w
        pdf.set_xy(x_after_num, pdf.get_y())
        pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - num_w, 5.2, en,
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        # Hindi indented under same number
        pdf.set_font("hind", "", 9.5)
        pdf.set_text_color(60, 60, 60)
        pdf.set_x(x_after_num)
        pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin - num_w, 5.0, hi,
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.set_text_color(40, 40, 40)
        pdf.ln(1)

    # Closing best wishes bilingual
    doc.ensure_space(10)
    pdf.set_font("hind", "B", 11)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 6, "All the best! / शुभकामनाएँ!",
             align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)
    pdf.set_text_color(0, 0, 0)


def _back_cover(doc: "_Doc", *, exam_title: str, setup: Optional["_SeriesSetup"] = None) -> None:
    """Professional back cover based on Sample (1).pdf visual grammar.

    Includes:
    - Space for Rough Work (bilingual)
    - Light grid/dots area
    - Branding footer
    - Disclaimer / copyright
    - Institute info if available
    """
    pdf = doc.pdf
    pdf.add_page()
    # Border
    pdf.set_draw_color(20, 40, 90)
    pdf.set_line_width(0.6)
    pdf.rect(pdf.l_margin - 2, pdf.t_margin - 2,
             pdf.w - pdf.l_margin - pdf.r_margin + 4,
             pdf.h - pdf.t_margin - pdf.b_margin + 4, style="D")
    pdf.set_line_width(0.2)

    # Title
    pdf.set_font("hind", "B", 14)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 8, "Space for Rough Work / रफ कार्य के लिए स्थान",
             align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)

    # Light grid area: draw dotted lines
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    y_start = pdf.get_y()
    y_end = pdf.h - pdf.b_margin - 30
    pdf.set_draw_color(210, 210, 210)
    pdf.set_line_width(0.15)
    # Horizontal light lines every 8mm
    y = y_start
    while y < y_end:
        pdf.line(pdf.l_margin, y, pdf.l_margin + usable, y)
        y += 8
    # Vertical light lines every 10mm
    x = pdf.l_margin
    y = y_start
    while x < pdf.l_margin + usable:
        pdf.line(x, y_start, x, y_end)
        x += 10
    pdf.set_line_width(0.2)
    pdf.set_draw_color(0, 0, 0)

    # Move cursor to below grid
    pdf.set_xy(pdf.l_margin, y_end + 4)

    # Branding footer
    pdf.set_font("hind", "B", 11)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 6, BRAND, align="C",
             new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    if setup and setup.active and setup.institute_name:
        pdf.set_font("hind", "", 10)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(0, 5, setup.institute_name, align="C",
                 new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.set_font("hind", "", 8.5)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 4.5,
                   "This booklet is for practice purpose only. Content is curated from standard sources. "
                   "© Journey for लबासना. All rights reserved. / "
                   "यह पुस्तिका केवल अभ्यास के लिए है। सामग्री मानक स्रोतों से संकलित है।",
                   align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(1)
    pdf.set_font("hind", "", 7.5)
    pdf.cell(0, 4, f"Generated: {datetime.now().strftime('%d %b %Y')} | {exam_title[:60]}",
             align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)


def _cover(doc: _Doc, *, exam_title: str, tagline: str, quiz_names: list[str],
           total: int, setup: Optional[_SeriesSetup] = None) -> None:
    pdf = doc.pdf
    active = setup is not None and setup.active
    pdf.add_page()

    # Professional border for front cover
    pdf.set_draw_color(20, 40, 90)
    pdf.set_line_width(0.8)
    pdf.rect(pdf.l_margin - 3, pdf.t_margin - 3,
             pdf.w - pdf.l_margin - pdf.r_margin + 6,
             pdf.h - pdf.t_margin - pdf.b_margin + 6, style="D")
    pdf.set_line_width(0.2)

    # Logo and institute
    if active and setup.logo:
        _cover_logo(doc, setup.logo)
    if active and setup.institute_name:
        pdf.set_font("hind", "B", 16)
        pdf.set_text_color(20, 40, 90)
        pdf.multi_cell(0, 8, setup.institute_name, align="C",
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.ln(1)

    # Brand - professional with adequate spacing to avoid overlap
    pdf.set_font("hind", "B", 24)
    pdf.set_text_color(20, 40, 90)
    pdf.multi_cell(0, 14, BRAND, align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("hind", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.multi_cell(0, 7, "Creator: Harish Tripathi | Journey for LBSNAA",
                   align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)

    # Tagline / Paper
    if tagline:
        pdf.set_font("hind", "", 11)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(0, 7, tagline, align="C",
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.ln(3)

    # Divider
    pdf.set_draw_color(20, 40, 90)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin + 20, pdf.get_y(), pdf.w - pdf.r_margin - 20, pdf.get_y())
    pdf.set_line_width(0.2)
    pdf.ln(5)

    # Exam title - prominent
    pdf.set_font("hind", "B", 18)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 9, exam_title, align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(2)

    # Subject bilingual handling: if subject contains Hindi, split or show as is
    if active and setup.subject:
        subj = setup.subject
        # If subject contains both EN and HI separated by | or -, try to display both
        if "|" in subj:
            en_part, hi_part = [s.strip() for s in subj.split("|", 1)]
            pdf.set_font("hind", "B", 13)
            pdf.set_text_color(20, 40, 90)
            pdf.multi_cell(0, 7, en_part, align="C",
                           new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
            if hi_part:
                pdf.set_font("hind", "", 12)
                pdf.set_text_color(40, 40, 40)
                pdf.multi_cell(0, 6, hi_part, align="C",
                               new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        else:
            pdf.set_font("hind", "B", 13)
            pdf.set_text_color(20, 40, 90)
            pdf.multi_cell(0, 7, subj, align="C",
                           new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.ln(1)

    # Professional info grid with boxed strip
    if active:
        _info_grid(doc, setup, total)

    # Source quiz names (preserve for tests)
    names = ", ".join(n for n in quiz_names if n) or "—"
    pdf.set_font("hind", "", 9.5)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, f"Source quiz(zes): {names}", align="C",
                   new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.multi_cell(
        0, 5,
        f"Total questions: {total}   •   Generated: {datetime.now().strftime('%d %b %Y')}",
        align="C", new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
    pdf.ln(3)

    # Candidate details
    if active and setup.candidate_fields:
        _candidate_boxes(doc, setup.candidate_fields)

    # Bilingual instructions - professional paired
    _render_bilingual_instructions(doc, setup, total)



def _question_block(doc: _Doc, number: int, question: dict,
                    show_solution: bool, *, show_answer: bool = True,
                    show_expl: bool = True, visuals: str = "auto") -> None:
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
        if show_answer:
            pdf.set_font("hind", "B", 10.5)
            pdf.set_text_color(22, 101, 52)  # dark green, print-safe
            pdf.multi_cell(0, 5.4, f"Answer: {letters}",
                           new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
        if show_expl and explanation:
            doc.filled_block(f"Explanation: {explanation}")
        if show_expl:
            _maybe_solution_visual(doc, question, visuals)
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


def _detailed_solutions(doc: _Doc, questions: list[dict],
                        visuals: str = "auto") -> None:
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
        _maybe_solution_visual(doc, q, visuals)
        pdf.ln(2)


def _answer_sheet(doc: _Doc, questions: list[dict],
                  setup: _SeriesSetup) -> None:
    """Detachable candidate bubble sheet (studio `answer_sheet` only).

    Prints the series identity (test/booklet/subject) plus candidate
    write-in boxes, then one bubble row per question with exactly as many
    circles as the question has options. Rows are drawn at explicit
    coordinates in two columns, so a row can never straddle a page break;
    circles are stroke-only outlines -- the sheet leaks no answers.
    """
    pdf = doc.pdf
    doc.heading("Answer Sheet", size=14)
    id_bits = []
    if setup.test_number:
        id_bits.append(f"Test No: {setup.test_number}")
    if setup.booklet_display:
        id_bits.append(f"Booklet: {setup.booklet_display}")
    if setup.subject:
        id_bits.append(f"Subject: {setup.subject}")
    if id_bits:
        pdf.set_font("hind", "B", 10)
        pdf.multi_cell(0, 5.4, "    ".join(id_bits),
                       new_x=doc._XPos.LMARGIN, new_y=doc._YPos.NEXT)
        pdf.ln(1)
    fields = setup.candidate_fields or ["Candidate Name", "Roll Number",
                                        "Date"]
    for label in fields[:7]:
        pdf.set_font("hind", "", 10)
        short = label[:28]
        pdf.cell(56, 7, short + ":", new_x=doc._XPos.RIGHT,
                 new_y=doc._YPos.TOP)
        pdf.rect(pdf.get_x(), pdf.get_y() + 0.4, 72, 6.2)
        pdf.ln(7)
    pdf.ln(1)
    pdf.set_font("hind", "", 9)
    for line in ("• Darken exactly one circle per question with a black or "
                 "blue pen.",
                 "• Do not tick, cross, or stray outside the circle.",
                 "• Rough work elsewhere — this sheet is evaluated as-is."):
        pdf.multi_cell(0, 4.8, line, new_x=doc._XPos.LMARGIN,
                       new_y=doc._YPos.NEXT)
    pdf.ln(2)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    col_w = usable / 2
    row_h = 7.0
    idx = 0
    total = len(questions)
    last_y = pdf.get_y()
    first_page = True
    while idx < total:
        if not first_page:
            pdf.add_page()
        first_page = False
        y_top = pdf.get_y()
        per_col = max(1, int((pdf.page_break_trigger - y_top) // row_h))
        rows_drawn = 0
        for col in range(2):
            if idx >= total:
                break
            x0 = pdf.l_margin + col * col_w
            col_rows = 0
            for _ in range(per_col):
                if idx >= total:
                    break
                n_opts = len(questions[idx].get("options", []) or [])
                pitch = min(6.2, (col_w - 15) / max(n_opts, 1))
                diam = max(2.0, min(4.6, pitch - 0.8))
                y = y_top + col_rows * row_h
                pdf.set_xy(x0, y)
                pdf.set_font("hind", "B", 10)
                pdf.cell(14, row_h, f"Q{idx + 1}")
                cx = x0 + 14
                cy = y + (row_h - diam) / 2
                for k in range(n_opts):
                    pdf.ellipse(cx, cy, diam, diam, style="D")
                    pdf.set_xy(cx, cy)
                    pdf.set_font("hind", "", 7 if diam < 3.5 else 8)
                    pdf.cell(diam, diam, chr(65 + k), align="C")
                    cx += pitch
                idx += 1
                col_rows += 1
            rows_drawn = max(rows_drawn, col_rows)
        last_y = y_top + rows_drawn * row_h
    pdf.set_xy(pdf.l_margin, min(last_y + 2, pdf.page_break_trigger))


def render_testseries_pdf(
    questions: list[dict],
    *,
    exam_title: str,
    tagline: str = "Test Series",
    quiz_names: Optional[list[str]] = None,
    solution_display: str = "end",
    series_setup: Optional[dict] = None,
    output_path: str | Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Render the test-series PDF. Returns {"pages", "questions", "bytes"}.

    Raises ValueError on empty/invalid input, RuntimeError on render failure.
    `series_setup` (Phase 4 M2, optional) applies the /newseries wizard
    configuration; omitting it keeps the historical rendering untouched.
    """
    if not questions:
        raise ValueError("No questions to render.")
    title, _ = sanitize_text(exam_title or "Mock Test", MAX_TITLE_CHARS)
    tag, _ = sanitize_text(tagline or "Test Series", 200)
    names = [sanitize_text(n, 200)[0] for n in (quiz_names or []) if n]
    inline = (solution_display or "end").lower() == "inline"
    setup = _SeriesSetup(series_setup)

    doc = _Doc(exam_title=title or "Mock Test",
               watermark_mode=setup.watermark_mode if setup.active else "none",
               watermark_text=setup.watermark_text,
               watermark_image=setup.wm_image)
    _cover(doc, exam_title=title or "Mock Test", tagline=tag or "Test Series",
           quiz_names=names, total=len(questions), setup=setup)
    doc.pdf.add_page()
    doc.heading("Questions", size=14)
    total = len(questions)
    show_answer = inline and setup.answer_key
    show_expl = inline and setup.solutions
    for i, q in enumerate(questions, 1):
        _question_block(doc, i, q, show_solution=show_answer or show_expl,
                        show_answer=show_answer, show_expl=show_expl,
                        visuals=setup.visuals)
        if progress_cb is not None and (i % 25 == 0 or i == total):
            progress_cb(i, total)
    if not inline:
        if setup.answer_sheet:
            doc.pdf.add_page()
            _answer_sheet(doc, questions, setup)
        if setup.answer_key:
            doc.pdf.add_page()
            _answer_key_grid(doc, questions)
            doc.pdf.ln(4)
        if setup.solutions:
            if not setup.answer_key:
                doc.pdf.add_page()
            _detailed_solutions(doc, questions, setup.visuals)
    elif setup.answer_sheet:
        doc.pdf.add_page()
        _answer_sheet(doc, questions, setup)

    # Professional back cover based on Sample (1).pdf
    try:
        _back_cover(doc, exam_title=title or "Mock Test", setup=setup)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Back cover skipped after failure")

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
