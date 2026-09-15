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

Two complementary mechanisms fix the text layer:

1. /ActualText marked content: each Pango text run is wrapped in
   /Span << /ActualText <FEFF UTF-16BE...> >> BDC ... EMC carrying the
   run's logical Unicode text verbatim.  Modern extractors (MuPDF >= 1.24 /
   PyMuPDF >= 1.24, pdfminer.six, pypdf, Acrobat, Chromium) use ActualText
   instead of the ToUnicode CMap for the enclosed glyphs.

2. Logical-order ToUnicode emission (this module's
   _wp_indic_emit_run_glyphs): runs whose glyph clusters are not monotonic
   (pre-base matras, split matras) are emitted into the TJ array in logical
   (cluster) order with position-preserving TJ adjustments, so every glyph
   id maps to exactly the characters it represents and visual-order
   concatenation already yields the logical text.  This is REQUIRED because
   older but widespread extractors (MuPDF < 1.24, i.e. PyMuPDF <= 1.23.x)
   ignore /ActualText entirely and decode purely from ToUnicode: without
   logical-order emission they extract e.g. विवरण as विविरण plus U+FFFD
   tofu for the matra glyphs.  Position-preserving emission keeps the
   visible render pixel-identical (verified by raster diff); only the
   glyph sequence order and the CMap change.

This module rewrites weasyprint.draw.draw_first_line at import time with
clearly delimited source modifications (the same approach used in
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

# --- Logical-order ToUnicode emission --------------------------------------
# Anchors delimiting the per-run glyph loop inside draw_first_line (the
# `string += '<'` opener through the run-end close block).  The loop body is
# preserved verbatim for color-emoji runs (font.svg / font.png, whose glyph
# placement feeds raster-image positioning) and replaced by a
# _wp_indic_emit_run_glyphs call for text runs.
_LOOP_START = "        string += '<'\n"
_LOOP_CLOSE = ("""\
        # Close the last glyphs list, remove if empty
        if string[-1] == '<':
            string = string[:-1]
        else:
            string += '>'
""")

_HELPER_PATH = ("""\
        else:
            # --- wp-indic-tounicode patch begin ---
            # Text run: collect plain-python glyph records, then emit via
            # the pure helper (logical order when clusters are reordered).
            _wp_recs = []
            for _wp_i in range(num_glyphs):
                _wp_gi = glyphs[_wp_i]
                _wp_g = _wp_gi.glyph
                _wp_w = _wp_gi.geometry.width
                if (_wp_g == pango.PANGO_GLYPH_EMPTY or
                        _wp_g & pango.PANGO_GLYPH_UNKNOWN_FLAG):
                    _wp_recs.append((_wp_g, 0, _wp_w, 0.0, 0.0, 0, True))
                    continue
                _wp_c = int(clusters[_wp_i])
                _wp_off = _wp_gi.geometry.x_offset / font_size
                _wp_rise = _wp_gi.geometry.y_offset / 1000
                if _wp_g not in font.widths:
                    pango.pango_font_get_glyph_extents(
                        pango_font, _wp_g, stream.ink_rect,
                        stream.logical_rect)
                    font.widths[_wp_g] = int(round(
                        units_to_double(stream.logical_rect.width * 1000) /
                        font_size))
                _wp_kern = int(
                    font.widths[_wp_g] -
                    units_to_double(_wp_w * 1000) / font_size + _wp_off)
                _wp_recs.append(
                    (_wp_g, _wp_c, _wp_w, _wp_off, _wp_rise, _wp_kern,
                     False))
            _wp_ops, _wp_pairs, _wp_dadv = _wp_indic_emit_run_glyphs(
                _wp_recs, offset, glyph_item.item.length, utf8_text,
                font_size, font.widths, font.bitmap)
            x_advance += _wp_dadv
            for _wp_gid, _wp_slice in _wp_pairs:
                if _wp_gid not in font.cmap:
                    font.cmap[_wp_gid] = _wp_slice
            previous_utf8_position = offset + glyph_item.item.length
            for _wp_f, _wp_r in _wp_ops:
                if _wp_r is None:
                    string += _wp_f
                else:
                    if string[-1] == '<':
                        string = string[:-1]
                    else:
                        string += '>'
                    stream.show_text(string)
                    stream.set_text_rise(-_wp_r)
                    string = _wp_f
                    stream.show_text(string)
                    stream.set_text_rise(0)
                    string = '<'
            # Close the last glyphs list, remove if empty
            if string[-1] == '<':
                string = string[:-1]
            else:
                string += '>'
            # --- wp-indic-tounicode patch end ---
""")


def _wp_indic_emit_run_glyphs(records, run_offset, run_length, utf8_text,
                              font_size, widths, is_bitmap):
    """Emit one Pango run's glyphs; pure function (no FFI).

    Args:
        records: list of (gid, cluster, width_pango, x_offset, rise,
            kerning, is_empty) tuples in visual order.  ``cluster`` is the
            run-relative byte offset of the glyph's shaping cluster.
        run_offset / run_length: byte range of the run in ``utf8_text``.
        utf8_text: the whole line's UTF-8 bytes.
        font_size: point size (same units WeasyPrint uses for TJ math).
        widths: live font.widths dict (gid -> thousandths advance).
        is_bitmap: True for EBDT bitmap fonts (2-digit TJ glyph codes).

    Returns (ops, cmap_pairs, advance_delta):
        ops: list of (fragment, rise) tuples; rise None means a plain
            text append, otherwise a text-rise sequence.
        cmap_pairs: (gid, unicode-slice) pairs in emission order; the call
            site applies first-wins into font.cmap.
        advance_delta: visual pen advance of the run (EM units) for the
            call site's x_advance bookkeeping.

    Runs with monotonic clusters keep WeasyPrint's exact visual emission
    (only the CMap slice source changes); runs with reordered clusters
    (pre-base / split matras) are emitted in logical (cluster) order with
    recomputed TJ adjustments that preserve every glyph's visual position
    to within rounding error, so visual-order concatenation of the CMap
    slices yields the logical text on extractors old and new.
    """
    if is_bitmap:
        def _hx(glyph):
            return "%02x" % glyph
    else:
        def _hx(glyph):
            return "%04x" % glyph

    real = [i for i, rec in enumerate(records) if not rec[6]]
    if not real:
        return ([("<", None)], [], 0.0)

    clusters = [records[i][1] for i in real]
    uniq = sorted(set(clusters))
    ends = uniq[1:] + [run_length]
    seg_text = {}
    for start, end in zip(uniq, ends):
        seg_text[start] = utf8_text[
            run_offset + start:run_offset + end].decode("utf-8", "replace")

    # Distribute each cluster's logical characters across the glyphs that
    # share it (visual order): single glyph takes all; first of several
    # takes the onset character; last takes the remainder; middle glyphs
    # take nothing (avoids duplication while keeping per-glyph slices
    # stable across occurrences of the same glyph id).
    by_cluster = {}
    for pos, i in enumerate(real):
        by_cluster.setdefault(records[i][1], []).append(pos)
    slices = {}
    for start, poses in by_cluster.items():
        chars = list(seg_text[start])
        if len(poses) == 1:
            slices[poses[0]] = seg_text[start]
        else:
            slices[poses[0]] = chars[0] if chars else ""
            slices[poses[-1]] = "".join(chars[1:]) if len(chars) > 1 else ""
            for pos in poses[1:-1]:
                slices[pos] = ""

    reorder = any(clusters[k] > clusters[k + 1]
                  for k in range(len(clusters) - 1))

    ops = []
    pairs = []
    pos_of = {i: pos for pos, i in enumerate(real)}

    if not reorder:
        # Visual emission, verbatim WeasyPrint positioning.
        pending = "<"
        for i, rec in enumerate(records):
            gid, _cl, width, xoff, rise, kern, empty = rec
            if empty:
                pending += ">{}<".format(-width / font_size)
                continue
            if rise:
                ops.append((pending, None))
                gidfrag = ("{}".format(-xoff) if xoff else "")
                gidfrag += "<%s>" % _hx(gid)
                ops.append((gidfrag, rise))
                # Hex stays open across the rise boundary (call site left
                # string ending in '<'), so do NOT reopen it here.
                pending = ""
            else:
                if xoff:
                    pending += ">{}<".format(-xoff)
                pending += _hx(gid)
            if kern:
                pending += ">{}<".format(kern)
            pairs.append((gid, slices[pos_of[i]]))
        d_adv = 0.0
        for i in real:
            gid, _cl, _w, xoff, _rise, kern, _e = records[i]
            d_adv += (widths[gid] + xoff - kern) / 1000
        ops.append((pending, None))
        return (ops, pairs, d_adv)

    # Reordered emission: simulate the visual pen, then emit glyphs in
    # logical (cluster) order with adjustments restoring visual positions.
    xs = {}
    pen = 0.0
    for i, rec in enumerate(records):
        gid, _cl, width, xoff, _rise, kern, empty = rec
        if empty:
            pen += width / (1000 * font_size)
            continue
        xs[i] = pen + xoff / 1000
        pen = xs[i] + (widths[gid] - kern) / 1000
    d_adv = 0.0
    for i in real:
        gid, _cl, _w, xoff, _rise, kern, _e = records[i]
        d_adv += (widths[gid] + xoff - kern) / 1000
    order = sorted(real, key=lambda i: records[i][1])
    pen = 0.0
    pending = "<"
    for i in order:
        gid, _cl, _w, _xoff, rise, _kern, _e = records[i]
        target = xs[i]
        num = round(-(target - pen) * 1000, 3)
        if num:
            pending += ">{}<".format(num)
        if rise:
            ops.append((pending, None))
            ops.append(("<%s>" % _hx(gid), rise))
            pending = ""
        else:
            pending += _hx(gid)
        pairs.append((gid, slices[pos_of[i]]))
        pen = target + widths[gid] / 1000
    ops.append((pending, None))
    return (ops, pairs, d_adv)


def apply_weasyprint_indic_actualtext_fix() -> bool:
    """Apply the ActualText + logical-order ToUnicode patch.

    Returns True if applied or already applied.
    """
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

    # Logical-order ToUnicode emission: branch color-emoji runs (verbatim
    # original loop, whose glyph placement feeds raster-image positioning)
    # from text runs (pure-helper emission).
    loop_start = new_source.find(_LOOP_START)
    close_at = new_source.find(_LOOP_CLOSE, loop_start + 1)
    if loop_start < 0 or close_at < 0:
        _LOG.warning("WeasyPrint glyph-loop anchors not found; patch skipped")
        return False
    loop_end = close_at + len(_LOOP_CLOSE)
    original_block = new_source[loop_start:loop_end]
    if "x_advance += " not in original_block:  # sanity: whole loop captured
        _LOG.warning("WeasyPrint glyph loop differs; patch skipped")
        return False
    reindented = "\n".join(
        ("    " + line) if line.strip() else line
        for line in original_block.split("\n"))
    replacement = ("        if font.svg or font.png:\n" + reindented
                   + _HELPER_PATH)
    new_source = (new_source[:loop_start] + replacement
                  + new_source[loop_end:])

    module_globals = wp_draw.__dict__
    assert 'draw_first_line' in module_globals
    # The compiled replacement calls the pure emitter as a module global.
    module_globals["_wp_indic_emit_run_glyphs"] = _wp_indic_emit_run_glyphs
    code = compile(new_source, "<wp-indic-actualtext draw_first_line>", "exec")
    exec(code, module_globals)  # noqa: S102 - runtime compat shim by design
    setattr(wp_draw, _PATCH_MARK, True)
    _LOG.info("Applied WeasyPrint Indic ActualText text-layer patch")
    return True


apply_weasyprint_indic_actualtext_fix()
