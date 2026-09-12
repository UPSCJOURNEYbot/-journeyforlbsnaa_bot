"""Phase 3 milestone 2: PDF-flow wiring integration tests (no network).

Proves the visual engine is wired into the detailed solution section:
visual-eligible questions get a real visual, others stay text-only,
uncertain data skips safely, and visual failures never break the PDF.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz  # PyMuPDF; already a project dependency

from pdf_service.render import render_testseries_pdf

BADGES = ("Locator Map", "Region Map", "Historical Map", "Process",
          "Flowchart", "Concept Map", "Mind Map", "Timeline", "Comparison",
          "Cause & Effect", "Cycle", "Diagram", "Classification",
          "Quick Revision")


def _q(question, options, explanation=""):
    return {"question": question, "options": options,
            "correct_option_id": 0, "explanation": explanation}


MAP_Q = _q("Where is Chilika lake?",
           ["Chilika", "Sambhar", "Wular", "Dal"],
           "Chilika is a brackish lagoon on the Odisha coast.")

TIMELINE_Q = _q("Arrange the following events in chronological order.",
                ["1857 first", "1919 second", "1942 third", "None"],
                ("The revolt of 1857 shook the empire. "
                 "In 1919 came a massacre. "
                 "The Quit India movement followed in 1942."))

PLAIN_Q = _q("What is 7 x 8?", ["54", "56", "58", "60"], "It equals 56.")

LONDON_Q = _q("Where is London?", ["London", "Paris", "Rome", "Madrid"],
              "London is the capital of the UK.")

UNKNOWN_Q = _q("Where is the city of Xyzabc located?",
               ["Xyzabc", "Nowhere", "Unknown", "None"],
               "No such city exists in our dataset records.")

HINDI_MAP_Q = _q("\u091a\u093f\u0932\u094d\u0915\u093e \u091d\u0940\u0932 "
                 "\u0915\u0939\u093e\u0901 \u0938\u094d\u0925\u093f\u0924 "
                 "\u0939\u0948?",
                 ["\u091a\u093f\u0932\u094d\u0915\u093e", "\u0938\u093e\u0902\u092d\u0930",
                  "\u0935\u0941\u0932\u0930", "\u0921\u0932"],
                 "\u091a\u093f\u0932\u094d\u0915\u093e \u0913\u0921\u093f\u0936\u093e "
                 "\u092e\u0947\u0902 \u090f\u0915 \u0916\u093e\u0930\u0947 "
                 "\u092a\u093e\u0928\u0940 \u0915\u0940 \u091d\u0940\u0932 \u0939\u0948\u0964")


class _RenderBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="viz-int-")
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def _pdf(self, questions, display="end"):
        self._n += 1
        out = str(Path(self._tmp.name) / ("t%d.pdf" % self._n))
        info = render_testseries_pdf(
            questions, exam_title="Integration Test", tagline="Test Series",
            quiz_names=["QZ"], solution_display=display, output_path=out)
        with open(out, "rb") as fh:
            self.assertTrue(fh.read(4) == b"%PDF")
        return out, info

    def _text_and_drawings(self, path):
        with fitz.open(path) as doc:
            text = "\n".join(page.get_text() for page in doc)
            drawings = sum(len(page.get_drawings()) for page in doc)
        return text, drawings


class WiringCases(_RenderBase):
    def test_map_visual_in_solution(self):
        path, _info = self._pdf([MAP_Q])
        text, drawings = self._text_and_drawings(path)
        self.assertIn("Not to scale", text)
        self.assertIn("Simplified outline", text)
        self.assertIn("Chilika", text)
        _plain, plain_drawings = self._text_and_drawings(
            self._pdf([PLAIN_Q])[0])
        self.assertGreaterEqual(drawings - plain_drawings, 10,
                                "map must add real vector geometry")

    def test_diagram_visual_in_solution(self):
        path, _info = self._pdf([TIMELINE_Q])
        text, drawings = self._text_and_drawings(path)
        self.assertIn("Timeline", text)
        _plain, plain_drawings = self._text_and_drawings(
            self._pdf([PLAIN_Q])[0])
        self.assertGreaterEqual(drawings - plain_drawings, 5)

    def test_inline_mode_visual(self):
        path, _info = self._pdf([MAP_Q], display="inline")
        text, _drawings = self._text_and_drawings(path)
        self.assertIn("Not to scale", text)
        self.assertIn("Chilika", text)

    def test_non_visual_text_only(self):
        path, _info = self._pdf([PLAIN_Q])
        text, _drawings = self._text_and_drawings(path)
        self.assertIn("It equals 56.", text)
        self.assertNotIn("Not to scale", text)
        self.assertNotIn("Simplified outline", text)
        for badge in BADGES:
            self.assertNotIn(badge, text)

    def test_uncertain_data_skipped(self):
        path, _info = self._pdf([LONDON_Q])
        text, _drawings = self._text_and_drawings(path)
        self.assertIn("London is the capital of the UK.", text)
        self.assertNotIn("Not to scale", text)

    def test_unknown_place_skipped(self):
        path, _info = self._pdf([UNKNOWN_Q])
        text, _drawings = self._text_and_drawings(path)
        self.assertIn("No such city exists", text)
        self.assertNotIn("Not to scale", text)

    def test_visual_failure_never_breaks(self):
        with patch("pdf_service.viz.mapdraw.draw_map",
                   side_effect=RuntimeError("map boom")), \
             patch("pdf_service.viz.templates.draw_diagram",
                   side_effect=RuntimeError("diagram boom")):
            path, info = self._pdf([MAP_Q, TIMELINE_Q])
        self.assertEqual(info["questions"], 2)
        text, _drawings = self._text_and_drawings(path)
        self.assertIn("Chilika is a brackish lagoon", text)
        self.assertIn("Quit India movement", text)
        self.assertNotIn("Not to scale", text)
        self.assertNotIn("Timeline", text)

    def test_tall_visual_page_safe(self):
        steps = "\n".join("%d. Step number %d of the procedure." % (i, i)
                          for i in range(1, 9))
        tall = _q("List the eight steps of the purification process.",
                  ["A", "B", "C", "D"], steps)
        path, info = self._pdf([PLAIN_Q, tall])
        self.assertEqual(info["questions"], 2)
        text, _drawings = self._text_and_drawings(path)
        # "procedure" wording -> flowchart (process-vs-flowchart routing is
        # pinned in the engine unit tests; here only page safety matters).
        self.assertIn("Flowchart", text)
        self.assertIn("Step number 8", text)

    def test_hindi_map_visual(self):
        path, _info = self._pdf([HINDI_MAP_Q])
        text, _drawings = self._text_and_drawings(path)
        self.assertIn("Not to scale", text)
        # Marker + caption Hindi labels render (same shaping pipeline as
        # body text; assert extraction-stable tokens -- see note below).
        self.assertIn("झील", text)
        self.assertIn("लबासना", text)

    def test_real_map_geometry(self):
        path, _info = self._pdf([MAP_Q])
        _text, drawings = self._text_and_drawings(path)
        _plain, plain_drawings = self._text_and_drawings(
            self._pdf([PLAIN_Q])[0])
        # Graticule + outline + markers + scale + arrow: a real locator
        # map, never a generic lat/long box.
        self.assertGreaterEqual(drawings - plain_drawings, 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
