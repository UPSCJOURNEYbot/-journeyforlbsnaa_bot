"""Parts D/E/F/G/I(counts)/K/N: test-series question-count matrix + studio E2E.

Renders 10 / 25 / 50 / 100 / 200 / 300-question papers with realistic mixed
Hindi/English MCQs (pure-EN, pure-HI, mixed stems; long stems/options/
explanations; unicode punctuation; multi-answer keys; empty explanations;
map/timeline/comparison visuals) and proves, per size:

  * every question stem appears exactly once in Questions (visual stems
    twice: stem + map-caption echo), in strict order -- no truncation,
    duplication or reordering;
  * the key grid carries Q1..QN with the exact expected letters (including
    multi-answer "A, C") -- no answer drift;
  * Detailed Solutions carries "QN.  Answer: L" + the explanation marker
    (or "No explanation provided.") in order -- no solution drift;
  * Hindi/unicode content extracts cleanly with no U+FFFD replacement chars;
  * exactly the expected visuals fired (badge counts), maps co-located with
    their question via the caption echo, diagrams on the solution page;
  * page geometry is sane (all text/drawings inside the page, no overprinted
    text blocks) and no page is blank;
  * page count stays bounded (no blank-page explosion).

Plus: 300-question time/memory bounds, a live-API render at the previously
untested size 25, multi-quiz concatenation order (bot payload builder ->
render), and a full studio E2E (wizard config -> build_series_setup ->
render) proving every Part-D configured value lands in the PDF and matches
the preview text.
"""

from __future__ import annotations

import itertools
import json
import re
import socket
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.request
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
import fitz  # noqa: E402  (PyMuPDF; already a project dependency)

import uvicorn  # noqa: E402

import pdf_service.app as appmod  # noqa: E402
from pdf_service.render import answer_letters, render_testseries_pdf  # noqa: E402

SIZES = (10, 25, 50, 100, 200, 300)

_HI_BITS = [
    "भारत की राजधानी", "नई दिल्ली", "स्थिति रिपोर्ट", "उत्तर प्रदेश",
    "क्षेत्रीय भाषा", "त्रिशूल धारण", "लब्धप्रतिष्ठ व्यक्ति", "श्रृंखला टूटना",
]

_OTHER_BADGES = ("Locator Map", "Region Map", "Historical Map", "Process",
                 "Flowchart", "Concept Map", "Mind Map", "Cause & Effect",
                 "Cycle", "Diagram", "Classification", "Quick Revision",
                 "Four Panels", "Mechanism", "Route Chain")


def is_map_slot(i: int) -> bool:
    return i % 25 == 0


def is_timeline_slot(i: int) -> bool:
    return i % 25 == 12


def is_comparison_slot(i: int) -> bool:
    return i % 25 == 6


def is_visual_slot(i: int) -> bool:
    return is_map_slot(i) or is_timeline_slot(i) or is_comparison_slot(i)


def make_matrix_questions(n: int) -> list[dict]:
    """Deterministic realistic mixed HI/EN set with unique markers.

    Markers: MX{i}S in the stem, MX{i}O* in the options, MX{i}E in the
    explanation. Visual slots use known-firing wordings (see
    test_viz_integration.py / milestone D) with the markers appended.
    """
    out = []
    for i in range(1, n + 1):
        hi = _HI_BITS[i % len(_HI_BITS)]
        if is_map_slot(i):
            out.append({
                "question": f"Where is Chilika lake? MX{i}S",
                "options": [f"Chilika MX{i}O", "Sambhar", "Wular", "Dal"],
                "correct_option_id": 0,
                "explanation": ("Chilika is a brackish lagoon on the "
                                f"Odisha coast. MX{i}E"),
            })
        elif is_timeline_slot(i):
            out.append({
                "question": ("Arrange the following events in chronological "
                             f"order. MX{i}S"),
                "options": [f"1857 first MX{i}O", "1919 second",
                            "1942 third", "None"],
                "correct_option_id": 0,
                "explanation": ("The revolt of 1857 shook the empire. In 1919 "
                                "came a massacre. The Quit India movement "
                                f"followed in 1942. MX{i}E"),
            })
        elif is_comparison_slot(i):
            out.append({
                "question": f"What are the differences between X and Y? MX{i}S",
                "options": [f"X MX{i}O", "Y", "Both", "Neither"],
                "correct_option_id": 2,
                "explanation": ("Compare X with Y.\n• X is fast and small.\n"
                                f"• Y is slow and large.\n• Both are useful. MX{i}E"),
            })
        else:
            if i % 3 == 0:
                stem = (f"MX{i}S: What is {i} x {i + 1}? Choose one. "
                        f"Dash–“quote” ₹{i} ✅ x²")
            elif i % 3 == 1:
                stem = (f"MX{i}S: यह {hi} प्रश्न संख्या {i} है। सही उत्तर चुनें। "
                        f"मूल्य–“उद्धरण” ₹{i} ✅")
            else:
                stem = (f"MX{i}S: What is {i} x {i + 1}? यह {hi} प्रश्न है। "
                        f"Dash–“quote” ₹{i} ✅ x²")
            if i % 17 == 0:
                stem += " Long-stem padding. " * 60
            opts = []
            for k, lab in enumerate("ABCD"):
                opt = f"MX{i}O{lab} {hi} {i * (k + 1)}"
                if i % 19 == 0:
                    opt += " padded-option" * 40
                opts.append(opt)
            if i % 11 == 0:
                expl = ""
            else:
                expl = f"MX{i}E because {i} reasons. व्याख्या {hi}।"
                if i % 23 == 0:
                    expl += " Long-explanation padding. " * 120
            out.append({
                "question": stem,
                "options": opts,
                "correct_option_id": ([0, 2] if i % 7 == 0 else i % 4),
                "explanation": expl,
            })
    return out


def _render_pdf(questions: list[dict], tmpdir: str, name: str, **kwargs) -> tuple[Path, fitz.Document, dict]:
    path = Path(tmpdir) / name
    info = render_testseries_pdf(questions, output_path=path, **kwargs)
    return path, fitz.open(path), info


def _assert_geometry_sane(case: unittest.TestCase, doc: fitz.Document,
                          *, check_drawings: bool = True) -> None:
    """Every text block / drawing inside the page; no overprinted text."""
    for pno, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
        for b in blocks:
            x0, y0, x1, y1 = b["bbox"]
            case.assertGreaterEqual(x0, -0.5, f"page {pno} text OOB: {b['bbox']}")
            case.assertGreaterEqual(y0, -0.5, f"page {pno} text OOB: {b['bbox']}")
            case.assertLessEqual(x1, w + 0.5, f"page {pno} text OOB: {b['bbox']}")
            case.assertLessEqual(y1, h + 0.5, f"page {pno} text OOB: {b['bbox']}")
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
            frac = area / max(1e-6, min(aa, bb))
            # Adjacent lines' glyph-padding slivers reach ~0.22; a real
            # overprint covers most of the smaller block (~1.0).
            case.assertLessEqual(
                frac, 0.35, f"page {pno} overprinted text blocks: {frac:.2f}")
        if check_drawings:
            for d in page.get_drawings():
                x0, y0, x1, y1 = d["rect"]
                case.assertGreaterEqual(x0, -1.0, f"page {pno} drawing OOB")
                case.assertGreaterEqual(y0, -1.0, f"page {pno} drawing OOB")
                case.assertLessEqual(x1, w + 1.0, f"page {pno} drawing OOB")
                case.assertLessEqual(y1, h + 1.0, f"page {pno} drawing OOB")


def _assert_mapping(case: unittest.TestCase, doc: fitz.Document,
                    questions: list[dict]) -> None:
    """Per-question stem/key/solution mapping, order, visuals, unicode."""
    n = len(questions)
    pages = [p.get_text() for p in doc]
    text = "\n".join(pages)

    case.assertNotIn("\ufffd", text, "replacement chars in extracted text")

    # -- stems: exactly once, except visual stems twice (stem + echo in
    #    the map caption / diagram title), and in strict order ------------
    stem_pos = []
    for i in range(1, n + 1):
        marker = f"MX{i}S"
        # Careful: MX1S is a substring of MX12S/MX100S...; count exact
        # tokens by scanning for the marker followed by a non-digit.
        pat = re.compile(r"MX%dS(?!\d)" % i)
        hits = pat.findall(text)
        if is_visual_slot(i):
            case.assertEqual(len(hits), 2, f"visual stem {marker} count")
        else:
            case.assertEqual(len(hits), 1, f"stem {marker} count")
        stem_pos.append(text.find(marker))
    case.assertEqual(stem_pos, sorted(stem_pos), "stems out of order")

    # -- options ---------------------------------------------------------
    for i in range(1, n + 1):
        if is_visual_slot(i):
            case.assertEqual(text.count(f"MX{i}O"), 1, f"visual opt marker {i}")
        else:
            for lab in "ABCD":
                case.assertEqual(text.count(f"MX{i}O{lab}"), 1,
                                 f"option marker {i}{lab}")

    # -- key grid: exact letters, in order --------------------------------
    key_pos = []
    for i, q in enumerate(questions, 1):
        letters = answer_letters(q["correct_option_id"], len(q["options"]))
        row = f"Q{i} – {letters}"
        case.assertEqual(text.count(row), 1, f"key row {row!r}")
        key_pos.append(text.find(row))
    case.assertEqual(key_pos, sorted(key_pos), "key rows out of order")

    # -- solutions: headings + explanations in order ----------------------
    sol_pos = []
    for i, q in enumerate(questions, 1):
        letters = answer_letters(q["correct_option_id"], len(q["options"]))
        head = f"Q{i}.  Answer: {letters}"
        case.assertEqual(text.count(head), 1, f"solution head {head!r}")
        sol_pos.append(text.find(head))
        if q["explanation"]:
            if is_timeline_slot(i) or is_comparison_slot(i):
                # Diagrams render extracted explanation content, so the
                # marker may also appear inside the visual (1 or 2 total).
                case.assertIn(text.count(f"MX{i}E"), (1, 2),
                              f"expl marker {i}")
            else:
                case.assertEqual(text.count(f"MX{i}E"), 1, f"expl marker {i}")
        else:
            case.assertIn("No explanation provided.", text)
    case.assertEqual(sol_pos, sorted(sol_pos), "solutions out of order")

    # -- visuals: exact badge counts, nothing unexpected ------------------
    n_maps = sum(1 for i in range(1, n + 1) if is_map_slot(i))
    n_timelines = sum(1 for i in range(1, n + 1) if is_timeline_slot(i))
    n_cmps = sum(1 for i in range(1, n + 1) if is_comparison_slot(i))
    case.assertEqual(text.count("Not to scale"), n_maps, "map count")
    case.assertEqual(text.count("Timeline"), n_timelines, "timeline count")
    case.assertEqual(text.count("Comparison"), n_cmps, "comparison count")
    for badge in _OTHER_BADGES:
        case.assertNotIn(badge, text, f"unexpected visual {badge!r}")

    # -- visual correspondence --------------------------------------------
    for i in range(1, n + 1):
        if is_map_slot(i):
            hit = any("Not to scale" in pg and f"MX{i}S" in pg for pg in pages)
            case.assertTrue(hit, f"map {i} not co-located with its stem")
        elif is_timeline_slot(i) or is_comparison_slot(i):
            badge = "Timeline" if is_timeline_slot(i) else "Comparison"
            q = questions[i - 1]
            letters = answer_letters(q["correct_option_id"], len(q["options"]))
            head = f"Q{i}.  Answer: {letters}"
            head_pg = next(p for p, pg in enumerate(pages) if head in pg)
            badge_pgs = [p for p, pg in enumerate(pages) if badge in pg]
            case.assertTrue(
                any(0 <= bp - head_pg <= 1 for bp in badge_pgs),
                f"{badge} {i} not on its solution page")

    # -- unicode survivors -------------------------------------------------
    case.assertIn("–", text)
    case.assertIn("₹", text)
    case.assertIn("x²", text)
    plain = sum(1 for i in range(1, n + 1) if not is_visual_slot(i))
    case.assertEqual(text.count("[Correct]"), plain, "[Correct] count")

    # -- bounded pages, no blank pages ------------------------------------
    case.assertLess(len(doc), max(30, n), "page-count explosion")
    for pno, page in enumerate(doc):
        has_text = bool(pages[pno].strip())
        has_draw = bool(page.get_drawings())
        case.assertTrue(has_text or has_draw, f"blank page {pno}")


class MatrixCases(unittest.TestCase):
    """Full mapping/geometry validation per matrix size (direct render)."""

    def test_matrix_all_sizes(self) -> None:
        for n in SIZES:
            with self.subTest(size=n):
                with tempfile.TemporaryDirectory(prefix="tsm-") as tmp:
                    qs = make_matrix_questions(n)
                    _, doc, info = _render_pdf(
                        qs, tmp, f"m{n}.pdf", exam_title=f"Matrix {n}",
                        tagline="Test Series", quiz_names=["QZ"])
                    try:
                        self.assertEqual(info["questions"], n)
                        _assert_mapping(self, doc, qs)
                        _assert_geometry_sane(self, doc)
                    finally:
                        doc.close()


class BoundsCases(unittest.TestCase):
    """300-question wall-time and Python-memory bounds."""

    def test_300_time_and_memory(self) -> None:
        qs = make_matrix_questions(300)
        with tempfile.TemporaryDirectory(prefix="tsb-") as tmp:
            tracemalloc.start()
            t0 = time.time()
            try:
                _, doc, info = _render_pdf(
                    qs, tmp, "b300.pdf", exam_title="Bounds 300",
                    tagline="Test Series", quiz_names=["QZ"])
                try:
                    dt = time.time() - t0
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    doc.close()
            finally:
                tracemalloc.stop()
        # Measured baseline: ~56 s, ~6 MB peak. Caps carry large headroom
        # for slower CI boxes while still catching runaway behavior.
        self.assertLess(dt, 240, f"300-Q render took {dt:.0f}s")
        self.assertLess(peak, 60_000_000, f"300-Q peak {peak / 1e6:.0f} MB")
        self.assertEqual(info["questions"], 300)


class _LiveServer(unittest.TestCase):
    base_url: str = ""
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None
    _tmpdir: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="pdfsvc-matrix-")
        appmod.JOBS_DIR = Path(cls._tmpdir.name)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(appmod.app, host="127.0.0.1", port=port,
                                log_level="error", access_log=False)
        cls._server = uvicorn.Server(config)
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        cls.base_url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base_url + "/healthz",
                                            timeout=5) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("Test PDF server did not start.")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._server is not None:
            cls._server.should_exit = True
        if cls._thread is not None:
            cls._thread.join(timeout=15)
        if cls._tmpdir is not None:
            cls._tmpdir.cleanup()
        super().tearDownClass()


class LiveMatrixCases(_LiveServer):
    """Live-API render at size 25 (wire path for a new matrix size)."""

    def test_live_25_end(self) -> None:
        qs = make_matrix_questions(25)
        req = urllib.request.Request(
            self.base_url + "/api/generate",
            data=json.dumps({
                "questions_json": qs,
                "institute_name": "Matrix Live",
                "tagline": "Test Series",
                "exam_title": "Matrix Live 25",
                "solution_display": "end",
                "quiz_names": ["QZ"],
                "async": True,
            }).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            job = json.loads(r.read().decode())
        deadline = time.time() + 300
        while time.time() < deadline:
            with urllib.request.urlopen(self.base_url + job["progress_url"],
                                        timeout=20) as r:
                st = json.loads(r.read().decode())
            self.assertNotEqual(st.get("status"), "error", st.get("error"))
            if st.get("status") == "done":
                break
            time.sleep(1.0)
        else:
            self.fail("live 25-Q job never reached done")
        with urllib.request.urlopen(self.base_url + job["download_url"],
                                    timeout=60) as r:
            pdf = r.read()
        self.assertTrue(pdf[:4] == b"%PDF", pdf[:20])
        with tempfile.TemporaryDirectory(prefix="tsl-") as tmp:
            path = Path(tmp) / "live25.pdf"
            path.write_bytes(pdf)
            doc = fitz.open(path)
            try:
                _assert_mapping(self, doc, qs)
                _assert_geometry_sane(self, doc)
            finally:
                doc.close()


class ConcatCases(unittest.TestCase):
    """Multi-quiz concatenation order: bot payload builder -> render."""

    def test_concat_order_preserved(self) -> None:
        from quizbot.creator_bot.handlers.reports import (
            _build_testseries_payload)
        quizzes = [
            {"quiz_name": f"QZ{k}",
             "questions": make_matrix_questions(10)[(k - 1) * 3:k * 3]}
            for k in (1, 2, 3)
        ]
        # Renumber markers per quiz so order is checkable after concat.
        flat_expect: list[str] = []
        for qi, quiz in enumerate(quizzes, 1):
            for q in quiz["questions"]:
                tag = f"C{qi}Q{q['question'].split('MX')[1].split('S')[0]}"
                q["question"] = q["question"].replace("MX", tag + "MX")
                flat_expect.append(tag)
        payload = _build_testseries_payload(quizzes, "end", "Concat")
        with tempfile.TemporaryDirectory(prefix="tsc-") as tmp:
            _, doc, info = _render_pdf(
                payload["questions_json"], tmp, "concat.pdf",
                exam_title="Concat", tagline=payload["tagline"],
                quiz_names=payload["quiz_names"])
            try:
                text = "\n".join(p.get_text() for p in doc)
                pos = [text.find(t) for t in flat_expect]
                self.assertTrue(all(p >= 0 for p in pos), "missing questions")
                self.assertEqual(pos, sorted(pos), "concat order drifted")
                self.assertEqual(info["questions"], 9)
            finally:
                doc.close()


class StudioE2ECases(unittest.TestCase):
    """Wizard config -> build_series_setup -> PDF: every value propagates."""

    def test_full_config_propagates_to_pdf(self) -> None:
        from quizbot.creator_bot.handlers.testseries_create import (
            TestSeriesConfig, _render_preview, build_series_setup)
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="tse-") as tmp:
            logo_path = Path(tmp) / "logo.png"
            Image.new("RGB", (120, 60), (180, 30, 30)).save(logo_path)
            wm_path = Path(tmp) / "wm.png"
            Image.new("RGBA", (200, 100), (30, 60, 180, 255)).save(wm_path)
            logo_bytes = logo_path.read_bytes()
            wm_bytes = wm_path.read_bytes()
            assets = {"logo": logo_bytes, "wm_image": wm_bytes}
            cfg = TestSeriesConfig(
                title="Studio E2E Mock", subject="Geography",
                test_number_mode="manual", test_number="3",
                booklet_series="Z",
                booklet_number_mode="manual", booklet_number="12",
                paper="Paper I", duration="2 hours", test_code="E2E1",
                institute_name="E2E Institute",
                logo_present=True, logo_bytes=len(logo_bytes),
                watermark_mode="both", watermark_text="E2E-WM",
                wm_image_present=True, wm_image_bytes=len(wm_bytes),
                tagline="E2E tagline", answer_key=True, solutions=True,
                visuals="auto", marks_correct=4.0, marks_negative=-1.0,
                cand_name=True, cand_roll=True,
                total_questions=25, max_marks=100.0)
            self.assertEqual(cfg.validate(), [])
            setup = build_series_setup(cfg, assets)
            preview = _render_preview(cfg)
            qs = make_matrix_questions(25)
            _, doc, _ = _render_pdf(
                qs, tmp, "e2e.pdf", exam_title=cfg.title,
                tagline=cfg.tagline, quiz_names=["QZ"],
                series_setup=setup)
            try:
                text = "\n".join(p.get_text() for p in doc)
                pages = [p.get_text() for p in doc]
                for value in ["Studio E2E Mock", "Geography", "Paper I", "3",
                              "Z-12", "E2E1", "2 hours", "E2E Institute",
                              "E2E tagline", "Candidate Name", "Roll Number",
                              "100", "+4", "-1"]:
                    self.assertIn(value, text, f"PDF missing {value!r}")
                    self.assertIn(value, preview, f"preview missing {value!r}")
                # Watermark text stamped on every page; images embedded.
                for pno, pg in enumerate(pages):
                    self.assertIn("E2E-WM", pg, f"wm missing page {pno}")
                self.assertTrue(doc[0].get_images(), "logo not embedded")
                self.assertTrue(any(p.get_images() for p in doc[1:]),
                                "watermark image not embedded")
                _assert_mapping(self, doc, qs)
            finally:
                doc.close()


if __name__ == "__main__":
    unittest.main()
