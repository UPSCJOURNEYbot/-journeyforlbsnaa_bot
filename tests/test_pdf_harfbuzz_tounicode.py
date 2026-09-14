"""ToUnicode integrity for HarfBuzz-shaped Devanagari in Test Series PDFs.

fpdf2 2.8.8 assigns the full logical text of a HarfBuzz cluster to the first
glyph only; when a cluster splits into several glyphs (the pre-base short-i
matra is the most common Devanagari case, plus some reph/nukta sequences) the
remaining glyphs get no ToUnicode mapping and extractors replace them with
the raw subset CID inside the word -- ``दिल्ली`` copied as ``दिGल्ली``.
``pdf_service.shape_compat`` distributes cluster characters across all
glyphs. These tests pin both the mapping rule (synthetic HarfBuzz output)
and the end-to-end result through the real renderer (regular + bold).
"""

from __future__ import annotations

import importlib
import pathlib
import unittest
import unittest.mock

import fpdf

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO_ROOT))

from pdf_service import shape_compat  # noqa: E402

# Importing render applies the patch at module load.
render = importlib.import_module("pdf_service.render")

# Words covering every failure class found during investigation:
#  - pre-base short-i matra (दि/कि/स्थि) -> two glyphs share one cluster
#  - reph / conjunct clusters (राष्ट्रीय, तर्कशक्ति)
#  - nukta + chandrabindu (अंग्रेज़ी, मुंबई)
#  - ligature conjuncts (क्षेत्र, श्रृंखला, ज्ञान)
HINDI_WORDS = [
    "दिल्ली", "कि", "दिन", "किताब", "कितने", "किलोमीटर",
    "स्थिति", "नीति", "मित्र", "चित्र", "पंक्ति", "कीर्ति",
    "मुंबई", "अंग्रेज़ी", "ख़ुशी", "रिज़र्व", "बैंक",
    "क्षेत्र", "ज्ञानी", "श्रृंखला",
    "राष्ट्रीय", "तर्कशक्ति", "निर्वाचन", "कर्तव्य", "दर्शन",
    "नई दिल्ली भारत की राजधानी है।",
    "सविनय अवज्ञा आंदोलन का इतिहास पढ़ें।",
]
# Words rendered in BOLD headings/titles as well.
BOLD_WORDS = ["दिल्ली", "राष्ट्रीय", "अंग्रेज़ी", "क्षेत्र"]


class _FakePositions:
    def __init__(self, advance=500):
        self.x_advance = advance
        self.y_advance = 0
        self.x_offset = 0
        self.y_offset = 0


class _FakeInfo:
    def __init__(self, codepoint, cluster):
        self.codepoint = codepoint
        self.cluster = cluster


class _FakeSubset:
    def __init__(self):
        self.assigned = []
        self._n = 0

    def get_glyph(self, *, glyph, unicode, glyph_name, glyph_width):
        self.assigned.append(tuple(unicode))
        return ("glyph", glyph, tuple(unicode))

    def pick_glyph(self, glyph):
        cid = self._n
        self._n += 1
        return cid


class _FakeHmtx:
    def __init__(self):
        self.metrics = {}


class _FakeTTFont:
    def __init__(self):
        self._hmtx = _FakeHmtx()
        self.containers = {"hmtx": self._hmtx}

    def __getitem__(self, key):
        return self.containers[key]

    def getGlyphName(self, codepoint):
        name = f"g{codepoint}"
        self._hmtx.metrics.setdefault(name, (0, 500))
        return name


class _FakeFont:
    def __init__(self, infos):
        self._infos = infos
        self.scale = 1.0
        self.ttfont = _FakeTTFont()
        self.subset = _FakeSubset()

    def perform_harfbuzz_shaping(self, text, font_size_pt, params):
        return self._infos, [_FakePositions() for _ in self._infos]


class ClusterDistributionTests(unittest.TestCase):
    def test_split_pre_base_cluster_distributes_all_chars(self):
        # दिल्ली : glyphs clusters [0,0,2,5] (emulates Hind + HarfBuzz).
        infos = [_FakeInfo(974, 0), _FakeInfo(331, 0),
                 _FakeInfo(865, 2), _FakeInfo(353, 5)]
        font = _FakeFont(infos)
        out = shape_compat._distributing_shape_text(
            font, "दिल्ली", 12.0, None)
        self.assertEqual(len(out), 4)
        # द(0926) -> reordered first glyph, ि(093F) -> base glyph,
        # ल ् ल (0932 094D 0932) -> ligature, ी(0940) -> matra.
        self.assertEqual(font.subset.assigned, [
            (0x0926,), (0x093F,),
            (0x0932, 0x094D, 0x0932), (0x0940,),
        ])
        # No empty mapping -> no raw-CID garbage possible.
        self.assertTrue(all(t for t in font.subset.assigned))

    def test_single_glyph_ligature_keeps_full_cluster(self):
        # ल्ल alone -> one glyph, cluster 0, three source chars.
        infos = [_FakeInfo(865, 0)]
        font = _FakeFont(infos)
        shape_compat._distributing_shape_text(font, "ल्ल", 12.0, None)
        self.assertEqual(font.subset.assigned,
                         [(0x0932, 0x094D, 0x0932)])

    def test_more_glyphs_than_chars_never_emits_empty_mapping(self):
        # Defensive synthetic: 3 glyphs share a 2-char cluster.
        infos = [_FakeInfo(10, 0), _FakeInfo(11, 0), _FakeInfo(12, 0)]
        font = _FakeFont(infos)
        shape_compat._distributing_shape_text(font, "ab", 12.0, None)
        self.assertEqual(len(font.subset.assigned), 3)
        self.assertTrue(all(t for t in font.subset.assigned))

    def test_latin_one_to_one_matches_identity(self):
        text = "AB"
        infos = [_FakeInfo(1, 0), _FakeInfo(2, 1)]
        font = _FakeFont(infos)
        shape_compat._distributing_shape_text(font, text, 12.0, None)
        self.assertEqual(font.subset.assigned,
                         [(ord("A"),), (ord("B"),)])

    def test_empty_string_is_noop(self):
        font = _FakeFont([])
        self.assertEqual(
            shape_compat._distributing_shape_text(font, "", 12.0, None), [])


class PatchInstallationTests(unittest.TestCase):
    def test_patch_active_and_idempotent(self):
        from fpdf.fonts import TTFFont
        self.assertTrue(
            getattr(TTFFont.shape_text, shape_compat._PATCH_ATTR, False))
        self.assertTrue(shape_compat.apply_harfbuzz_tounicode_fix())
        before = TTFFont.shape_text
        self.assertTrue(shape_compat.apply_harfbuzz_tounicode_fix())
        self.assertIs(TTFFont.shape_text, before)

    def test_pinned_fpdf_version(self):
        self.assertIn(fpdf.__version__, shape_compat.TESTED_FPDF_VERSIONS)

    def test_untested_fpdf_version_fails_loudly(self):
        from fpdf.fonts import TTFFont
        marker = shape_compat._PATCH_ATTR
        had = getattr(TTFFont.shape_text, marker, False)
        original = TTFFont.shape_text
        try:
            # Simulate a brand-new, never-patched fpdf on a higher version.
            def fresh(self, text, size, params):  # pragma: no cover - marker
                return []
            TTFFont.shape_text = fresh
            with unittest.mock.patch.object(fpdf, "__version__", "9.9.9"):
                with self.assertRaises(RuntimeError):
                    shape_compat.apply_harfbuzz_tounicode_fix()
        finally:
            TTFFont.shape_text = original
            if had:
                setattr(TTFFont.shape_text, marker, True)


class RendererRoundTripTests(unittest.TestCase):
    def _render(self, questions, *, display="end"):
        import tempfile
        from pdf_service.render import render_testseries_pdf
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "ts.pdf"
            render_testseries_pdf(
                questions, exam_title="टेक्स्ट लेयर परीक्षण Exam",
                tagline="Test Series", quiz_names=["ROUNDTRIP"],
                solution_display=display, output_path=out)
            data = out.read_bytes()
        self.assertTrue(data[:5] == b"%PDF-")
        return data

    def _extract(self, data):
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            return "\n".join(p.get_text() for p in doc)
        finally:
            doc.close()

    def test_end_layout_hindi_roundtrips_without_raw_cid(self):
        questions = [
            {"question": f"प्रश्न {i}: {word} किस राज्य/भाषा से सम्बंधित है?",
             "options": [f"{word} विकल्प क", f"{word} विकल्प ख",
                         "तीसरा विकल्प", "चौथा विकल्प"],
             "correct_option_id": 0,
             "explanation": f"व्याख्या: {word} एक परीक्षण शब्द है।"}
            for i, word in enumerate(HINDI_WORDS, 1)
        ]
        text = self._extract(self._render(questions))
        for word in HINDI_WORDS:
            # Whole phrases vs single words; the phrase entries contain spaces
            token = word
            self.assertIn(token, text, f"corrupted in PDF text layer: {token}")
        # Bold title words also come through.
        for word in BOLD_WORDS:
            self.assertIn(word, text)
        # The bug signature: C0 control chars / (cid:n) from unmapped glyphs.
        controls = [c for c in text if ord(c) < 0x20 and c not in "\n\r\t"]
        self.assertEqual(controls, [], f"raw-CID/control chars: {controls!r}")
        self.assertNotIn("(cid:", text)
        # Latin content still intact.
        self.assertIn("ROUNDTRIP", text)
        self.assertIn("Answer Key", text)
        self.assertIn("Detailed Solutions", text)

    def test_inline_layout_hindi_roundtrips(self):
        questions = [
            {"question": "नई दिल्ली भारत की राजधानी क्या है?",
             "options": ["मुंबई", "दिल्ली", "कोलकाता", "चेन्नई"],
             "correct_option_id": 1,
             "explanation": "नई दिल्ली राष्ट्रीय राजधानी है।"},
            {"question": "किताब में क्षेत्र का वर्णन किस पंक्ति में है?",
             "options": ["पहली", "दूसरी", "तीसरी", "चौथी"],
             "correct_option_id": [0, 1],
             "explanation": "अंग्रेज़ी और हिन्दी दोनों में।"},
        ]
        text = self._extract(self._render(questions, display="inline"))
        for word in ("नई दिल्ली", "राजधानी", "क्षेत्र", "पंक्ति",
                     "अंग्रेज़ी", "मुंबई", "कोलकाता"):
            self.assertIn(word, text, word)
        controls = [c for c in text if ord(c) < 0x20 and c not in "\n\r\t"]
        self.assertEqual(controls, [])


if __name__ == "__main__":
    unittest.main()
