"""Professional Test Series creation workflow (/newseries) tests.

Offline: no Telegram network, no MongoDB, no PDF service (API mocked).
Covers the TestSeriesConfig object, the button/text/photo wizard flow,
preview/edit/cancel/generate, user isolation, and bridge wiring.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _ensure_loop() -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed loop")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


_ensure_loop()

from quizbot.creator_bot import state as creator_state  # noqa: E402
from quizbot.creator_bot.handlers import testseries_create as tsc  # noqa: E402
from quizbot.creator_bot.handlers.quiz_creation import cancel_cmd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_UIDS = itertools.count(960000)
_MSGIDS = itertools.count(100)


def _uid():
    return next(_UIDS)


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 120
JPG = b"\xff\xd8\xff\xe0" + b"y" * 120
NOT_IMAGE = b"this is plain text, not an image file...."
MCQ_DOC = ("Q.1. What is 2+2?\nA) 3\nB) 4 \u2705\nC) 5\n"
           "Ex: Basic math.\n").encode()


def _quizzes(n=2):
    return [{"quiz_name": "f.txt", "questions": [
        {"question": "q%d" % i, "options": ["a", "b"],
         "correct_option_id": 0, "explanation": ""} for i in range(n)]}]


class _FakeBot:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.documents: list[tuple] = []
        self.deleted: list[int] = []

    async def get_file(self, file_id):
        parent = self

        class _F:
            async def download_as_bytearray(self):
                return bytearray(parent.files[file_id])

        return _F()

    async def send_message(self, chat_id, text, reply_markup=None,
                           parse_mode=None, disable_web_page_preview=False,
                           **k):
        self.sent.append((chat_id, text, reply_markup))
        return _FakeMsg(self, 777, text=text)

    async def edit_message_text(self, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None, **k):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return _FakeMsg(self, 777, text=text)

    async def send_document(self, chat_id, document, filename=None,
                            caption=None, reply_markup=None, parse_mode=None,
                            **k):
        data = document.read() if hasattr(document, "read") else document
        self.documents.append((chat_id, filename, caption, bytes(data)))
        return _FakeMsg(self, 777)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeClient:
    def __init__(self, bot):
        self._bot = bot

    async def download_media(self, file_id, in_memory=True):
        return io.BytesIO(self._bot.files[file_id])


class _FakeMsg:
    def __init__(self, bot, uid, text=None, document=None, photo=None):
        self._bot = bot
        self.message_id = next(_MSGIDS)
        self.chat = SimpleNamespace(id=777, type="private")
        self.chat_id = 777
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.caption = None
        self.reply_markup = None
        self.reply_to_message = None
        self.document = document
        self.photo = photo
        self.video = None
        self.poll = None
        self.date = None

    async def reply(self, text, reply_markup=None, **k):
        self._bot.sent.append((self.chat.id, text, reply_markup))
        return _FakeMsg(self._bot, self.from_user.id, text=text)

    async def edit_text(self, text, reply_markup=None, **k):
        self._bot.edits.append((self.message_id, text, reply_markup))
        self.text = text
        self.reply_markup = reply_markup
        return self

    async def reply_document(self, document, file_name=None, caption=None,
                             **k):
        data = document.read() if hasattr(document, "read") else document
        self._bot.documents.append((file_name, caption, bytes(data)))
        return self

    async def delete(self):
        self._bot.deleted.append(self.message_id)


class _FakeCB:
    def __init__(self, bot, uid, data):
        self._bot = bot
        self.id = "cb1"
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = _FakeMsg(bot, uid)
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **k):
        self.answers.append((text, show_alert))


def _datas(markup):
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _doc(name="qs.txt", size=200, mime="text/plain", fid="mcq1"):
    return SimpleNamespace(file_id=fid, file_name=name,
                           file_size=size, mime_type=mime)


def _photo(fid="img1", size=200):
    return SimpleNamespace(file_id=fid, file_size=size)


class _FlowBase(unittest.TestCase):
    def setUp(self):
        self.bot = _FakeBot()
        self.client = _FakeClient(self.bot)
        patcher = patch.object(tsc.config, "PDF_API_BASE", "http://pdf.test")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for store in (creator_state.testseries_create,
                      creator_state.testseries_upload,
                      creator_state.quiz_creation):
            for uid in list(store._data.keys()):
                if uid >= 960000:
                    store.pop(uid, None)

    def arun(self, coro):
        return asyncio.run(coro)

    def start(self, uid, args=""):
        return self.arun(tsc.newseries_cmd(
            self.client, _FakeMsg(self.bot, uid, text="/newseries " + args)))

    def tap(self, uid, code, value):
        cb = _FakeCB(self.bot, uid, "tsc_%s_%d_%s" % (code, uid, value))
        self.arun(tsc.creation_cb(self.client, cb))
        return cb

    def say(self, uid, text):
        return self.arun(tsc.handle_create_text(
            self.client, _FakeMsg(self.bot, uid, text=text)))

    def send_photo(self, uid, fid="img1", data=PNG, as_list=False):
        self.bot.files[fid] = data
        photo = _photo(fid, len(data))
        self.arun(tsc.handle_create_photo(
            self.client, _FakeMsg(self.bot, uid,
                                  photo=[photo] if as_list else photo)))

    def send_doc(self, uid, name="qs.txt", mime="text/plain", data=MCQ_DOC,
                 fid="mcq1"):
        self.bot.files[fid] = data
        self.arun(tsc.handle_create_document(
            self.client, _FakeMsg(self.bot, uid,
                                  document=_doc(name, len(data), mime, fid))))

    def seed(self, uid, step, config=None, quizzes=None):
        creator_state.testseries_create[uid] = {
            "step": step, "config": dict(config or {}), "assets": {},
            "quizzes": quizzes if quizzes is not None else _quizzes(2),
            "source": "file", "edit_return": False, "edit_section": None,
            "ts": 0.0}

    def step(self, uid):
        return creator_state.testseries_create[uid]["step"]

    def cfg(self, uid):
        return creator_state.testseries_create[uid]["config"]


class ConfigCases(unittest.TestCase):
    def test_defaults(self):
        cfg = tsc.TestSeriesConfig()
        self.assertEqual(cfg.marks_correct, 2.0)
        self.assertEqual(cfg.marks_negative, -0.66)
        self.assertTrue(cfg.answer_key)
        self.assertTrue(cfg.solutions)
        self.assertEqual(cfg.visuals, "auto")
        self.assertEqual(cfg.watermark_mode, "none")
        self.assertEqual(cfg.booklet_series, "A")
        self.assertEqual(cfg.test_number_mode, "auto")
        self.assertEqual(cfg.enabled_candidate_fields(), [])

    def _full(self):
        return tsc.TestSeriesConfig(
            title="T", subject="S", test_number_mode="manual",
            test_number="3", booklet_series="B", booklet_number_mode="manual",
            booklet_number="12", cand_name=True, institute_name="I",
            watermark_mode="both", watermark_text="W", wm_image_present=True,
            tagline="TG", total_questions=5, max_marks=10.0)

    def test_validate_ok_full(self):
        self.assertEqual(self._full().validate(), [])

    def test_validate_catches_missing(self):
        cfg = tsc.TestSeriesConfig()
        problems = cfg.validate()
        self.assertIn("Title is missing.", problems)
        self.assertIn("Institute name is missing.", problems)
        self.assertIn("No questions attached.", problems)
        cfg.test_number_mode = "manual"
        self.assertIn("Manual test number is missing.", cfg.validate())
        cfg.booklet_number_mode = "manual"
        self.assertIn("Manual booklet number is missing.", cfg.validate())
        cfg.watermark_mode = "both"
        problems = cfg.validate()
        self.assertIn("Watermark text is missing.", problems)
        self.assertIn("Watermark image is missing.", problems)

    def test_validate_marks_ranges(self):
        cfg = self._full()
        for bad in (0.0, -1.0, 101.0):
            cfg.marks_correct = bad
            self.assertTrue(any("Correct-answer" in p for p in cfg.validate()))
        cfg.marks_correct = 2.0
        for bad in (0.5, -101.0):
            cfg.marks_negative = bad
            self.assertTrue(any("Negative" in p for p in cfg.validate()))

    def test_to_dict_json_safe(self):
        cfg = self._full()
        data = cfg.to_dict()
        json.dumps(data)  # must not raise
        self.assertEqual(data["candidate_fields"], ["Candidate Name"])
        self.assertNotIn("logo", data)

    def test_derive_totals(self):
        quizzes = [{"questions": [
            {"options": ["a", "b"]}, {"options": []}, {"options": ["", None]}]}]
        total, maximum = tsc._derive_totals(quizzes, 2.0)
        self.assertEqual((total, maximum), (1, 2.0))
        total, maximum = tsc._derive_totals(_quizzes(3), 0.5)
        self.assertEqual((total, maximum), (3, 1.5))

    def test_watermark_four_states(self):
        base = dict(title="T", institute_name="I", total_questions=1)
        modes = {
            "none": {},
            "text": {"watermark_text": "W"},
            "image": {"wm_image_present": True},
            "both": {"watermark_text": "W", "wm_image_present": True},
        }
        for mode, extra in modes.items():
            cfg = tsc.TestSeriesConfig(watermark_mode=mode, **base, **extra)
            self.assertEqual(cfg.validate(), [], mode)

    def test_summary_escapes_user_text(self):
        cfg = tsc.TestSeriesConfig(title="A_B*C", institute_name="I",
                                   booklet_series="B", total_questions=2,
                                   max_marks=4.0)
        text = tsc._render_preview(cfg)
        self.assertIn("A\\_B\\*C", text)
        for bit in ("Questions:", "Max marks:", "Key:", "Solutions:",
                    "Visuals:", "Watermark:", "Candidate boxes:"):
            self.assertIn(bit, text)


class EntryCases(_FlowBase):
    def test_bare_start_asks_intake(self):
        uid = _uid()
        self.start(uid)
        self.assertEqual(self.step(uid), "intake")
        _chat, text, markup = self.bot.sent[-1]
        self.assertIn("New Test Series", text)
        datas = _datas(markup)
        self.assertIn("tsc_in_%d_file" % uid, datas)
        self.assertIn("tsc_in_%d_qids" % uid, datas)

    def test_guards(self):
        uid = _uid()
        with patch.object(tsc.config, "PDF_API_BASE", ""):
            self.start(uid)
        self.assertNotIn(uid, creator_state.testseries_create)
        self.assertIn("not configured", self.bot.sent[-1][1])
        with patch.object(tsc, "is_premium_user",
                          AsyncMock(return_value=False)):
            self.start(_uid())
        self.assertIn("Premium feature", self.bot.sent[-1][1])

    def test_entry_blocked_inside_other_flow(self):
        uid = _uid()
        creator_state.testseries_upload[uid] = {"step": "awaiting_file"}
        self.start(uid)
        self.assertNotIn(uid, creator_state.testseries_create)
        self.assertIn("/cancel", self.bot.sent[-1][1])

    def test_restart_discards_previous_draft(self):
        uid = _uid()
        self.start(uid)
        self.tap(uid, "in", "qids")
        self.start(uid)
        self.assertEqual(self.step(uid), "intake")
        self.assertEqual(self.cfg(uid), {})
        self.assertIn("fresh setup", self.bot.sent[-2][1])


class IntakeCases(_FlowBase):
    def _repo(self, quizzes):
        class _R:
            async def get(self, qid):
                return quizzes.get(qid)
        return _R()

    def test_file_intake_ok(self):
        uid = _uid()
        self.start(uid)
        self.tap(uid, "in", "file")
        self.assertEqual(self.step(uid), "intake_file")
        self.send_doc(uid)
        self.assertEqual(self.step(uid), "title")
        sess = creator_state.testseries_create[uid]
        self.assertEqual(len(sess["quizzes"][0]["questions"]), 1)
        self.assertEqual(sess["source"], "file")

    def test_file_intake_bad_stays(self):
        uid = _uid()
        self.start(uid)
        self.tap(uid, "in", "file")
        self.send_doc(uid, data=b"Q.1. No options here\n")
        self.assertEqual(self.step(uid), "intake_file")
        self.assertIn(uid, creator_state.testseries_create)

    def test_wrong_ext_rejected(self):
        uid = _uid()
        self.start(uid)
        self.tap(uid, "in", "file")
        self.send_doc(uid, name="q.zip", mime="application/zip", data=b"xx")
        self.assertEqual(self.step(uid), "intake_file")

    def test_qids_intake_with_mocks(self):
        uid = _uid()
        quizzes = {"G1": {"qid": "G1", "quiz_name": "One", "creator_id": uid,
                           "questions": [{"question": "q", "options": ["a", "b"],
                                          "correct_option_id": 0}]},
                   "G9": {"qid": "G9", "quiz_name": "Alien", "creator_id": 1,
                          "questions": [{"question": "q", "options": ["a"]}]}}
        with patch.object(tsc, "QuizRepository", lambda *a: self._repo(quizzes)), \
             patch.object(tsc, "get_db", lambda: None):
            self.start(uid)
            self.tap(uid, "in", "qids")
            self.assertEqual(self.step(uid), "intake_qids")
            self.say(uid, "G1 G9 GONE")
        self.assertEqual(self.step(uid), "title")
        sess = creator_state.testseries_create[uid]
        self.assertEqual(len(sess["quizzes"]), 1)
        self.assertIn("Skipped", self.bot.sent[-2][1])

    def test_qids_empty_and_invalid_stay(self):
        uid = _uid()
        with patch.object(tsc, "QuizRepository", lambda *a: self._repo({})), \
             patch.object(tsc, "get_db", lambda: None):
            self.start(uid)
            self.tap(uid, "in", "qids")
            self.say(uid, "   ")
            self.assertEqual(self.step(uid), "intake_qids")
            self.say(uid, "NOPE")
            self.assertEqual(self.step(uid), "intake_qids")

    def test_command_qids_and_title_prefill(self):
        uid = _uid()
        quizzes = {"G1": {"qid": "G1", "quiz_name": "One", "creator_id": uid,
                           "questions": [{"question": "q", "options": ["a", "b"],
                                          "correct_option_id": 0}]}}
        with patch.object(tsc, "QuizRepository", lambda *a: self._repo(quizzes)), \
             patch.object(tsc, "get_db", lambda: None):
            self.start(uid, "G1 title=My_Mock")
        self.assertEqual(self.step(uid), "subject")
        self.assertEqual(self.cfg(uid)["title"], "My Mock")

    def test_command_bad_qids_no_session(self):
        uid = _uid()
        with patch.object(tsc, "QuizRepository", lambda *a: self._repo({})), \
             patch.object(tsc, "get_db", lambda: None):
            self.start(uid, "NOPE")
        self.assertNotIn(uid, creator_state.testseries_create)


class ButtonStepCases(_FlowBase):
    def test_subject_buttons_and_custom_text(self):
        uid = _uid()
        self.seed(uid, "subject")
        cb = self.tap(uid, "sj", "s2")
        self.assertEqual(self.cfg(uid)["subject"], "Polity")
        self.assertEqual(self.step(uid), "test_number")
        self.assertEqual(cb.answers, [(None, False)])
        uid2 = _uid()
        self.seed(uid2, "subject")
        self.say(uid2, "Ancient History")
        self.assertEqual(self.cfg(uid2)["subject"], "Ancient History")
        uid3 = _uid()
        self.seed(uid3, "subject")
        cb = self.tap(uid3, "sj", "s9")
        self.assertEqual(self.step(uid3), "subject")
        self.assertTrue(cb.answers[0][1])

    def test_test_number_auto_and_manual(self):
        uid = _uid()
        self.seed(uid, "test_number")
        self.tap(uid, "tn", "auto")
        self.assertEqual(self.cfg(uid)["test_number_mode"], "auto")
        self.assertEqual(self.step(uid), "booklet_series")
        uid2 = _uid()
        self.seed(uid2, "test_number")
        self.tap(uid2, "tn", "manual")
        self.assertEqual(self.step(uid2), "test_number_input")
        self.say(uid2, "   ")
        self.assertEqual(self.step(uid2), "test_number_input")
        self.say(uid2, "7")
        self.assertEqual(self.cfg(uid2)["test_number"], "7")
        self.assertEqual(self.step(uid2), "booklet_series")

    def test_booklet_series_all(self):
        for value, nxt in (("A", "booklet_number"), ("D", "booklet_number")):
            uid = _uid()
            self.seed(uid, "booklet_series")
            self.tap(uid, "bs", value)
            self.assertEqual(self.cfg(uid)["booklet_series"], value)
            self.assertEqual(self.step(uid), nxt)
        uid = _uid()
        self.seed(uid, "booklet_series")
        self.tap(uid, "bs", "custom")
        self.assertEqual(self.step(uid), "booklet_series_input")
        self.say(uid, "")
        self.assertEqual(self.step(uid), "booklet_series_input")
        self.say(uid, "E")
        self.assertEqual(self.cfg(uid)["booklet_series"], "E")

    def test_booklet_number_auto_manual(self):
        uid = _uid()
        self.seed(uid, "booklet_number")
        self.tap(uid, "bn", "auto")
        self.assertEqual(self.step(uid), "cand_name")
        uid2 = _uid()
        self.seed(uid2, "booklet_number")
        self.tap(uid2, "bn", "manual")
        self.say(uid2, "42")
        self.assertEqual(self.cfg(uid2)["booklet_number"], "42")
        self.assertEqual(self.step(uid2), "cand_name")

    def test_candidate_fields_yes_no(self):
        uid = _uid()
        self.seed(uid, "cand_name")
        codes = ["cn", "cr", "cg", "cb", "cd", "cs", "ce"]
        for i, code in enumerate(codes):
            self.tap(uid, code, "1" if i % 2 == 0 else "0")
        cfg = self.cfg(uid)
        self.assertTrue(cfg["cand_name"])
        self.assertFalse(cfg["cand_roll"])
        self.assertTrue(cfg["cand_regid"])
        self.assertTrue(cfg["cand_evalsig"])
        self.assertEqual(self.step(uid), "institute")

    def test_settings_buttons(self):
        uid = _uid()
        self.seed(uid, "answer_key")
        self.tap(uid, "ak", "0")
        self.tap(uid, "so", "1")
        self.assertFalse(self.cfg(uid)["answer_key"])
        self.assertTrue(self.cfg(uid)["solutions"])
        for value in ("auto", "yes", "no"):
            uid2 = _uid()
            self.seed(uid2, "visuals")
            self.tap(uid2, "vi", value)
            self.assertEqual(self.cfg(uid2)["visuals"], value)
            self.assertEqual(self.step(uid2), "marks_correct")
        cb = self.tap(uid, "vi", "maybe")
        self.assertTrue(cb.answers[0][1])

    def test_marks_presets_and_custom(self):
        uid = _uid()
        self.seed(uid, "marks_correct")
        self.tap(uid, "mc", "p2")
        self.assertEqual(self.cfg(uid)["marks_correct"], 2.0)
        self.tap(uid, "mn", "n066")
        self.assertEqual(self.cfg(uid)["marks_negative"], -0.66)
        uid2 = _uid()
        self.seed(uid2, "marks_correct")
        self.tap(uid2, "mc", "custom")
        self.say(uid2, "abc")
        self.assertEqual(self.step(uid2), "marks_correct_input")
        self.say(uid2, "4")
        self.assertEqual(self.cfg(uid2)["marks_correct"], 4.0)
        self.tap(uid2, "mn", "custom")
        self.say(uid2, "1")
        self.assertEqual(self.step(uid2), "marks_negative_input")
        self.say(uid2, "-0.25")
        self.assertEqual(self.cfg(uid2)["marks_negative"], -0.25)
        self.assertEqual(self.step(uid2), "paper")


class TextImageStepCases(_FlowBase):
    def test_title_and_institute_required(self):
        uid = _uid()
        self.seed(uid, "title")
        self.say(uid, "  ")
        self.assertEqual(self.step(uid), "title")
        self.say(uid, "UPSC Mock 7")
        self.assertEqual(self.step(uid), "subject")
        uid2 = _uid()
        self.seed(uid2, "institute")
        self.say(uid2, "")
        self.assertEqual(self.step(uid2), "institute")
        self.say(uid2, "Journey Academy")
        self.assertEqual(self.cfg(uid2)["institute_name"], "Journey Academy")

    def test_logo_upload_and_skip(self):
        uid = _uid()
        self.seed(uid, "logo")
        self.tap(uid, "lg", "upload")
        self.assertEqual(self.step(uid), "logo_upload")
        self.send_photo(uid, fid="logo1", data=PNG)
        sess = creator_state.testseries_create[uid]
        self.assertEqual(sess["assets"]["logo"], PNG)
        self.assertEqual(self.step(uid), "watermark")
        uid2 = _uid()
        self.seed(uid2, "logo")
        self.tap(uid2, "lg", "skip")
        self.assertIsNone(creator_state.testseries_create[uid2]["assets"].get("logo"))
        self.assertEqual(self.step(uid2), "watermark")

    def test_logo_rejects_bad_images(self):
        uid = _uid()
        self.seed(uid, "logo_upload")
        self.send_photo(uid, fid="bad", data=NOT_IMAGE)
        self.assertEqual(self.step(uid), "logo_upload")
        big = b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)
        self.send_photo(uid, fid="big", data=big)
        self.assertEqual(self.step(uid), "logo_upload")

    def test_logo_accepts_list_shape_and_doc(self):
        uid = _uid()
        self.seed(uid, "logo_upload")
        self.send_photo(uid, fid="l2", data=JPG, as_list=True)
        self.assertEqual(self.step(uid), "watermark")
        uid2 = _uid()
        self.seed(uid2, "logo_upload")
        self.send_doc(uid2, name="wm.png", mime="image/png", data=PNG,
                      fid="l3")
        self.assertEqual(self.step(uid2), "watermark")

    def test_watermark_modes(self):
        uid = _uid()
        self.seed(uid, "watermark")
        self.tap(uid, "wm", "skip")
        self.assertEqual(self.cfg(uid)["watermark_mode"], "none")
        self.assertEqual(self.step(uid), "tagline")
        uid2 = _uid()
        self.seed(uid2, "watermark")
        self.tap(uid2, "wm", "text")
        self.say(uid2, "")
        self.assertEqual(self.step(uid2), "wm_text")
        self.say(uid2, "DO NOT COPY")
        self.assertEqual(self.step(uid2), "tagline")
        uid3 = _uid()
        self.seed(uid3, "watermark")
        self.tap(uid3, "wm", "both")
        self.say(uid3, "Sample")
        self.assertEqual(self.step(uid3), "wm_image")
        self.send_photo(uid3, fid="w1", data=PNG)
        sess = creator_state.testseries_create[uid3]
        self.assertEqual(sess["assets"]["wm_image"], PNG)
        self.assertEqual(sess["config"]["watermark_mode"], "both")
        self.assertEqual(self.step(uid3), "tagline")

    def test_watermark_image_only(self):
        uid = _uid()
        self.seed(uid, "watermark")
        self.tap(uid, "wm", "image")
        self.assertEqual(self.step(uid), "wm_image")
        self.send_doc(uid, name="x.txt", mime="text/plain", data=b"xx",
                      fid="x1")
        self.assertEqual(self.step(uid), "wm_image")
        self.send_photo(uid, fid="w2", data=JPG)
        self.assertEqual(self.step(uid), "tagline")

    def test_logo_and_watermark_independent(self):
        uid = _uid()
        self.seed(uid, "logo")
        self.tap(uid, "lg", "upload")
        self.send_photo(uid, fid="a", data=PNG)
        self.tap(uid, "wm", "image")
        self.send_photo(uid, fid="b", data=JPG)
        sess = creator_state.testseries_create[uid]
        self.assertEqual(sess["assets"]["logo"], PNG)
        self.assertEqual(sess["assets"]["wm_image"], JPG)

    def test_tagline_and_identity_skips(self):
        uid = _uid()
        self.seed(uid, "tagline")
        self.tap(uid, "tg", "skip")
        self.assertEqual(self.cfg(uid)["tagline"], "")
        self.assertEqual(self.step(uid), "answer_key")
        uid2 = _uid()
        self.seed(uid2, "paper")
        self.say(uid2, "Paper II")
        self.tap(uid2, "du", "skip")
        self.say(uid2, "JFL-GEO-01-2027")
        cfg = self.cfg(uid2)
        self.assertEqual((cfg["paper"], cfg["duration"], cfg["test_code"]),
                         ("Paper II", "", "JFL-GEO-01-2027"))
        self.assertEqual(self.step(uid2), "preview")


class SafetyCases(_FlowBase):
    def test_stale_button_ignored(self):
        uid = _uid()
        self.seed(uid, "test_number")
        self.tap(uid, "tn", "auto")
        cb = self.tap(uid, "tn", "manual")  # old prompt's button
        self.assertTrue(cb.answers[0][1])
        self.assertEqual(self.cfg(uid)["test_number_mode"], "auto")
        self.assertEqual(self.step(uid), "booklet_series")

    def test_wrong_user_rejected(self):
        uid = _uid()
        self.seed(uid, "test_number")
        cb = _FakeCB(self.bot, uid + 1, "tsc_tn_%d_auto" % uid)
        self.arun(tsc.creation_cb(self.client, cb))
        self.assertTrue(cb.answers[0][1])
        self.assertEqual(self.step(uid), "test_number")

    def test_expired_and_malformed(self):
        ghost = _uid()
        cb = _FakeCB(self.bot, ghost, "tsc_tn_%d_auto" % ghost)
        self.arun(tsc.creation_cb(self.client, cb))
        self.assertIn("expired", (cb.answers[0][0] or "").lower())
        for bad in ("", "nope", "tsc_tn", "tsc_tn_x_auto", "tsc_zz_1_1"):
            uid = _uid()
            self.seed(uid, "test_number")
            cb = _FakeCB(self.bot, uid, bad)
            self.arun(tsc.creation_cb(self.client, cb))
            self.assertTrue(cb.answers and cb.answers[0][1])

    def test_unexpected_inputs_nudge_without_corruption(self):
        uid = _uid()
        self.seed(uid, "test_number")
        self.say(uid, "hello")
        self.assertEqual(self.step(uid), "test_number")
        self.assertIn("tap one of the buttons", self.bot.sent[-1][1])
        self.send_photo(uid, fid="p", data=PNG)
        self.assertEqual(self.step(uid), "test_number")
        self.send_doc(uid, data=MCQ_DOC)
        self.assertEqual(self.step(uid), "test_number")
        uid2 = _uid()
        self.seed(uid2, "title")
        self.send_photo(uid2, fid="p2", data=PNG)
        self.assertEqual(self.step(uid2), "title")
        self.assertIn("typed reply", self.bot.sent[-1][1])
        uid3 = _uid()
        self.seed(uid3, "logo_upload")
        self.say(uid3, "not an image")
        self.assertEqual(self.step(uid3), "logo_upload")

    def test_two_users_isolated(self):
        a, b = _uid(), _uid()
        self.seed(a, "title")
        self.seed(b, "title")
        self.say(a, "Paper A")
        self.assertEqual(self.step(a), "subject")
        self.assertEqual(self.step(b), "title")
        self.say(b, "Paper B")
        self.assertEqual(self.cfg(a)["title"], "Paper A")
        self.assertEqual(self.cfg(b)["title"], "Paper B")
        self.seed(a, "logo_upload")
        self.seed(b, "logo_upload")
        self.send_photo(a, fid="la", data=PNG)
        self.send_photo(b, fid="lb", data=JPG)
        self.assertEqual(creator_state.testseries_create[a]["assets"]["logo"], PNG)
        self.assertEqual(creator_state.testseries_create[b]["assets"]["logo"], JPG)

    def test_cancel_button_clears_everything(self):
        uid = _uid()
        self.seed(uid, "title")
        creator_state.testseries_create[uid]["assets"]["logo"] = PNG
        cb = self.tap(uid, "xx", "cancel")
        self.assertNotIn(uid, creator_state.testseries_create)
        self.assertEqual(cb.answers, [("Cancelled.", False)])

    def test_cancel_command_clears_new_session(self):
        uid = _uid()
        self.seed(uid, "watermark")
        self.arun(cancel_cmd(self.client,
                            _FakeMsg(self.bot, uid, text="/cancel")))
        self.assertNotIn(uid, creator_state.testseries_create)
        self.assertIn("setup cancelled", self.bot.sent[-1][1])


class PreviewEditGenerateCases(_FlowBase):
    def _complete(self, uid):
        self.seed(uid, "preview", config={
            "title": "Final Mock", "subject": "Polity",
            "test_number_mode": "manual", "test_number": "3",
            "booklet_series": "B", "booklet_number_mode": "auto",
            "cand_name": True, "institute_name": "Academy",
            "watermark_mode": "none", "answer_key": True,
            "solutions": True, "visuals": "auto",
            "marks_correct": 2.0, "marks_negative": -0.66,
            "test_code": "JFL-01"})

    def test_preview_shows_compact_summary(self):
        uid = _uid()
        self._complete(uid)
        text, markup = tsc._prompt("preview", uid,
                                   creator_state.testseries_create[uid])
        for bit in ("Final Mock", "Polity", "Test No.: 3", "Booklet: B-Auto",
                    "Questions: **2**", "Max marks: **4**", "+2 / -0.66",
                    "Candidate boxes: Candidate Name", "Academy",
                    "Watermark: None", "Code: JFL-01"):
            self.assertIn(bit, text)
        datas = _datas(markup)
        self.assertIn("tsc_pv_%d_generate" % uid, datas)
        self.assertIn("tsc_pv_%d_edit" % uid, datas)
        self.assertIn("tsc_pv_%d_cancel" % uid, datas)

    def test_edit_flow_preserves_other_fields(self):
        uid = _uid()
        self._complete(uid)
        self.tap(uid, "pv", "edit")
        self.assertEqual(self.step(uid), "edit_menu")
        datas = _datas(self.bot.edits[-1][2])
        self.assertIn("tsc_em_%d_branding" % uid, datas)
        self.tap(uid, "em", "branding")
        self.assertEqual(self.step(uid), "institute")
        self.say(uid, "New Academy")
        self.tap(uid, "lg", "skip")
        self.tap(uid, "wm", "skip")
        self.say(uid, "Fresh tagline")
        self.assertEqual(self.step(uid), "preview")
        cfg = self.cfg(uid)
        self.assertEqual(cfg["institute_name"], "New Academy")
        self.assertEqual(cfg["tagline"], "Fresh tagline")
        self.assertEqual(cfg["title"], "Final Mock")  # untouched kept
        self.assertEqual(cfg["test_number"], "3")

    def test_edit_back_button(self):
        uid = _uid()
        self._complete(uid)
        self.tap(uid, "pv", "edit")
        self.tap(uid, "em", "back")
        self.assertEqual(self.step(uid), "preview")

    def test_edit_single_step_section(self):
        uid = _uid()
        self._complete(uid)
        self.tap(uid, "pv", "edit")
        self.tap(uid, "em", "title")
        self.say(uid, "Renamed")
        self.assertEqual(self.step(uid), "preview")
        self.assertEqual(self.cfg(uid)["title"], "Renamed")
        self.assertEqual(self.cfg(uid)["subject"], "Polity")

    def test_generate_success_cleans_up(self):
        uid = _uid()
        self._complete(uid)
        seen = {}
        from quizbot.creator_bot.handlers import reports as rep

        async def fake_api(quizzes, mode, title, **kw):
            seen.update(mode=mode, title=title, **kw)
            seen["n"] = len(quizzes[0]["questions"])
            return b"%PDF-fake"

        with patch.object(tsc, "_generate_pdf_via_api", fake_api):
            cb = self.tap(uid, "pv", "generate")
        self.assertEqual(seen["mode"], "keyonly")
        self.assertEqual(seen["title"], "Final Mock")
        self.assertEqual(seen["institute_name"], "Academy")
        self.assertEqual(seen["n"], 2)
        self.assertEqual(len(self.bot.documents), 1)
        _name, caption, _data = self.bot.documents[0]
        self.assertIn("Final Mock", caption)
        self.assertIn("Questions: 2", caption)
        self.assertNotIn(uid, creator_state.testseries_create)
        self.assertEqual(cb.answers, [(None, False)])

    def test_generate_busy_keeps_session(self):
        uid = _uid()
        self._complete(uid)

        async def driver():
            await tsc._TSR_LOCK.acquire()
            try:
                cb = _FakeCB(self.bot, uid, "tsc_pv_%d_generate" % uid)
                await tsc.creation_cb(self.client, cb)
            finally:
                tsc._TSR_LOCK.release()

        with patch.object(tsc, "_generate_pdf_via_api",
                          AsyncMock(return_value=b"x")) as api:
            self.arun(driver())
            api.assert_not_called()
        self.assertEqual(self.step(uid), "preview")
        self.assertIn("System busy", self.bot.sent[-1][1])

    def test_generate_failure_keeps_retryable_session(self):
        uid = _uid()
        self._complete(uid)

        async def boom(*a, **k):
            raise RuntimeError("service down")

        with patch.object(tsc, "_generate_pdf_via_api", boom):
            self.tap(uid, "pv", "generate")
        self.assertEqual(self.step(uid), "preview")
        self.assertIn(uid, creator_state.testseries_create)
        self.assertIn("service down", self.bot.edits[-1][1])

    def test_generate_blocked_when_invalid(self):
        uid = _uid()
        self.seed(uid, "preview", config={"title": ""}, quizzes=[])
        with patch.object(tsc, "_generate_pdf_via_api",
                          AsyncMock(return_value=b"x")) as api:
            self.tap(uid, "pv", "generate")
            api.assert_not_called()
        self.assertIn("Setup incomplete", self.bot.sent[-1][1])

    def test_double_generate_single_call(self):
        uid = _uid()
        self._complete(uid)
        with patch.object(tsc, "_generate_pdf_via_api",
                          AsyncMock(return_value=b"%PDF")) as api:
            self.tap(uid, "pv", "generate")
            cb = self.tap(uid, "pv", "generate")
            api.assert_called_once()
        self.assertIn("expired", (cb.answers[0][0] or "").lower())

    def test_payload_overrides_use_existing_contract(self):
        from quizbot.creator_bot.handlers.reports import \
            _build_testseries_payload
        quizzes = _quizzes(1)
        plain = _build_testseries_payload(quizzes, "keyonly", "T")
        self.assertEqual(plain["institute_name"], "Quiz Creator")
        self.assertEqual(plain["tagline"], "Test Series")
        custom = _build_testseries_payload(
            quizzes, "keyonly", "T", institute_name="Academy",
            tagline="Hello")
        self.assertEqual(custom["institute_name"], "Academy")
        self.assertEqual(custom["tagline"], "Hello")
        self.assertEqual(set(custom), set(plain), "no new API fields")


class FullPathCases(_FlowBase):
    def test_end_to_end_file_to_preview(self):
        uid = _uid()
        self.start(uid)
        self.tap(uid, "in", "file")
        self.send_doc(uid)
        self.say(uid, "Grand Mock")
        self.tap(uid, "sj", "s0")
        self.tap(uid, "tn", "manual")
        self.say(uid, "5")
        self.tap(uid, "bs", "custom")
        self.say(uid, "E")
        self.tap(uid, "bn", "auto")
        for code in ("cn", "cr", "cg", "cb", "cd", "cs", "ce"):
            self.tap(uid, code, "1")
        self.say(uid, "My Institute")
        self.tap(uid, "lg", "upload")
        self.send_photo(uid, fid="logo", data=PNG)
        self.tap(uid, "wm", "both")
        self.say(uid, "SAMPLE")
        self.send_photo(uid, fid="wm", data=JPG)
        self.say(uid, "Dream Series")
        self.tap(uid, "ak", "1")
        self.tap(uid, "so", "1")
        self.tap(uid, "vi", "auto")
        self.tap(uid, "mc", "custom")
        self.say(uid, "4")
        self.tap(uid, "mn", "n033")
        self.tap(uid, "pp", "skip")
        self.say(uid, "3 hours")
        self.say(uid, "JFL-X-01")
        self.assertEqual(self.step(uid), "preview")
        cfg = tsc.build_config(creator_state.testseries_create[uid])
        self.assertEqual(cfg.validate(), [])
        self.assertEqual(cfg.total_questions, 1)
        self.assertEqual(cfg.max_marks, 4.0)
        self.assertTrue(cfg.logo_present)
        self.assertEqual(cfg.watermark_mode, "both")
        self.assertTrue(cfg.wm_image_present)
        self.assertEqual(len(cfg.enabled_candidate_fields()), 7)


class BridgeCases(_FlowBase):
    def test_newseries_registered_private_once(self):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler
        from quizbot.runner_bot.creator_bridge import register_creator_bridge
        app = (Application.builder()
               .token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build())
        register_creator_bridge(app)
        by_command: dict[str, list] = {}
        patterns: list[str] = []
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    for cmd in h.commands:
                        by_command.setdefault(cmd, []).append(h)
                elif isinstance(h, CallbackQueryHandler):
                    patterns.append(getattr(h.pattern, "pattern", ""))
        self.assertIn("newseries", by_command)
        self.assertEqual(len(by_command["newseries"]), 1)
        self.assertIn("PRIVATE", repr(by_command["newseries"][0].filters))
        self.assertIn("^tsc_", patterns)
        self.assertEqual(len(by_command["testseries"]), 1)

    def test_filter_matches_create_session(self):
        from quizbot.runner_bot.creator_bridge import _CreatorStateFilter
        filt = _CreatorStateFilter()
        plain = SimpleNamespace(effective_user=SimpleNamespace(id=_uid()))
        self.assertFalse(filt.filter(plain))
        uid = _uid()
        creator_state.testseries_create[uid] = {"step": "title"}
        hit = SimpleNamespace(effective_user=SimpleNamespace(id=uid))
        self.assertTrue(filt.filter(hit))

    def _ptb_update(self, uid, bot, message=None, query=None):
        return (SimpleNamespace(effective_message=message,
                                effective_user=SimpleNamespace(id=uid),
                                callback_query=query),
                SimpleNamespace(bot=bot))

    def _ptb_msg(self, uid, text=None, document=None, photo=None):
        return SimpleNamespace(
            message_id=9, chat=SimpleNamespace(id=777, type="private"),
            from_user=SimpleNamespace(id=uid), text=text, caption=None,
            reply_markup=None, reply_to_message=None, document=document,
            photo=photo if photo is not None else [], video=None, poll=None,
            date=None)

    def test_router_dispatches_new_session_messages(self):
        from quizbot.runner_bot.creator_bridge import _creator_message_router
        uid = _uid()
        self.seed(uid, "title")
        update, ctx = self._ptb_update(uid, self.bot,
                                       message=self._ptb_msg(uid, text="Hello"))
        self.arun(_creator_message_router(update, ctx))
        self.assertEqual(self.cfg(uid)["title"], "Hello")
        # Commands must not be consumed by the wizard branch.
        self.seed(uid, "title")
        update, ctx = self._ptb_update(uid, self.bot,
                                       message=self._ptb_msg(uid, text="/limit"))
        self.arun(_creator_message_router(update, ctx))
        self.assertEqual(self.step(uid), "title")

    def test_router_dispatches_photo_and_document(self):
        from quizbot.runner_bot.creator_bridge import _creator_message_router
        uid = _uid()
        self.seed(uid, "logo_upload")
        self.bot.files["px"] = PNG
        msg = self._ptb_msg(uid, photo=[_photo("px", len(PNG))])
        update, ctx = self._ptb_update(uid, self.bot, message=msg)
        self.arun(_creator_message_router(update, ctx))
        self.assertEqual(self.step(uid), "watermark")
        uid2 = _uid()
        self.seed(uid2, "intake_file")
        self.bot.files["mcq1"] = MCQ_DOC
        msg = self._ptb_msg(uid2, document=_doc())
        update, ctx = self._ptb_update(uid2, self.bot, message=msg)
        self.arun(_creator_message_router(update, ctx))
        self.assertEqual(self.step(uid2), "title")

    def test_callback_router_routes_tsc(self):
        from quizbot.runner_bot.creator_bridge import _creator_callback_router
        uid = _uid()
        self.seed(uid, "test_number")
        query = SimpleNamespace(
            id="q1", data="tsc_tn_%d_auto" % uid,
            from_user=SimpleNamespace(id=uid),
            message=self._ptb_msg(uid, text="old"),
            answer=AsyncMock())
        update, ctx = self._ptb_update(uid, self.bot, query=query)
        self.arun(_creator_callback_router(update, ctx))
        self.assertEqual(self.cfg(uid)["test_number_mode"], "auto")
        self.assertEqual(self.step(uid), "booklet_series")

    def test_newseries_hidden_from_menus(self):
        text = (ROOT / "BOTFATHER_COMMANDS.txt").read_text().lower()
        self.assertNotIn("newseries", text)
        commands = {line.split("-", 1)[0].strip()
                    for line in text.splitlines() if "-" in line}
        self.assertNotIn("newseries", commands)


if __name__ == "__main__":
    unittest.main(verbosity=2)
