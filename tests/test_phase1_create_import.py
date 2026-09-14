"""Phase 1 — /create file import, canonical MCQ parser and malformed-input
reporting (acceptance matrix A/B/D).

Covers:
* .txt / .md / .markdown / .pdf extension dispatch (case-insensitive);
* every accepted answer marker form (✅, Answer/Ans/Correct Answer/Correct
  Option, Hindi labels, numeric, parenthesized, "option B"/"choice B");
* conflict detection (✅ vs Answer, multiple ✅, repeated disagreeing
  Answer lines, out-of-range answers) — never a silent guess;
* explanation labels incl. multiline/extra-details/Hindi;
* statement / Assertion-Reason / chronology / numbered-statement layouts;
* continuous (no blank line) PDF text and separate end-of-paper answer keys;
* bilingual / Devanagari / multiline-option handling;
* structured processed/skipped feedback (no false success message).
"""

from __future__ import annotations

import io
import unittest
import unittest.mock
from unittest import IsolatedAsyncioTestCase

import fitz  # PyMuPDF

from quizbot.creator_bot import parsing, state
from quizbot.creator_bot.handlers import file_import
from quizbot.creator_bot.handlers import quiz_creation


def parse_one(text: str):
    """Parse a single block strictly (require answer)."""
    return parsing.parse_question_block_strict(text)


def parse_doc(text: str):
    return parsing.parse_question_document(text)


def import_bytes(content: bytes, filename: str):
    out: list[dict] = []
    count, error, report = file_import.process_uploaded_file(
        content, filename, out, [])
    return count, error, report, out


# ---------------------------------------------------------------------------
# A4 — extension dispatch
# ---------------------------------------------------------------------------
class ExtensionDispatchTests(unittest.TestCase):
    BODY = (
        "First question?\nA) one\nB) two ✅\nC) three\nD) four\n\n"
        "Second question?\nA) one\nB) two\nC) three ✅\nD) four\n"
    )

    def _assert_two(self, count, error, report, out):
        self.assertEqual(error, None)
        self.assertEqual(count, 2, report)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["correct_option_id"], 1)
        self.assertEqual(out[1]["correct_option_id"], 2)

    def test_txt(self):
        r = import_bytes(self.BODY.encode(), "quiz.txt")
        self._assert_two(*r)

    def test_md(self):
        md = "## Quiz\n\n" + self.BODY.replace("\n\n", "\n\n---\n\n")
        r = import_bytes(md.encode(), "quiz.md")
        self._assert_two(*r)

    def test_markdown(self):
        r = import_bytes(self.BODY.encode(), "quiz.markdown")
        self._assert_two(*r)

    def test_uppercase_extensions(self):
        for ext in ("TXT", "MD", "MARKDOWN", "Pdf"):
            if ext.lower() == "pdf":
                continue
            count, error, report, out = import_bytes(
                self.BODY.encode(), f"quiz.{ext}")
            self.assertEqual(count, 2, f"{ext}: {error} / {report}")

    def test_pdf_text_layer(self):
        pdf = self._make_pdf([
            "1. First question?", "A) one", "B) two", "C) three",
            "D) four", "Answer: B", "",
            "2. Second question?", "A) one", "B) two", "C) three",
            "D) four", "Answer: C", ""])
        count, error, report, out = import_bytes(pdf, "quiz.PDF")
        self.assertIsNone(error, error)
        self.assertEqual(count, 2, report)
        self.assertEqual(out[0]["correct_option_id"], 1)
        self.assertEqual(out[1]["correct_option_id"], 2)

    def test_pdf_separate_answer_key(self):
        lines = [
            "1. Capital of India?", "A) Mumbai", "B) Delhi", "C) Kolkata",
            "D) Chennai",
            "2. 2 + 2 equals?", "A) 3", "B) 4", "C) 5", "D) 6",
            "", "ANSWER KEY", "1-B", "2-B"]
        count, error, report, out = import_bytes(
            self._make_pdf(lines), "key.pdf")
        self.assertIsNone(error, error)
        self.assertEqual(count, 2, report)
        self.assertEqual(out[0]["correct_option_id"], 1)
        self.assertEqual(out[1]["correct_option_id"], 1)

    def test_pdf_packed_no_blank_lines_with_q_badges(self):
        lines = [
            "Q1. First question?", "A) one", "B) two", "Answer: B",
            "C) three", "D) four",
            "Q2. Second question?", "A) one", "B) two", "C) three",
            "D) four", "Answer: D",
        ]
        count, error, report, out = import_bytes(
            self._make_pdf(lines), "packed.pdf")
        self.assertIsNone(error, error)
        self.assertEqual(count, 2, report)
        self.assertEqual([q["correct_option_id"] for q in out], [1, 3])

    def test_pdf_answer_key_and_solutions_section(self):
        lines = [
            "1. First question?", "A) one", "B) two", "C) three", "D) four",
            "2. Second question?", "A) one", "B) two", "C) three", "D) four",
            "", "ANSWER KEY & SOLUTIONS",
            "1. B The first answer follows directly from the setup.",
            "2. C Number two is resolved by elimination of A and B.",
        ]
        count, error, report, out = import_bytes(
            self._make_pdf(lines), "sols.pdf")
        self.assertIsNone(error, error)
        self.assertEqual(count, 2, report)
        self.assertEqual([q["correct_option_id"] for q in out], [1, 2])
        self.assertIn("setup", out[0]["explanation"])
        self.assertIn("elimination", out[1]["explanation"])

    def test_pdf_unmappable_key_reports_exact_failure(self):
        lines = [
            "1. First?", "A) a", "B) b", "C) c", "D) d",
            "2. Second?", "A) a", "B) b", "C) c", "D) d",
            "3. Third?", "A) a", "B) b", "C) c", "D) d",
            "", "ANSWER KEY", "1-B", "2-C",
        ]
        count, error, report, out = import_bytes(
            self._make_pdf(lines), "partial.pdf")
        self.assertEqual(count, 2)
        reasons = [(s["reason"], s["ordinal"]) for s in report["skipped"]]
        self.assertIn((parsing.RE_ANSWER_KEY_ASSOCIATION, 3), reasons)
        self.assertIsNone(error)

    def test_unsupported_extension_message(self):
        count, error, report, out = import_bytes(b"x", "quiz.exe")
        self.assertEqual(count, None)
        self.assertIn("Supported", error)
        self.assertEqual(out, [])

    @staticmethod
    def _make_pdf(lines):
        doc = fitz.open()
        page = doc.new_page()
        y = 60
        for line in lines:
            page.insert_text((50, y), line, fontsize=10, fontname="helv")
            y += 16
            if y > 800:
                page = doc.new_page()
                y = 60
        bio = io.BytesIO()
        doc.save(bio)
        doc.close()
        return bio.getvalue()


class _FakeDoc:
    def __init__(self, name, mime=""):
        self.file_name = name
        self.mime_type = mime
        self.file_id = "fake-file-id"


class _FakeStatus:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)
        self.text = text


class _FakeGateMessage:
    def __init__(self, name, mime=""):
        self.document = _FakeDoc(name, mime)

        class _U:
            id = 5001
        self.from_user = _U()
        self.replies = []
        self.statuses = []

    async def reply(self, text):
        status = _FakeStatus(text)
        self.replies.append(text)
        self.statuses.append(status)
        return status


class _FakeGateClient:
    def __init__(self, payload=b""):
        self.downloads = 0
        self._payload = payload

    async def download_media(self, *a, **k):
        self.downloads += 1
        return io.BytesIO(self._payload)


class _FakeUserRepo:
    async def get_or_create(self, uid):
        return {"remove_words": []}


class DispatcherGateTests(IsolatedAsyncioTestCase):
    """Only txt/md/markdown/pdf (+ images for OCR) are accepted by the
    /create document dispatcher; the JSON flow survives via paste/link."""

    def setUp(self):
        state.quiz_creation[5001] = {"questions": []}

    def tearDown(self):
        state.quiz_creation.pop(5001, None)

    async def _run(self, name, mime="", payload=b""):
        c = _FakeGateClient(payload)
        m = _FakeGateMessage(name, mime)
        with unittest.mock.patch.object(quiz_creation, "get_db", return_value=None), \
             unittest.mock.patch.object(quiz_creation, "UserRepository",
                                        return_value=_FakeUserRepo()):
            await quiz_creation.handle_document(c, m)
        return c, m

    async def test_json_attachment_rejected_with_paste_hint(self):
        c, m = await self._run("quiz.json", "application/json")
        self.assertEqual(c.downloads, 0)
        self.assertIn("Paste", m.replies[0])

    async def test_other_extensions_rejected(self):
        for name in ("quiz.docx", "quiz.csv", "quiz.exe", "quiz.xlsx",
                     "quiz.txt.zip"):
            c, m = await self._run(name)
            self.assertEqual(c.downloads, 0, name)
            self.assertIn("TXT", m.replies[0], name)
            self.assertIn("PDF", m.replies[0], name)
            self.assertNotIn("JSON", m.replies[0], name)

    async def test_disguised_mime_cannot_bypass_extension_gate(self):
        c, m = await self._run("quiz.exe", "text/plain")
        self.assertEqual(c.downloads, 0)
        self.assertIn("Supported", m.replies[0])
        c, m = await self._run("quiz.JSON", "application/octet-stream")
        self.assertEqual(c.downloads, 0)
        self.assertIn("Paste", m.replies[0])

    async def test_case_insensitive_accepted_extensions_pass_gate(self):
        for name in ("quiz.TXT", "quiz.MD", "quiz.MARKDOWN", "quiz.PDF"):
            c, m = await self._run(name, payload=b"garbage not a quiz")
            self.assertEqual(c.downloads, 1, name)
            all_text = m.replies + [s.text for s in m.statuses]
            self.assertFalse(
                any("Supported files" in t for t in all_text),
                (name, all_text))

    async def test_image_document_still_routed_to_ocr_flow(self):
        c, m = await self._run("qs.png", "image/png", payload=b"not-an-image")
        self.assertEqual(c.downloads, 1)
        all_text = m.replies + [s.text for s in m.statuses]
        self.assertFalse(
            any("Supported files" in t for t in all_text), all_text)


class PastedJsonFlowTests(IsolatedAsyncioTestCase):
    """The JSON flow survives the attachment ban via pasted text."""

    class _Msg:
        def __init__(self, text):
            self.text = text
            self.reply_to_message = None
            self.photo = None

            class _U:
                id = 5002
            self.from_user = _U()
            self.replies = []

        async def reply(self, text):
            self.replies.append(text)

    def setUp(self):
        state.quiz_creation[5002] = {"questions": []}

    def tearDown(self):
        state.quiz_creation.pop(5002, None)

    async def test_pasted_json_imports(self):
        payload = (
            '{"questions": [{"question_text": "Cap of Italy?", '
            '"options": [{"id": 1, "text": "Rome"}, '
            '{"id": 2, "text": "Milan"}], "correct_option_id": 1}]}')
        m = self._Msg(payload)
        await quiz_creation.handle_creation_message(None, m)
        ud = state.quiz_creation[5002]
        self.assertEqual(len(ud["questions"]), 1, m.replies)
        self.assertEqual(ud["questions"][0]["correct_option_id"], 0)
        self.assertIn("saved", m.replies[0])

    async def test_brace_prose_falls_through_to_text_parser(self):
        m = self._Msg("{important} Which option?\nA) x\nB) y ✅\nC) z\nD) w")
        await quiz_creation.handle_creation_message(None, m)
        ud = state.quiz_creation[5002]
        self.assertEqual(len(ud["questions"]), 1, m.replies)
        self.assertTrue(ud["questions"][0]["question"].startswith("{important}"))

    async def test_pasted_text_applies_remove_words(self):
        class _Repo:
            async def get_or_create(self, uid):
                return {"remove_words": ["Telegram"]}

        body = ("Telegram quiz: which?\nA) Telegram x\nB) y ✅\n"
                "C) z\nD) w")
        m = self._Msg(body)
        with unittest.mock.patch.object(quiz_creation, "get_db", return_value=None), \
             unittest.mock.patch.object(quiz_creation, "UserRepository",
                                        return_value=_Repo()):
            await quiz_creation.handle_creation_message(None, m)
        ud = state.quiz_creation[5002]
        self.assertEqual(len(ud["questions"]), 1, m.replies)
        q = ud["questions"][0]
        self.assertNotIn("Telegram", q["question"])
        self.assertNotIn("Telegram", q["options"][0])


class PublicJsonLinkTests(IsolatedAsyncioTestCase):
    """Public-link fetch: JSON share payloads use the structured flow and
    ordinary text pages use the canonical parser (both size-counted)."""

    def _fake_aiohttp(self, body, ctype):
        import sys
        import types

        class _Resp:
            status = 200
            headers = {"content-type": ctype}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def content_iter(self):
                for i in range(0, len(body), 37):
                    yield body[i:i + 37]

            @property
            def content(self):
                outer = self

                class _Iter:
                    def iter_chunked(self, n):
                        return outer.content_iter()
                return _Iter()

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, allow_redirects=None):
                return _Resp()

        fake = types.ModuleType("aiohttp")
        fake.ClientSession = _Session
        fake.ClientTimeout = lambda **k: None
        return unittest.mock.patch.dict(sys.modules, {"aiohttp": fake})

    async def test_json_link_payload_is_structured_parsed(self):
        import json as _json
        payload = {
            "questions": [
                {"question_text": "Capital of Japan?", "options": [
                    {"id": "a", "text": "Osaka"}, {"id": "b", "text": "Tokyo"},
                    {"id": "c", "text": "Kyoto"}],
                 "correct_option_id": "b", "explanation": "Tokyo."}]}
        body = _json.dumps(payload).encode()
        with self._fake_aiohttp(body, "application/json"):
            out: list = []
            count, error, report = await file_import.process_public_url(
                "https://example.com/quiz.json", out, [])
        self.assertIsNone(error, error)
        self.assertEqual(count, 1)
        self.assertEqual(out[0]["correct_option_id"], 1)
        self.assertEqual(out[0]["explanation"], "Tokyo.")

    async def test_chunked_text_link_still_parses(self):
        body = (
            "First?\nA) one\nB) two ✅\nC) three\nD) four\n\n"
            "Second?\nA) one\nB) two\nC) three ✅\nD) four\n").encode()
        with self._fake_aiohttp(body, "text/plain"):
            out: list = []
            count, error, report = await file_import.process_public_url(
                "https://example.com/quiz.txt", out, [])
        self.assertIsNone(error, error)
        self.assertEqual(count, 2)
        self.assertEqual([q["correct_option_id"] for q in out], [1, 2])


# ---------------------------------------------------------------------------
# A1 — answer-marker forms
# ---------------------------------------------------------------------------
class AnswerFormTests(unittest.TestCase):
    BASE = ["Q stem?", "A) x", "B) y", "C) z", "D) w"]

    def _co(self, answer_line):
        r = parse_one("\n".join(self.BASE + [answer_line]))
        self.assertIsNotNone(r.question, f"{answer_line}: {r.reason}")
        return r.question["correct_option_id"]

    def test_dot_colon_lowercase_labels(self):
        self.assertEqual(parse_one("Q?\na. x\nb. y ✅\nc. z\nd. w").question["correct_option_id"], 1)
        self.assertEqual(self._co("Ans: b"), 1)
        self.assertEqual(self._co("ans: B"), 1)
        self.assertEqual(self._co("Correct Answer: C"), 2)
        self.assertEqual(self._co("Correct Option: D"), 3)
        self.assertEqual(self._co("Answer: b"), 1)
        self.assertEqual(self._co("Answer: 2"), 1)
        self.assertEqual(self._co("Answer: (B)"), 1)
        self.assertEqual(self._co("Answer: option B"), 1)
        self.assertEqual(self._co("Answer: choice C"), 2)
        self.assertEqual(self._co("उत्तर: B"), 1)
        self.assertEqual(self._co("सही उत्तर: C"), 2)

    def test_hindi_answer_labels_devanagari_options(self):
        text = ("भारत की राजधानी?\nक) मुंबई\nख) दिल्ली\nग) कोलकाता\nघ) चेन्नई\nउत्तर: ख")
        r = parse_one(text)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["options"][1], "दिल्ली")
        self.assertEqual(r.question["correct_option_id"], 1)

    def test_checkmark_before_and_after(self):
        a = parse_one("Q?\nA) x\nB) ✅ y\nC) z\nD) w")
        b = parse_one("Q?\nA) x\nB) y ✅\nC) z\nD) w")
        self.assertEqual(a.question["correct_option_id"], 1)
        self.assertEqual(b.question["correct_option_id"], 1)

    def test_trailing_punctuation_tolerated(self):
        self.assertEqual(self._co("Answer: B."), 1)
        self.assertEqual(self._co("Answer: b)"), 1)

    def test_checkmark_and_answer_agree_is_fine(self):
        r = parse_one("Q?\nA) x\nB) y ✅\nC) z\nD) w\nAnswer: B")
        self.assertIsNotNone(r.question)
        self.assertEqual(r.question["correct_option_id"], 1)


# ---------------------------------------------------------------------------
# B — conflicts / malformed never guessed
# ---------------------------------------------------------------------------
class ConflictTests(unittest.TestCase):
    def test_checkmark_vs_answer_conflict(self):
        r = parse_one("Q?\nA) x ✅\nB) y\nC) z\nD) w\nAnswer: B")
        self.assertIsNone(r.question)
        self.assertEqual(r.reason, parsing.RE_CONFLICTING_ANSWERS)

    def test_multiple_checkmarks_single_answer_engine(self):
        r = parse_one("Q?\nA) x ✅\nB) y ✅\nC) z\nD) w")
        self.assertEqual(r.reason, parsing.RE_MULTIPLE_CORRECT_UNSUPPORTED)

    def test_repeated_answer_lines_disagree(self):
        r = parse_one("Q?\nA) x\nB) y\nC) z ✅\nD) w\nAnswer: B\nAnswer: C")
        self.assertIsNone(r.question)
        self.assertIn(r.reason, (parsing.RE_CONFLICTING_ANSWERS,))
        r2 = parse_one("Q?\nA) x\nB) y\nC) z\nD) w\nAnswer: B\nAnswer: C")
        self.assertIsNone(r2.question)
        self.assertEqual(r2.reason, parsing.RE_CONFLICTING_ANSWERS)

    def test_repeated_answer_lines_agree(self):
        r = parse_one("Q?\nA) x\nB) y\nC) z\nD) w\nAnswer: B\nAnswer: (B)")
        self.assertIsNotNone(r.question)
        self.assertEqual(r.question["correct_option_id"], 1)

    def test_answer_out_of_range_letter(self):
        for bad in ("Answer: F", "Answer: 9"):
            r = parse_one(f"Q?\nA) x\nB) y\nC) z\nD) w\n{bad}")
            self.assertIsNone(r.question, bad)
            self.assertEqual(r.reason, parsing.RE_ANSWER_OUT_OF_RANGE, bad)

    def test_missing_answer(self):
        r = parse_one("Q?\nA) x\nB) y\nC) z\nD) w")
        self.assertEqual(r.reason, parsing.RE_MISSING_ANSWER)

    def test_insufficient_options(self):
        r = parse_one("Q?\nA) x\nAnswer: A")
        self.assertEqual(r.reason, parsing.RE_INSUFFICIENT_OPTIONS)

    def test_unrecognized_answer_token(self):
        r = parse_one("Q?\nA) x\nB) y\nC) z\nD) w\nAnswer: banana")
        self.assertIsNone(r.question)
        self.assertEqual(r.reason, parsing.RE_UNRECOGNIZED_ANSWER)

    def test_partial_document_does_not_falsely_report_success(self):
        doc = (
            "Good one?\nA) x\nB) y ✅\nC) z\nD) w\n\n"
            "Broken one?\nA) x\nB) y\nC) z\nD) w\n")
        res = parse_doc(doc)
        self.assertEqual(res.processed, 1)
        self.assertEqual(len(res.skipped), 1)
        self.assertEqual(res.skipped[0].reason, parsing.RE_MISSING_ANSWER)

    def test_importer_report_has_skipped_summary(self):
        body = ("Good one?\nA) x\nB) y ✅\nC) z\nD) w\n\n"
                "Broken one?\nA) x\nB) y\nC) z\nD) w\n")
        count, error, report, out = import_bytes(body.encode(), "q.txt")
        self.assertEqual(count, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertIn("no answer", report["skipped_summary"])

    def test_all_rejected_returns_zero_with_reasons_no_success(self):
        body = "Broken one?\nA) x\nB) y\nC) z\nD) w\n"
        count, error, report, out = import_bytes(body.encode(), "q.txt")
        self.assertEqual(count, 0)
        self.assertEqual(out, [])
        self.assertTrue(report["error_summary"])


# ---------------------------------------------------------------------------
# A1 — explanations / multiline structures / segmentation
# ---------------------------------------------------------------------------
class StructureTests(unittest.TestCase):
    def test_explanation_labels_and_multiline(self):
        for label, expect in (
                ("Solution:", "line one\nline two"),
                ("Explanation:", "because y"),
                ("Ex:", "short"),
                ("व्याख्या:", "सही विकल्प दूसरा है।"),
                ("समाधान:", "यह सही है।")):
            text = f"Q?\nA) x\nB) y ✅\nC) z\nD) w\n{label} {expect}"
            r = parse_one(text)
            self.assertIsNotNone(r.question, f"{label}: {r.reason}")
            self.assertIn(expect.split("\n")[0], r.question["explanation"])

    def test_extra_details_and_hindi_extra(self):
        r = parse_one("Q?\nA) x\nB) y ✅\nC) z\nD) w\n"
                      "Explanation: base\nExtra details:\n- point one\n- point two")
        self.assertIn("point one", r.question["explanation"])
        r = parse_one("Q?\nA) x\nB) y ✅\nC) z\nD) w\n"
                      "Explanation: base\nअतिरिक्त जानकारी: और विवरण")
        self.assertIn("और विवरण", r.question["explanation"])

    def test_next_question_not_absorbed_into_explanation(self):
        doc = parse_doc(
            "Q1 first?\nA) a\nB) b ✅\nC) c\nD) d\nExplanation: first reason\n\n"
            "Q2 second?\nA) a\nB) b\nC) c ✅\nD) d\nExplanation: second reason")
        self.assertEqual(doc.processed, 2)
        self.assertIn("first reason", doc.questions[0]["explanation"])
        self.assertIn("second reason", doc.questions[1]["explanation"])

    def test_multiline_question(self):
        r = parse_one("This is the stem\ncontinued on two lines?\nA) x\nB) y ✅\nC) z\nD) w")
        self.assertIn("continued on two lines", r.question["question"])

    def test_blank_line_between_stem_and_options_joins(self):
        body = ("Long stem that spans\nan introductory line?\n\n"
                "A) x\nB) y ✅\nC) z\nD) w")
        doc = parse_doc(body)
        self.assertEqual(doc.processed, 1, [s.detail for s in doc.skipped])
        self.assertIn("introductory line", doc.questions[0]["question"])
        self.assertEqual(len(doc.questions[0]["options"]), 4)
        body += "\n\nSecond?\nA) p\nB) q ✅\nC) r\nD) s"
        doc = parse_doc(body)
        self.assertEqual(doc.processed, 2, [s.detail for s in doc.skipped])

    def test_up_to_ten_options_and_limit(self):
        q10 = "Ten?\n" + "\n".join(f"{chr(65 + i)}) opt{i}" for i in range(10)) + "\nAnswer: J"
        doc = parse_doc(q10)
        self.assertEqual(doc.processed, 1, [s.detail for s in doc.skipped])
        self.assertEqual(len(doc.questions[0]["options"]), 10)
        self.assertEqual(doc.questions[0]["correct_option_id"], 9)
        q11 = "Eleven?\n" + "\n".join(
            f"{chr(65 + i)}) opt{i}" for i in range(11)) + "\nAnswer: K"
        doc = parse_doc(q11)
        self.assertEqual(doc.processed, 0)
        self.assertEqual(doc.skipped[0].reason, parsing.RE_TOO_MANY_OPTIONS)

    def test_multiline_options_join_not_fifth_option(self):
        r = parse_one("Q?\nA) first option\n   wrapped continuation\nB) second ✅\nC) third\nD) fourth")
        self.assertIsNotNone(r.question)
        self.assertEqual(len(r.question["options"]), 4)
        self.assertIn("wrapped continuation", r.question["options"][0])

    def test_numbered_statements_not_confused_with_question(self):
        stem = ("Consider the following statements:\n"
                "(1) Statement one.\n(2) Statement two.\n"
                "Which is/are correct?\nA) 1 only\nB) 2 only\n"
                "C) Both 1 and 2 ✅\nD) Neither")
        r = parse_one(stem)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["correct_option_id"], 2)
        self.assertIn("(1)", r.question["question"])
        self.assertEqual(len(r.question["options"]), 4)

    def test_assertion_reason(self):
        stem = ("Assertion (A): Sky is blue.\nReason (R): Rayleigh scattering.\n"
                "A) Both A and R are true and R explains A ✅\n"
                "B) Both true, R does not explain\nC) A true R false\nD) Both false")
        r = parse_one(stem)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertIn("Assertion", r.question["question"])

    def test_chronology_numbered_choices(self):
        stem = ("Arrange chronologically:\n1. Event A\n2. Event B\n3. Event C\n"
                "A) 1, 2, 3 ✅\nB) 2, 1, 3\nC) 3, 1, 2\nD) 1, 3, 2")
        r = parse_one(stem)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertEqual(len(r.question["options"]), 4)

    def test_number_inside_option_text_safe(self):
        stem = "Which year?\nA) It happened in 1947 ✅\nB) 1950\nC) 1962\nD) 1971"
        r = parse_one(stem)
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertEqual(len(r.question["options"]), 4)

    def test_continuous_packed_questions(self):
        text = ("Q1. First?\nA) x\nB) y ✅\nC) z\nD) w\n"
                "Q2. Second?\nA) p\nB) q\nC) r ✅\nD) s\n"
                "Q3. Third?\nA) m\nB) n\nC) o\nD) t ✅")
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 3, [s.detail for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 2, 3])

    def test_matching_pairs_style_safe(self):
        stem = ("Match the rivers (List I) to cities (List II):\n"
                "List I: 1. Ganga 2. Yamuna\nList II: A. Delhi B. Varanasi\n"
                "A) 1-B, 2-A ✅\nB) 1-A, 2-B\nC) 1-B only\nD) 2-A only")
        r = parse_one(stem)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertEqual(len(r.question["options"]), 4)
        self.assertIn("Ganga", r.question["question"])

    def test_question_number_mentioned_in_explanation_is_not_a_new_block(self):
        text = (
            "Q1. First stem?\nA) x\nB) y ✅\nC) z\nD) w\n"
            "Explanation: recall Q1 of chapter 1; compare Q2 below.\n"
            "Q2. Second stem?\nA) p\nB) q\nC) r ✅\nD) s\n"
            "Explanation: see Q1 again for context.")
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 2, [s.detail for s in doc.skipped])
        self.assertIn("compare Q2 below", doc.questions[0]["explanation"])
        self.assertIn("see Q1 again", doc.questions[1]["explanation"])

    def test_qdot_and_q_space_variants(self):
        for head in ("Q.5", "Q 5", "Q5."):
            text = f"{head} Stem here?\nA) x\nB) y ✅\nC) z\nD) w"
            doc = parse_doc(text)
            self.assertEqual(doc.processed, 1, f"{head}: {[s.detail for s in doc.skipped]}")
            self.assertFalse(doc.questions[0]["question"].lower().startswith("q"),
                             f"{head}: badge not stripped")

    def test_source_reference_not_treated_as_answer(self):
        r = parse_one("Q?\nA) x\nB) y ✅\nC) z\nD) w\n"
                      "Source: https://example.com/book\nExplanation: ok")
        self.assertIsNotNone(r.question)
        self.assertEqual(r.question["explanation"], "ok")


# ---------------------------------------------------------------------------
# A2 — separate answer-key layouts
# ---------------------------------------------------------------------------
class AnswerKeyAssociationTests(unittest.TestCase):
    QUESTIONS = (
        "1. Capital of India?\nA) Mumbai\nB) Delhi\nC) Kolkata\nD) Chennai\n"
        "2. Sun rises in the?\nA) West\nB) East\nC) North\nD) South\n"
        "3. 2+2?\nA) 3\nB) 4\nC) 5\nD) 6")

    def test_compact_rows(self):
        text = self.QUESTIONS + "\n\nANSWER KEY\n1-B\n2-B\n3-B"
        doc = parse_doc(text)
        self.assertTrue(doc.answer_key_detected)
        self.assertEqual(doc.processed, 3, [s.detail for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 1, 1])

    def test_dot_rows_with_parentheses(self):
        text = self.QUESTIONS + "\n\nAnswers\n1. (b)\n2. (b)\n3. (b)"
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 3, [s.detail for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 1, 1])

    def test_grid_layout(self):
        text = self.QUESTIONS + "\n\nAnswers\n1 2 3\nB B A"
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 3, [s.detail for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 1, 0])

    def test_solutions_section_carries_explanations(self):
        text = self.QUESTIONS + (
            "\n\nANSWER KEY & SOLUTIONS\n"
            "1. B\nExplanation: Delhi is the capital.\n"
            "2. B\nExplanation: Rises in the east.\n"
            "3. B\nExplanation: 2+2 is 4.")
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 3, [s.detail for s in doc.skipped])
        for q, bit in zip(doc.questions, ("Delhi", "east", "2+2 is 4")):
            self.assertIn(bit, q["explanation"])

    def test_inline_pairs_on_lines(self):
        text = self.QUESTIONS + "\n\nANSWERS\nQ1-B, Q2-B\nQ3-A"
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 3, [s.detail for s in doc.skipped])
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 1, 0])

    def test_inline_answer_agrees_with_key(self):
        text = ("1. Q one?\nA) a\nB) b ✅\nC) c\nD) d\n"
                "2. Q two?\nA) a\nB) b\nC) c ✅\nD) d\n\nANSWER KEY\n1-B\n2-C")
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 2)
        self.assertEqual([q["correct_option_id"] for q in doc.questions], [1, 2])

    def test_inline_answer_conflicts_with_key(self):
        text = ("1. Q one?\nA) a ✅\nB) b\nC) c\nD) d\n"
                "2. Q two?\nA) a\nB) b\nC) c ✅\nD) d\n\nANSWER KEY\n1-C\n2-C")
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 1)
        self.assertEqual(doc.questions[0]["correct_option_id"], 2)
        self.assertTrue(any(s.reason == parsing.RE_CONFLICTING_ANSWERS
                            for s in doc.skipped))

    def test_unmappable_question_reported_not_guessed(self):
        text = ("1. Q one?\nA) a\nB) b\nC) c\nD) d\n"
                "2. Q two?\nA) a\nB) b\nC) c\nD) d\n\nANSWER KEY\n1-B\n9-A")
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 1)
        self.assertTrue(any(s.reason == parsing.RE_ANSWER_KEY_ASSOCIATION
                            for s in doc.skipped))

    def test_malformed_key_section_reported(self):
        text = self.QUESTIONS + "\n\nANSWER KEY\n(all the best! no letters here)"
        doc = parse_doc(text)
        self.assertEqual(doc.processed, 0)
        self.assertTrue(any(s.reason in (parsing.RE_MALFORMED_ANSWER_KEY,
                                         parsing.RE_ANSWER_KEY_ASSOCIATION)
                            for s in doc.skipped))


# ---------------------------------------------------------------------------
# A1 — unicode / bilingual
# ---------------------------------------------------------------------------
class UnicodeTests(unittest.TestCase):
    def test_hindi_heavy_document(self):
        body = (
            "भारत की राजधानी क्या है?\nक) मुंबई\nख) नई दिल्ली ✅\nग) कोलकाता\nघ) चेन्नई\n"
            "व्याख्या: नई दिल्ली राष्ट्रीय राजधानी है।\n\n"
            "स्थिति रिपोर्ट किस पंक्ति में है?\nक) पहली पंक्ति ✅\nख) दूसरी\nग) तीसरी किताब\nघ) कोई नहीं\n"
            "Explanation: यह स्थिति परीक्षण है।")
        doc = parse_doc(body)
        self.assertEqual(doc.processed, 2, [s.detail for s in doc.skipped])
        self.assertIn("नई दिल्ली", doc.questions[0]["options"][1])
        self.assertIn("राष्ट्रीय राजधानी", doc.questions[0]["explanation"])
        self.assertEqual(doc.questions[1]["correct_option_id"], 0)

    def test_mixed_hindi_english_math(self):
        body = ("If 2x + 4 = 12, तो x का मान क्या है?\nA) 2\nB) 4 ✅\nC) 6\nD) 8\n"
                "Explanation: 2x = 8, so x = 4.")
        r = parse_one(body)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["correct_option_id"], 1)

    def test_unicode_punctuation_and_spacing(self):
        self.assertEqual(parse_one("Q?\nA) x\nB) y ✅\nC) z\nD) w").question["correct_option_id"], 1)
        r = parse_one("प्रश्न?\nA) एक\nB) दो ✅\nC) तीन\nD) चार\nउत्तर : B")
        self.assertIsNotNone(r.question)

    def test_hindi_copyable_text_no_raw_cid_is_pdf_services_job(self):
        r = parse_one("किताब किस पंक्ति में?\nA) पहली किताब ✅\nB) दूसरी\nC) तीसरी\nD) चौथी")
        self.assertIn("किताब", r.question["question"])


# ---------------------------------------------------------------------------
# Regression — legacy structured RT:/ID: style and JSON path
# ---------------------------------------------------------------------------
class LegacyRegressionTests(unittest.TestCase):
    def test_rt_id_structured_block(self):
        block = (
            "RT: Reference text here\nID: AwXy123\n"
            "Question body?\nA) x\nB) y ✅\nC) z\nD) w")
        out: list[dict] = []
        n = file_import._process_txt(block + "\n\n", [], out)
        self.assertEqual(n, 1)
        self.assertEqual(out[0]["reply_text"], "Reference text here")
        self.assertEqual(out[0]["file_id"], "AwXy123")
        self.assertEqual(out[0]["correct_option_id"], 1)

    def test_json_simple_schema_still_imports(self):
        import json
        data = {"questions": [
            {"question_text": "Q one?", "options": [
                {"id": "a", "text": "x"}, {"id": "b", "text": "y"},
                {"id": "c", "text": "z"}, {"id": "d", "text": "w"}],
             "correct_option_id": "b", "explanation": "why"},
        ]}
        out: list[dict] = []
        skipped: list[dict] = []
        n = file_import._process_json(data, [], out, skipped)
        self.assertEqual(n, 1)
        self.assertEqual(out[0]["correct_option_id"], 1)
        self.assertEqual(out[0]["explanation"], "why")

    def test_json_bad_answer_id_reported_not_silent(self):
        data = {"questions": [
            {"question_text": "Q?", "options": [
                {"id": "a", "text": "x"}, {"id": "b", "text": "y"}],
             "correct_option_id": "zzz"},
        ]}
        out, skipped = [], []
        n = file_import._process_json(data, [], out, skipped)
        self.assertEqual(n, 0)
        self.assertEqual(skipped[0]["reason"], parsing.RE_ANSWER_OUT_OF_RANGE)


# ---------------------------------------------------------------------------
# D — robustness / security of the import surface
# ---------------------------------------------------------------------------
class RobustnessTests(unittest.TestCase):
    def test_corrupt_pdf_does_not_crash_or_pretend(self):
        out: list = []
        count, error, report = file_import.process_uploaded_file(
            b"%PDF-1.5\ntotally broken bytes", "broken.pdf", out, [])
        self.assertEqual(out, [])
        self.assertNotEqual(count, 0)
        self.assertTrue(error)
        self.assertNotIn("Traceback", error)

    def test_non_utf8_text_file_friendly_error(self):
        out: list = []
        count, error, report = file_import.process_uploaded_file(
            b"\xff\xfe\x00Q", "quiz.txt", out, [])
        self.assertIsNone(count)
        self.assertIn("UTF-8", error)

    def test_oversized_upload_rejected_without_parsing(self):
        out: list = []
        big = b"A) x\n" * (6 * 1024 * 1040)
        self.assertGreater(len(big), 25 * 1024 * 1024)
        count, error, report = file_import.process_uploaded_file(
            big, "quiz.txt", out, [])
        self.assertIsNone(count)
        self.assertIn("25 MB", error)
        self.assertEqual(out, [])

    def test_path_traversal_filename_is_ignored(self):
        body = b"Q?\nA) x\nB) y \xe2\x9c\x85\nC) z\nD) w"
        count, error, report = file_import.process_uploaded_file(
            body, "../../../../etc/passwd.txt", [], [])
        self.assertEqual(count, 1)
        self.assertIsNone(error)

    def test_oversized_repeated_blocks_bounded_and_reported(self):
        blocks = []
        for i in range(1, 301):
            if i % 3:
                blocks.append(f"Q{i}?\nA) x\nB) y ✅\nC) z\nD) w")
            else:
                blocks.append(f"Q{i}?\nA) x\nB) y\nC) z\nD) w")
        count, error, report, out = import_bytes(
            ("\n\n".join(blocks)).encode(), "big.txt")
        self.assertEqual(count, 200)
        self.assertEqual(len(report["skipped"]), 100)
        self.assertTrue(all(s["reason"] == parsing.RE_MISSING_ANSWER
                            for s in report["skipped"]))

    def test_injection_content_stays_literal(self):
        body = ("<img src=x onerror=alert(1)>?\nA) safe\nB) hit ✅\n"
                "C) ${jndi:ldap://x}\nD) `rm -rf`")
        res = parse_doc(body)
        self.assertEqual(res.processed, 1)
        q = res.questions[0]
        self.assertIn("<img src=x onerror=alert(1)>?", q["question"])
        self.assertIn("${jndi:ldap://x}", q["options"][2])
        self.assertIn("rm -rf", q["options"][3])

    def test_malformed_unicode_does_not_crash_parser(self):
        for junk in ("\u202e", "\x00", "\u200b", "\ud800"):
            body = f"Q{junk} stem?\nA) x\nB) y ✅\nC) z\nD) w"
            r = parse_doc(body)
            self.assertEqual(r.processed, 1, repr(junk))
            self.assertEqual(r.questions[0]["correct_option_id"], 1)

    def test_url_is_reference_not_answer(self):
        body = ("Q?\nA) x\nB) y ✅\nC) z\nD) w\n"
                "Reference: https://example.com/Answer-B-book\nAnswer: B")
        r = parse_one(body)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(r.question["correct_option_id"], 1)


# ---------------------------------------------------------------------------
# A/B — parser hardening edge cases found during self-review
# ---------------------------------------------------------------------------
class ParserHardeningTests(unittest.TestCase):
    def test_markdown_title_and_bullet_options(self):
        md = ("# Quiz\n\n**First stem?**\n- A) one\n- B) two ✅\n"
              "- C) three\n- D) four\n\n"
              "Second stem?\n* A) alpha\n* B) beta ✅\n* C) gamma\n* D) delta\n")
        out = []
        count, error, report = file_import.process_uploaded_file(
            md.encode(), "quiz.markdown", out, [])
        self.assertIsNone(error, report)
        self.assertEqual(count, 2)
        self.assertEqual(report["skipped"], [])
        self.assertEqual([q["correct_option_id"] for q in out], [1, 1])

    def test_numbered_statements_are_not_options(self):
        body = (
            "Consider the following statements about the legislature:\n"
            "(1) Lok Sabha can have up to 552 members.\n"
            "(2) Rajya Sabha is a permanent chamber.\n"
            "Choose the correct code.\n"
            "A) 1 only\nB) 2 only\nC) Both 1 and 2 ✅\nD) Neither\n")
        r = parse_one(body)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(len(r.question["options"]), 4)
        self.assertEqual(r.question["correct_option_id"], 2)
        self.assertIn("(1) Lok Sabha", r.question["question"])
        self.assertIn("(2) Rajya Sabha", r.question["question"])

    def test_numbered_statements_hindi_with_dot_labels(self):
        body = (
            "निम्न कथनों पर विचार करें:\n"
            "1. लोकसभा में 552 सदस्य हो सकते हैं।\n"
            "2. राज्यसभा स्थायी सदन है।\n"
            "क) केवल 1\nख) केवल 2\nग) दोनों\nघ) न तो 1 न ही 2 ✅\n")
        r = parse_one(body)
        self.assertIsNotNone(r.question, r.reason)
        self.assertEqual(len(r.question["options"]), 4)
        self.assertEqual(r.question["correct_option_id"], 3)
        self.assertIn("1. लोकसभा", r.question["question"])

    def test_ten_devanagari_labels_accepted_eleventh_rejected(self):
        labels = list("कखगघङचछजझञ")
        body = "दस विकल्पों वाला प्रश्न?\n" + "\n".join(
            f"{l}) विकल्प {i}" + (" ✅" if i == 3 else "")
            for i, l in enumerate(labels))
        r = parse_one(body)
        self.assertIsNone(r.reason, r.detail)
        self.assertEqual(len(r.question["options"]), 10)
        self.assertEqual(r.question["correct_option_id"], 3)
        body += "\nट) ग्यारहवाँ"
        r2 = parse_one(body)
        self.assertEqual(r2.reason, parsing.RE_TOO_MANY_OPTIONS)

    def test_check_mark_before_label(self):
        for prefix in ("✅ A) x", "- ✅ A) x", "✅A) x", "* ✅ A) x"):
            body = f"Q?\n{prefix}\nB) y\nC) z\nD) w\n"
            r = parse_one(body)
            self.assertIsNotNone(r.question, (prefix, r.reason, r.detail))
            self.assertEqual(r.question["correct_option_id"], 0, prefix)

    def test_check_mark_before_numeric_label(self):
        body = "Sum?\n✅ 1) three\n2) four\n3) five\n4) six\n"
        r = parse_doc(body)
        self.assertEqual(r.processed, 1)
        self.assertEqual(r.questions[0]["correct_option_id"], 0)

    def test_empty_option_is_rejected_not_silently_shifted(self):
        body = "Q?\nA)\nB) y ✅\nC) z\nD) w\n"
        r = parse_one(body)
        self.assertIsNone(r.question)
        self.assertEqual(r.reason, parsing.RE_INSUFFICIENT_OPTIONS)
        body2 = "Q?\nA)\nB)\nC) z ✅\nD) w\n"
        r2 = parse_one(body2)
        self.assertEqual(r2.reason, parsing.RE_INSUFFICIENT_OPTIONS)

    def test_assertion_reason_matching_chronology_forms(self):
        forms = [
            ("Assertion (A): A statement.\nReason (R): a reason.\n"
             "A) Both true and R explains A ✅\nB) Both true otherwise\n"
             "C) A true R false\nD) A false R true", 0),
            ("Match List-I with List-II:\nList-I: 1. Earth 2. Moon\n"
             "List-II: A. Satellite B. Planet\n"
             "A) 1-B, 2-A ✅\nB) 1-A, 2-B\nC) 1-B, 2-B\nD) none", 0),
            ("Arrange chronologically:\n1. Event one 2. Event two 3. Event three\n"
             "A) 1, 2, 3 ✅\nB) 2, 1, 3\nC) 3, 1, 2\nD) 1, 3, 2", 0),
            ("Bharat ki rajdhani?\nA) Mumbai\nB) Nai Dilli ✅\n"
             "C) Chennai\nD) Kolkata", 1),
        ]
        for body, expected in forms:
            r = parse_doc(body)
            self.assertEqual(r.processed, 1,
                             (body[:40], [(s.reason, s.detail) for s in r.skipped]))
            self.assertEqual(len(r.questions[0]["options"]), 4)
            self.assertEqual(r.questions[0]["correct_option_id"], expected)

    def test_nested_explanation_extra_details_hindi_source(self):
        body = (
            "Q?\nA) x\nB) y ✅\nC) z\nD) w\n"
            "Explanation: Main point.\nExtra details: supporting\n"
            "- nested bullet\n  - deeper bullet\n"
            "Source: https://example.com/book")
        r = parse_one(body)
        self.assertIsNotNone(r.question, r.detail)
        expl = r.question["explanation"]
        self.assertIn("Main point.", expl)
        self.assertIn("nested bullet", expl)
        self.assertIn("deeper bullet", expl)
        self.assertNotIn("example.com", expl)
        body_hi = (
            "कौन-सा?\nक) एक\nख) दो ✅\nग) तीन\nघ) चार\n"
            "व्याख्या: मुख्य बात।\nअतिरिक्त जानकारी: पूरक।\nस्रोत: कुछ पुस्तक")
        r_hi = parse_one(body_hi)
        self.assertEqual(r_hi.question["correct_option_id"], 1)
        self.assertIn("मुख्य बात", r_hi.question["explanation"])
        self.assertIn("पूरक", r_hi.question["explanation"])
        self.assertNotIn("स्रोत", r_hi.question["explanation"])

    def test_title_pages_are_ignored_not_reported_as_bad_questions(self):
        body = (
            "MOCK TEST\nGeneral Studies Paper\nTime: 2 hours\nMax marks: 200\n\n"
            "1. First real question?\nA) a\nB) b ✅\nC) c\nD) d")
        r = parse_doc(body)
        self.assertEqual(r.processed, 1, [(s.reason, s.detail) for s in r.skipped])
        self.assertEqual(r.skipped, [])
        self.assertEqual(r.questions[0]["correct_option_id"], 1)

    def test_bare_numeric_options_still_parse_when_no_letters(self):
        body = ("What is 2 + 2?\n1) 3\n2) 4 ✅\n3) 5\n4) 6\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 1, [(s.reason, s.detail) for s in r.skipped])
        q = r.questions[0]
        self.assertEqual([o.strip() for o in q["options"]], ["3", "4", "5", "6"])
        self.assertEqual(q["correct_option_id"], 1)

    def test_numeric_range_never_segments_questions(self):
        body = (
            "Which fiscal range is shown in the graph?\n"
            "A) 100-150\nB) 150-200 ✅\nC) 200-250\nD) 250-300\n\n"
            "The 1947-50 period is described by which term?\n"
            "A) term one\nB) term two ✅\nC) term three\nD) term four\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 2, [(s.reason, s.detail) for s in r.skipped])
        self.assertIn("150-200", r.questions[0]["options"][1])
        self.assertEqual(r.questions[1]["correct_option_id"], 1)

    def test_q_badge_without_separator_is_prose(self):
        body = ("Q12revenue forecast methodology is best described as?\n"
                "A) naive\nB) consensus ✅\nC) stochastic\nD) none\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 1, [(s.reason, s.detail) for s in r.skipped])
        self.assertTrue(r.questions[0]["question"].startswith("Q12revenue"))

    def test_answer_key_pair_in_prose_is_not_a_key_row(self):
        body = (
            "1. Which curve?\nA) IS\nB) LM ✅\nC) AD\nD) AS\n\n"
            "ANSWER KEY & SOLUTIONS\n"
            "Solution 1: In figure 2-B the money market clears on LM; "
            "see also chart 3-C for the demand side.\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 1, [(s.reason, s.detail) for s in r.skipped])
        self.assertEqual(r.questions[0]["correct_option_id"], 1)
        self.assertNotIn(2, [s.ordinal for s in r.skipped if s.ordinal])

    def test_multiline_explanation_across_blank_lines_kept_with_question(self):
        body = (
            "Which right is guaranteed by Article 21?\n"
            "A) Equality\nB) Property\nC) Life and liberty ✅\nD) Speech\n\n"
            "Explanation: Article 21 protects life and personal liberty.\n\n"
            "It applies to all persons, not only citizens, and requires\n"
            "a procedure established by law.\n\n"
            "Which article provides for President's rule?\n"
            "A) 352\nB) 356 ✅\nC) 360\nD) 370\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 2, [(s.reason, s.detail) for s in r.skipped])
        expl = r.questions[0]["explanation"]
        self.assertIn("Article 21 protects life", expl)
        self.assertIn("procedure established by law", expl)
        self.assertNotIn("356", expl)
        self.assertEqual(r.questions[1]["correct_option_id"], 1)

    def test_options_separated_from_stem_by_blank_line_rejoin(self):
        body = (
            "Which organelle performs photosynthesis?\n\n"
            "A) Mitochondrion\nB) Ribosome\nC) Chloroplast ✅\nD) Nucleus\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 1, [(s.reason, s.detail) for s in r.skipped])
        self.assertEqual(len(r.questions[0]["options"]), 4)
        self.assertEqual(r.questions[0]["correct_option_id"], 2)

    def test_solution_explanation_not_absorbed_into_next_question(self):
        body = (
            "Q1. First?\nA) x\nB) y ✅\nC) z\nD) w\n\n"
            "Q2. Second?\nA) x\nB) y\nC) z ✅\nD) w\n\n"
            "ANSWER KEY\n"
            "1. B The first answer follows from the setup; note in the\n"
            "   2-C pairing below how option ordering changes.\n"
            "2. C Second explanation line, which must not be attached to Q1.\n")
        r = parse_doc(body)
        self.assertEqual(r.processed, 2, [(s.reason, s.detail) for s in r.skipped])
        self.assertEqual(r.questions[0]["correct_option_id"], 1)
        self.assertEqual(r.questions[1]["correct_option_id"], 2)
        self.assertIn("first answer", (r.questions[0].get("explanation") or "").lower())
        self.assertNotIn("first answer",
                         (r.questions[1].get("explanation") or "").lower())
        self.assertIn("second explanation",
                      (r.questions[1].get("explanation") or "").lower())
        self.assertNotIn("second explanation",
                         (r.questions[0].get("explanation") or "").lower())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# A1c — special stem layouts (statements, Assertion-Reason, pairs, chronology)
# ---------------------------------------------------------------------------
class SpecialLayoutTests(unittest.TestCase):
    STATEMENT = (
        "Consider the following statements:\n"
        "(1) The Sun rises in the east.\n"
        "(2) The Moon emits its own light.\n"
        "Which of the above statements is/are correct?\n"
        "A) 1 only ✅\nB) 2 only\nC) Both 1 and 2\nD) Neither 1 nor 2")

    ASSERTION_REASON = (
        "Assertion (A): The earth revolves around the sun.\n"
        "Reason (R): Gravitational pull keeps planets in orbit.\n"
        "Codes:\n"
        "A) Both A and R are true and R is the correct explanation of A ✅\n"
        "B) Both A and R are true but R is not the correct explanation\n"
        "C) A is true but R is false\n"
        "D) A is false but R is true")

    PAIRS = (
        "Consider the following pairs:\n"
        "Pair 1 : Delhi - Capital\n"
        "Pair 2 : Mumbai - Finance hub\n"
        "Which of the pairs given above is/are correctly matched?\n"
        "A) Pair 1 only ✅\nB) Pair 2 only\nC) Both pairs\nD) Neither pair")

    CHRONOLOGY = (
        "Arrange the following events in chronological order:\n"
        "1) First war of independence\n"
        "2) Quit India movement\n"
        "3) Formation of the republic\n"
        "A) 1, 2, 3 ✅\nB) 2, 1, 3\nC) 3, 1, 2\nD) 1, 3, 2")

    def test_statement_question_parenthesised_numbers_are_stem(self):
        r = parse_one(self.STATEMENT)
        self.assertIsNotNone(r.question, (r.reason, r.detail))
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertEqual(len(r.question["options"]), 4)
        self.assertIn("(1)", r.question["question"])
        self.assertIn("(2)", r.question["question"])
        self.assertNotIn("emits", " ".join(r.question["options"]))

    def test_assertion_reason_layout(self):
        r = parse_one(self.ASSERTION_REASON)
        self.assertIsNotNone(r.question, (r.reason, r.detail))
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertEqual(len(r.question["options"]), 4)
        self.assertIn("Assertion", r.question["question"])
        self.assertIn("Reason", r.question["question"])

    def test_matching_pairs_layout(self):
        r = parse_one(self.PAIRS)
        self.assertIsNotNone(r.question, (r.reason, r.detail))
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertIn("Pair 1 :", r.question["question"])

    def test_chronology_numbered_stem_lines_not_options(self):
        r = parse_one(self.CHRONOLOGY)
        self.assertIsNotNone(r.question, (r.reason, r.detail))
        self.assertEqual(r.question["correct_option_id"], 0)
        self.assertEqual(r.question["options"],
                         ["1, 2, 3", "2, 1, 3", "3, 1, 2", "1, 3, 2"])
        self.assertIn("1) First war", r.question["question"])

    def test_multiline_option_wrap(self):
        body = ("Which long option?\n"
                "A) first option that\nwraps onto a second line\n"
                "B) second option ✅\nC) third\nD) fourth")
        r = parse_one(body)
        self.assertIsNotNone(r.question, (r.reason, r.detail))
        self.assertEqual(r.question["correct_option_id"], 1)
        self.assertIn("wraps onto a second line",
                      r.question["options"][0])

    def test_multiline_stem_then_options(self):
        body = ("This is a stem\nthat runs across several lines\n"
                "and keeps going.\nA) first ✅\nB) second\nC) third\nD) fourth")
        r = parse_one(body)
        self.assertIsNotNone(r.question, (r.reason, r.detail))
        self.assertIn("keeps going", r.question["question"])
        self.assertEqual(len(r.question["options"]), 4)
