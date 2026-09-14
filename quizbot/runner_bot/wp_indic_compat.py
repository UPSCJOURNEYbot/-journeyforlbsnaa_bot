"""Runtime compatibility patch for WeasyPrint's Indic text layer.

Why this exists
---------------
WeasyPrint 62.x builds each font subset's ToUnicode CMap purely from glyph
ids (weasyprint/draw.py -> font.cmap[glyph] = utf8_slice).  For Devanagari
(and other Indic scripts with *pre-base* vowel signs such as short-i) Pango
emits the matra glyph *before* its consonant base in the content stream,
with both glyphs sharing the same shaping cluster.  When the same consonant
glyph id is also used standalone, one glyph id ends up mapping to the whole
"vowel + consonant" cluster while the standalone consonant inherits that
mapping, so text extraction/copy/search yields e.g. विवरण -> विविरण and
विवाह -> विविाह.  The visible PDF is always correct; only the logical text
layer is corrupted.  Upgrading Pango/HarfBuzz or swapping fonts (Hind, Noto
Sans Devanagari, DejaVu) does not help because the defect is in
WeasyPrint's own glyph->unicode slice bookkeeping.

The standards-compliant fix is the PDF /ActualText marked-content feature:
each Pango text run is wrapped in
/Span << /ActualText <FEFF UTF-16BE...> >> BDC ... EMC carrying the run's
logical Unicode text verbatim.  Spec-conformant extractors (pdfminer.six,
PyMuPDF/MuPDF, pypdf, Acrobat, Chromium) then use ActualText instead of the
ToUnicode CMap for the enclosed glyphs, so the logical layer is exactly what
Pango laid out, regardless of glyph reordering or glyph-id reuse.

This module rewrites weasyprint.draw.draw_first_line at import time with a
small, clearly delimited source modification (the same approach used in
pdf_service/shape_compat.py for fpdf2).  It verifies expected source
anchors before applying and leaves WeasyPrint untouched otherwise.
"""

from __future__ import annotations

import inspect
import logging

_LOG = logging.getLogger(__name__)

_PATCH_MARK = "_wp_indic_actualtext_patch"

_RUN_HEAD = ("""\
    run = first_line.runs[0]
    while run != ffi.NULL:
""")

_RUN_HEAD_NEW = ("""\
    run = first_line.runs[0]
    _wp_span_open = False
    while run != ffi.NULL:
""")

_RUN_GATE = ("""\
        utf8_positions.append(offset + glyph_item.item.length)

        # --- wp-indic-actualtext patch begin ---
        if _wp_span_open:
            if string:
                stream.show_text(string)
                string = ''
            stream.stream.append(b'EMC')
            _wp_span_open = False
        _wp_run_text = utf8_text[
            offset:offset + glyph_item.item.length].decode(
                'utf-8', 'replace')
        stream.stream.append(
            b'/Span << /ActualText <FEFF'
            + _wp_run_text.encode('utf-16-be').hex().encode('ascii')
            + b'> >> BDC')
        _wp_span_open = True
        # --- wp-indic-actualtext patch end ---
""")

_RUN_TAIL = ("""\
    # Draw text
    stream.show_text(string)

    return emojis
""")

_RUN_TAIL_NEW = ("""\
    # Draw text
    stream.show_text(string)
    if _wp_span_open:
        stream.stream.append(b'EMC')
        _wp_span_open = False

    return emojis
""")


def apply_weasyprint_indic_actualtext_fix() -> bool:
    """Apply the ActualText patch; True if applied or already applied."""
    try:
        import weasyprint.draw as wp_draw
    except Exception as exc:  # pragma: no cover
        _LOG.warning("WeasyPrint unavailable for Indic ActualText patch: %s",
                     exc)
        return False

    draw_first_line = getattr(wp_draw, "draw_first_line", None)
    if not callable(draw_first_line):
        _LOG.warning("weasyprint.draw lacks draw_first_line; patch skipped")
        return False

    # Already patched in this interpreter? The replaced function is a live
    # object compiled from an in-memory string, so its source is unreadable;
    # a module-level marker plus our filename fingerprint proves it.
    if getattr(wp_draw, _PATCH_MARK, False):
        filename = getattr(
            getattr(draw_first_line, "__code__", None), "co_filename", "")
        if "wp-indic-actualtext" in filename:
            return True

    try:
        source = inspect.getsource(wp_draw.draw_first_line)
    except (OSError, TypeError):
        # Callable but no readable source: either our patched function without
        # a marker (re-mark) or an unknown replacement (do not touch it).
        if "wp-indic-actualtext" in getattr(
                getattr(draw_first_line, "__code__", None),
                "co_filename", ""):
            setattr(wp_draw, _PATCH_MARK, True)
            return True
        _LOG.warning("Cannot read WeasyPrint draw_first_line source")
        return False

    if "wp-indic-actualtext" in source:
        setattr(wp_draw, _PATCH_MARK, True)
        return True

    for anchor in (_RUN_HEAD, _RUN_TAIL):
        if anchor not in source:
            _LOG.warning(
                "WeasyPrint draw_first_line layout differs from the supported "
                "62.x layout; Indic ActualText patch not applied")
            return False

    new_source = source.replace(_RUN_HEAD, _RUN_HEAD_NEW, 1)

    gate_anchor = ("        utf8_positions.append("
                   "offset + glyph_item.item.length)\n")
    if gate_anchor not in new_source:
        _LOG.warning("WeasyPrint utf8_positions anchor not found")
        return False
    new_source = new_source.replace(gate_anchor, _RUN_GATE, 1)
    new_source = new_source.replace(_RUN_TAIL, _RUN_TAIL_NEW, 1)

    module_globals = wp_draw.__dict__
    assert 'draw_first_line' in module_globals
    code = compile(new_source, "<wp-indic-actualtext draw_first_line>", "exec")
    exec(code, module_globals)  # noqa: S102 - runtime compat shim by design
    setattr(wp_draw, _PATCH_MARK, True)
    _LOG.info("Applied WeasyPrint Indic ActualText text-layer patch")
    return True


apply_weasyprint_indic_actualtext_fix()
