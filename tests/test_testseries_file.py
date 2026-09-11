"""Direct MCQ file -> Test Series flow: parser, PDF/OCR, config, wiring.

Offline suite (no network): Format A/B parsing, mixed files, validation
with per-question problem reports, zero-silent-skip scale checks, PDF
text-layer + header/footer dedup + OCR fallback, paper-settings config,
payload reuse, and bridge/cancel wiring.
"""
import asyncio
import io
import itertools
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _ensure_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


_ensure_loop()

from quizbot.creator_bot import state as creator_state  # noqa: E402
from quizbot.creator_bot.handlers import testseries_file as tsf  # noqa: E402
from quizbot.runner_bot.creator_bridge import (  # noqa: E402
    _CreatorStateFilter,
    _creator_message_router,
    BridgeMessage,
)

_UIDS = itertools.count(950000)


def _uid():
    return next(_UIDS)


# ─── Sample documents ────────────────────────────────────────────────
FORMAT_A = """Q.1. Who is known as the father of the Indian Constitution?
A) Jawaharlal Nehru
B) Dr. B.R. Ambedkar ✅
C) Mahatma Gandhi
D) Sardar Patel
Ex: Dr. Ambedkar chaired the Drafting Committee.
"""

FORMAT_A_VARIANTS = """Q26 Which planet is called the Red Planet?
a) Venus
b) Mars ✅
c) Jupiter
d) Saturn

Question 27. What is H2O commonly known as?
A. Water ✅
B. Salt
C. Oxygen
D. Hydrogen
"""

FORMAT_B = """Q.26. Which gas is most abundant in Earth's atmosphere?
A. Oxygen
B. Hydrogen
C. Nitrogen
D. Carbon dioxide
Answer: C
Solution: Nitrogen forms about 78% of air.
Extra details: Asked in SSC CGL 2023.
"""

FORMAT_B_TEXT_ANSWER = """Q.27. What is the currency of Japan?
A) Won
B) Yen
C) Dollar
D) Rupee
Answer: Yen
Solution: Japan uses the Yen (¥).
"""


class ParserFormatACases(unittest.TestCase):
    def test_format_a_exact(self):
        r = tsf.parse_testseries_text(FORMAT_A)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(len(r.questions), 1)
        q = r.questions[0]
        self.assertEqual(q.number, 1)
        self.assertIn("father of the Indian Constitution", q.question)
        self.assertEqual(len(q.options), 4)
        self.assertEqual(q.correct_index, 1)
        self.assertIn("Drafting Committee", q.explanation)

    def test_format_a_header_and_option_variants(self):
        r = tsf.parse_testseries_text(FORMAT_A_VARIANTS)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual([q.number for q in r.questions], [26, 27])
        self.assertEqual(r.questions[0].correct_index, 1)  # b) Mars
        self.assertEqual(r.questions[1].correct_index, 0)  # A. Water
        self.assertNotIn("✅", r.questions[0].options[1])


class ParserFormatBCases(unittest.TestCase):
    def test_format_b_letter_answer(self):
        r = tsf.parse_testseries_text(FORMAT_B)
        self.assertTrue(r.ok, r.problems)
        q = r.questions[0]
        self.assertEqual(q.correct_index, 2)
        self.assertIn("Solution: Nitrogen", q.explanation)
        self.assertIn("Extra details: Asked in SSC CGL 2023.", q.explanation)

    def test_format_b_option_text_answer(self):
        r = tsf.parse_testseries_text(FORMAT_B_TEXT_ANSWER)
        self.assertTrue(r.ok, r.problems)
        q = r.questions[0]
        self.assertEqual(q.options[q.correct_index], "Yen")
        self.assertIn("¥", q.explanation)

    def test_answer_letter_spellings(self):
        for value in ("c)", "(C)", "C.", "c"):
            text = ("Q.1. Pick C?\nA) a\nB) b\nC) c\nD) d\nAnswer: %s\n" % value)
            r = tsf.parse_testseries_text(text)
            self.assertTrue(r.ok, (value, r.problems))
            self.assertEqual(r.questions[0].correct_index, 2, value)


class ParserMixedCases(unittest.TestCase):
    def test_mixed_a_and_b_in_one_file(self):
        r = tsf.parse_testseries_text(FORMAT_A + "\n" + FORMAT_B + "\n" + FORMAT_B_TEXT_ANSWER)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(len(r.questions), 3)
        self.assertEqual([q.correct_index for q in r.questions], [1, 2, 1])

    def test_checkmark_plus_solution_mix(self):
        text = ("Q.5. Mixed markers?\nA) yes ✅\nB) no\nAnswer: A\nSolution: both agree.\n")
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.questions[0].correct_index, 0)

    def test_multiline_unicode_hindi(self):
        text = (
            "प्रश्न 8. भारत की राजधानी क्या है?\n"
            "यह प्रश्न दो पंक्तियों में है।\n"
            "A) मुंबई\n"
            "B) नई दिल्ली ✅\n"
            "C) कोलकाता\n"
            "D) चेन्नई\n"
            "Ex: नई दिल्ली • भारत की राजधानी है।\n"
            "दूसरी पंक्ति — bullets • ✅ preserved.\n"
        )
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        q = r.questions[0]
        self.assertEqual(q.correct_index, 1)
        self.assertIn("दो पंक्तियों", q.question)
        self.assertIn("दूसरी पंक्ति", q.explanation)

    def test_hindi_answer_and_explanation_aliases(self):
        text = "Q.9. Capital of France?\nA) Rome\nB) Paris\nC) Madrid\nD) Berlin\nउत्तर: B\nव्याख्या: Paris is the capital.\n"
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.questions[0].correct_index, 1)
        self.assertIn("Paris is the capital", r.questions[0].explanation)

    def test_multiline_option_wrap(self):
        text = ("Q.3. Wrapped option?\nA) first half\nsecond half of A ✅\nB) other\nC) more\nD) last\n")
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        self.assertIn("second half", r.questions[0].options[0])
        self.assertEqual(r.questions[0].correct_index, 0)

    def test_trailing_notes_preserved_not_dropped(self):
        text = "Q.4. Notes kept?\nA) yes ✅\nB) no\nTopic: Polity\nDifficulty: easy\n"
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        self.assertIn("Notes: Topic: Polity", r.questions[0].explanation)
        self.assertIn("Difficulty: easy", r.questions[0].explanation)

    def test_options_after_solution_still_parse(self):
        text = "Q.6. Late options?\nSolution: C is right.\nA) a\nB) b\nC) c\nAnswer: C\n"
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.questions[0].correct_index, 2)


class AnswerResolutionCases(unittest.TestCase):
    def test_missing_answer_reports_question_number(self):
        text = "Q.17. No answer here?\nA) a\nB) b\nC) c\n"
        r = tsf.parse_testseries_text(text)
        self.assertFalse(r.ok)
        self.assertEqual(r.problems, ["Q17 — answer not detected"])
        self.assertEqual(len(r.questions), 0)

    def test_disagreeing_markers_block(self):
        text = "Q.2. Disagree?\nA) a ✅\nB) b\nAnswer: B\n"
        r = tsf.parse_testseries_text(text)
        self.assertFalse(r.ok)
        self.assertIn("Q2 — ✅ option and Answer: disagree", r.problems)

    def test_multiple_checkmarks_block(self):
        text = "Q.3. Two marks?\nA) a ✅\nB) b ✅\nC) c\n"
        r = tsf.parse_testseries_text(text)
        self.assertFalse(r.ok)
        self.assertTrue(any("2 ✅-marked options" in p for p in r.problems), r.problems)

    def test_answer_text_matching_no_option_blocks(self):
        text = 'Q.4. Bad text?\nA) apple\nB) ball\nAnswer: carrot\n'
        r = tsf.parse_testseries_text(text)
        self.assertFalse(r.ok)
        self.assertTrue(any('matches no option' in p for p in r.problems), r.problems)

    def test_answer_letter_with_text_suffix(self):
        text = "Q.5. Suffixed?\nA) Apple\nB) Banana\nAnswer: B - Banana\n"
        r = tsf.parse_testseries_text(text)
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.questions[0].correct_index, 1)


class MalformedCases(unittest.TestCase):
    def test_single_option_blocks(self):
        r = tsf.parse_testseries_text("Q.1. One option?\nA) only ✅\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("only 1 option(s)" in p for p in r.problems), r.problems)

    def test_no_options_blocks(self):
        r = tsf.parse_testseries_text("Q.1. Just a stem, no options.\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("only 0 option(s)" in p for p in r.problems), r.problems)

    def test_empty_question_text_blocks(self):
        r = tsf.parse_testseries_text("Q.1.\nA) a ✅\nB) b\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("question text missing" in p for p in r.problems), r.problems)

    def test_too_many_options_blocks(self):
        lines = ["Q.1. Eleven?"]
        for i, letter in enumerate("ABCDEFGHIJK"):
            lines.append("%s) opt %s%s" % (letter, letter, " ✅" if i == 0 else ""))
        r = tsf.parse_testseries_text("\n".join(lines) + "\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("11 options found (maximum 10)" in p for p in r.problems), r.problems)

    def test_duplicate_letters_block(self):
        r = tsf.parse_testseries_text("Q.1. Dupes?\nA) a ✅\nA) b\nC) c\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("duplicate option letter(s): A" in p for p in r.problems), r.problems)

    def test_multiple_answer_lines_block(self):
        r = tsf.parse_testseries_text("Q.1. Two answers?\nA) a\nB) b\nAnswer: A\nAnswer: B\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("2 Answer: lines" in p for p in r.problems), r.problems)

    def test_no_questions_detected(self):
        r = tsf.process_testseries_upload(b"Hello, this is not a quiz file.\n", "notes.txt")
        self.assertFalse(r.ok)
        self.assertIn("No questions detected", r.error)

    def test_binary_rejected(self):
        r = tsf.process_testseries_upload(b"a\x00b\x00c", "x.txt")
        self.assertFalse(r.ok)
        self.assertIn("readable text", r.error)

    def test_over_question_cap_rejected(self):
        doc = "\n\n".join("Q.%d. Cap?\nA) a ✅\nB) b\n" % i for i in range(1, 2002))
        r = tsf.parse_testseries_text(doc)
        self.assertFalse(r.ok)
        self.assertIn("maximum 2000", r.error)

    def test_problem_report_truncates_long_lists(self):
        result = tsf.FileParseResult(ok=False, total_blocks=40, problems=["Q%d — bad" % i for i in range(1, 41)])
        text = tsf.render_problem_report(result, "big.txt")
        self.assertIn("…and 10 more", text)
        self.assertLess(len(text), 4000)


def _gen_doc(n):
    """Build an n-question mixed-format doc; returns (text, expected_indexes)."""
    blocks, expected = [], []
    for i in range(1, n + 1):
        style = i % 3
        correct = i % 4
        letters = ["A", "B", "C", "D"]
        opts = ["Option %s of Q%d" % (letter, i) for letter in letters]
        if style == 0:  # Format A
            hdr = "Q.%d." % i if i % 3 == 0 else ("Q%d" % i if i % 3 == 1 else "Question %d" % i)
            delim = ")" if i % 2 == 0 else "."
            low = (i % 5 == 3)
            lines = ["%s What is the answer to question %d?" % (hdr, i)]
            if i % 7 == 0:
                lines.append("यह प्रश्न %d का हिंदी विवरण है — stem line two." % i)
            for j, letter in enumerate(letters):
                mark = " ✅" if j == correct else ""
                shown = letter.lower() if low else letter
                lines.append("%s%s %s%s" % (shown, delim, opts[j], mark))
                if i % 5 == 0 and j == 0:
                    lines.append("(wrap) more about option A of Q%d" % i)
            lines.append("Ex: Because option %s is right for Q%d." % (letters[correct], i))
            if i % 11 == 0:
                lines.append("continued vyakhya — दूसरी पंक्ति for Q%d." % i)
        elif style == 1:  # Format B, letter answer
            hdr = "Question %d" % i if i % 2 == 0 else "Q.%d." % i
            spell = ["%s", "%s)", "(%s)", "%s."][i % 4] % letters[correct]
            if i % 4 == 1:
                spell = spell.lower()
            lines = ["%s Letter-style question %d?" % (hdr, i)]
            lines += ["%s. %s" % (letter, opt) for letter, opt in zip(letters, opts)]
            lines.append("Answer: %s" % spell)
            lines.append("Solution: Pick %s for Q%d." % (letters[correct], i))
            if i % 4 == 0:
                lines.append("Extra details: revision note %d." % i)
        else:  # Format B, option-text answer
            hdr = "Q%d" % i if i % 2 == 0 else "Question %d." % i
            lines = ["%s Text-style question %d?" % (hdr, i)]
            lines += ["%s) %s" % (letter, opt) for letter, opt in zip(letters, opts)]
            lines.append("Answer: %s" % opts[correct])
            lines.append("Solution: The text matches for Q%d." % i)
        blocks.append("\n".join(lines))
        expected.append(correct)
    return "\n\n".join(blocks) + "\n", expected


class NoSilentSkipScaleCases(unittest.TestCase):
    def _assert_full_parse(self, n):
        doc, expected = _gen_doc(n)
        r = tsf.parse_testseries_text(doc)
        self.assertTrue(r.ok, r.problems[:3])
        self.assertEqual(r.total_blocks, n)
        self.assertEqual(len(r.questions), n, "every block must parse; nothing silently skipped")
        self.assertEqual(r.problems, [])
        got = [q.correct_index for q in r.questions]
        self.assertEqual(got, expected)

    def test_scale_10(self):
        self._assert_full_parse(10)

    def test_scale_55(self):
        self._assert_full_parse(55)

    def test_scale_100(self):
        self._assert_full_parse(100)

    def test_scale_200(self):
        self._assert_full_parse(200)

    def test_scale_300(self):
        self._assert_full_parse(300)

    def test_scale_300_builds_service_payload(self):
        doc, _ = _gen_doc(300)
        r = tsf.parse_testseries_text(doc)
        self.assertTrue(r.ok)
        from quizbot.creator_bot.handlers.reports import _build_testseries_payload
        payload = _build_testseries_payload(
            [{"quiz_name": "f.txt", "questions": [q.to_payload() for q in r.questions]}],
            "keyonly", "Scale300")
        self.assertEqual(len(payload["questions_json"]), 300)
        self.assertEqual(payload["solution_display"], "end")

    def test_injected_breaks_all_reported_none_lost(self):
        doc, _ = _gen_doc(100)
        blocks = doc.split("\n\n")
        blocks[17] = blocks[17].replace(" ✅", "")  # Q18 (Format A) loses its answer
        kept = [ln for ln in blocks[41].split("\n") if not ln.startswith(("B)", "C)", "D)"))]
        blocks[41] = "\n".join(kept)  # Q42 left with one option
        blocks[76] = blocks[76].replace("\nB)", "\nA)", 1)  # Q77 duplicate letter
        r = tsf.parse_testseries_text("\n\n".join(blocks))
        self.assertFalse(r.ok)
        self.assertEqual(r.total_blocks, 100)
        self.assertEqual(len(r.questions), 97)
        labels = {p.split(" — ")[0] for p in r.problems}
        self.assertEqual(labels, {"Q18", "Q42", "Q77"})


def _make_pdf(pages):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    pdf = FPDF(format="A5")
    for lines in pages:
        pdf.add_page()
        pdf.set_font("helvetica", size=11)
        for line in lines:
            pdf.multi_cell(0, 6, line if line else " ",
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


class PdfExtractionCases(unittest.TestCase):
    def test_text_pdf_parses_with_headers_deduped(self):
        header, footer = "SSC CGL MOCK 2026", "CONFIDENTIAL"
        pages = []
        for i in range(1, 5):
            pages.append([
                header, "",
                "Q.%d. PDF question %d?" % (i, i),
                "A) alpha %d" % i, "B) beta %d" % i,
                "C) gamma %d" % i, "D) None of the above",
                "Answer: B",
                "Solution: beta wins on page %d." % i, "",
                footer,
            ])
        r = tsf.process_testseries_upload(_make_pdf(pages), "paper.pdf")
        self.assertTrue(r.ok, r.problems or r.error)
        self.assertTrue(r.is_pdf and not r.ocr_used)
        self.assertEqual(r.pages, 4)
        self.assertEqual(len(r.questions), 4)
        for q in r.questions:
            blob = q.question + "".join(q.options) + q.explanation
            self.assertNotIn("SSC CGL MOCK", blob)
            self.assertNotIn("CONFIDENTIAL", blob)
            self.assertEqual(q.options[3], "None of the above")
            self.assertEqual(q.correct_index, 1)

    def test_corrupt_pdf_rejected_cleanly(self):
        r = tsf.process_testseries_upload(b"definitely not a pdf", "x.pdf")
        self.assertFalse(r.ok)
        self.assertIn("Could not read this PDF", r.error)

    def test_scanned_pdf_ocr_success_via_stub(self):
        from fpdf import FPDF
        pdf = FPDF(format="A5")
        pdf.add_page()
        blank = bytes(pdf.output())
        fake = types.ModuleType("pytesseract")
        fake.image_to_string = lambda image, lang="": FORMAT_A
        sys.modules["pytesseract"] = fake
        try:
            r = tsf.process_testseries_upload(blank, "scan.pdf")
        finally:
            del sys.modules["pytesseract"]
        self.assertTrue(r.ok, r.problems or r.error)
        self.assertTrue(r.ocr_used)
        self.assertEqual(len(r.questions), 1)
        self.assertEqual(r.questions[0].correct_index, 1)

    def test_scanned_pdf_without_ocr_gives_clear_message(self):
        from fpdf import FPDF
        pdf = FPDF(format="A5")
        pdf.add_page()
        blank = bytes(pdf.output())
        sys.modules["pytesseract"] = None  # force ImportError inside helper
        try:
            r = tsf.process_testseries_upload(blank, "scan.pdf")
        finally:
            del sys.modules["pytesseract"]
        self.assertFalse(r.ok)
        self.assertIn("scanned", r.error)
        self.assertIn("Tesseract", r.error)

    def test_visual_only_report_pdf_gets_actionable_tip(self):
        no_answers = "\n\n".join(
            "Q.%d. Report Q%d?\nA) a\nB) b\nC) c\nD) d\n" % (i, i) for i in (1, 2, 3))
        r = tsf.process_testseries_upload(_make_pdf([no_answers.split("\n")]), "report.pdf")
        self.assertFalse(r.ok)
        text = tsf.render_problem_report(r, "report.pdf")
        self.assertIn("answer not detected", text)
        self.assertIn("/testseries <QUIZ_ID>", text)


class FileImportOcrRefactorCases(unittest.TestCase):
    """The OCR-helper extraction must preserve file_import behavior."""

    def test_ocr_helper_exists_and_matches_legacy_call_shape(self):
        from quizbot.creator_bot.handlers import file_import
        import inspect
        sig = inspect.signature(file_import.ocr_pdf_text)
        self.assertEqual(list(sig.parameters)[:2], ["content", "pages"])
        self.assertEqual(sig.parameters["max_pages"].default, 30)

    def test_process_pdf_still_fails_cleanly_without_ocr(self):
        from quizbot.creator_bot.handlers import file_import
        sys.modules["pytesseract"] = None
        try:
            with self.assertRaises(RuntimeError) as ctx:
                file_import._process_pdf(_make_pdf([["plain page, no questions"]]), [], [])
        finally:
            del sys.modules["pytesseract"]
        self.assertIn("No usable questions", str(ctx.exception))
        self.assertIn("OCR also failed", str(ctx.exception))


class ConfigCases(unittest.TestCase):
    def test_defaults_and_generate_words(self):
        for text in ("generate", "DEFAULTS", "go", "title=Mock_Test"):
            meta, err = tsf.parse_file_config(text)
            self.assertIsNone(err, text)
        meta, _ = tsf.parse_file_config("generate")
        self.assertEqual(meta["title"], "Mock Test")
        self.assertEqual(meta["mode"], "keyonly")

    def test_all_keys_and_underscore_spacing(self):
        meta, err = tsf.parse_file_config(
            "title=SSC_Mock_1 subject=Polity test=Test_1 exam=SSC_CGL marks=100 neg=0.25 mode=inline")
        self.assertIsNone(err)
        self.assertEqual(
            (meta["title"], meta["subject"], meta["test"], meta["exam"],
             meta["marks"], meta["neg"], meta["mode"]),
            ("SSC Mock 1", "Polity", "Test 1", "SSC CGL", "100", "0.25", "inline"))

    def test_negative_alias(self):
        meta, err = tsf.parse_file_config("negative=1/4")
        self.assertIsNone(err)
        self.assertEqual(meta["neg"], "1/4")

    def test_unknown_key_rejected(self):
        meta, err = tsf.parse_file_config("titel=Typo")
        self.assertIsNone(meta)
        self.assertIn("Unknown setting", err)

    def test_unknown_word_rejected(self):
        meta, err = tsf.parse_file_config("generate please")
        self.assertIsNone(meta)
        self.assertIn("Unrecognized", err)

    def test_bad_mode_rejected(self):
        meta, err = tsf.parse_file_config("mode=everything")
        self.assertIsNone(meta)
        self.assertIn("mode must be", err)

    def test_tagline_caption_filename_builders(self):
        meta, _ = tsf.parse_file_config("title=SSC_Mock/1 subject=Polity marks=100 neg=0.25 mode=inline")
        self.assertEqual(tsf.build_file_tagline(meta), "Polity • Marks: 100 • Negative: 0.25")
        caption = tsf.build_file_caption(meta, 25, "qs.txt")
        self.assertIn("Questions: 25", caption)
        self.assertIn("Subject: Polity", caption)
        self.assertIn("Mode: inline", caption)
        self.assertNotIn("SSC_Mock/1", caption)  # markdown-escaped
        self.assertEqual(tsf.build_file_filename(meta, 25), "MockTest_SSC_Mock_1_25Q.pdf")
        bare, _ = tsf.parse_file_config("generate")
        self.assertEqual(tsf.build_file_tagline(bare), "Test Series")


class PayloadIntegrationCases(unittest.TestCase):
    def test_generate_honors_tagline_override(self):
        _ensure_loop()
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
             patch.object(rep, "get_session", AsyncMock(return_value=_Session())):
            out = asyncio.run(rep._generate_pdf_via_api(
                quizzes, "inline", "T", tagline="Subj • Test 1"))
        self.assertEqual(out, b"%PDF-fake")
        self.assertEqual(captured["tagline"], "Subj • Test 1")
        self.assertEqual(captured["exam_title"], "T")
        self.assertEqual(captured["solution_display"], "inline")

    def test_generate_default_tagline_unchanged(self):
        _ensure_loop()
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
             patch.object(rep, "get_session", AsyncMock(return_value=_Session())):
            asyncio.run(rep._generate_pdf_via_api(quizzes, "keyonly", "T"))
        self.assertEqual(captured["tagline"], "Test Series")


# ─── Offline Telegram wiring ──────────────────────────────────────────
class _FakeSent:
    _next = itertools.count(1)

    def __init__(self, chat_id, text="", caption=None):
        self.message_id = next(self._next)
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.from_user = SimpleNamespace(id=999)
        self.text = text
        self.caption = caption
        self.reply_markup = None
        self.document = None
        self.photo = None
        self.video = None
        self.poll = None


class _FakeBot:
    def __init__(self, file_bytes=b""):
        self.file_bytes = file_bytes
        self.sent = []
        self.edits = []
        self.documents = []
        self.deleted = []

    async def get_file(self, file_id):
        parent = self

        class _F:
            async def download_as_bytearray(self):
                return bytearray(parent.file_bytes)

        return _F()

    async def send_message(self, chat_id, text, reply_markup=None,
                           parse_mode=None, disable_web_page_preview=False, **k):
        self.sent.append((chat_id, text))
        return _FakeSent(chat_id, text=text)

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None, **k):
        self.edits.append((chat_id, message_id, text))
        return _FakeSent(chat_id, text=text)

    async def send_document(self, chat_id, document, filename=None,
                            caption=None, reply_markup=None, parse_mode=None, **k):
        data = document.read() if hasattr(document, "read") else document
        self.documents.append((chat_id, filename, caption, bytes(data)))
        return _FakeSent(chat_id, caption=caption)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _incoming(uid, text=None, document=None):
    return SimpleNamespace(
        message_id=1, chat=SimpleNamespace(id=777, type="private"),
        from_user=SimpleNamespace(id=uid), text=text, caption=None,
        reply_to_message=None, document=document, photo=None,
        video=None, poll=None, reply_markup=None, date=None)


def _update_context(uid, bot, message):
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=uid))
    return update, SimpleNamespace(bot=bot)


def _doc(name="qs.txt", size=100, mime="text/plain"):
    return SimpleNamespace(file_id="fid", file_name=name,
                           file_size=size, mime_type=mime)


class BridgeWiringCases(unittest.TestCase):
    def tearDown(self):
        for uid in list(creator_state.testseries_upload._data.keys()):
            if uid >= 950000:
                creator_state.testseries_upload.pop(uid, None)
        for uid in list(creator_state.quiz_creation._data.keys()):
            if uid >= 950000:
                creator_state.quiz_creation.pop(uid, None)

    def test_filter_matches_upload_session_only(self):
        uid = _uid()
        filt = _CreatorStateFilter()
        upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid))
        self.assertFalse(filt.filter(upd))
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        try:
            self.assertTrue(filt.filter(upd))
        finally:
            creator_state.testseries_upload.pop(uid, None)

    def test_bare_testseries_asks_for_file(self):
        _ensure_loop()
        import quizbot.creator_bot.handlers.reports as rep
        import quizbot.shared.config as config
        uid = _uid()
        bot = _FakeBot()
        msg = BridgeMessage(bot, _incoming(uid, text="/testseries"))
        with patch.object(config, "PDF_API_BASE", "http://127.0.0.1:9"), \
             patch.object(rep, "is_premium_user", AsyncMock(return_value=True)):
            asyncio.run(rep.testseries_cmd(None, msg))
        try:
            sess = creator_state.testseries_upload.get(uid)
            self.assertIsNotNone(sess)
            self.assertEqual(sess["step"], "awaiting_file")
            self.assertTrue(any(".txt" in t and ".pdf" in t for _, t in bot.sent), bot.sent)
        finally:
            creator_state.testseries_upload.pop(uid, None)

    def test_document_flow_parses_and_prompts_config(self):
        _ensure_loop()
        uid = _uid()
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        bot = _FakeBot(file_bytes=(FORMAT_A + "\n" + FORMAT_B).encode())
        upd, ctx = _update_context(uid, bot, _incoming(uid, document=_doc()))
        asyncio.run(_creator_message_router(upd, ctx))
        sess = creator_state.testseries_upload.get(uid)
        self.assertEqual(sess["step"], "awaiting_config")
        self.assertEqual(len(sess["questions"]), 2)
        self.assertTrue(any("File parsed" in t for _, _, t in bot.edits), bot.edits)

    def test_broken_file_reports_problems_and_stays(self):
        _ensure_loop()
        uid = _uid()
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        bad = FORMAT_A + "\nQ.99. Missing answer?\nA) a\nB) b\nC) c\n"
        bot = _FakeBot(file_bytes=bad.encode())
        upd, ctx = _update_context(uid, bot, _incoming(uid, document=_doc()))
        asyncio.run(_creator_message_router(upd, ctx))
        self.assertEqual(creator_state.testseries_upload.get(uid)["step"], "awaiting_file")
        report = " ".join(t for _, _, t in bot.edits)
        self.assertIn("Could not build", report)
        self.assertIn("Q99 — answer not detected", report)

    def test_wrong_type_and_oversize_rejected(self):
        _ensure_loop()
        uid = _uid()
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        bot = _FakeBot()
        upd, ctx = _update_context(uid, bot, _incoming(uid, document=_doc("run.exe", 10, "application/x-ms")))
        asyncio.run(_creator_message_router(upd, ctx))
        self.assertTrue(any(".txt" in t for _, t in bot.sent), bot.sent)
        upd, ctx = _update_context(uid, bot, _incoming(uid, document=_doc("big.pdf", 16 * 1024 * 1024)))
        asyncio.run(_creator_message_router(upd, ctx))
        self.assertTrue(any("too large" in t for _, t in bot.sent), bot.sent)

    def test_text_while_awaiting_file_nudges(self):
        _ensure_loop()
        uid = _uid()
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        bot = _FakeBot()
        upd, ctx = _update_context(uid, bot, _incoming(uid, text="hello?"))
        asyncio.run(_creator_message_router(upd, ctx))
        self.assertTrue(any("Please send the" in t for _, t in bot.sent), bot.sent)

    def test_config_flow_generates_pdf_and_clears_session(self):
        _ensure_loop()
        uid = _uid()
        questions = [
            {"question": "q1", "options": ["a", "b"], "correct_option_id": 0, "explanation": ""},
            {"question": "q2", "options": ["c", "d"], "correct_option_id": 1, "explanation": "why"},
        ]
        creator_state.testseries_upload[uid] = {
            "step": "awaiting_config", "questions": questions, "filename": "qs.txt"}
        bot = _FakeBot()
        upd, ctx = _update_context(uid, bot, _incoming(uid, text="title=Polity_Test mode=inline"))
        with patch.object(tsf, "_generate_pdf_via_api", AsyncMock(return_value=b"%PDF-fake")) as gen:
            asyncio.run(_creator_message_router(upd, ctx))
        self.assertEqual(gen.await_count, 1)
        _, kwargs = gen.await_args
        self.assertEqual(kwargs.get("tagline"), "Test Series")
        self.assertIsNone(creator_state.testseries_upload.get(uid))
        self.assertEqual(len(bot.documents), 1)
        _, filename, caption, data = bot.documents[0]
        self.assertEqual(filename, "MockTest_Polity_Test_2Q.pdf")
        self.assertIn("Questions: 2", caption)
        self.assertEqual(data, b"%PDF-fake")

    def test_bad_config_keeps_session(self):
        _ensure_loop()
        uid = _uid()
        creator_state.testseries_upload[uid] = {
            "step": "awaiting_config",
            "questions": [{"question": "q", "options": ["a", "b"],
                           "correct_option_id": 0, "explanation": ""}],
            "filename": "qs.txt"}
        bot = _FakeBot()
        upd, ctx = _update_context(uid, bot, _incoming(uid, text="titel=Typo"))
        with patch.object(tsf, "_generate_pdf_via_api", AsyncMock(return_value=b"x")) as gen:
            asyncio.run(_creator_message_router(upd, ctx))
        self.assertEqual(gen.await_count, 0)
        self.assertIsNotNone(creator_state.testseries_upload.get(uid))
        self.assertTrue(any("Unknown setting" in t for _, t in bot.sent), bot.sent)


class CancelCases(unittest.TestCase):
    def test_cancel_clears_upload_session(self):
        _ensure_loop()
        from quizbot.creator_bot.handlers import quiz_creation
        uid = _uid()
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        bot = _FakeBot()
        msg = BridgeMessage(bot, _incoming(uid, text="/cancel"))
        asyncio.run(quiz_creation.cancel_cmd(None, msg))
        self.assertIsNone(creator_state.testseries_upload.get(uid))
        self.assertTrue(any("upload cancelled" in t for _, t in bot.sent), bot.sent)

    def test_cancel_existing_behaviour_preserved(self):
        _ensure_loop()
        from quizbot.creator_bot.handlers import quiz_creation
        uid = _uid()
        creator_state.quiz_creation[uid] = {"questions": []}
        bot = _FakeBot()
        asyncio.run(quiz_creation.cancel_cmd(None, BridgeMessage(bot, _incoming(uid, text="/cancel"))))
        self.assertIsNone(creator_state.quiz_creation.get(uid))
        self.assertTrue(any("Cancelled." in t for _, t in bot.sent), bot.sent)
        uid2 = _uid()
        bot2 = _FakeBot()
        asyncio.run(quiz_creation.cancel_cmd(None, BridgeMessage(bot2, _incoming(uid2, text="/cancel"))))
        self.assertTrue(any("Nothing to cancel" in t for _, t in bot2.sent), bot2.sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
