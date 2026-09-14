"""fpdf2 2.8.x HarfBuzz-shaped text: correct ToUnicode for split Indic clusters.

Why this module exists
-----------------------
``deploy_pdf_service.sh`` proved the *visual* Devanagari rendering correct,
but a real generated Test Series PDF exposed a text-layer defect: copying or
searching ``दिल्ली`` produced ``दिGल्ली`` / ``दि\\x02ल्ली`` (the stray glyph
varied with subset order). The printed page was perfect; the PDF's ToUnicode
CMap was not.

Root cause in fpdf2 2.8.8 (``fpdf/fonts.py::TTFFont.shape_text``):

* HarfBuzz emits several glyphs for one Devanagari grapheme cluster. The
  pre-base short-i matra is the common case -- ``दिल्ली`` shapes to glyphs
  ``[ि-form(cluster 0), द-form(cluster 0), ल्ल(cluster 2), ी(cluster 5)]``:
  TWO glyphs legitimately share ``cluster=0``.
* fpdf2 assigns the cluster's full logical text to the FIRST glyph and
  ``pop()``s the mapping, so every LATER glyph of the same cluster receives
  an empty Unicode tuple.
* Such glyphs are omitted from the ToUnicode CMap, and PDF text extractors
  then fall back to rendering the raw subset CID (``G``, ``\\x02``,
  ``(cid:6)`` ...) inside the word. Conjunct/pre-base-matra words appear in
  nearly every Hindi line, so the searchable/copyable text of essentially
  every Test Series PDF was corrupted (PyMuPDF and pdfminer both affected).

The fix (applied here as a contained, version-guarded replacement for
``TTFFont.shape_text``): group the glyphs of each HarfBuzz cluster and
DISTRIBUTE the cluster's logical characters across them -- every drawn glyph
gets a non-empty, stable ToUnicode mapping. For the pre-base matra the
reordered (leftmost) glyph receives the consonant and the base glyph the
matra, which reconstructs the logical word under BOTH stream-order and
position-order extraction. Single-glyph clusters (Latin, digits, ligature
conjuncts) behave exactly like upstream.

Only ToUnicode metadata changes; glyph choices, advances and positioning are
taken from HarfBuzz unchanged, so the visual output is byte-identical in
shape. Verified round-trip (render -> PyMuPDF/pdfminer extract -> exact
match) over every Devanagari string present in this repository plus a large
corpus of conjunct/reph/nukta/pre-base-matra words, regular and bold.
"""

from __future__ import annotations

import logging
from bisect import bisect_left

logger = logging.getLogger(__name__)

# Versions this replacement was written and tested against. requirements.txt
# pins fpdf2 exactly; if a different version is ever installed we fail loudly
# at import instead of silently shipping the old corrupted text layer.
TESTED_FPDF_VERSIONS = {"2.8.8"}
_PATCH_ATTR = "_journey_hb_tounicode_distribution_fix"


def _distributing_shape_text(
    self, text: str, font_size_pt: float, text_shaping_params
):
    """Shape ``text`` with HarfBuzz and map EVERY output glyph to Unicode.

    Drop-in behavioral replacement for ``TTFFont.shape_text`` in fpdf2 2.8.8
    (same inputs/outputs and the same ``text_info`` dictionaries); only the
    cluster -> Unicode assignment differs (see module docstring).
    """
    if len(text) == 0:
        return []
    glyph_infos, glyph_positions = self.perform_harfbuzz_shaping(
        text, font_size_pt, text_shaping_params
    )
    text_info: list[dict] = []

    def get_cluster_from_text_index(cluster_list, index: int) -> int:
        pos = bisect_left(cluster_list, index)
        if pos == 0:
            return cluster_list[0]
        if pos == len(cluster_list) or cluster_list[pos] != index:
            return cluster_list[pos - 1]
        return cluster_list[pos]

    cluster_list = sorted({int(gi.cluster) for gi in glyph_infos})
    cluster_mapping: dict[int, list[int]] = {}
    for i in range(len(text)):
        cl = get_cluster_from_text_index(cluster_list, i)
        cluster_mapping.setdefault(cl, []).append(i)

    # Group CONSECUTIVE glyphs sharing a cluster (HarfBuzz cluster values are
    # monotonic, so repeats always form one run).
    groups: list[tuple[int, list]] = []
    for gi in glyph_infos:
        cluster_value = int(gi.cluster)
        if groups and groups[-1][0] == cluster_value:
            groups[-1][1].append(gi)
        else:
            groups.append((cluster_value, [gi]))

    seq = 0
    for cluster_value, gis in groups:
        chars = [ord(text[i]) for i in cluster_mapping.get(cluster_value, [])]
        k = len(gis)
        for j, gi in enumerate(gis):
            if k == 1:
                # Ligature glyph covering the whole cluster (e.g. ल्ल).
                unicode_values = chars
            elif j < k - 1:
                # First glyphs of a split cluster each take one logical char
                # in logical order (pre-base matra: the reordered leftmost
                # glyph takes the consonant).
                unicode_values = chars[j : j + 1]
            else:
                # Last glyph of a split cluster keeps the remaining chars.
                unicode_values = chars[k - 1 :]
            if not unicode_values:
                # Defensive: more glyphs than logical chars (not observed for
                # Devanagari). Never emit an unmapped code -- duplicate the
                # first cluster char instead of raw-CID garbage.
                unicode_values = chars[:1] if chars else [0x20]

            gname = self.ttfont.getGlyphName(gi.codepoint)
            gwidth = round(
                self.scale * self.ttfont["hmtx"].metrics[gname][0]
            )
            glyph = self.subset.get_glyph(
                glyph=gi.codepoint,
                unicode=tuple(unicode_values),
                glyph_name=gname,
                glyph_width=gwidth,
            )
            if glyph is None:
                seq += 1
                continue
            gp = glyph_positions[seq]
            force_positioning = (
                gwidth != gp.x_advance
                or gp.x_offset != 0
                or gp.y_offset != 0
                or gp.y_advance != 0
            )
            text_info.append(
                {
                    "mapped_char": self.subset.pick_glyph(glyph),
                    "x_advance": gp.x_advance,
                    "y_advance": gp.y_advance,
                    "x_offset": gp.x_offset,
                    "y_offset": gp.y_offset,
                    "force_positioning": force_positioning,
                }
            )
            seq += 1
    return text_info


def apply_harfbuzz_tounicode_fix() -> bool:
    """Install the fix once. Returns True when active; idempotent.

    Raises RuntimeError if the installed fpdf2 is not a tested version or
    does not expose the internals the replacement relies on -- a loud failure
    at import is preferable to silently shipping corrupt PDF text layers.
    """
    import fpdf

    try:
        from fpdf.fonts import TTFFont
    except ImportError as exc:  # pragma: no cover - structural guard
        raise RuntimeError(
            "pdf_service requires fpdf2 with fpdf.fonts.TTFFont; cannot "
            f"install the Devanagari ToUnicode fix: {exc}"
        ) from exc

    if getattr(TTFFont.shape_text, _PATCH_ATTR, False):
        return True

    version = getattr(fpdf, "__version__", "unknown")
    if version not in TESTED_FPDF_VERSIONS:
        raise RuntimeError(
            f"fpdf2 {version} is installed but pdf_service's Devanagari "
            f"ToUnicode fix was only validated for "
            f"{sorted(TESTED_FPDF_VERSIONS)}. Pin fpdf2==2.8.8 in "
            "requirements.txt or port pdf_service/shape_compat.py to the new "
            "fpdf2 release before deploying."
        )
    if not callable(getattr(TTFFont, "perform_harfbuzz_shaping", None)):
        raise RuntimeError(
            "Installed fpdf2 lacks TTFFont.perform_harfbuzz_shaping expected "
            "by pdf_service/shape_compat.py; refusing to patch."
        )

    TTFFont.shape_text = _distributing_shape_text
    setattr(TTFFont.shape_text, _PATCH_ATTR, True)
    logger.info(
        "pdf_service: fpdf2 %s Devanagari ToUnicode distribution fix active.",
        version,
    )
    return True
