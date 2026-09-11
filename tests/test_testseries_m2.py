"""Phase 4 milestone 2: wizard configuration applied to the actual PDF output.

Proves the /newseries TestSeriesConfig changes the real rendered PDF:

  * bot mapping (config -> series_setup payload) incl. base64 images
  * legacy payload/rendering untouched when no setup is sent
  * identification grid, candidate boxes, branding, logo
  * watermark none/text/image/both on every page
  * answer-key / solutions toggles, visuals auto/yes/no
  * marks math incl. custom marks and arbitrary 10..300 counts
  * end-to-end API renders (10/300Q) with fitz inspection

Conventions follow tests/test_pdf_service.py (live uvicorn + PyMuPDF) and
tests/test_viz_integration.py (direct render_testseries_pdf into tmpdir).
No generated PDFs are committed; image fixtures are built in-test with PIL.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch

warnings.simplefilter("ignore")
import fitz  # noqa: E402  (PyMuPDF; already a project dependency)
import uvicorn  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402  (Pillow; project dependency)

import pdf_service.app as appmod  # noqa: E402
from pdf_service.render import render_testseries_pdf  # noqa: E402
from quizbot.creator_bot import state as creator_state  # noqa: E402
from quizbot.creator_bot.handlers import testseries_create as tsc  # noqa: E402

POLL_DEADLINE = 300

ALL_CANDIDATES = ["Candidate Name", "Roll Number",
                  "Registration / Candidate ID", "Batch", "Date",
                  "Candidate Signature", "Evaluator / Invigilator Signature"]


# ─── fixtures ─────────────────────────────────────────────────────────
def _png_fixture(w: int = 240, h: int = 120, fmt: str = "PNG") -> bytes:
    """Deterministic raster fixture (default aspect 2.0)."""
    img = Image.new("RGB", (w, h), (178, 34, 34))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, w - 8, h - 8], outline=(255, 255, 255), width=4)
    draw.text((16, h // 2 - 8), "M2FIX", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _plain_questions(n: int) -> list[dict]:
    """Viz-neutral arithmetic questions with unique markers."""
    out = []
    for i in range(1, n + 1):
        out.append({
            "question": f"M2-Q{i}-STEM: What is {i} times {i + 1}?",
            "options": [f"M2-Q{i}-OPT-{lab} value {i * (k + 1)}"
                        for k, lab in enumerate("ABCD")],
            "correct_option_id": i % 4,
            "explanation": f"M2-Q{i}-EXPL: it equals {i * (i + 1)}.",
        })
    return out


def _hindi_questions() -> list[dict]:
    base = _plain_questions(10)
    base[0]["question"] += " भारत की राजधानी क्या है?"
    base[1]["options"][0] += " उत्तर प्रदेश"
    base[2]["explanation"] += " व्याख्या श्रृंखला ज्ञानी।"
    return base


MAP_Q = {"question": "Where is Chilika lake?",
         "options": ["Chilika", "Sambhar", "Wular", "Dal"],
         "correct_option_id": 0,
         "explanation": "Chilika is a brackish lagoon on the Odisha coast."}

PLAIN_Q = {"question": "What is 7 x 8?",
           "options": ["54", "56", "58", "60"],
           "correct_option_id": 1,
           "explanation": "It equals 56."}


def _setup(**over) -> dict:
    base = {
        "subject": "Geography", "test_number": "3",
        "booklet_series": "B", "booklet_number": "12",
        "paper": "Paper I", "test_code": "M2-CODE-77", "duration": "2 hours",
        "marks_correct": 2.0, "marks_negative": -0.66,
        "candidate_fields": ["Candidate Name", "Roll Number", "Date"],
        "institute_name": "M2 Institute of Testing",
        "tagline": "M2 Tagline Marker",
        "watermark_mode": "none", "watermark_text": "",
        "answer_key": True, "solutions": True, "visuals": "auto",
    }
    base.update(over)
    return base


def _assert_no_overlap(case: unittest.TestCase, page,
                       ignore: tuple = ()) -> None:
    words = [w for w in page.get_text("words") if w[4] not in ignore]
    for i in range(len(words)):
        ax0, ay0, ax1, ay1 = words[i][:4]
        area_a = max(0.0, (ax1 - ax0) * (ay1 - ay0))
        if area_a <= 0:
            continue
        for j in range(i + 1, len(words)):
            bx0, by0, bx1, by1 = words[j][:4]
            ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
            iy = max(0.0, min(ay1, by1) - max(ay0, by0))
            area_b = max(0.0, (bx1 - bx0) * (by1 - by0))
            if area_b <= 0:
                continue
            if ix * iy / min(area_a, area_b) > 0.7:
                case.fail(f"overlapping words on page {page.number}: "
                          f"{words[i][4]!r} vs {words[j][4]!r}")


class _DirectBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="m2-")
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def _render(self, questions, setup=None, display="end",
                title="M2 Title Marker"):
        self._n += 1
        out = str(Path(self._tmp.name) / ("m2-%d.pdf" % self._n))
        info = render_testseries_pdf(
            questions, exam_title=title,
            tagline=(setup or {}).get("tagline") or "Test Series",
            quiz_names=["M2QZ"], solution_display=display,
            series_setup=setup, output_path=out)
        self.assertTrue(Path(out).read_bytes()[:4] == b"%PDF")
        return out, info

    @staticmethod
    def _full_text(doc) -> str:
        return "\n".join(p.get_text() for p in doc)

    def _page_images(self, page):
        return page.get_images(full=True)


# ─── A. bot mapping: config -> series_setup payload ───────────────────
class SetupMappingCases(unittest.TestCase):
    def test_build_series_setup_full_mapping(self):
        cfg = tsc.TestSeriesConfig(
            title="T", subject="Polity", test_number_mode="manual",
            test_number="7", booklet_series="C", booklet_number_mode="manual",
            booklet_number="9", cand_name=True, cand_roll=True,
            cand_evalsig=True, institute_name="Academy", tagline="Tag!",
            watermark_mode="both", watermark_text="WM",
            answer_key=False, solutions=True, visuals="no",
            marks_correct=4.0, marks_negative=-0.25, paper="Paper II",
            duration="3 hours", test_code="CD-1")
        assets = {"logo": b"\x89PNG-logo", "wm_image": b"\xff\xd8-wm"}
        setup = tsc.build_series_setup(cfg, assets)
        self.assertEqual(setup["subject"], "Polity")
        self.assertEqual(setup["test_number"], "7")
        self.assertEqual(setup["booklet_series"], "C")
        self.assertEqual(setup["booklet_number"], "9")
        self.assertEqual(setup["paper"], "Paper II")
        self.assertEqual(setup["test_code"], "CD-1")
        self.assertEqual(setup["duration"], "3 hours")
        self.assertEqual(setup["marks_correct"], 4.0)
        self.assertEqual(setup["marks_negative"], -0.25)
        self.assertEqual(setup["candidate_fields"],
                         ["Candidate Name", "Roll Number",
                          "Evaluator / Invigilator Signature"])
        self.assertEqual(setup["institute_name"], "Academy")
        self.assertEqual(setup["watermark_mode"], "both")
        self.assertEqual(setup["watermark_text"], "WM")
        self.assertFalse(setup["answer_key"])
        self.assertTrue(setup["solutions"])
        self.assertEqual(setup["visuals"], "no")
        self.assertEqual(base64.b64decode(setup["logo_b64"]), b"\x89PNG-logo")
        self.assertEqual(base64.b64decode(setup["wm_image_b64"]), b"\xff\xd8-wm")
        json.dumps(setup)  # must stay JSON-safe

    def test_build_series_setup_auto_numbers_omitted(self):
        cfg = tsc.TestSeriesConfig(
            title="T", institute_name="I", test_number_mode="auto",
            test_number="", booklet_number_mode="auto", booklet_number="")
        setup = tsc.build_series_setup(cfg, {})
        self.assertEqual(setup["test_number"], "")
        self.assertEqual(setup["booklet_number"], "")
        self.assertIsNone(setup["logo_b64"])
        self.assertIsNone(setup["wm_image_b64"])
        self.assertEqual(setup["candidate_fields"], [])

    def test_payload_builder_includes_setup_verbatim(self):
        from quizbot.creator_bot.handlers.reports import \
            _build_testseries_payload
        quizzes = [{"quiz_name": "q", "questions": [
            {"question": "qq", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": ""}]}]
        setup = _setup()
        payload = _build_testseries_payload(quizzes, "keyonly", "T",
                                            series_setup=setup)
        self.assertIs(payload["series_setup"], setup)
        self.assertEqual(payload["solution_display"], "end")
        self.assertEqual(len(payload["questions_json"]), 1)

    def test_payload_builder_legacy_shape_unchanged(self):
        from quizbot.creator_bot.handlers.reports import \
            _build_testseries_payload
        quizzes = [{"quiz_name": "q", "questions": [
            {"question": "qq", "options": ["a", "b"],
             "correct_option_id": 1, "explanation": ""}]}]
        plain = _build_testseries_payload(quizzes, "keyonly", "T")
        self.assertNotIn("series_setup", plain)
        self.assertEqual(
            set(plain),
            {"questions_json", "institute_name", "tagline", "exam_title",
             "solution_display", "quiz_names", "async"})

    def test_max_marks_math_for_every_count_10_to_300(self):
        """No enum: every integer count 10..300 derives totals arithmetically."""
        for n in range(10, 301):
            quizzes = [{"questions": [
                {"options": ["a", "b"]} for _ in range(n)]}]
            for mc in (2.0, 1.0, 4.0, 2.5):
                total, maximum = tsc._derive_totals(quizzes, mc)
                self.assertEqual(total, n, f"count {n}")
                self.assertEqual(maximum, round(n * mc, 2),
                                 f"count {n} marks {mc}")

    def test_totals_skip_optionless_questions(self):
        quizzes = [{"questions": [{"options": ["a", "b"]},
                                  {"options": []},
                                  {"options": ["", None]}]}]
        total, maximum = tsc._derive_totals(quizzes, 2.0)
        self.assertEqual((total, maximum), (1, 2.0))

    def test_do_generate_sends_setup_to_api(self):
        uid = 910001
        self.addCleanup(creator_state.testseries_create.pop, uid, None)
        png = _png_fixture()
        creator_state.testseries_create[uid] = {
            "step": "preview",
            "config": {"title": "Final", "institute_name": "Acad",
                       "subject": "Polity", "test_number_mode": "manual",
                       "test_number": "5", "marks_correct": 1.0,
                       "marks_negative": -0.33, "cand_name": True,
                       "watermark_mode": "text", "watermark_text": "WMM",
                       "answer_key": True, "solutions": False,
                       "visuals": "no"},
            "assets": {"logo": png, "wm_image": None},
            "quizzes": [{"quiz_name": "q", "questions": [
                {"question": "qq", "options": ["a", "b"],
                 "correct_option_id": 0, "explanation": ""}]}],
            "source": "qids"}

        class _Status:
            async def edit_text(self, *a, **k):
                pass

            async def delete(self):
                pass

        class _Target:
            def __init__(self):
                self.message = self
                self.docs = []

            async def reply(self, *a, **k):
                return _Status()

            async def reply_document(self, **k):
                self.docs.append(k)

        seen = {}

        async def fake_api(quizzes, mode, title, **kw):
            seen.update(kw)
            return b"%PDF-x"

        target = _Target()
        with patch.object(tsc, "_generate_pdf_via_api", fake_api):
            asyncio.run(tsc._do_generate(None, target, uid))
        setup = seen.get("series_setup")
        self.assertIsNotNone(setup, "generate must send series_setup")
        self.assertEqual(setup["subject"], "Polity")
        self.assertEqual(setup["test_number"], "5")
        self.assertEqual(base64.b64decode(setup["logo_b64"]), png)
        self.assertIsNone(setup["wm_image_b64"])
        self.assertEqual(setup["watermark_text"], "WMM")
        self.assertFalse(setup["solutions"])
        self.assertEqual(setup["marks_correct"], 1.0)
        self.assertEqual(len(target.docs), 1)
        self.assertNotIn(uid, creator_state.testseries_create)


# ─── B. HTTP pass-through (mocked service) ────────────────────────────
class _MockHttpBase(unittest.TestCase):
    def _run_generate(self, **kw):
        import quizbot.creator_bot.handlers.reports as rep
        import quizbot.shared.config as config
        captured = {}

        def fake(*a, **k):
            if a[0] == "GET":
                return (200, {"status": "done"})
            captured.update(k.get("json_body") or {})
            return (200, {"progress_url": "/p", "download_url": "/d"})

        class _Resp:
            status = 200

            async def read(self):
                return b"%PDF-fake"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def get(self, url):
                return _Resp()

        quizzes = [{"quiz_name": "f", "questions": [
            {"question": "q", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": ""}]}]
        with patch.object(config, "PDF_API_BASE", "http://127.0.0.1:9"), \
             patch.object(rep, "request_json", AsyncMock(side_effect=fake)), \
             patch.object(rep, "get_session",
                          AsyncMock(return_value=_Session())):
            out = asyncio.run(rep._generate_pdf_via_api(
                quizzes, "keyonly", "T", **kw))
        self.assertEqual(out, b"%PDF-fake")
        return captured


class ApiPassthroughCases(_MockHttpBase):
    def test_generate_embeds_setup_in_post_body(self):
        setup = _setup()
        captured = self._run_generate(series_setup=setup)
        self.assertEqual(captured["series_setup"], setup)

    def test_generate_legacy_body_has_no_setup(self):
        captured = self._run_generate()
        self.assertNotIn("series_setup", captured)

    def test_generate_model_accepts_setup(self):
        model = appmod.GenerateIn(
            questions_json=[{"question": "q", "options": ["a", "b"],
                             "correct_option_id": 0, "explanation": ""}],
            series_setup=_setup())
        self.assertEqual(model.series_setup["test_number"], "3")
        legacy = appmod.GenerateIn(
            questions_json=[{"question": "q", "options": ["a", "b"],
                             "correct_option_id": 0, "explanation": ""}])
        self.assertEqual(legacy.series_setup, {})


# ─── C. legacy parity (no setup -> historical rendering) ──────────────
class LegacyParityCases(_DirectBase):
    def _legacy_text(self, setup):
        path, info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            return self._full_text(doc), info

    def test_no_setup_is_legacy(self):
        full, info = self._legacy_text(None)
        self.assertIn("unless your instructor says otherwise", full)
        self.assertNotIn("Candidate Details", full)
        self.assertNotIn("Correct Marks", full)
        self.assertIn("Answer Key", full)
        self.assertIn("Detailed Solutions", full)
        self.assertIn("M2-Q4-STEM", full)
        self.assertEqual(info["questions"], 4)

    def test_empty_setup_is_legacy(self):
        full, _info = self._legacy_text({})
        self.assertIn("unless your instructor says otherwise", full)
        self.assertNotIn("Candidate Details", full)


# ─── D. identification block ──────────────────────────────────────────
class IdentificationCases(_DirectBase):
    def test_full_identification_grid(self):
        path, _info = self._render(_plain_questions(10), _setup())
        with fitz.open(path) as doc:
            pages = [p.get_text() for p in doc]
            full = "\n".join(pages)
            self.assertIn("Geography", full)
            self.assertIn("Paper I", full)
            self.assertIn("Test No", full)
            self.assertIn("B-12", full)
            self.assertIn("M2-CODE-77", full)
            self.assertIn("2 hours", full)
            self.assertIn("Total Questions", full)
            self.assertIn("Maximum Marks", full)
            self.assertIn("20", full)  # 10 x +2
            self.assertIn("+2", full)
            self.assertIn("-0.66", full)
            self.assertIn("Each question carries +2 marks.", full)
            self.assertIn("Negative marking: -0.66 per wrong answer.", full)
            self.assertIn("M2 Institute of Testing", full)
            self.assertIn("M2 Tagline Marker", full)
            self.assertIn("Journey for लबासना", full)
            self.assertIn("M2 Title Marker", full)
            self.assertNotIn("�", full)
            for idx, text in enumerate(pages):
                self.assertTrue(text.strip(), f"page {idx} blank")
            for page in list(doc)[:3]:
                _assert_no_overlap(self, page)

    def test_auto_numbers_omitted(self):
        setup = _setup(test_number="", booklet_series="",
                       booklet_number="", paper="", test_code="",
                       duration="", subject="")
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Test No", full)
            self.assertNotIn("Booklet", full)
            self.assertIn("Total Questions", full)
            self.assertIn("Maximum Marks", full)

    def test_booklet_series_without_number(self):
        setup = _setup(booklet_series="C", booklet_number="")
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Booklet", full)
            self.assertIn("C", full)
            self.assertNotIn("C-", full)

    def test_custom_marks_math(self):
        setup = _setup(marks_correct=4.0, marks_negative=-0.25)
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("+4", full)
            self.assertIn("-0.25", full)
            self.assertIn("40", full)  # 10 x +4
            self.assertIn("Each question carries +4 marks.", full)

    def test_zero_negative_marks_line(self):
        setup = _setup(marks_negative=0.0)
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("There is no negative marking.", full)
            self.assertNotIn("Negative marking:", full)


# ─── E. candidate fields ──────────────────────────────────────────────
class CandidateCases(_DirectBase):
    def test_all_seven_fields_rendered(self):
        setup = _setup(candidate_fields=list(ALL_CANDIDATES))
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Candidate Details", full)
            for label in ALL_CANDIDATES:
                self.assertIn(label, full, label)
            for page in doc:
                _assert_no_overlap(self, page)

    def test_no_fields_no_section(self):
        setup = _setup(candidate_fields=[])
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Candidate Details", full)
            self.assertNotIn("Candidate Name", full)

    def test_subset_only_enabled_shown(self):
        setup = _setup(candidate_fields=["Roll Number", "Date"])
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Candidate Details", full)
            self.assertIn("Roll Number", full)
            self.assertIn("Date", full)
            self.assertNotIn("Candidate Name", full)
            self.assertNotIn("Batch", full)
            self.assertNotIn("Candidate Signature", full)


# ─── F. branding + logo ───────────────────────────────────────────────
class BrandingCases(_DirectBase):
    def test_logo_institute_tagline(self):
        setup = _setup(logo_b64=_b64(_png_fixture()))
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            cover = doc[0]
            self.assertGreaterEqual(len(self._page_images(cover)), 1,
                                    "cover must embed the logo")
            for page in list(doc)[1:]:
                self.assertEqual(len(self._page_images(page)), 0,
                                 f"page {page.number} must be image-free")
            full = self._full_text(doc)
            self.assertIn("M2 Institute of Testing", full)
            self.assertIn("M2 Tagline Marker", full)

    def test_logo_absent_no_images(self):
        path, _info = self._render(_plain_questions(10), _setup())
        with fitz.open(path) as doc:
            for page in doc:
                self.assertEqual(len(self._page_images(page)), 0,
                                 f"page {page.number} must be image-free")

    def test_logo_jpeg_format(self):
        setup = _setup(logo_b64=_b64(_png_fixture(fmt="JPEG")))
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            self.assertGreaterEqual(len(self._page_images(doc[0])), 1)
            self.assertIn("M2-Q4-STEM", self._full_text(doc))

    def test_corrupt_logo_skipped_pdf_ok(self):
        setup = _setup(logo_b64=_b64(b"not-an-image-at-all"))
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            for page in doc:
                self.assertEqual(len(self._page_images(page)), 0)
            self.assertIn("M2-Q4-STEM", self._full_text(doc))

    def test_logo_aspect_preserved(self):
        wide = _png_fixture(w=300, h=100)  # aspect 3.0
        setup = _setup(logo_b64=_b64(wide))
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            images = self._page_images(doc[0])
            self.assertEqual(len(images), 1)
            rect = doc[0].get_image_bbox(images[0])
            self.assertAlmostEqual(rect.width / rect.height, 3.0, delta=0.05)


# ─── G. watermark matrix ──────────────────────────────────────────────
class WatermarkCases(_DirectBase):
    WM_TEXT = "M2WMSAMPLE"

    def test_none_has_no_watermark(self):
        setup = _setup(watermark_mode="none", watermark_text=self.WM_TEXT,
                       wm_image_b64=_b64(_png_fixture()))
        path, _info = self._render(_plain_questions(6), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn(self.WM_TEXT, full)
            for page in doc:
                self.assertEqual(len(self._page_images(page)), 0)

    def test_text_on_every_page(self):
        setup = _setup(watermark_mode="text", watermark_text=self.WM_TEXT)
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            self.assertGreaterEqual(doc.page_count, 3)
            for page in doc:
                self.assertIn(self.WM_TEXT, page.get_text(),
                              f"page {page.number} missing watermark")
                self.assertEqual(len(self._page_images(page)), 0)
            for page in list(doc)[:3]:
                _assert_no_overlap(self, page, ignore=(self.WM_TEXT,))

    def test_watermark_keeps_header_and_cover_placement(self):
        """Watermark drawing is cursor-neutral: cover starts at the top
        margin and inner-page headers stay left-aligned."""
        for mode, extra in (("text", {"watermark_text": self.WM_TEXT}),
                            ("image", {"wm_image_b64": _b64(_png_fixture())}),
                            ("both", {"watermark_text": self.WM_TEXT,
                                      "wm_image_b64": _b64(_png_fixture())})):
            with self.subTest(mode=mode):
                setup = _setup(watermark_mode=mode, **extra)
                path, _info = self._render(_plain_questions(10), setup)
                with fitz.open(path) as doc:
                    self.assertGreaterEqual(doc.page_count, 2)
                    inner = [w for w in doc[1].get_text("words")
                             if w[4] == "Journey"]
                    self.assertTrue(inner, "inner header missing")
                    # Left margin is 15mm ~= 42.5pt.
                    self.assertAlmostEqual(inner[0][0], 42.5, delta=6.0)
                    cover_top = min(
                        w[1] for w in doc[0].get_text("words") if w[4])
                    self.assertLess(cover_top, 120.0,
                                    "cover must start near the top")

    def test_image_on_every_page_aspect_kept(self):
        setup = _setup(watermark_mode="image",
                       wm_image_b64=_b64(_png_fixture()))
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            self.assertGreaterEqual(doc.page_count, 3)
            for page in doc:
                images = self._page_images(page)
                self.assertEqual(len(images), 1,
                                 f"page {page.number} must carry the mark")
                rect = page.get_image_bbox(images[0])
                self.assertAlmostEqual(rect.width / rect.height, 2.0,
                                       delta=0.05)
                self.assertGreater(rect.width, 100, "mark too small")

    def test_both_text_and_image_no_overlap(self):
        setup = _setup(watermark_mode="both", watermark_text=self.WM_TEXT,
                       wm_image_b64=_b64(_png_fixture()))
        path, _info = self._render(_plain_questions(10), setup)
        with fitz.open(path) as doc:
            for page in doc:
                self.assertIn(self.WM_TEXT, page.get_text())
                self.assertEqual(len(self._page_images(page)), 1)
                words = [w for w in page.get_text("words")
                         if w[4] == self.WM_TEXT]
                self.assertTrue(words, "watermark word not found")
                rect = page.get_image_bbox(self._page_images(page)[0])
                for x0, y0, x1, y1, *_ in words:
                    inter = max(0.0, min(x1, rect.x1) - max(x0, rect.x0)) * \
                        max(0.0, min(y1, rect.y1) - max(y0, rect.y0))
                    self.assertEqual(inter, 0.0,
                                     "text and image marks must not overlap")
            self.assertIn("M2-Q10-STEM", self._full_text(doc))

    def test_bad_watermark_image_skipped(self):
        setup = _setup(watermark_mode="image",
                       wm_image_b64=_b64(b"junk-bytes-here"))
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            for page in doc:
                self.assertEqual(len(self._page_images(page)), 0)
            self.assertIn("M2-Q4-STEM", self._full_text(doc))

    def test_unknown_mode_treated_as_none(self):
        setup = _setup(watermark_mode="bogus", watermark_text=self.WM_TEXT)
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            self.assertNotIn(self.WM_TEXT, self._full_text(doc))

    def test_image_mode_without_text(self):
        setup = _setup(watermark_mode="image", watermark_text=self.WM_TEXT,
                       wm_image_b64=_b64(_png_fixture()))
        path, _info = self._render(_plain_questions(4), setup)
        with fitz.open(path) as doc:
            self.assertNotIn(self.WM_TEXT, self._full_text(doc))
            for page in doc:
                self.assertEqual(len(self._page_images(page)), 1)


# ─── H. answer key / solutions / visuals ──────────────────────────────
class KeySolutionsVisualsCases(_DirectBase):
    def test_key_off_solutions_on(self):
        setup = _setup(answer_key=False, solutions=True)
        path, _info = self._render(_plain_questions(6), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Answer Key", full)
            self.assertIn("Detailed Solutions", full)
            self.assertIn("M2-Q3-EXPL", full)
            self.assertIn("M2-Q6-STEM", full)

    def test_key_on_solutions_off(self):
        setup = _setup(answer_key=True, solutions=False)
        path, _info = self._render(_plain_questions(6), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Answer Key", full)
            self.assertNotIn("Detailed Solutions", full)
            self.assertNotIn("M2-Q3-EXPL", full)
            key_region = full.split("Answer Key", 1)[1]
            self.assertIn("Q1", key_region)
            self.assertIn("B", key_region)  # Q1: 1 % 4 -> B

    def test_both_off_pure_paper(self):
        setup = _setup(answer_key=False, solutions=False)
        path, _info = self._render(_plain_questions(6), setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Answer Key", full)
            self.assertNotIn("Detailed Solutions", full)
            self.assertNotIn("Answer:", full)
            self.assertNotIn("M2-Q2-EXPL", full)
            for i in range(1, 7):
                self.assertIn(f"M2-Q{i}-STEM", full)
                self.assertIn(f"M2-Q{i}-OPT-A", full)

    def test_visuals_no_suppresses_map(self):
        setup = _setup(visuals="no")
        path, _info = self._render([MAP_Q], setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Not to scale", full)
            self.assertNotIn("Simplified outline", full)
            self.assertIn("Chilika is a brackish lagoon", full)

    def test_visuals_yes_allows_map(self):
        setup = _setup(visuals="yes")
        path, _info = self._render([MAP_Q], setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Not to scale", full)
            self.assertIn("Simplified outline", full)
            self.assertIn("Chilika", full)

    def test_visuals_auto_allows_map(self):
        setup = _setup(visuals="auto")
        path, _info = self._render([MAP_Q], setup)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Not to scale", full)

    def test_plain_question_never_visual(self):
        for mode in ("auto", "yes"):
            setup = _setup(visuals=mode)
            path, _info = self._render([dict(PLAIN_Q)], setup)
            with fitz.open(path) as doc:
                full = self._full_text(doc)
                self.assertNotIn("Not to scale", full)
                self.assertIn("It equals 56.", full)

    def test_inline_key_on_solutions_off(self):
        setup = _setup(answer_key=True, solutions=False)
        path, _info = self._render(_plain_questions(3), setup, display="inline")
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertEqual(full.count("Answer:"), 3)
            self.assertNotIn("M2-Q1-EXPL", full)

    def test_inline_key_off_solutions_on(self):
        setup = _setup(answer_key=False, solutions=True)
        path, _info = self._render(_plain_questions(3), setup, display="inline")
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Answer:", full)
            self.assertIn("M2-Q1-EXPL", full)

    def test_inline_both_off_pure_questions(self):
        setup = _setup(answer_key=False, solutions=False)
        path, _info = self._render(_plain_questions(3), setup, display="inline")
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Answer:", full)
            self.assertNotIn("M2-Q1-EXPL", full)
            self.assertIn("M2-Q3-STEM", full)


# ─── I. volume: representative + arbitrary counts ─────────────────────
class VolumeCases(_DirectBase):
    def _spot_check(self, n, setup, max_marks):
        started = time.time()
        questions = _plain_questions(n)
        path, info = self._render(questions, setup)
        elapsed = time.time() - started
        self.assertLess(elapsed, 170, "render too slow for bot poll budget")
        self.assertEqual(info["questions"], n)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("M2-Q1-STEM", full)
            self.assertIn(f"M2-Q{n}-STEM", full)
            self.assertIn(f"M2-Q{n // 2}-OPT-C", full)
            self.assertIn(str(max_marks), full)
            self.assertIn("Maximum Marks", full)
            self.assertIn("Journey for लबासना", full)
            self.assertNotIn("�", full)
            for idx, page in enumerate(doc):
                self.assertTrue(page.get_text().strip(),
                                f"page {idx} blank")
        return path

    def test_55_full_config(self):
        setup = _setup(candidate_fields=list(ALL_CANDIDATES),
                       logo_b64=_b64(_png_fixture()),
                       watermark_mode="text", watermark_text="M2WM55")
        path = self._spot_check(55, setup, 110)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("Candidate Details", full)
            self.assertIn("Answer Key", full)
            self.assertIn("Detailed Solutions", full)
            for page in doc:
                self.assertIn("M2WM55", page.get_text())

    def test_100_image_watermark_key_off(self):
        setup = _setup(watermark_mode="image",
                       wm_image_b64=_b64(_png_fixture()),
                       answer_key=False, solutions=True)
        path = self._spot_check(100, setup, 200)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertNotIn("Answer Key", full)
            self.assertIn("Detailed Solutions", full)
            for page in (doc[0], doc[-1]):
                self.assertEqual(len(self._page_images(page)), 1)

    def test_200_custom_marks(self):
        setup = _setup(marks_correct=4.0, marks_negative=-1.0)
        path = self._spot_check(200, setup, 800)
        with fitz.open(path) as doc:
            full = self._full_text(doc)
            self.assertIn("+4", full)
            self.assertIn("Negative marking: -1 per wrong answer.", full)

    def test_arbitrary_37_and_137_counts(self):
        for n in (37, 137):
            with self.subTest(n=n):
                self._spot_check(n, _setup(), n * 2)


# ─── J. end-to-end API renders ────────────────────────────────────────
class _LiveServer(unittest.TestCase):
    base_url: str = ""
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None
    _tmpdir: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="m2svc-")
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
        raise RuntimeError("M2 test PDF server did not start.")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._server is not None:
            cls._server.should_exit = True
        if cls._thread is not None:
            cls._thread.join(timeout=15)
        if cls._tmpdir is not None:
            cls._tmpdir.cleanup()
        super().tearDownClass()

    def _get(self, path: str, timeout: int = 30):
        with urllib.request.urlopen(self.base_url + path,
                                    timeout=timeout) as r:
            return r.status, r.read()

    def _post(self, path: str, obj: dict, timeout: int = 90):
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())

    def _generate_and_download(self, questions: list[dict], setup: dict,
                               title: str) -> bytes:
        status, job = self._post("/api/generate", {
            "questions_json": questions,
            "institute_name": setup.get("institute_name") or "Quiz Creator",
            "tagline": setup.get("tagline") or "Test Series",
            "exam_title": title,
            "solution_display": "end",
            "quiz_names": ["M2API"],
            "async": True,
            "series_setup": setup,
        })
        self.assertEqual(status, 200, job)
        deadline = time.time() + POLL_DEADLINE
        while time.time() < deadline:
            s, body = self._get(job["progress_url"])
            self.assertEqual(s, 200, body)
            st = json.loads(body.decode())
            if st.get("status") == "done":
                break
            self.assertNotEqual(st.get("status"), "error", st.get("error"))
            time.sleep(0.5)
        else:
            self.fail(f"Job {job['job_id']} never reached done.")
        s, pdf = self._get(job["download_url"], timeout=60)
        self.assertEqual(s, 200)
        self.assertTrue(pdf[:4] == b"%PDF", pdf[:20])
        return pdf


class ApiM2Cases(_LiveServer):
    def test_api_10_full_config(self):
        setup = _setup(candidate_fields=list(ALL_CANDIDATES),
                       logo_b64=_b64(_png_fixture()),
                       watermark_mode="both", watermark_text="M2WMAPI",
                       wm_image_b64=_b64(_png_fixture()))
        started = time.time()
        pdf = self._generate_and_download(_hindi_questions(), setup,
                                          "M2 API Full परीक्षा")
        print(f"\n  [M2-API/10Q] {len(pdf)} bytes in "
              f"{time.time() - started:.1f}s", flush=True)
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            full = "\n".join(p.get_text() for p in doc)
            self.assertIn("Journey for लबासना", full)
            self.assertIn("M2 API Full परीक्षा", full)
            self.assertIn("M2 Institute of Testing", full)
            self.assertIn("M2 Tagline Marker", full)
            self.assertIn("B-12", full)
            self.assertIn("M2-CODE-77", full)
            self.assertIn("Maximum Marks", full)
            for label in ALL_CANDIDATES:
                self.assertIn(label, full, label)
            self.assertIn("Answer Key", full)
            self.assertIn("Detailed Solutions", full)
            for snippet in ("भारत", "उत्तर", "व्याख्या"):
                self.assertIn(snippet, full, snippet)
            self.assertNotIn("�", full)
            for page in doc:
                self.assertIn("M2WMAPI", page.get_text(),
                              f"page {page.number} missing watermark")
                self.assertGreaterEqual(len(page.get_images(full=True)), 1)
        finally:
            doc.close()

    def test_api_10_minimal_config(self):
        setup = _setup(subject="", test_number="", booklet_series="",
                       booklet_number="", paper="", test_code="",
                       duration="", candidate_fields=[], institute_name="",
                       tagline="", answer_key=False, solutions=False)
        pdf = self._generate_and_download(_plain_questions(10), setup,
                                          "M2 API Minimal")
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            full = "\n".join(p.get_text() for p in doc)
            self.assertIn("M2 API Minimal", full)
            self.assertIn("M2-Q10-STEM", full)
            self.assertNotIn("Answer Key", full)
            self.assertNotIn("Detailed Solutions", full)
            self.assertNotIn("Candidate Details", full)
            self.assertNotIn("Test No", full)
            for page in doc:
                self.assertEqual(len(page.get_images(full=True)), 0)
        finally:
            doc.close()

    def test_api_300_full_config(self):
        setup = _setup(candidate_fields=["Candidate Name", "Roll Number"],
                       watermark_mode="text", watermark_text="M2WM300")
        started = time.time()
        pdf = self._generate_and_download(_plain_questions(300), setup,
                                          "M2 API Scale 300")
        elapsed = time.time() - started
        print(f"\n  [M2-API/300Q] {len(pdf)} bytes in {elapsed:.1f}s",
              flush=True)
        self.assertLess(elapsed, 170, "render too slow for bot poll budget")
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            full = "\n".join(p.get_text() for p in doc)
            self.assertIn("M2-Q1-STEM", full)
            self.assertIn("M2-Q300-STEM", full)
            self.assertIn("M2-Q150-OPT-C", full)
            self.assertIn("600", full)  # 300 x +2
            self.assertIn("Maximum Marks", full)
            self.assertIn("Answer Key", full)
            self.assertIn("Detailed Solutions", full)
            for page in (doc[0], doc[len(doc) // 2], doc[-1]):
                self.assertIn("M2WM300", page.get_text())
        finally:
            doc.close()

    def test_api_garbage_setup_stays_lenient(self):
        setup = {"watermark_mode": "bogus", "watermark_text": "M2WMX",
                 "answer_key": "yes-please", "solutions": 42,
                 "visuals": "sometimes", "marks_correct": "lots",
                 "candidate_fields": "not-a-list",
                 "logo_b64": "!!!not-base64!!!",
                 "wm_image_b64": _b64(b"junk") * 3,
                 "subject": ["weird", "type"], "extra_unknown": {"a": 1}}
        pdf = self._generate_and_download(_plain_questions(6), setup,
                                          "M2 API Lenient")
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            full = "\n".join(p.get_text() for p in doc)
            self.assertIn("M2-Q6-STEM", full)
            self.assertIn("Answer Key", full)  # bool fallback True
            self.assertIn("Detailed Solutions", full)
            self.assertNotIn("M2WMX", full)
            self.assertNotIn("Candidate Details", full)
            for page in doc:
                self.assertEqual(len(page.get_images(full=True)), 0)
        finally:
            doc.close()


if __name__ == "__main__":
    unittest.main()
