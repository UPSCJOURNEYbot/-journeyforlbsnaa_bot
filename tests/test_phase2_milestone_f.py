"""Phase 2 milestone F: robustness and scale.

safe_decide_visual never raises (None/wrong types/empty/adversarial
unicode/huge inputs all return a spec or None); pathological inputs
complete in bounded time with deterministic results; the render path
tolerates empty/None payloads and spaceless words. Offline throughout.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from pdf_service.viz import engine as viz
from pdf_service.viz import templates
from pdf_service.viz.engine import VisualSpec
from tests.test_phase2_milestone_b import FakePDF

WORDS = ["alpha", "beta", "gamma", "delta",
         "zeta", "theta", "kappa", "lambda"]


def decide(question, explanation=""):
    return viz.safe_decide_visual(question, (), explanation)


class AdversarialCases(unittest.TestCase):
    def test_never_raises(self):
        cases = [
            (None, None),
            (123, 456),
            (["q"], {"x": 1}),
            ("", ""),
            ("   ", "\n\t "),
            ("?", "• a\n• b\n• c"),
            ("%s %s", "%(a)s • %d • {x}"),
            ("Zählen? 你好",
             "• Z̷a̷l̷g̷o̷ text • emoji 🎉🎉 • RTL עברית • CJK 日本語 • end."),
            ("Steps?",
             "1. " + "A" * 500 + "\n2. " + "B" * 500 + "\n3. " + "C" * 500),
            ("What?" * 2000, "Blah. " * 20000),
        ]
        for question, expl in cases:
            with self.subTest(question=str(question)[:20]):
                spec = decide(question, expl)
                if spec is not None:
                    self.assertIn(spec.visual_type, viz.SUPPORTED_TYPES)
                    self.assertTrue(spec.evidence)


class ScaleCases(unittest.TestCase):
    def test_ten_thousand_bullets_bounded(self):
        expl = "\n".join("• item number %d here" % i for i in range(10000))
        started = time.time()
        first = decide("List things.", expl)
        elapsed = time.time() - started
        self.assertLess(elapsed, 15.0)
        self.assertIsNotNone(first)
        # Year-like numbers still count as year evidence at scale.
        self.assertEqual(first.visual_type, "timeline")
        second = decide("List things.", expl)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_nested_thousands_keep_mind_capped(self):
        expl = "".join(
            "• top %s branch\n  • sub detail one here\n"
            "  • sub detail two here\n" % WORDS[i % 8]
            for i in range(2000))
        started = time.time()
        first = decide("Describe the system.", expl)
        elapsed = time.time() - started
        self.assertLess(elapsed, 15.0)
        self.assertIsNotNone(first)
        self.assertEqual(first.visual_type, "mind_map")
        self.assertEqual(first.payload["branches"][:6],
                         first.payload["branches"])
        second = decide("Describe the system.", expl)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_scan_window_units(self):
        self.assertEqual(len(viz._bullets("• x\n" * 500)), 256)
        self.assertEqual(len(viz._numbered_items("1. x\n" * 500)), 256)
        self.assertEqual(len(viz._flat_items("• x\n" * 500)), 256)

    def test_three_hundred_bullets_classify_capped(self):
        expl = ("Types of things:\n" + "\n".join(
            "• thing number %s" % WORDS[i % 8] for i in range(300)))
        spec = decide("What are the types of things?", expl)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")
        self.assertEqual(len(spec.payload["items"]), 6)

    def test_three_hundred_flat_is_infographic_not_panels(self):
        expl = ("Features list:\n" + "\n".join(
            "• feature %s" % WORDS[i % 8] for i in range(300)))
        spec = decide("What are the features?", expl)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "infographic")
        self.assertEqual(len(spec.payload["points"]), 8)


class RenderEdgeCases(unittest.TestCase):
    def _draws(self, spec):
        est = templates.estimate_height_for_spec(spec, 170.0)
        templates.draw_diagram(FakePDF(), spec, (10.0, 10.0, 170.0, est))

    def test_empty_payloads_draw(self):
        self._draws(VisualSpec(visual_type="process", subject="geography",
                               title="T", payload={"steps": []}))
        self._draws(VisualSpec(visual_type="timeline", subject="history",
                               title="T", payload={"events": []}))
        self._draws(VisualSpec(visual_type="mind_map", subject="geography",
                               title="T",
                               payload={"center": "C", "branches": []}))
        self._draws(VisualSpec(visual_type="process", subject="geography",
                               title="T", payload={}))

    def test_none_label_and_spaceless_draw(self):
        self._draws(VisualSpec(
            visual_type="process", subject="geography", title="T",
            payload={"steps": [{"label": None}, {"label": "x"},
                               {"label": "y"}]}))
        self._draws(VisualSpec(
            visual_type="flowchart", subject="polity", title="T",
            payload={"steps": [{"label": "A" * 500},
                               {"label": "B" * 500},
                               {"label": "C" * 500}]}))

    def test_spaceless_renders_in_real_pdf(self):
        import tempfile

        import fitz

        from pdf_service.render import render_testseries_pdf
        questions = [{
            "question": "What is the procedure?",
            "options": ["A1", "A2", "A3", "A4"],
            "correct_option_id": 0,
            "explanation": ("1. " + "Alfa" * 125 + "\n2. Notification.\n"
                            "3. Declaration."),
        }]
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "f.pdf")
            render_testseries_pdf(
                questions, exam_title="F", tagline="Test Series",
                quiz_names=["QZ"], solution_display="end",
                output_path=out)
            with fitz.open(out) as doc:
                text = "\n".join(p.get_text() for p in doc)
        self.assertIn("Flowchart", text)


class PerfSanityCases(unittest.TestCase):
    def test_realistic_sizes_fast(self):
        expl = ("Consider the following statements.\n" + "\n".join(
            "• Statement %d about tributaries here." % i
            for i in range(40)))
        started = time.time()
        for _ in range(5):
            decide("Consider the following statements.", expl)
        self.assertLess(time.time() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
