"""Part H: page-break safety for the test-series renderer.

Proves with real PDFs:

  * solution visuals are atomic: a ~117 mm flowchart swept through every
    start-of-page offset (0..11 padding questions) never splits across a
    page break, in both end and inline modes;
  * consecutive visuals, and visuals adjacent to very long explanations,
    stay complete, co-located and inside page bounds;
  * long stems/options/explanations flow across pages without corrupting
    geometry (no out-of-bounds text/drawings, no overprinted blocks,
    no blank pages);
  * an extreme cover (long institute/subject/tagline, all candidate
    fields, logo + watermark images) stays inside bounds.
"""

from __future__ import annotations

import itertools
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
import fitz  # noqa: E402  (PyMuPDF; already a project dependency)

from pdf_service.render import render_testseries_pdf  # noqa: E402


def _plain(i: int) -> dict:
    return {"question": f"Padding question {i} about {i * 7}?",
            "options": [f"opt {i}-{k}" for k in "ABCD"],
            "correct_option_id": i % 4,
            "explanation": f"Padding explanation {i}."}


def _tall_flowchart(tag: str) -> dict:
    steps = "\n".join(f"{n}. Election stage {n} {tag} work."
                      for n in range(1, 9))
    return {"question": f"What is the election procedure? {tag}Q",
            "options": ["Elect A", "Elect B", "Elect C", "Elect D"],
            "correct_option_id": 0,
            "explanation": steps + f" {tag}E"}


def _map_q(tag: str) -> dict:
    return {"question": f"Where is Chilika lake? {tag}Q",
            "options": [f"Chilika {tag}O", "Sambhar", "Wular", "Dal"],
            "correct_option_id": 0,
            "explanation": ("Chilika is a brackish lagoon on the Odisha "
                            f"coast. {tag}E")}


def _render(questions: list[dict], tmpdir: str, name: str,
            **kwargs) -> tuple[fitz.Document, dict]:
    path = Path(tmpdir) / name
    info = render_testseries_pdf(questions, output_path=path, **kwargs)
    return fitz.open(path), info


def _assert_geometry(case: unittest.TestCase, doc: fitz.Document) -> None:
    for pno, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
        for b in blocks:
            x0, y0, x1, y1 = b["bbox"]
            case.assertGreaterEqual(x0, -0.5, f"page {pno} text OOB")
            case.assertGreaterEqual(y0, -0.5, f"page {pno} text OOB")
            case.assertLessEqual(x1, w + 0.5, f"page {pno} text OOB")
            case.assertLessEqual(y1, h + 0.5, f"page {pno} text OOB")
        for a, b in itertools.combinations(blocks, 2):
            ix0 = max(a["bbox"][0], b["bbox"][0])
            iy0 = max(a["bbox"][1], b["bbox"][1])
            ix1 = min(a["bbox"][2], b["bbox"][2])
            iy1 = min(a["bbox"][3], b["bbox"][3])
            area = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            if area <= 0:
                continue
            aa = (a["bbox"][2] - a["bbox"][0]) * (a["bbox"][3] - a["bbox"][1])
            bb = (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])
            case.assertLessEqual(area / max(1e-6, min(aa, bb)), 0.35,
                                 f"page {pno} overprinted text")
        for d in page.get_drawings():
            x0, y0, x1, y1 = d["rect"]
            case.assertGreaterEqual(x0, -1.0, f"page {pno} drawing OOB")
            case.assertGreaterEqual(y0, -1.0, f"page {pno} drawing OOB")
            case.assertLessEqual(x1, w + 1.0, f"page {pno} drawing OOB")
            case.assertLessEqual(y1, h + 1.0, f"page {pno} drawing OOB")
        case.assertTrue(page.get_text().strip() or page.get_drawings(),
                        f"blank page {pno}")


def _assert_flowchart_whole(case: unittest.TestCase, doc: fitz.Document,
                            tag: str, expected_badges: int = 1) -> None:
    """The flowchart was not split across a page break.

    The visual always follows its explanation, so every tag-scoped visual
    string (diagram title echo, node texts) must live on the badge page or
    earlier; anything on a later page is a split fragment.
    """
    pages = [p.get_text() for p in doc]
    total = sum(pg.count("Flowchart") for pg in pages)
    case.assertEqual(total, expected_badges, "flowchart badge count")
    badge_pgs = [i for i, pg in enumerate(pages) if "Flowchart" in pg]
    own_badge = [i for i in badge_pgs if f"{tag}Q" in pages[i]]
    case.assertEqual(len(own_badge), 1, f"title page for {tag}")
    for i in range(own_badge[0] + 1, len(pages)):
        case.assertNotIn(f"{tag}Q", pages[i], f"flowchart {tag} split")
        case.assertNotIn(f"Election stage 8 {tag} work.", pages[i],
                         f"flowchart {tag} split")


class AtomicityCases(unittest.TestCase):
    """Tall-visual atomicity at every page offset, end + inline modes."""

    def test_flowchart_atomic_end_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            for pads in range(12):
                with self.subTest(pads=pads):
                    tag = f"P{pads}"
                    qs = [_plain(i) for i in range(pads)]
                    qs.append(_tall_flowchart(tag))
                    doc, _ = _render(qs, tmp, f"e{pads}.pdf",
                                     exam_title="Sweep End", tagline="TS",
                                     quiz_names=["QZ"])
                    try:
                        _assert_flowchart_whole(self, doc, tag)
                        _assert_geometry(self, doc)
                    finally:
                        doc.close()

    def test_flowchart_atomic_inline_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            for pads in range(12):
                with self.subTest(pads=pads):
                    tag = f"I{pads}"
                    qs = [_plain(i) for i in range(pads)]
                    qs.append(_tall_flowchart(tag))
                    doc, _ = _render(qs, tmp, f"i{pads}.pdf",
                                     exam_title="Sweep Inline", tagline="TS",
                                     quiz_names=["QZ"], solution_display="inline")
                    try:
                        _assert_flowchart_whole(self, doc, tag)
                        _assert_geometry(self, doc)
                    finally:
                        doc.close()


class AdjacencyCases(unittest.TestCase):
    """Consecutive visuals and visuals next to very long explanations."""

    def test_consecutive_visuals(self) -> None:
        qs = [_map_q("C0"), _tall_flowchart("C1"), _map_q("C2"),
              _tall_flowchart("C3"), _map_q("C4"), _tall_flowchart("C5")]
        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            doc, _ = _render(qs, tmp, "consec.pdf", exam_title="Consec",
                             tagline="TS", quiz_names=["QZ"])
            try:
                text = "\n".join(p.get_text() for p in doc)
                self.assertEqual(text.count("Not to scale"), 3)
                self.assertEqual(text.count("Flowchart"), 3)
                pages = [p.get_text() for p in doc]
                for tag in ("C0", "C2", "C4"):
                    self.assertTrue(
                        any("Not to scale" in pg and f"{tag}Q" in pg
                            for pg in pages),
                        f"map {tag} not co-located")
                for tag in ("C1", "C3", "C5"):
                    _assert_flowchart_whole(self, doc, tag, expected_badges=3)
                _assert_geometry(self, doc)
            finally:
                doc.close()

    def test_visual_after_long_explanation(self) -> None:
        long_q = _plain(0)
        long_q["explanation"] = "Long explanation. " * 400  # ~7k chars
        qs = [long_q, _map_q("AL")]
        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            doc, _ = _render(qs, tmp, "afterlong.pdf", exam_title="AfterLong",
                             tagline="TS", quiz_names=["QZ"])
            try:
                pages = [p.get_text() for p in doc]
                self.assertTrue(
                    any("Not to scale" in pg and "ALQ" in pg for pg in pages),
                    "map after long explanation not co-located")
                _assert_geometry(self, doc)
            finally:
                doc.close()

    def test_long_explanation_after_visual(self) -> None:
        long_q = _plain(0)
        long_q["explanation"] = "Long explanation. " * 400
        qs = [_map_q("LA"), long_q]
        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            doc, _ = _render(qs, tmp, "beforelong.pdf", exam_title="BeforeLong",
                             tagline="TS", quiz_names=["QZ"])
            try:
                pages = [p.get_text() for p in doc]
                self.assertTrue(
                    any("Not to scale" in pg and "LAQ" in pg for pg in pages),
                    "map before long explanation not co-located")
                _assert_geometry(self, doc)
            finally:
                doc.close()

    def test_long_options_flow_cleanly(self) -> None:
        q = _plain(0)
        q["question"] = "Long stem. " * 200
        q["options"] = [f"Long option {k}. " * 100 for k in "ABCD"]
        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            doc, info = _render([q], tmp, "longopt.pdf", exam_title="LongOpt",
                                tagline="TS", quiz_names=["QZ"])
            try:
                self.assertGreater(len(doc), 2, "expected multi-page flow")
                text = "\n".join(p.get_text() for p in doc)
                self.assertIn("Long stem.", text)
                self.assertIn("Long option A.", text)
                self.assertEqual(info["questions"], 1)
                _assert_geometry(self, doc)
            finally:
                doc.close()


class CoverExtremeCases(unittest.TestCase):
    """Extreme cover content stays inside bounds with all boxes/images."""

    def test_cover_extremes(self) -> None:
        from quizbot.creator_bot.handlers.testseries_create import (
            TestSeriesConfig, build_series_setup)
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="tsh-") as tmp:
            logo = Path(tmp) / "logo.png"
            Image.new("RGB", (400, 200), (180, 30, 30)).save(logo)
            wm = Path(tmp) / "wm.png"
            Image.new("RGBA", (300, 300), (30, 60, 180, 255)).save(wm)
            cfg = TestSeriesConfig(
                title="Extreme Cover " + "Mock " * 20,
                subject="General Studies " + "Paper " * 15,
                test_number_mode="manual", test_number="12345",
                booklet_series="ZZ", booklet_number_mode="manual",
                booklet_number="99999", paper="Paper IV of VII",
                duration="3 hours 30 minutes", test_code="EXTREME-1" * 5,
                institute_name="Institute of " + "Very " * 25 + "Long Names",
                logo_present=True, watermark_mode="both",
                watermark_text="EXTREME-WATERMARK-TEXT",
                wm_image_present=True, tagline="Tag " * 40,
                marks_correct=4.0, marks_negative=-1.33,
                cand_name=True, cand_roll=True, cand_regid=True,
                cand_batch=True, cand_date=True, cand_candsig=True,
                cand_evalsig=True, total_questions=5, max_marks=20.0)
            setup = build_series_setup(
                cfg, {"logo": logo.read_bytes(), "wm_image": wm.read_bytes()})
            qs = [_plain(i) for i in range(5)]
            doc, _ = _render(qs, tmp, "extreme.pdf", exam_title=cfg.title,
                             tagline=cfg.tagline, quiz_names=["QZ"],
                             series_setup=setup)
            try:
                text = "\n".join(p.get_text() for p in doc)
                for value in ["ZZ-99999", "12345", "Paper IV of VII",
                              "3 hours 30 minutes", "Candidate Name",
                              "Roll Number", "Registration / Candidate ID",
                              "Batch", "Date", "Candidate Signature",
                              "Evaluator / Invigilator Signature",
                              "20", "+4", "-1.33"]:
                    self.assertIn(value, text, f"cover missing {value!r}")
                # Watermark text is rotated; drawings/text bounds still hold
                # for content, but skip the strict text-overlap rule (the
                # watermark underprint overlaps content by design).
                for pno, page in enumerate(doc):
                    w, h = page.rect.width, page.rect.height
                    for b in [b for b in page.get_text("dict")["blocks"]
                              if b["type"] == 0]:
                        x0, y0, x1, y1 = b["bbox"]
                        self.assertGreaterEqual(x0, -0.5)
                        self.assertGreaterEqual(y0, -0.5)
                        self.assertLessEqual(x1, w + 0.5)
                        self.assertLessEqual(y1, h + 0.5)
                    self.assertTrue(page.get_text().strip()
                                    or page.get_drawings(),
                                    f"blank page {pno}")
            finally:
                doc.close()


if __name__ == "__main__":
    unittest.main()
