"""Phase 6 — Gemini Podcast system tests (spec items A-Y).

Covers: per-user encrypted keys (set/change/remove/isolation, never
leaked), key-gated UX, varied brand ads + soft outros, two-host structure,
question numbering, Test Series ranges, PDF detection/selection caps,
chunking + MP3 splitting, retries/error mapping, and preservation of the
pre-existing podcast functionality. No network, no Mongo, no ffmpeg needed
(external boundaries are faked/mocked).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import quizbot.runner_bot.handlers.podcast as pod
import quizbot.runner_bot.podcast_security as sec

TEST_SECRET = "phase6-test-master-secret-0123456789abcdef"
KEY_A = "AIzaSyTESTKEYAAAAAAA11111111111111111111"
KEY_B = "AIzaSyTESTKEYBBBBBBB22222222222222222222"
KEY_NEW = "AIzaSyTESTKEYCCCCCCC33333333333333333333"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Telegram fakes
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    _ids = [0]

    def __init__(self, chat_id=100, text="", from_uid=1):
        FakeMessage._ids[0] += 1
        self.message_id = FakeMessage._ids[0]
        self.chat = FakeChat(chat_id)
        self.chat_id = chat_id
        self.text = text
        self.from_user = FakeUser(from_uid)
        self.reply_to_message = None
        self.document = None
        self.edits = []
        self.markups = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        self.text = text
        if "reply_markup" in kw:
            self.markups.append(kw["reply_markup"])
        return self

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)
        return self

    async def delete(self):
        self.deleted = True
        return True


class FakeDoc:
    def __init__(self, file_id="f1", file_name="q.pdf",
                 mime_type="application/pdf"):
        self.file_id = file_id
        self.file_name = file_name
        self.mime_type = mime_type


class FakeFile:
    def __init__(self, data: bytes):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class FakeCallbackQuery:
    def __init__(self, data, uid=1, chat_id=100):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage(chat_id=chat_id, from_uid=uid)
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append((text, kw))


class FakeUpdate:
    def __init__(self, uid=1, chat_id=100, message=None, callback_query=None):
        self.effective_user = FakeUser(uid)
        self.effective_chat = FakeChat(chat_id)
        self.message = message
        self.callback_query = callback_query


class FakeBot:
    def __init__(self):
        self.sent = []      # (chat_id, text, kwargs)
        self.audios = []    # kwargs dicts
        self.files = {}     # file_id -> bytes

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return FakeMessage(chat_id=chat_id, text=text)

    async def send_audio(self, **kw):
        data = kw.get("audio")
        if hasattr(data, "read"):
            kw = dict(kw)
            kw["audio_bytes"] = data.read()
        self.audios.append(kw)
        return FakeMessage(chat_id=kw.get("chat_id", 0))

    async def get_file(self, file_id):
        return FakeFile(self.files[file_id])


class FakeCtx:
    def __init__(self, bot, args=None):
        self.bot = bot
        self.args = args or []


def all_texts(bot):
    return [t for _, t, _ in bot.sent]


def buttons_of(message_kw):
    markup = message_kw.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Store / Gemini fakes
# ---------------------------------------------------------------------------

class FakeKeyRepo:
    def __init__(self):
        self.store = {}

    async def get_encrypted(self, uid):
        return self.store.get(uid)

    async def has(self, uid):
        return uid in self.store

    async def save(self, uid, enc):
        self.store[uid] = enc

    async def delete(self, uid):
        return self.store.pop(uid, None) is not None


def make_quiz(qid="abc123", n=5):
    return {
        "qid": qid,
        "quiz_name": f"Quiz {qid}",
        "questions": [
            {"question": f"Stem {i}?",
             "options": [f"opt{i}a", f"opt{i}b", f"opt{i}c", f"opt{i}d"],
             "correct_option_id": i % 4,
             "explanation": f"Expl {i}."}
            for i in range(1, n + 1)
        ],
    }


class FakeQuizRepo:
    def __init__(self, quizzes):
        self.quizzes = {q["qid"]: q for q in quizzes}

    async def list_by_creator(self, uid):
        return [{"qid": q, "quiz_name": v["quiz_name"]}
                for q, v in self.quizzes.items()]

    async def get(self, qid):
        return self.quizzes.get(qid)


class FakeGenResp:
    def __init__(self, text):
        self.text = text


class FakeGenModels:
    """Fake for client.models; script queue or exception sequence."""

    def __init__(self, script="OK", exc_sequence=None):
        self.script = script
        self.exc_sequence = list(exc_sequence or [])
        self.calls = []

    def generate_content(self, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents,
                           "config": config})
        if self.exc_sequence:
            raise self.exc_sequence.pop(0)
        return FakeGenResp(self.script)


class FakeGenClient:
    def __init__(self, models):
        self.models = models


class Phase6Base(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get(sec.ENV_VAR)
        os.environ[sec.ENV_VAR] = TEST_SECRET
        sec.clear_cache()
        pod.PODCAST_SESSIONS.clear()
        pod._PROMO_ROTATION.clear()
        pod._OUTRO_ROTATION.clear()
        self.keys = FakeKeyRepo()
        self._repo_patch = patch.object(pod, "_key_repo",
                                        return_value=self.keys)
        self._repo_patch.start()
        self.addCleanup(self._repo_patch.stop)
        self.addCleanup(pod.PODCAST_SESSIONS.clear)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(sec.ENV_VAR, None)
        else:
            os.environ[sec.ENV_VAR] = self._old_env
        sec.clear_cache()

    def _key_text(self, text):
        return SimpleNamespace(text=text)

    def cmd(self, uid, text="/podcast", chat=100, args=None, reply=None):
        bot = FakeBot()
        ctx = FakeCtx(bot, args)
        msg = FakeMessage(chat_id=chat, text=text, from_uid=uid)
        msg.reply_to_message = reply
        return FakeUpdate(uid, chat, message=msg), ctx, bot

    def cbq(self, uid, data, chat=100):
        bot = FakeBot()
        ctx = FakeCtx(bot)
        return FakeUpdate(uid, chat,
                          callback_query=FakeCallbackQuery(data, uid, chat)), ctx, bot

    def txt(self, uid, text, chat=100):
        bot = FakeBot()
        ctx = FakeCtx(bot)
        return FakeUpdate(uid, chat,
                          message=FakeMessage(chat_id=chat, text=text,
                                              from_uid=uid)), ctx, bot


# ---------------------------------------------------------------------------
# A/B/C/D/E/F/G/H — key lifecycle, isolation, secrecy
# ---------------------------------------------------------------------------

class KeyLifecycleCases(Phase6Base):
    def _set_key(self, uid, key, bot=None):
        """Drive Set-button + key text through to a saved key."""
        bot = bot or FakeBot()
        ctx = FakeCtx(bot)
        bot.msg_objs = []
        _orig_send = bot.send_message

        async def _send(chat_id, text, **kw):
            m = await _orig_send(chat_id, text, **kw)
            bot.msg_objs.append(m)
            return m

        bot.send_message = _send
        upd, _, _ = self.cbq(uid, f"podkey_{uid}_set")
        upd.callback_query.message = FakeMessage()
        run(pod.podcast_key_callback(
            FakeUpdate(uid, 100,
                       callback_query=upd.callback_query), ctx))
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(FakeGenModels("OK"))):
            msg = FakeMessage(chat_id=100, text=key, from_uid=uid)
            run(pod.podcast_key_text(FakeUpdate(uid, 100, message=msg),
                                     ctx))
        return bot, msg

    def test_a_bare_podcast_without_key_asks_for_key(self):
        upd, ctx, bot = self.cmd(11)
        run(pod.podcast_command(upd, ctx))
        self.assertEqual(len(bot.sent), 1)
        _cid, text, kw = bot.sent[0]
        self.assertIn("पहले अपनी Gemini API Key सेट करें", text)
        btns = buttons_of(kw)
        self.assertIn(("🔑 Set Gemini API Key", "podkey_11_set"), btns)

    def test_a_topic_without_key_asks_for_key_no_generation(self):
        upd, ctx, bot = self.cmd(12, "/podcast gravity", args=["gravity"])
        with patch.object(pod, "_gemini_generate",
                          AsyncMock()) as gen:
            run(pod.podcast_command(upd, ctx))
            gen.assert_not_called()
        self.assertIn("पहले अपनी Gemini API Key सेट करें", all_texts(bot)[0])

    def test_b_set_key_saves_encrypted(self):
        _bot, msg = self._set_key(21, KEY_A)
        stored = self.keys.store.get(21)
        self.assertIsNotNone(stored)
        self.assertNotIn(KEY_A, stored)
        self.assertNotEqual(stored, KEY_A)
        self.assertEqual(sec.decrypt_api_key(stored), KEY_A)
        self.assertTrue(msg.deleted, "key message must be deleted")
        edits = [e for m in _bot.msg_objs for e in ([m.text] + m.edits)]
        joined = "\n".join(all_texts(_bot) + edits)
        for _cid, _t, kw in _bot.sent:
            for _label, data in buttons_of(kw):
                joined += data or ""
        self.assertIn("save ho gayi", joined)
        self.assertNotIn(KEY_A, joined)
        self.assertIn(sec.mask_api_key(KEY_A), joined)
        self.assertNotIn(21, pod.PODCAST_SESSIONS)

    def test_c_second_podcast_does_not_ask_key(self):
        self._set_key(31, KEY_A)
        upd, ctx, bot = self.cmd(31)
        run(pod.podcast_command(upd, ctx))
        joined = "\n".join(all_texts(bot))
        self.assertNotIn("पहले अपनी Gemini API Key सेट करें", joined)
        self.assertIn("Source chunein", joined)
        btns = buttons_of(bot.sent[0][2])
        labels = [t for t, _ in btns]
        self.assertIn("📚 Test Series", labels)
        self.assertIn("📄 PDF", labels)
        self.assertIn("📝 Topic/Text", labels)

    def test_d_change_key_replaces(self):
        self._set_key(41, KEY_A)
        bot, _m = self._set_key(41, KEY_NEW)
        self.assertEqual(sec.decrypt_api_key(self.keys.store[41]), KEY_NEW)

    def test_e_remove_key_deletes(self):
        self._set_key(51, KEY_A)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        upd, _, _ = self.cbq(51, "podkey_51_remove")
        run(pod.podcast_key_callback(upd, ctx))
        self.assertIn("Pakka", upd.callback_query.message.text)
        upd2, _, _ = self.cbq(51, "podkey_51_remove_yes")
        run(pod.podcast_key_callback(upd2, ctx))
        self.assertNotIn(51, self.keys.store)
        self.assertIn("remove kar di gayi", upd2.callback_query.message.text)

    def test_f_generate_after_removal_asks_key(self):
        self._set_key(61, KEY_A)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        upd, _, _ = self.cbq(61, "podkey_61_remove_yes")
        run(pod.podcast_key_callback(upd, ctx))
        upd2, ctx2, bot2 = self.cmd(61, "/podcast topic here",
                                    args=["topic", "here"])
        run(pod.podcast_command(upd2, ctx2))
        self.assertIn("पहले अपनी Gemini API Key सेट करें",
                      all_texts(bot2)[0])

    def test_g_user_isolation(self):
        self._set_key(71, KEY_A)
        self._set_key(72, KEY_B)
        # B's menu shows B's masked key, never A's.
        upd, ctx, bot = self.cmd(72)
        run(pod.podcast_command(upd, ctx))
        joined = "\n".join(all_texts(bot))
        self.assertIn(sec.mask_api_key(KEY_B), joined)
        self.assertNotIn(sec.mask_api_key(KEY_A), joined)
        self.assertNotIn(KEY_A, joined)
        # Generations use each user's own key.
        seen = []

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            seen.append(api_key)
            return "[FEMALE] hi\n[MALE] hello"

        async def fake_tts(lines, out, api_key, progress_cb=None):
            Path(out).write_bytes(b"fake-mp3")

        async def go(uid, marker):
            b = FakeBot()
            c = FakeCtx(b)
            with patch.object(pod, "_gemini_generate", fake_gen), \
                 patch.object(pod, "_gemini_tts_and_merge", fake_tts):
                u, _, _ = self.cmd(uid, f"/podcast {marker}",
                                   args=[marker])
                await pod.podcast_command(u, c)
            return b

        b1 = run(go(71, "AAAmarker"))
        b2 = run(go(72, "BBBmarker"))
        self.assertEqual(len(b1.audios), 1)
        self.assertEqual(len(b2.audios), 1)
        used = set(seen)
        self.assertEqual(used, {KEY_A, KEY_B})

    def test_h_key_never_printed_or_logged(self):
        records = []

        class Cap(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        root = logging.getLogger()
        handler = Cap()
        root.addHandler(handler)
        try:
            bot, _m = self._set_key(81, KEY_A)
            upd, ctx, bot2 = self.cmd(81, "/podcast x", args=["x"])
            with patch.object(pod, "_gemini_generate",
                              AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
                 patch.object(pod, "_gemini_tts_and_merge",
                              AsyncMock(side_effect=RuntimeError(
                                  f"tts blew up on {KEY_A}"))):
                run(pod.podcast_command(upd, ctx))
        finally:
            root.removeHandler(handler)
        blob = "\n".join(all_texts(bot) + all_texts(bot2))
        for _cid, _t, kw in bot.sent + bot2.sent:
            for _label, data in buttons_of(kw):
                blob += data or ""
        self.assertNotIn(KEY_A, blob)
        for rec in records:
            self.assertNotIn(KEY_A, rec)

    def test_short_key_rejected_step_kept(self):
        bot = FakeBot()
        ctx = FakeCtx(bot)
        pod.PODCAST_SESSIONS[91] = {"step": "await_key", "chat_id": 100}
        msg = FakeMessage(chat_id=100, text="short", from_uid=91)
        run(pod.podcast_key_text(FakeUpdate(91, 100, message=msg), ctx))
        self.assertIn("chhoti", all_texts(bot)[0])
        self.assertEqual(pod.PODCAST_SESSIONS[91]["step"], "await_key")
        self.assertNotIn(91, self.keys.store)

    def test_invalid_key_not_saved_retry_kept(self):
        bot = FakeBot()
        ctx = FakeCtx(bot)
        sent_msgs = []
        orig = bot.send_message

        async def _send(chat_id, text, **kw):
            m = await orig(chat_id, text, **kw)
            sent_msgs.append(m)
            return m

        bot.send_message = _send
        pod.PODCAST_SESSIONS[92] = {"step": "await_key", "chat_id": 100}
        models = FakeGenModels(exc_sequence=[Exception("API_KEY_INVALID")])
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(models)):
            msg = FakeMessage(chat_id=100, text=KEY_A, from_uid=92)
            run(pod.podcast_key_text(FakeUpdate(92, 100, message=msg),
                                     ctx))
        self.assertNotIn(92, self.keys.store)
        self.assertEqual(pod.PODCAST_SESSIONS[92]["step"], "await_key")
        self.assertEqual(len(sent_msgs), 1, "only the validating status is sent")
        self.assertIn("valid nahi", sent_msgs[0].text)
        self.assertNotIn(KEY_A, sent_msgs[0].text)
        upd, ctx, bot = self.cbq(101, "podkey_999_set")
        run(pod.podcast_key_callback(upd, ctx))
        texts = [t for t, _kw in upd.callback_query.answers]
        self.assertTrue(any(t and "Not your session" in t for t in texts))

    def test_callback_expired_session(self):
        upd, ctx, bot = self.cbq(102, "pod_default_102_question")
        run(pod.podcast_callback(upd, ctx))
        self.assertIn("expired", upd.callback_query.message.text.lower())


# ---------------------------------------------------------------------------
# Security primitives
# ---------------------------------------------------------------------------

class SecurityCases(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get(sec.ENV_VAR)
        os.environ[sec.ENV_VAR] = TEST_SECRET
        sec.clear_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop(sec.ENV_VAR, None)
        else:
            os.environ[sec.ENV_VAR] = self._old
        sec.clear_cache()

    def test_roundtrip(self):
        token = sec.encrypt_api_key(KEY_A)
        self.assertNotIn(KEY_A, token)
        self.assertEqual(sec.decrypt_api_key(token), KEY_A)

    def test_wrong_master_secret_fails_cleanly(self):
        token = sec.encrypt_api_key(KEY_A)
        os.environ[sec.ENV_VAR] = "a-different-master-secret-000000"
        sec.clear_cache()
        with self.assertRaises(ValueError) as cm:
            sec.decrypt_api_key(token)
        self.assertNotIn(KEY_A, str(cm.exception))

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            sec.encrypt_api_key("   ")

    def test_mask_formats(self):
        self.assertEqual(sec.mask_api_key(KEY_A), "AIza••••1111")
        self.assertEqual(sec.mask_api_key("12345678"), "••••5678")
        self.assertEqual(sec.mask_api_key("abc"), "••••")
        self.assertEqual(sec.mask_api_key(""), "••••")

    def test_redact(self):
        self.assertEqual(sec.redact(f"oops {KEY_A} end", [KEY_A]),
                         "oops [redacted] end")
        self.assertEqual(sec.redact("nothing", [KEY_A]), "nothing")
        self.assertEqual(sec.redact("", [KEY_A]), "")

    def test_fernet_key_secret_accepted(self):
        from cryptography.fernet import Fernet
        os.environ[sec.ENV_VAR] = Fernet.generate_key().decode()
        sec.clear_cache()
        self.assertEqual(sec.decrypt_api_key(sec.encrypt_api_key(KEY_A)),
                         KEY_A)

    def test_real_repo_shape_with_fake_collection(self):
        from quizbot.database.repositories import PodcastKeyRepository

        class FakeCol:
            def __init__(self):
                self.docs = {}

            async def find_one(self, filt, proj=None):
                return dict(self.docs.get(filt["user_id"], {})) or None

            async def count_documents(self, filt, limit=None):
                return 1 if filt["user_id"] in self.docs else 0

            async def update_one(self, filt, update, upsert=False):
                uid = filt["user_id"]
                doc = self.docs.setdefault(uid, {"user_id": uid})
                doc.update(update["$set"])
                for k, v in update.get("$setOnInsert", {}).items():
                    doc.setdefault(k, v)

            async def delete_one(self, filt):
                class R:
                    pass
                r = R()
                r.deleted_count = 1 if self.docs.pop(
                    filt["user_id"], None) else 0
                return r

        class FakeDB:
            def __init__(self):
                self.col = FakeCol()

            def collection(self, name):
                assert name == "podcast_keys"
                return self.col

        async def go():
            repo = PodcastKeyRepository(FakeDB())
            assert await repo.has(5) is False
            assert await repo.get_encrypted(5) is None
            await repo.save(5, "ENC")
            assert await repo.has(5) is True
            assert await repo.get_encrypted(5) == "ENC"
            assert await repo.delete(5) is True
            assert await repo.delete(5) is False

        run(go())


# ---------------------------------------------------------------------------
# I/J/K/L/M/N — ads, outros, brand, structure
# ---------------------------------------------------------------------------

class BrandStructureCases(Phase6Base):
    def test_i_branded_opening_exists(self):
        lines = pod._build_ad_lines(1)
        self.assertGreaterEqual(len(lines), 2)
        speakers = {s for s, _ in lines}
        self.assertEqual(speakers, {"FEMALE", "MALE"})
        joined = " ".join(t for _, t in lines)
        self.assertIn("Journey for लबासना", joined)
        words = len(joined.split())
        self.assertGreaterEqual(words, 30, "ad too short for ~30s")
        self.assertLessEqual(words, 130, "ad too long for ~30s")

    def test_j_hindi_brand_everywhere(self):
        for tpl in pod.PROMO_TEMPLATES:
            self.assertIn("लबासना", " ".join(t for _, t in tpl))
        for tpl in pod.OUTRO_TEMPLATES:
            self.assertIn("लबासना", " ".join(t for _, t in tpl))
        self.assertIn("Journey for लबासना", pod.SCRIPT_RULES)
        self.assertIn("Journey for लबासना", pod.QUESTION_RULES)

    def test_k_no_latin_brand(self):
        for tpl in pod.PROMO_TEMPLATES + pod.OUTRO_TEMPLATES:
            for _s, t in tpl:
                self.assertNotIn("LBSNAA", t)
        self.assertNotIn("LBSNAA", pod.SCRIPT_RULES)
        self.assertNotIn("LBSNAA", pod.QUESTION_RULES)
        self.assertNotIn("LBSNAA", pod.TAKEAWAY_BRIDGE[1])
        fixed = pod._enforce_brand_text("Welcome to Journey for LBSNAA show")
        self.assertIn("लबासना", fixed)
        self.assertNotIn("LBSNAA", fixed)
        out = pod._finalize_lines([("FEMALE", "x LBSNAA y"), ("MALE", "ok")])
        self.assertNotIn("LBSNAA", out[0][1])
        self.assertEqual(out[1][1], "ok")

    def test_l_ads_vary_across_generations(self):
        seen = {" ".join(t for _, t in pod._build_ad_lines(7)) for _ in range(7)}
        self.assertEqual(len(seen), 7, "each of 7 generations must differ")
        again = " ".join(t for _, t in pod._build_ad_lines(7))
        first = " ".join(t for _, t in pod.PROMO_TEMPLATES[0])
        self.assertEqual(again, first, "rotation wraps deterministically")

    def test_m_soft_branded_ending_exists(self):
        lines = pod._build_outro_lines(1)
        self.assertGreaterEqual(len(lines), 2)
        joined = " ".join(t for _, t in lines)
        self.assertIn("Journey for लबासना", joined)
        self.assertLessEqual(len(joined.split()), 80, "outro must be short")
        seen = {" ".join(t for _, t in pod._build_outro_lines(9))
                for _ in range(5)}
        self.assertEqual(len(seen), 5)

    def test_n_two_host_episode_structure(self):
        body = [("FEMALE", "concept body"), ("MALE", "more body")]
        ep = pod._assemble_episode(3, body)
        head = " ".join(t for _, t in ep[:2])
        tail = " ".join(t for _, t in ep[-2:])
        self.assertIn("Journey for लबासना", head, "ad must open")
        self.assertIn("Journey for लबासना", tail, "outro must close")
        speakers = {s for s, _ in ep}
        self.assertEqual(speakers, {"FEMALE", "MALE"})
        texts = [t for _, t in ep]
        self.assertIn("concept body", texts)
        self.assertIn(pod.TAKEAWAY_BRIDGE[1], texts)
        bridge_idx = texts.index(pod.TAKEAWAY_BRIDGE[1])
        self.assertGreater(bridge_idx, 2)
        self.assertLess(bridge_idx, len(texts) - 2)


# ---------------------------------------------------------------------------
# O/P — question numbering + detailed explanations
# ---------------------------------------------------------------------------

class QuestionScriptCases(Phase6Base):
    def test_o_question_numbers_spoken_in_order(self):
        prompts = []

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            prompts.append(prompt)
            return "[FEMALE] concept talk\n[MALE] example talk"

        with patch.object(pod, "_gemini_generate", fake_gen):
            lines = run(pod._generate_question_script(
                1, [(1, "Q1 block"), (2, "Q2 block")], KEY_A))
        texts = [t for _, t in lines]
        i1 = next(i for i, t in enumerate(texts)
                  if "Question Number 1" in t)
        c1 = next(i for i, t in enumerate(texts)
                  if "Question Number 1" in t and "complete" in t)
        i2 = next(i for i, t in enumerate(texts)
                  if "Question Number 2" in t)
        self.assertLess(i1, c1)
        self.assertLess(c1, i2, "Q1 must finish before Q2 starts")
        self.assertTrue(texts[i1].startswith("Ab hum Question Number 1"))

    def test_o_markers_present_even_if_model_omits(self):
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[MALE] no numbers here")):
            lines = run(pod._generate_question_script(
                1, [(5, "Q5 block")], KEY_A))
        joined = " ".join(t for _, t in lines)
        self.assertIn("Question Number 5", joined)

    def test_p_detailed_explanation_prompt(self):
        prompts = []

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            prompts.append(prompt)
            return "[FEMALE] x"

        with patch.object(pod, "_gemini_generate", fake_gen):
            run(pod._generate_question_script(1, [(3, "block")], KEY_A))
        prompt = prompts[0]
        for needle in ("Question Number 3",
                       "why EACH other option is wrong",
                       "elimination technique",
                       "memory trick",
                       "revision note",
                       "real-life example",
                       "correct answer"):
            self.assertIn(needle, prompt)

    def test_empty_dialogue_raises(self):
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="no tags here")):
            with self.assertRaises(RuntimeError):
                run(pod._generate_question_script(1, [(1, "b")], KEY_A))


# ---------------------------------------------------------------------------
# Q — Test Series range flow
# ---------------------------------------------------------------------------

class TestSeriesFlowCases(Phase6Base):
    def setUp(self):
        super().setUp()
        self.quiz = make_quiz("qz1", 5)
        self.qpatch = patch.object(pod, "_quiz_repo",
                                   return_value=FakeQuizRepo([self.quiz]))
        self.qpatch.start()
        self.addCleanup(self.qpatch.stop)

    def _with_key(self, uid):
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)

    def test_q_tokenizer_ranges(self):
        self.assertEqual(pod._tokenize_numbers("1-10"), list(range(1, 11)))
        self.assertEqual(pod._tokenize_numbers("5-15"), list(range(5, 16)))
        self.assertEqual(pod._tokenize_numbers("3"), [3])
        self.assertEqual(pod._tokenize_numbers("1-3,5"), [1, 2, 3, 5])
        self.assertEqual(pod._tokenize_numbers("1, 1, 2"), [1, 2])
        self.assertEqual(pod._tokenize_numbers("1 - 3"), [1, 2, 3])
        for bad in ("abc", "10-1", "", "1-x"):
            with self.assertRaises(ValueError, msg=bad):
                pod._tokenize_numbers(bad)

    def test_q_no_quizzes_hint(self):
        self._with_key(201)
        with patch.object(pod, "_quiz_repo",
                          return_value=FakeQuizRepo([])):
            upd, ctx, bot = self.cbq(201, "podmenu_201_ts")
            run(pod.podcast_menu_callback(upd, ctx))
        self.assertIn("/create", upd.callback_query.message.text)

    def test_q_pick_quiz_then_range_generates_exact(self):
        self._with_key(202)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        upd, _, _ = self.cbq(202, "podmenu_202_ts")
        run(pod.podcast_menu_callback(upd, ctx))
        self.assertEqual(pod.PODCAST_SESSIONS[202]["step"], "ts_pick")
        upd2, _, _ = self.cbq(202, "podts_202_qz1")
        run(pod.podcast_testseries_callback(upd2, ctx))
        self.assertIn("1-10", upd2.callback_query.message.text)
        captured = {}

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            captured.setdefault("prompts", []).append(prompt)
            return "[FEMALE] d\n[MALE] e"

        async def fake_tts(lines, out, api_key, progress_cb=None):
            Path(out).write_bytes(b"mp3")

        with patch.object(pod, "_gemini_generate", fake_gen), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts):
            msg = FakeMessage(chat_id=100, text="2-4", from_uid=202)
            run(pod.podcast_range_text(FakeUpdate(202, 100, message=msg),
                                       ctx))
        # Exactly Q2, Q3, Q4 -- no skips, no extras, in order.
        self.assertEqual(len(captured["prompts"]), 3)
        for prompt, qnum in zip(captured["prompts"], (2, 3, 4)):
            self.assertIn(f"SOURCE — QUESTION {qnum}", prompt)
            self.assertIn(f"Stem {qnum}?", prompt)
        self.assertEqual(len(bot.audios), 1)

    def test_q_missing_numbers_reported_not_skipped(self):
        self._with_key(203)
        pod.PODCAST_SESSIONS[203] = {"step": "await_range", "chat_id": 100,
                                     "qid": "qz1", "qname": "Quiz qz1",
                                     "count": 5}
        bot = FakeBot()
        ctx = FakeCtx(bot)
        with patch.object(pod, "_gemini_generate",
                          AsyncMock()) as gen:
            msg = FakeMessage(chat_id=100, text="4-7", from_uid=203)
            run(pod.podcast_range_text(FakeUpdate(203, 100, message=msg),
                                       ctx))
            gen.assert_not_called()
        joined = "\n".join(all_texts(bot))
        self.assertIn("6, 7", joined)
        self.assertIn("1-5", joined)

    def test_q_range_cap_documented(self):
        self._with_key(204)
        pod.PODCAST_SESSIONS[204] = {"step": "await_range", "chat_id": 100,
                                     "qid": "qz1", "qname": "Q", "count": 500}
        bot = FakeBot()
        ctx = FakeCtx(bot)
        msg = FakeMessage(chat_id=100, text="1-301", from_uid=204)
        run(pod.podcast_range_text(FakeUpdate(204, 100, message=msg),
                                   ctx))
        self.assertIn("max 300", "\n".join(all_texts(bot)))

    def test_q_wrong_session_qid_rejected(self):
        self._with_key(205)
        pod.PODCAST_SESSIONS[205] = {"step": "ts_pick", "chat_id": 100,
                                     "qids": ["qz1"]}
        upd, ctx, bot = self.cbq(205, "podts_205_other")
        run(pod.podcast_testseries_callback(upd, ctx))
        self.assertIn("is session ka nahi", upd.callback_query.message.text)

    def test_quiz_block_formatting(self):
        block = pod._quiz_question_block(2, {
            "question": "2+2?", "options": ["3", "4"],
            "correct_option_id": [0, 1], "explanation": ""})
        self.assertIn("Question: 2+2?", block)
        self.assertIn("A) 3", block)
        self.assertIn("Correct Answer: A, B", block)
        self.assertIn("Not provided", block)
        single = pod._quiz_question_block(1, {
            "question": "s", "options": ["a", "b"],
            "correct_option": 1, "explanation": "why"})
        self.assertIn("Correct Answer: B", single)


# ---------------------------------------------------------------------------
# R/S/T/U — PDF detection + selection caps
# ---------------------------------------------------------------------------

QUESTION_PDF_TEXT = """General Knowledge Test

Q.1. What is 2 + 2?
A) 3
B) 4
C) 5
D) 6
Answer: B

Q2. Capital of France?
a) London
b) Paris
c) Rome
d) Madrid
Correct Answer: b

Q 3. Which gas do plants absorb?
A) Oxygen
B) Nitrogen
C) Carbon dioxide
D) Hydrogen
Answer: C

Question 4. 15 + 27 = ?
A) 40
B) 41
C) 42
D) 43
Answer: C
"""

CONTENT_PDF_TEXT = """The Mauryan Empire (322-185 BCE) was a geographically
extensive Iron Age historical power. Chandragupta Maurya founded the
empire with the help of Chanakya. Ashoka the Great ruled almost the
entire Indian subcontinent. The administration was highly centralised
with a large standing army and an efficient spy system.
"""


class PdfDetectionCases(Phase6Base):
    def test_r_question_detection(self):
        self.assertTrue(pod._looks_like_questions(QUESTION_PDF_TEXT))
        blocks = pod._extract_question_blocks(QUESTION_PDF_TEXT)
        self.assertEqual([n for n, _ in blocks], [1, 2, 3, 4])
        self.assertIn("2 + 2", blocks[0][1])
        self.assertIn("Answer: B", blocks[0][1])

    def test_r_content_not_detected_as_questions(self):
        self.assertFalse(pod._looks_like_questions(CONTENT_PDF_TEXT))

    def test_r_question_prefix_variants(self):
        for variant in ("Q.5. stem here x", "Q5. stem here x",
                        "Q 5. stem here x", "Question 5. stem here x",
                        "5. stem here x"):
            text = f"{variant}\nA) a\nB) b\nC) c\nD) d\n" * 2
            blocks = pod._extract_question_blocks(text)
            self.assertTrue(blocks, variant)

    def test_s_page_selection(self):
        self.assertEqual(pod._parse_selection("1,3,7", 13), [1, 3, 7])
        self.assertEqual(pod._parse_selection("1-3", 13), [1, 2, 3])
        self.assertEqual(pod._parse_selection("default", 20),
                         list(range(1, 11)))
        self.assertEqual(pod._parse_selection("all", 5), [1, 2, 3, 4, 5])

    def test_t_max_10_questions_enforced(self):
        with self.assertRaises(ValueError):
            pod._parse_selection("1-11", 20)
        with self.assertRaises(ValueError):
            pod._parse_selection("1,2,3,4,5,6,7,8,9,10,11", 20)

    def test_u_max_10_pages_and_bounds(self):
        with self.assertRaises(ValueError):
            pod._parse_selection("1-6", 5)
        with self.assertRaises(ValueError):
            pod._parse_selection("0", 5)
        self.assertEqual(pod._parse_selection("1-10", 20), list(range(1, 11)))


# ---------------------------------------------------------------------------
# V/W — chunking + MP3 merge/split
# ---------------------------------------------------------------------------

class ChunkAudioCases(Phase6Base):
    def test_v_tts_chunking_order_no_loss_no_dup(self):
        lines = [("FEMALE" if i % 2 == 0 else "MALE", f"line-{i}-" + "x" * 50)
                 for i in range(40)]
        chunks = pod._chunk_lines_for_tts(lines, max_chars=500)
        self.assertGreater(len(chunks), 1)
        flat = [ln for ch in chunks for ln in ch]
        self.assertEqual(flat, lines)
        self.assertEqual(len({id(l) for l in flat}), len(lines))

    def test_v_single_small_chunk(self):
        lines = [("FEMALE", "hi"), ("MALE", "hello")]
        self.assertEqual(pod._chunk_lines_for_tts(lines), [lines])

    def test_w_merge_calls_ffmpeg_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            wavs = []
            for i in range(2):
                p = work / f"{i:04d}.wav"
                p.write_bytes(b"RIFF-fake")
                wavs.append(p)
            out = str(work / "out.mp3")
            seen = {}

            async def fake_run(*args, capture_stdout=False):
                seen["args"] = args
                Path(out).write_bytes(b"mp3")
                return 0, ""

            with patch.object(pod, "_run_cmd", fake_run):
                run(pod._merge_wavs_to_mp3(wavs, out, work))
            args = seen["args"]
            self.assertEqual(args[0], "ffmpeg")
            self.assertIn("libmp3lame", args)
            self.assertIn(out, args)
            concat = (work / "concat.txt").read_text()
            self.assertIn("0000.wav", concat)
            self.assertIn("0001.wav", concat)

    def test_w_merge_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            w = work / "0000.wav"
            w.write_bytes(b"x")
            with patch.object(pod, "_run_cmd",
                              AsyncMock(return_value=(1, "boom"))):
                with self.assertRaises(RuntimeError):
                    run(pod._merge_wavs_to_mp3([w], str(work / "o.mp3"),
                                               work))

    def test_v_split_small_file_untouched(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3",
                                         delete=False) as fh:
            fh.write(b"tiny")
            path = fh.name
        try:
            with patch.object(pod, "_run_cmd",
                              AsyncMock(side_effect=AssertionError(
                                  "no ffmpeg needed"))) as m:
                self.assertEqual(run(pod._split_audio_if_needed(path)), [path])
                m.assert_not_called()
        finally:
            os.unlink(path)

    def test_v_split_big_file_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.mp3"
            big.write_bytes(b"x" * (3 * 1024 * 1024))

            async def fake_run(*a, capture_stdout=False):
                if a[0] == "ffprobe":
                    return 0, "60.0\n"
                # Simulate ffmpeg segment output files.
                for i in range(3):
                    Path(a[-1].replace("%03d", f"{i:03d}")).write_bytes(b"part")
                return 0, ""

            with patch.object(pod, "_run_cmd", fake_run), \
                 patch.object(pod.config, "TEMP_DIR", Path(tmp)):
                parts = run(pod._split_audio_if_needed(
                    str(big), max_bytes=1024 * 1024))
            self.assertEqual(len(parts), 3)
            self.assertEqual(parts, sorted(parts))
            for p in parts:
                os.unlink(p)

    def test_v_split_fallback_on_probe_failure(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3",
                                         delete=False) as fh:
            fh.write(b"x" * 100)
            path = fh.name
        try:
            with patch.object(pod, "_run_cmd",
                              AsyncMock(return_value=(1, ""))):
                self.assertEqual(
                    run(pod._split_audio_if_needed(path, max_bytes=10)),
                    [path])
        finally:
            os.unlink(path)

    def test_probe_duration_parse(self):
        with patch.object(pod, "_run_cmd",
                          AsyncMock(return_value=(0, "12.50\n"))):
            self.assertAlmostEqual(run(pod._probe_duration("x")), 12.5)
        with patch.object(pod, "_run_cmd",
                          AsyncMock(return_value=(1, ""))):
            self.assertIsNone(run(pod._probe_duration("x")))

    def test_multipart_send_and_cleanup(self):
        uid = 301
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p1 = str(Path(tmp) / "a.mp3")
        p2 = str(Path(tmp) / "b.mp3")
        Path(p1).write_bytes(b"1")
        Path(p2).write_bytes(b"2")
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge",
                          AsyncMock()), \
             patch.object(pod, "_split_audio_if_needed",
                          AsyncMock(return_value=[p1, p2])):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "topic",
                                      "content", "lbl"))
        self.assertEqual(len(bot.audios), 2)
        self.assertIn("Part 1/2", bot.audios[0]["caption"])
        self.assertIn("Part 2/2", bot.audios[1]["caption"])
        self.assertFalse(os.path.exists(p1))
        self.assertFalse(os.path.exists(p2))


# ---------------------------------------------------------------------------
# Gemini plumbing: validation, retries, errors, TTS extraction
# ---------------------------------------------------------------------------

class GeminiPlumbingCases(Phase6Base):
    def test_classify_table(self):
        self.assertEqual(
            pod._classify_gemini_error(Exception("400 API_KEY_INVALID")).kind,
            "invalid_key")
        self.assertEqual(
            pod._classify_gemini_error(Exception("429 quota exceeded")).kind,
            "quota")
        self.assertEqual(
            pod._classify_gemini_error(Exception("503 unavailable")).kind,
            "transient")
        self.assertEqual(
            pod._classify_gemini_error(Exception("weird")).kind, "other")

    def test_retry_then_success(self):
        models = FakeGenModels(
            script="[FEMALE] ok",
            exc_sequence=[Exception("503 unavailable"),
                          Exception("500 boom")])
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(models)), \
             patch.object(pod, "_retry_delay", AsyncMock()) as nap:
            out = run(pod._gemini_generate("p", KEY_A, retries=2))
        self.assertEqual(out, "[FEMALE] ok")
        self.assertEqual(len(models.calls), 3)
        self.assertEqual(nap.await_count, 2)

    def test_no_retry_on_invalid_key(self):
        models = FakeGenModels(exc_sequence=[Exception("API_KEY_INVALID")])
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(models)):
            with self.assertRaises(pod.GeminiRequestError) as cm:
                run(pod._gemini_generate("p", KEY_A, retries=2))
        self.assertEqual(cm.exception.kind, "invalid_key")
        self.assertEqual(len(models.calls), 1)

    def test_no_retry_on_quota(self):
        models = FakeGenModels(exc_sequence=[Exception("429 quota bad")])
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(models)):
            with self.assertRaises(pod.GeminiRequestError) as cm:
                run(pod._gemini_generate("p", KEY_A, retries=2))
        self.assertEqual(cm.exception.kind, "quota")
        self.assertEqual(len(models.calls), 1)

    def test_transient_exhaustion_raises(self):
        models = FakeGenModels(exc_sequence=[Exception("503 x")] * 5)
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(models)), \
             patch.object(pod, "_retry_delay", AsyncMock()):
            with self.assertRaises(pod.GeminiRequestError) as cm:
                run(pod._gemini_generate("p", KEY_A, retries=2))
        self.assertEqual(cm.exception.kind, "transient")
        self.assertEqual(len(models.calls), 3)

    def test_validate_ok_invalid_quota(self):
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(FakeGenModels("OK"))):
            ok, _msg = run(pod._validate_gemini_key(KEY_A))
            self.assertTrue(ok)
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(FakeGenModels(
                              exc_sequence=[Exception("invalid api key")]))):
            ok, msg = run(pod._validate_gemini_key("bad"))
            self.assertFalse(ok)
            self.assertIn("valid nahi", msg)
        with patch.object(pod, "_new_genai_client",
                          return_value=FakeGenClient(FakeGenModels(
                              exc_sequence=[Exception("quota gone")]))):
            ok, msg = run(pod._validate_gemini_key("q"))
            self.assertFalse(ok)
            self.assertIn("quota", msg)

    def test_user_facing_error_redacts(self):
        msg = pod._user_facing_error(Exception(f"bad {KEY_A} thing"),
                                     secrets=[KEY_A])
        self.assertNotIn(KEY_A, msg)
        self.assertIn("[redacted]", msg)
        self.assertIn("Audio taiyaar",
                      pod._user_facing_error(Exception("ffmpeg missing")))
        fatal = pod.GeminiRequestError("quota", "QMSG")
        self.assertEqual(pod._user_facing_error(fatal), "❌ QMSG")

    def test_pcm_extraction_shapes(self):
        inline = SimpleNamespace(data=b"pcm-bytes")
        part = SimpleNamespace(inline_data=inline)
        resp = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[part]))])
        self.assertEqual(pod._pcm_from_response(resp), b"pcm-bytes")
        inline2 = SimpleNamespace(
            data=base64.b64encode(b"pcm2").decode())
        part2 = SimpleNamespace(inline_data=inline2)
        resp2 = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[part2]))])
        self.assertEqual(pod._pcm_from_response(resp2), b"pcm2")
        with self.assertRaises(RuntimeError):
            pod._pcm_from_response(SimpleNamespace(candidates=[]))

    def test_tts_chunk_writes_wav_and_retries(self):
        pcm = b"\x00\x01" * 100
        inline = SimpleNamespace(data=pcm)
        part = SimpleNamespace(inline_data=inline)
        tts_resp = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[part]))])
        models = FakeGenModels(script="unused")
        models.calls = []
        orig = models.generate_content

        def gen(model, contents, config=None):
            models.calls.append({"model": model, "contents": contents,
                                 "config": config})
            if len(models.calls) == 1:
                raise Exception("503 unavailable")
            return tts_resp

        models.generate_content = gen
        with tempfile.TemporaryDirectory() as tmp:
            wav = str(Path(tmp) / "t.wav")
            with patch.object(pod, "_new_genai_client",
                              return_value=FakeGenClient(models)), \
                 patch.object(pod, "_retry_delay", AsyncMock()):
                run(pod._gemini_tts_chunk([("FEMALE", "namaste")], wav,
                                           KEY_A))
            self.assertEqual(len(models.calls), 2)
            self.assertEqual(models.calls[0]["model"], pod.GEMINI_TTS_MODEL)
            import wave as _wave
            with _wave.open(wav, "rb") as wf:
                self.assertEqual(wf.getframerate(), 24000)
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.readframes(100), pcm)

    def test_generation_quota_message_exact(self):
        uid = 302
        bot = FakeBot()
        ctx = FakeCtx(bot)
        sent_msgs = []
        orig = bot.send_message

        async def _send(chat_id, text, **kw):
            m = await orig(chat_id, text, **kw)
            sent_msgs.append(m)
            return m

        bot.send_message = _send
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(side_effect=pod.GeminiRequestError(
                              "quota", pod._QUOTA_MSG))):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "t",
                                      "content", "l"))
        self.assertEqual(len(sent_msgs), 1)
        self.assertIn("quota abhi available nahi", sent_msgs[0].text)
        self.assertEqual(bot.audios, [])

    def test_generation_invalid_key_prompts_reset(self):
        uid = 303
        bot = FakeBot()
        ctx = FakeCtx(bot)
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(side_effect=pod.GeminiRequestError(
                              "invalid_key", "bad"))):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "t",
                                      "content", "l"))
        joined = "\n".join(all_texts(bot))
        self.assertIn("पहले अपनी Gemini API Key सेट करें", joined)


# ---------------------------------------------------------------------------
# X — pre-existing functionality preserved
# ---------------------------------------------------------------------------

def _question_pdf_bytes():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Q.1. What is 2+2?\nA) 3\nB) 4\nC) 5\nD) 6\nAnswer: B\n\n"
        "Q.2. Capital of France?\nA) London\nB) Paris\nC) Rome\nD) Madrid\n"
        "Answer: B\n",
    )
    return doc.tobytes()


def _content_pdf_bytes():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), CONTENT_PDF_TEXT)
    return doc.tobytes()


class ExistingFunctionalityCases(Phase6Base):
    def test_x_model_constants_explicit(self):
        self.assertEqual(pod.GEMINI_TEXT_MODEL, "gemini-2.5-flash")
        self.assertTrue(pod.GEMINI_TTS_MODEL.startswith("gemini-2.5-flash"))
        self.assertIn("tts", pod.GEMINI_TTS_MODEL)
        self.assertEqual(pod.TTS_VOICE_FEMALE, "Kore")
        self.assertEqual(pod.TTS_VOICE_MALE, "Puck")

    def test_x_parse_dialogue(self):
        raw = "[FEMALE] hello\nnoise\n[MALE]: world\n[female] again"
        self.assertEqual(pod._parse_dialogue(raw),
                         [("FEMALE", "hello"), ("MALE", "world"),
                          ("FEMALE", "again")])
        self.assertEqual(pod._parse_dialogue("nothing"), [])

    def test_x_legacy_comma_selection(self):
        self.assertEqual(pod._parse_selection("1,3,7,8,9,10,13", 13),
                         [1, 3, 7, 8, 9, 10, 13])

    def test_x_reply_to_text_flow(self):
        uid = 401
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        reply = FakeMessage(chat_id=100, text="photosynthesis",
                            from_uid=uid)
        msg = FakeMessage(chat_id=100, text="/podcast", from_uid=uid)
        msg.reply_to_message = reply
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge",
                          AsyncMock(side_effect=lambda l, o, k, **kw: Path(
                              o).write_bytes(b"m"))):
            run(pod.podcast_command(FakeUpdate(uid, 100, message=msg),
                                    ctx))
        self.assertEqual(len(bot.audios), 1)
        self.assertIn("लबासना", bot.audios[0]["title"])

    def test_x_progress_stage_messages(self):
        uid = 402
        bot = FakeBot()
        ctx = FakeCtx(bot)
        seen = []

        async def fake_tts(lines, out, api_key, progress_cb=None):
            if progress_cb:
                await progress_cb("audio")
            Path(out).write_bytes(b"m")

        status_msgs = []
        orig_send = bot.send_message

        async def send(chat_id, text, **kw):
            m = await orig_send(chat_id, text, **kw)
            status_msgs.append(m)
            return m

        bot.send_message = send
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "topic",
                                      "content", "lbl"))
        status = status_msgs[0]
        trail = [bot.sent[0][1], status.text] + status.edits
        joined = "\n".join(trail)
        for stage in ("script तैयार", "Voices generate", "Audio तैयार",
                      "तैयार है"):
            self.assertIn(stage, joined)

    def test_x_pdf_question_default10_flow(self):
        uid = 403
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        bot.files["pdf1"] = _question_pdf_bytes()
        pod.PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": 100}
        msg = FakeMessage(chat_id=100, from_uid=uid)
        msg.document = FakeDoc(file_id="pdf1")
        run(pod.podcast_document(FakeUpdate(uid, 100, message=msg), ctx))
        sess = pod.PODCAST_SESSIONS.get(uid)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["mode"], "question")
        self.assertEqual(len(sess["questions"]), 2)
        prompts = []

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            prompts.append(prompt)
            return "[FEMALE] d\n[MALE] e"

        async def fake_tts(lines, out, api_key, progress_cb=None):
            Path(out).write_bytes(b"m")

        with patch.object(pod, "_gemini_generate", fake_gen), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts):
            upd, _, _ = self.cbq(uid, f"pod_default_{uid}_question")
            run(pod.podcast_callback(upd, ctx))
        self.assertEqual(len(prompts), 2)
        self.assertIn("SOURCE — QUESTION 1", prompts[0])
        self.assertIn("SOURCE — QUESTION 2", prompts[1])
        self.assertEqual(len(bot.audios), 1)

    def test_x_pdf_content_default_flow(self):
        uid = 404
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        bot = FakeBot()
        ctx = FakeCtx(bot)
        bot.files["pdf2"] = _content_pdf_bytes()
        pod.PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": 100}
        msg = FakeMessage(chat_id=100, from_uid=uid)
        msg.document = FakeDoc(file_id="pdf2")
        run(pod.podcast_document(FakeUpdate(uid, 100, message=msg), ctx))
        self.assertEqual(pod.PODCAST_SESSIONS[uid]["mode"], "content")

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            assert "study/content material" in prompt
            return "[FEMALE] d\n[MALE] e"

        async def fake_tts(lines, out, api_key, progress_cb=None):
            Path(out).write_bytes(b"m")

        with patch.object(pod, "_gemini_generate", fake_gen), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts):
            upd, _, _ = self.cbq(uid, f"pod_default_{uid}_content")
            run(pod.podcast_callback(upd, ctx))
        self.assertEqual(len(bot.audios), 1)

    def test_x_pdf_custom_selection_and_errors(self):
        uid = 405
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        pages = [f"page {i} text" for i in range(1, 6)]
        pod.PODCAST_SESSIONS[uid] = {"step": "custom", "chat_id": 100,
                                     "mode": "content", "pages": pages,
                                     "path": None}
        bot = FakeBot()
        ctx = FakeCtx(bot)
        seen = {}

        async def fake_gen(prompt, api_key, max_tokens=4096, retries=2):
            seen["prompt"] = prompt
            return "[FEMALE] d\n[MALE] e"

        async def fake_tts(lines, out, api_key, progress_cb=None):
            Path(out).write_bytes(b"m")

        with patch.object(pod, "_gemini_generate", fake_gen), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts):
            msg = FakeMessage(chat_id=100, text="1,3", from_uid=uid)
            run(pod.podcast_custom_text(FakeUpdate(uid, 100, message=msg),
                                        ctx))
        self.assertIn("[PAGE 1]", seen["prompt"])
        self.assertIn("[PAGE 3]", seen["prompt"])
        self.assertNotIn("[PAGE 2]", seen["prompt"])
        # Out-of-range reports clearly, session kept for retry.
        pod.PODCAST_SESSIONS[uid] = {"step": "custom", "chat_id": 100,
                                     "mode": "content", "pages": pages,
                                     "path": None}
        msg2 = FakeMessage(chat_id=100, text="99", from_uid=uid)
        run(pod.podcast_custom_text(FakeUpdate(uid, 100, message=msg2),
                                    ctx))
        self.assertIn("outside 1-5", "\n".join(all_texts(bot)))
        self.assertEqual(pod.PODCAST_SESSIONS[uid]["step"], "custom")

    def test_x_pdf_custom_question_missing_reported(self):
        uid = 406
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        pod.PODCAST_SESSIONS[uid] = {
            "step": "custom", "chat_id": 100, "mode": "question",
            "questions": [(1, "b1"), (2, "b2")], "path": None}
        bot = FakeBot()
        ctx = FakeCtx(bot)
        msg = FakeMessage(chat_id=100, text="1,5", from_uid=uid)
        run(pod.podcast_custom_text(FakeUpdate(uid, 100, message=msg),
                                    ctx))
        self.assertIn("5", "\n".join(all_texts(bot)))
        self.assertIn("नहीं मिले", "\n".join(all_texts(bot)))

    def test_x_cancel_flow(self):
        uid = 407
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        pod.PODCAST_SESSIONS[uid] = {"step": "select", "chat_id": 100,
                                     "path": path}
        upd, ctx, bot = self.cbq(uid, f"pod_cancel_{uid}")
        run(pod.podcast_callback(upd, ctx))
        self.assertNotIn(uid, pod.PODCAST_SESSIONS)
        self.assertFalse(os.path.exists(path))
        self.assertIn("cancelled", upd.callback_query.message.text.lower())

    def test_x_non_pdf_document_rejected(self):
        uid = 408
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        pod.PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": 100}
        bot = FakeBot()
        ctx = FakeCtx(bot)
        msg = FakeMessage(chat_id=100, from_uid=uid)
        msg.document = FakeDoc(file_name="notes.txt", mime_type="text/plain")
        run(pod.podcast_document(FakeUpdate(uid, 100, message=msg), ctx))
        self.assertIn("PDF upload", "\n".join(all_texts(bot)))

    def test_x_zero_question_fallback_to_content(self):
        self.assertFalse(pod._looks_like_questions("Q lonely header\n\nplain text"))
        blocks = pod._extract_question_blocks("Q lonely header\n\nplain text")
        # Fallback keeps sequential numbering rather than crashing.
        self.assertTrue(all(isinstance(n, int) for n, _ in blocks))

    def test_x_register_single_application(self):
        seen = []

        class FakeApp:
            def add_handler(self, handler, group=0):
                seen.append((handler, group))

        pod.register(FakeApp())
        from telegram.ext import (CallbackQueryHandler, CommandHandler,
                                  MessageHandler)
        cmds = [h for h, _g in seen if isinstance(h, CommandHandler)]
        cbs = [h for h, _g in seen if isinstance(h, CallbackQueryHandler)]
        msgs = [h for h, _g in seen if isinstance(h, MessageHandler)]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(len(cbs), 4)
        self.assertEqual(len(msgs), 5)
        import re as _re
        pats = [h.pattern.pattern if hasattr(h.pattern, "pattern") else h.pattern
                for h in cbs]
        self.assertTrue(any(_re.match(p, "pod_default_1_x") for p in pats))
        self.assertTrue(any(_re.match(p, "podkey_1_set") for p in pats))
        self.assertTrue(any(_re.match(p, "podmenu_1_ts") for p in pats))
        self.assertTrue(any(_re.match(p, "podts_1_qz") for p in pats))
        # No second polling client may be introduced by this module.
        src = Path(pod.__file__).read_text()
        self.assertNotIn("ApplicationBuilder", src)
        self.assertNotIn("Updater(", src)
        self.assertNotIn("run_polling", src)


class PreservedBehaviorExtraCases(Phase6Base):
    def test_menu_pdf_and_topic_set_source_step(self):
        uid = 501
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        for action in ("pdf", "topic"):
            upd, ctx, bot = self.cbq(uid, f"podmenu_{uid}_{action}")
            run(pod.podcast_menu_callback(upd, ctx))
            self.assertEqual(pod.PODCAST_SESSIONS[uid]["step"], "source")

    def test_menu_without_key_asks_key(self):
        upd, ctx, bot = self.cbq(502, "podmenu_502_pdf")
        run(pod.podcast_menu_callback(upd, ctx))
        self.assertIn("Gemini API Key",
                      upd.callback_query.message.text)

    def test_podcast_text_source_flow(self):
        uid = 503
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        pod.PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": 100}
        bot = FakeBot()
        ctx = FakeCtx(bot)
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge",
                          AsyncMock(side_effect=lambda l, o, k, **kw: Path(
                              o).write_bytes(b"m"))):
            msg = FakeMessage(chat_id=100, text="photosynthesis detail",
                              from_uid=uid)
            run(pod.podcast_text(FakeUpdate(uid, 100, message=msg), ctx))
        self.assertEqual(len(bot.audios), 1)
        self.assertNotIn(uid, pod.PODCAST_SESSIONS)

    def test_podkey_back_returns_to_menu(self):
        uid = 504
        self.keys.store[uid] = sec.encrypt_api_key(KEY_A)
        upd, ctx, bot = self.cbq(uid, f"podkey_{uid}_back")
        run(pod.podcast_key_callback(upd, ctx))
        self.assertIn("Source chunein", upd.callback_query.message.text)

    def test_extract_tagged_questions_exact(self):
        src = "[QUESTION 2]\nblock two\n\n[QUESTION 4]\nblock four\n"
        self.assertEqual(pod._extract_tagged_questions(src),
                         [(2, "block two"), (4, "block four")])
        # Untagged sources fall back to plain block extraction.
        self.assertEqual(pod._extract_tagged_questions("Q.1. a\n\nQ.2. b")[0][0], 1)

    def test_script_prompt_tasks(self):
        qp = pod._script_prompt("src", "question", "lbl")
        self.assertIn("question material (lbl)", qp)
        self.assertIn("why EACH other option is wrong", qp)
        cp = pod._script_prompt("src", "content", "lbl")
        self.assertIn("study/content material (lbl)", cp)


if __name__ == "__main__":
    unittest.main()
