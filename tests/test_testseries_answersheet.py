"""Part I: optional bubble answer sheet (renderer + /newseries toggle).

Proves with real PDFs:

  * one bubble row per question with exactly as many circles as the
    question has options (2..10), rows Q1..QN contiguous across pages;
  * series identity (test/booklet/subject) printed, candidate write-in
    boxes rendered (explicit fields, or Name/Roll/Date defaults);
  * the sheet leaks no answers (no "Answer:" text, every drawing is a
    stroke-only outline -- no filled bubbles);
  * sheet pages are inside bounds with no blank pages;
  * the sheet is off unless explicitly enabled (legacy parity), in both
    end and inline modes.

Plus wizard coverage: config default False, setup mapping, preview line,
static chain solutions -> answer_sheet -> visuals, and the edit-mode
return walks (manual numbers / custom marks complete their input steps
before returning to the preview).
"""

from __future__ import annotations

import itertools
import re
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
import fitz  # noqa: E402  (PyMuPDF; already a project dependency)

from pdf_service.render import render_testseries_pdf  # noqa: E402


def _sheet_questions(n: int) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        if i % 9 == 0:
            k = 10
        elif i % 3 == 0:
            k = 2
        elif i % 3 == 1:
            k = 5
        else:
            k = 4
        out.append({"question": f"Sheet question {i}?",
                    "options": [f"o{i}-{x}" for x in range(k)],
                    "correct_option_id": i % k,
                    "explanation": f"Sheet expl {i}."})
    return out


def _render(questions: list[dict], tmpdir: str, name: str,
            **kwargs) -> tuple[fitz.Document, dict]:
    path = Path(tmpdir) / name
    info = render_testseries_pdf(questions, output_path=path, **kwargs)
    return fitz.open(path), info


def _sheet_pages(doc: fitz.Document) -> list[int]:
    pages = [p.get_text() for p in doc]
    start = next(i for i, t in enumerate(pages) if "Answer Sheet" in t)
    end = len(pages)
    for i in range(start + 1, len(pages)):
        if "Answer Key" in pages[i] or "Detailed Solutions" in pages[i]:
            end = i
            break
    return list(range(start, end))


def _parse_rows(doc: fitz.Document, idxs: list[int]) -> dict[int, list[str]]:
    lines: list[str] = []
    for i in idxs:
        lines.extend(ln.strip() for ln in doc[i].get_text().splitlines())
    rows: dict[int, list[str]] = {}
    i = 0
    while i < len(lines):
        m = re.fullmatch(r"Q(\d+)", lines[i] or "")
        if not m:
            i += 1
            continue
        letters: list[str] = []
        j = i + 1
        while j < len(lines) and re.fullmatch(r"[A-J]", lines[j] or ""):
            letters.append(lines[j])
            j += 1
        rows[int(m.group(1))] = letters
        i = j
    return rows


def _assert_sheet_geometry(case: unittest.TestCase, doc: fitz.Document,
                           idxs: list[int]) -> None:
    for pno in idxs:
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
        for b in blocks:
            x0, y0, x1, y1 = b["bbox"]
            case.assertGreaterEqual(x0, -0.5)
            case.assertGreaterEqual(y0, -0.5)
            case.assertLessEqual(x1, w + 0.5)
            case.assertLessEqual(y1, h + 0.5)
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
            case.assertLessEqual(area / max(1e-6, min(aa, bb)), 0.35)
        for d in page.get_drawings():
            x0, y0, x1, y1 = d["rect"]
            case.assertGreaterEqual(x0, -1.0)
            case.assertGreaterEqual(y0, -1.0)
            case.assertLessEqual(x1, w + 1.0)
            case.assertLessEqual(y1, h + 1.0)
        case.assertTrue(page.get_text().strip(), f"blank sheet page {pno}")


class SheetRenderCases(unittest.TestCase):
    SETUP = {"subject": "GS", "test_number": "3", "booklet_series": "A",
             "booklet_number": "12",
             "candidate_fields": ["Candidate Name", "Roll Number"],
             "answer_sheet": True}

    def test_rows_match_option_counts(self) -> None:
        qs = _sheet_questions(30)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            doc, _ = _render(qs, tmp, "rows.pdf", exam_title="Rows",
                             tagline="TS", quiz_names=["QZ"],
                             series_setup=dict(self.SETUP))
            try:
                idxs = _sheet_pages(doc)
                rows = _parse_rows(doc, idxs)
                self.assertEqual(sorted(rows), list(range(1, 31)))
                for i, q in enumerate(qs, 1):
                    want = [chr(65 + k) for k in range(len(q["options"]))]
                    self.assertEqual(rows[i], want, f"row Q{i}")
                _assert_sheet_geometry(self, doc, idxs)
            finally:
                doc.close()

    def test_identity_and_candidates(self) -> None:
        qs = _sheet_questions(5)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            doc, _ = _render(qs, tmp, "ident.pdf", exam_title="Ident",
                             tagline="TS", quiz_names=["QZ"],
                             series_setup=dict(self.SETUP))
            try:
                idxs = _sheet_pages(doc)
                text = "".join(doc[i].get_text() for i in idxs)
                for value in ("Test No: 3", "Booklet: A-12", "Subject: GS",
                              "Candidate Name:", "Roll Number:"):
                    self.assertIn(value, text)
            finally:
                doc.close()

    def test_default_candidate_rows(self) -> None:
        qs = _sheet_questions(5)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            doc, _ = _render(qs, tmp, "def.pdf", exam_title="Def",
                             tagline="TS", quiz_names=["QZ"],
                             series_setup={"answer_sheet": True})
            try:
                idxs = _sheet_pages(doc)
                text = "".join(doc[i].get_text() for i in idxs)
                for value in ("Candidate Name:", "Roll Number:", "Date:"):
                    self.assertIn(value, text)
            finally:
                doc.close()

    def test_no_answer_leak(self) -> None:
        qs = _sheet_questions(20)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            doc, _ = _render(qs, tmp, "leak.pdf", exam_title="Leak",
                             tagline="TS", quiz_names=["QZ"],
                             series_setup=dict(self.SETUP))
            try:
                idxs = _sheet_pages(doc)
                text = "".join(doc[i].get_text() for i in idxs)
                self.assertNotIn("Answer:", text)
                n_outline = 0
                for i in idxs:
                    for d in doc[i].get_drawings():
                        self.assertFalse(d.get("fill"),
                                         "filled shape on answer sheet")
                        n_outline += 1
                # 20 rows x 2..10 circles (+ write-in boxes): outlines only.
                self.assertGreater(n_outline, 20)
            finally:
                doc.close()

    def test_multipage_sheet(self) -> None:
        qs = _sheet_questions(100)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            doc, _ = _render(qs, tmp, "multi.pdf", exam_title="Multi",
                             tagline="TS", quiz_names=["QZ"],
                             series_setup=dict(self.SETUP))
            try:
                idxs = _sheet_pages(doc)
                self.assertGreater(len(idxs), 1, "expected a page break")
                rows = _parse_rows(doc, idxs)
                self.assertEqual(sorted(rows), list(range(1, 101)))
                _assert_sheet_geometry(self, doc, idxs)
            finally:
                doc.close()

    def test_off_by_default(self) -> None:
        qs = _sheet_questions(5)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            for name, kw in (("legacy", {}),
                             ("noflag", {"series_setup": {"subject": "GS"}}),
                             ("false", {"series_setup": {"answer_sheet": False}})):
                doc, _ = _render(qs, tmp, f"{name}.pdf", exam_title=name,
                                 tagline="TS", quiz_names=["QZ"], **kw)
                try:
                    text = "\n".join(p.get_text() for p in doc)
                    self.assertNotIn("Answer Sheet", text, name)
                finally:
                    doc.close()

    def test_inline_mode(self) -> None:
        qs = _sheet_questions(10)
        with tempfile.TemporaryDirectory(prefix="tss-") as tmp:
            doc, _ = _render(qs, tmp, "inline.pdf", exam_title="Inline",
                             tagline="TS", quiz_names=["QZ"],
                             solution_display="inline",
                             series_setup=dict(self.SETUP))
            try:
                idxs = _sheet_pages(doc)
                rows = _parse_rows(doc, idxs)
                self.assertEqual(sorted(rows), list(range(1, 11)))
            finally:
                doc.close()


class SheetWizardCases(unittest.TestCase):
    def test_config_default_mapping_preview(self) -> None:
        from quizbot.creator_bot.handlers.testseries_create import (
            TestSeriesConfig, _render_preview, build_series_setup)
        cfg = TestSeriesConfig(title="T", institute_name="I",
                               total_questions=5)
        self.assertFalse(cfg.answer_sheet)
        self.assertIn("Sheet: No", _render_preview(cfg))
        self.assertFalse(build_series_setup(cfg, {})["answer_sheet"])
        cfg.answer_sheet = True
        self.assertIn("Sheet: Yes", _render_preview(cfg))
        self.assertTrue(build_series_setup(cfg, {})["answer_sheet"])

    def test_static_chain(self) -> None:
        from quizbot.creator_bot.handlers.testseries_create import (
            _STATIC_NEXT)
        self.assertEqual(_STATIC_NEXT["solutions"], "answer_sheet")
        self.assertEqual(_STATIC_NEXT["answer_sheet"], "visuals")

    def _walk(self, section: str, walk: list[str], **cfg_over) -> dict:
        from quizbot.creator_bot.handlers.testseries_create import (
            _advance, SECTION_START)
        quiz = {"quiz_name": "QZ", "questions": [
            {"question": "Q?", "options": ["a", "b"], "correct_option_id": 0,
             "explanation": ""}]}
        cfg = dict(title="T", subject="S", test_number_mode="auto",
                   booklet_series="A", booklet_number_mode="auto",
                   institute_name="I", watermark_mode="none",
                   answer_key=True, solutions=True, visuals="auto",
                   marks_correct=2.0, marks_negative=-0.66)
        cfg.update(cfg_over)
        sess = {"step": SECTION_START[section], "config": cfg, "assets": {},
                "quizzes": [quiz], "source": "qids", "edit_return": True,
                "edit_section": section}
        for expect in walk:
            self.assertEqual(sess["step"], expect, (section, walk))
            if expect == "marks_correct_input":
                sess["config"]["marks_correct"] = 4.0
            if expect == "marks_negative_input":
                sess["config"]["marks_negative"] = -1.0
            _advance(sess)
        return sess

    def test_edit_walk_settings(self) -> None:
        sess = self._walk("settings", ["answer_key", "solutions",
                                       "answer_sheet", "visuals"])
        self.assertEqual(sess["step"], "preview")
        self.assertFalse(sess["edit_return"])

    def test_edit_walk_manual_numbers_completes_inputs(self) -> None:
        sess = self._walk(
            "numbers",
            ["test_number", "test_number_input", "booklet_series",
             "booklet_series_input", "booklet_number",
             "booklet_number_input"],
            test_number_mode="manual", test_number="3",
            booklet_series="custom", booklet_number_mode="manual",
            booklet_number="12")
        self.assertEqual(sess["step"], "preview")
        self.assertFalse(sess["edit_return"])

    def test_edit_walk_auto_numbers(self) -> None:
        sess = self._walk("numbers", ["test_number", "booklet_series",
                                      "booklet_number"])
        self.assertEqual(sess["step"], "preview")
        self.assertFalse(sess["edit_return"])

    def test_edit_walk_custom_marks(self) -> None:
        sess = self._walk("marks", ["marks_correct", "marks_correct_input",
                                    "marks_negative", "marks_negative_input"],
                          marks_correct="custom", marks_negative="custom")
        self.assertEqual(sess["step"], "preview")
        self.assertFalse(sess["edit_return"])

    def test_edit_walk_preset_marks(self) -> None:
        sess = self._walk("marks", ["marks_correct", "marks_negative"])
        self.assertEqual(sess["step"], "preview")
        self.assertFalse(sess["edit_return"])


if __name__ == "__main__":
    unittest.main()
