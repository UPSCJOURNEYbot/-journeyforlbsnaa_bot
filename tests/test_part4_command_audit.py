"""PART 4 -- audit of the remaining unaudited commands (offline).

Covers (29 commands):
  /pause /resume /stop /pdfquiz /features /limit /leaders /aspirants
  /add /rem /remall /auth /batch /createbatch /searchbatch
  /edit /stopedit /myquizzes /del /info /search /quiz /setpromo /listquiz
  /whtml /settings /remove /mywords /clearlist

Offline: no Telegram network, no MongoDB (repositories mocked).
Every test uses a fresh UID to avoid the creator rate-limiter.
"""

from __future__ import annotations

import asyncio
import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_uids = itertools.count(950000)


def _uid() -> int:
    return next(_uids)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def make_creator_msg(text, uid=None, chat_id=None, chat_type="private"):
    uid = _uid() if uid is None else uid
    chat_id = uid if chat_id is None else chat_id
    m = SimpleNamespace(
        id=1,
        from_user=SimpleNamespace(id=uid, first_name="Tester"),
        text=text,
        command=text.split(),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        reply_to_message=None,
        reply=AsyncMock(),
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
        reply_photo=AsyncMock(),
        document=None,
        photo=None,
        poll=None,
    )
    # reply() returns a fake "pending" message with edit_text/delete
    pending = SimpleNamespace(
        edit_text=AsyncMock(), delete=AsyncMock(), reply=AsyncMock(),
    )
    m.reply.return_value = pending
    m._pending = pending
    m._uid = uid
    return m


def make_creator_cb(data, uid, msg=None):
    if msg is None:
        msg = SimpleNamespace(
            edit_text=AsyncMock(), reply=AsyncMock(),
            delete=AsyncMock(), reply_document=AsyncMock(),
        )
    cb = SimpleNamespace(
        id="cb1",
        from_user=SimpleNamespace(id=uid),
        data=data,
        message=msg,
        answer=AsyncMock(),
    )
    return cb


def make_runner_update(text="/pause", uid=None, chat_id=None, chat_type="private", args=None):
    uid = _uid() if uid is None else uid
    chat_id = uid if chat_id is None else chat_id
    message = SimpleNamespace(
        chat_id=chat_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=uid, first_name="P"),
        sender_chat=None,
        text=text,
        message_id=1,
    )
    update = SimpleNamespace(message=message, effective_user=message.from_user,
                             effective_chat=message.chat, effective_message=message)
    ctx = SimpleNamespace(args=args or [], bot=SimpleNamespace())
    return update, ctx, uid, chat_id


def _clear_creator_state(uid=None):
    from quizbot.creator_bot import state as cs
    for store in (cs.quiz_creation, cs.edit_sessions, cs.batch_sessions,
                  cs.search_state, cs.testseries_upload, cs.testseries_create):
        try:
            if uid is None:
                for k in list(store._data.keys()):
                    store.pop(k, None)
            else:
                store.pop(uid, None)
        except Exception:
            pass
    if uid is not None:
        cs.clear_quiz_list_cache(uid)
        cs._rl_hits.pop(uid, None)
        cs._rl_warned.pop(uid, None)


def _clear_runner_state():
    from quizbot.runner_bot import state as rs
    rs.PDF_QUIZ_SESSIONS.clear()
    rs.session_mgr.sessions.clear()
    rs.session_mgr._activity.clear()
    rs.last_working_ai.clear()


# ===========================================================================
# 1. Bridge dispatch: private-only enforcement
# ===========================================================================

class BridgePrivateEnforcementCases(unittest.TestCase):
    """Legacy Pyrogram registrations mark these private-only; the single-bot
    bridge must preserve that, otherwise private data leaks into groups."""

    def _private_cmds_in_bridge(self):
        # Runtime check: capture the filters each CommandHandler is
        # registered with. Private-only commands get ChatType.PRIVATE.
        from unittest.mock import MagicMock
        from quizbot.runner_bot import creator_bridge as br
        app = MagicMock()
        added = []
        app.add_handler.side_effect = lambda h, group=0: added.append(h)
        br.register_creator_bridge(app)
        priv = set()
        for h in added:
            cmds = getattr(h, "commands", None)
            if not cmds:
                continue
            # PTB CommandHandler stores frozenset of command strings; an
            # unfiltered handler defaults to UpdateType.MESSAGES, so match
            # the private filter by repr.
            has_private = repr(h.filters) == "filters.ChatType.PRIVATE"
            for c in cmds:
                if has_private:
                    priv.add(c)
        return priv

    def test_batch_family_is_private(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("batch", "createbatch", "searchbatch"):
            self.assertIn(cmd, priv, f"/{cmd} must be private-only")

    def test_auth_family_is_private(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("add", "rem", "remall", "auth"):
            self.assertIn(cmd, priv, f"/{cmd} must be private-only")

    def test_edit_family_is_private(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("edit", "stopedit"):
            self.assertIn(cmd, priv, f"/{cmd} must be private-only")

    def test_quiz_mgmt_is_private(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("myquizzes", "del", "info", "search", "quiz", "setpromo"):
            self.assertIn(cmd, priv, f"/{cmd} must be private-only")

    def test_settings_words_are_private(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("settings", "remove", "mywords", "clearlist"):
            self.assertIn(cmd, priv, f"/{cmd} must be private-only")

    def test_reports_and_misc_private(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("whtml", "limit", "leaders", "aspirants"):
            self.assertIn(cmd, priv, f"/{cmd} must be private-only")

    def test_public_cmds_stay_public(self):
        priv = self._private_cmds_in_bridge()
        for cmd in ("help", "features", "listquiz"):
            self.assertNotIn(cmd, priv, f"/{cmd} must stay group-accessible")


# ===========================================================================
# 2. /pause /resume /stop
# ===========================================================================

class PauseResumeStopCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_pause_no_session(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/pause")
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.pause_quiz(update, ctx))
            ssm.assert_awaited_once()
            self.assertIn("No quiz running", ssm.await_args.args[2])

    def test_resume_no_paused(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/resume")
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.resume_quiz(update, ctx))
            self.assertIn("No quiz paused", ssm.await_args.args[2])

    def test_stop_no_session(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/stop")
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.stop_quiz(update, ctx))
            self.assertIn("No quiz running", ssm.await_args.args[2])

    def test_pause_sets_flag_private(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/pause")
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "paused": False, "questions": []}))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.pause_quiz(update, ctx))
            self.assertTrue(session_mgr.get(cid)["paused"])
            self.assertIn("Paused", ssm.await_args.args[2])

    def test_resume_clears_flag_private(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/resume")
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "paused": True, "questions": [],
                                      "is_private": False, "waiting_for_answer": False}))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.resume_quiz(update, ctx))
            self.assertFalse(session_mgr.get(cid)["paused"])
            self.assertIn("Resumed", ssm.await_args.args[2])

    def test_resume_private_waiting_does_not_duplicate_question(self):
        """REGRESSION: resume() must not re-send the current question while a
        poll is still open (waiting_for_answer=True). Re-sending orphans the
        live poll and double-spawns timeout tasks."""
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/resume")
        _run(session_mgr.create(cid, {
            "quiz_id": "Q1", "paused": True, "questions": [{"question": "q"}],
            "is_private": True, "waiting_for_answer": True, "current_index": 0,
            "quiz_data": {"timer": 30}, "polls": {},
        }))
        with patch.object(qp, "safe_send_message", new=AsyncMock()), \
             patch.object(qp, "send_private_question", new=AsyncMock()) as spq:
            _run(qp.resume_quiz(update, ctx))
            spq.assert_not_awaited()

    def test_pause_group_requires_admin(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/pause", chat_type="group", chat_id=-1001)
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "paused": False}))
        ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.pause_quiz(update, ctx))
            self.assertIn("Admin only", ssm.await_args.args[2])
            self.assertFalse(session_mgr.get(cid)["paused"])

    def test_stop_group_requires_admin(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/stop", chat_type="supergroup", chat_id=-1002)
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "paused": False}))
        ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.stop_quiz(update, ctx))
            self.assertIn("Admin only", ssm.await_args.args[2])
            self.assertIsNotNone(session_mgr.get(cid))

    def test_pause_exception_gives_user_feedback(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/pause")
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "paused": False}))
        with patch.object(session_mgr, "update", new=AsyncMock(side_effect=RuntimeError("db down"))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.pause_quiz(update, ctx))
            # User must get SOME response, not silence
            ssm.assert_awaited()
            _clear_runner_state()


# ===========================================================================
# 3. /pdfquiz
# ===========================================================================

class PdfQuizParseRangeCases(unittest.TestCase):
    def test_valid_range(self):
        from quizbot.runner_bot.handlers.pdf_quiz import _parse_range
        self.assertEqual(_parse_range("1-10", 20), (1, 10))

    def test_all_caps(self):
        from quizbot.runner_bot.handlers.pdf_quiz import _parse_range
        s, e = _parse_range("all", 8)
        self.assertEqual((s, e), (1, 8))

    def test_single_number_raises_valueerror_not_indexerror(self):
        """REGRESSION: '5' used to raise IndexError which callers (catching
        only ValueError) missed -> generic crash / swallowed message."""
        from quizbot.runner_bot.handlers.pdf_quiz import _parse_range
        with self.assertRaises(ValueError):
            _parse_range("5", 20)

    def test_garbage_raises_valueerror(self):
        from quizbot.runner_bot.handlers.pdf_quiz import _parse_range
        with self.assertRaises(ValueError):
            _parse_range("abc", 20)

    def test_reversed_range_raises_valueerror(self):
        from quizbot.runner_bot.handlers.pdf_quiz import _parse_range
        with self.assertRaises(ValueError):
            _parse_range("5-3", 20)


class PdfQuizFlowCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_message_handler_invalid_range_replies_usefully(self):
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        from quizbot.runner_bot.state import PDF_QUIZ_SESSIONS
        uid = _uid()
        PDF_QUIZ_SESSIONS[uid] = {"step": "pages", "total_pages": 10, "chat_id": uid}
        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=uid),
                                    chat_id=uid, text="bogus!!!"))
        ctx = SimpleNamespace(bot=MagicMock())
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            consumed = _run(pq.pdfquiz_message_handler(update, ctx))
            self.assertTrue(consumed)
            self.assertIn("Invalid format", ssm.await_args.args[2])
            # session stays in pages step for retry
            self.assertEqual(PDF_QUIZ_SESSIONS[uid]["step"], "pages")

    def test_message_handler_single_number_replies_usefully(self):
        """'5' must be treated as invalid range with a useful reply, not an
        unhandled IndexError that returns False (message leaks through)."""
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        from quizbot.runner_bot.state import PDF_QUIZ_SESSIONS
        uid = _uid()
        PDF_QUIZ_SESSIONS[uid] = {"step": "pages", "total_pages": 10, "chat_id": uid}
        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=uid),
                                    chat_id=uid, text="5"))
        ctx = SimpleNamespace(bot=MagicMock())
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            consumed = _run(pq.pdfquiz_message_handler(update, ctx))
            self.assertTrue(consumed)
            ssm.assert_awaited()
            self.assertIn("Invalid format", ssm.await_args.args[2])

    def test_message_handler_valid_range_advances(self):
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        from quizbot.runner_bot.state import PDF_QUIZ_SESSIONS
        uid = _uid()
        PDF_QUIZ_SESSIONS[uid] = {"step": "pages", "total_pages": 10, "chat_id": uid}
        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=uid),
                                    chat_id=uid, text="1-3"))
        ctx = SimpleNamespace(bot=MagicMock())
        with patch.object(pq, "safe_send_message", new=AsyncMock()):
            consumed = _run(pq.pdfquiz_message_handler(update, ctx))
            self.assertTrue(consumed)
            self.assertEqual(PDF_QUIZ_SESSIONS[uid]["step"], "count")
            self.assertEqual(PDF_QUIZ_SESSIONS[uid]["page_start"], 1)
            self.assertEqual(PDF_QUIZ_SESSIONS[uid]["page_end"], 3)

    def test_message_handler_cancel_aborts_session(self):
        """REGRESSION: without a cancel path the user is stuck -- every text
        they send is hijacked as a page-range attempt with no exit."""
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        from quizbot.runner_bot.state import PDF_QUIZ_SESSIONS
        uid = _uid()
        PDF_QUIZ_SESSIONS[uid] = {"step": "pages", "total_pages": 10, "chat_id": uid,
                                  "pdf_path": "/tmp/x.pdf"}
        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=uid),
                                    chat_id=uid, text="cancel"))
        ctx = SimpleNamespace(bot=MagicMock())
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm, \
             patch.object(pq, "remove_file", new=AsyncMock()) as rm:
            consumed = _run(pq.pdfquiz_message_handler(update, ctx))
            self.assertTrue(consumed)
            self.assertNotIn(uid, PDF_QUIZ_SESSIONS)
            ssm.assert_awaited()
            rm.assert_awaited_once()

    def test_message_handler_ignores_other_chat(self):
        """Range replies must come from the same chat the wizard started in,
        otherwise group text from the same user in another chat hijacks."""
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        from quizbot.runner_bot.state import PDF_QUIZ_SESSIONS
        uid = _uid()
        PDF_QUIZ_SESSIONS[uid] = {"step": "pages", "total_pages": 10, "chat_id": -1005}
        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=uid),
                                    chat_id=uid, text="1-3"))
        ctx = SimpleNamespace(bot=MagicMock())
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            consumed = _run(pq.pdfquiz_message_handler(update, ctx))
            self.assertFalse(consumed)
            ssm.assert_not_awaited()
            self.assertEqual(PDF_QUIZ_SESSIONS[uid]["step"], "pages")

    def test_callback_malformed_data_answers_usefully(self):
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        uid = _uid()
        query = SimpleNamespace(
            data="pdfq_", from_user=SimpleNamespace(id=uid),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        update = SimpleNamespace(callback_query=query)
        ctx = SimpleNamespace(bot=MagicMock())
        _run(pq.pdfquiz_callback(update, ctx))
        # Must answer the button (no hanging spinner) even on bad data
        query.answer.assert_awaited()

    def test_callback_wrong_user_rejected(self):
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        from quizbot.runner_bot.state import PDF_QUIZ_SESSIONS
        owner = _uid()
        clicker = _uid()
        PDF_QUIZ_SESSIONS[owner] = {"step": "count", "chat_id": owner}
        query = SimpleNamespace(
            data=f"pdfq_count_{owner}_10", from_user=SimpleNamespace(id=clicker),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        update = SimpleNamespace(callback_query=query)
        ctx = SimpleNamespace(bot=MagicMock())
        _run(pq.pdfquiz_callback(update, ctx))
        # second answer call carries the rejection
        texts = [str(c) for c in query.answer.await_args_list]
        self.assertTrue(any("Not your session" in t for t in texts))

    def test_preview_escapes_html(self):
        """PDF text with <>& must be escaped in the preview edit or Telegram
        rejects the whole message with 'Can't parse entities'."""
        import inspect
        from quizbot.runner_bot.handlers import pdf_quiz as pq
        src = inspect.getsource(pq._pdfquiz_generate_flow)
        self.assertIn("esc(", src)


# ===========================================================================
# 4. /features /limit
# ===========================================================================

class FeaturesLimitCases(unittest.TestCase):
    def test_features_replies(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/features")
        _run(adm.features_cmd(MagicMock(), m))
        m.reply.assert_awaited_once()
        self.assertIn("Features", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_limit_replies_with_usage(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/limit")
        _run(adm.limit_cmd(MagicMock(), m))
        m.reply.assert_awaited_once()
        txt = m.reply.await_args.args[0]
        self.assertIn("General commands", txt)
        self.assertIn("/create", txt)
        _clear_creator_state(m._uid)


# ===========================================================================
# 5. /leaders /aspirants
# ===========================================================================

class LeadersCases(unittest.TestCase):
    def test_usage_without_arg(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/leaders")
        _run(adm.leaders_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_quiz_not_found(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/leaders NOPE123")
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(adm, "QuizRepository", return_value=repo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.leaders_cmd(MagicMock(), m))
        self.assertIn("not found", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_no_attempts(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/leaders Q1")
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value={"qid": "Q1", "quiz_name": "Quiz"})
        brepo = MagicMock()
        brepo.page = AsyncMock(return_value=[])
        with patch.object(adm, "QuizRepository", return_value=qrepo), \
             patch.object(adm, "LeaderboardRepository", return_value=brepo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.leaders_cmd(MagicMock(), m))
        self.assertIn("No one has attempted", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_leaders_sends_pages_and_cleans_status(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/leaders Q1")
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value={"qid": "Q1", "quiz_name": "Quiz"})
        rows = [{"user_name": "A", "score": 5, "total_questions": 5, "time_taken": 60}]
        brepo = MagicMock()
        brepo.page = AsyncMock(side_effect=[rows, []])
        with patch.object(adm, "QuizRepository", return_value=qrepo), \
             patch.object(adm, "LeaderboardRepository", return_value=brepo), \
             patch.object(adm, "get_db", lambda: None), \
             patch.object(adm, "send_rich_or_fallback", new=AsyncMock(return_value=True)) as srf:
            _run(adm.leaders_cmd(MagicMock(), m))
        srf.assert_awaited()
        m._pending.delete.assert_awaited()
        _clear_creator_state(m._uid)


# ===========================================================================
# 6. /add /rem /remall /auth
# ===========================================================================

class AuthFamilyCases(unittest.TestCase):
    def test_add_usage(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/add")
        with patch.object(au, "subscribe_gate", new=AsyncMock(return_value=False)):
            _run(au.add_auth_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_add_bad_id(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/add abc")
        with patch.object(au, "subscribe_gate", new=AsyncMock(return_value=False)):
            _run(au.add_auth_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_add_ok(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/add -100123")
        repo = MagicMock()
        repo.add = AsyncMock(return_value=[-100123])
        with patch.object(au, "subscribe_gate", new=AsyncMock(return_value=False)), \
             patch.object(au, "AuthChatRepository", return_value=repo), \
             patch.object(au, "get_db", lambda: None):
            _run(au.add_auth_cmd(MagicMock(), m))
        repo.add.assert_awaited_once_with(m._uid, -100123)
        self.assertIn("authorized", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_rem_usage(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/rem")
        _run(au.rem_auth_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_rem_ok(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/rem -100123")
        repo = MagicMock()
        repo.remove = AsyncMock(return_value=[])
        with patch.object(au, "AuthChatRepository", return_value=repo), \
             patch.object(au, "get_db", lambda: None):
            _run(au.rem_auth_cmd(MagicMock(), m))
        repo.remove.assert_awaited_once_with(m._uid, -100123)
        self.assertIn("removed", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_remall_ok(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/remall")
        repo = MagicMock()
        repo.clear = AsyncMock()
        with patch.object(au, "AuthChatRepository", return_value=repo), \
             patch.object(au, "get_db", lambda: None):
            _run(au.remall_auth_cmd(MagicMock(), m))
        repo.clear.assert_awaited_once_with(m._uid)
        self.assertIn("removed", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_auth_non_owner_rejected(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/auth 123 1 month")
        with patch.object(au.config, "OWNER_ID", 1), \
             patch.object(au.config, "ADMIN_IDS", []):
            _run(au.auth_cmd(MagicMock(), m))
        self.assertIn("Owner only", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_auth_bad_format(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/auth 123")
        with patch.object(au.config, "OWNER_ID", m._uid):
            _run(au.auth_cmd(MagicMock(), m))
        self.assertIn("Format", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_auth_bad_unit(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/auth 123 1 fortnight")
        with patch.object(au.config, "OWNER_ID", m._uid):
            _run(au.auth_cmd(MagicMock(), m))
        self.assertIn("Invalid unit", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_auth_negative_duration_rejected(self):
        """REGRESSION: negative/zero durations were clamped to 1 day via
        max(1, ...) and falsely granted premium instead of being rejected."""
        from quizbot.creator_bot.handlers import auth as au
        for txt in ("/auth 123 -5 days", "/auth 123 0 days"):
            m = make_creator_msg(txt)
            with patch.object(au.config, "OWNER_ID", m._uid), \
                 patch.object(au, "grant_and_notify", new=AsyncMock()) as grant:
                _run(au.auth_cmd(MagicMock(), m))
                grant.assert_not_awaited()
                self.assertIn("positive", m.reply.await_args.args[0].lower())
                _clear_creator_state(m._uid)

    def test_auth_invalid_target_rejected(self):
        """User IDs are positive; 0/negative must not be granted."""
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/auth -5 1 month")
        with patch.object(au.config, "OWNER_ID", m._uid), \
             patch.object(au, "grant_and_notify", new=AsyncMock()) as grant:
            _run(au.auth_cmd(MagicMock(), m))
            grant.assert_not_awaited()
            _clear_creator_state(m._uid)

    def test_auth_ok(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/auth 123 1 month")
        c = MagicMock()
        c.send_message = AsyncMock()
        with patch.object(au.config, "OWNER_ID", m._uid), \
             patch.object(au, "grant_and_notify", new=AsyncMock(return_value="01-Jan-2027 01:00 PM")):
            _run(au.auth_cmd(c, m))
        self.assertIn("granted premium", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)


# ===========================================================================
# 7. /batch /createbatch /searchbatch + callbacks
# ===========================================================================

class BatchCmdCases(unittest.TestCase):
    def test_createbatch_starts_wizard(self):
        from quizbot.creator_bot.handlers import batches as bat
        from quizbot.creator_bot import state as cs
        m = make_creator_msg("/createbatch")
        _run(bat.createbatch_cmd(MagicMock(), m))
        self.assertEqual(cs.batch_sessions[m._uid]["step"], "name")
        self.assertIn("batch name", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_batch_list_empty(self):
        from quizbot.creator_bot.handlers import batches as bat
        m = make_creator_msg("/batch")
        repo = MagicMock()
        repo.list_by_creator = AsyncMock(return_value=[])
        with patch.object(bat, "BatchRepository", return_value=repo), \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.batch_cmd(MagicMock(), m))
        m.reply.assert_awaited()
        _clear_creator_state(m._uid)

    def test_searchbatch_usage(self):
        from quizbot.creator_bot.handlers import batches as bat
        m = make_creator_msg("/searchbatch")
        _run(bat.searchbatch_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_searchbatch_none(self):
        from quizbot.creator_bot.handlers import batches as bat
        m = make_creator_msg("/searchbatch xyz")
        repo = MagicMock()
        repo.search = AsyncMock(return_value=[])
        with patch.object(bat, "BatchRepository", return_value=repo), \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.searchbatch_cmd(MagicMock(), m))
        self.assertIn("No batches", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_searchbatch_ok(self):
        from quizbot.creator_bot.handlers import batches as bat
        m = make_creator_msg("/searchbatch upsc")
        repo = MagicMock()
        repo.search = AsyncMock(return_value=[
            {"name": "UPSC Batch", "batch_id": "B1", "description": "desc"}])
        with patch.object(bat, "BatchRepository", return_value=repo), \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.searchbatch_cmd(MagicMock(), m))
        self.assertIn("UPSC Batch", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_batch_wizard_full_flow(self):
        from quizbot.creator_bot.handlers import batches as bat
        from quizbot.creator_bot import state as cs
        uid = _uid()
        m0 = make_creator_msg("/createbatch", uid=uid)
        _run(bat.createbatch_cmd(MagicMock(), m0))
        repo = MagicMock()
        repo.create = AsyncMock(return_value={"batch_id": "B1", "name": "N",
                                              "description": None, "contact_info": None,
                                              "payment_link": None, "chats": [], "quizzes": []})
        repo.get = AsyncMock(return_value={"batch_id": "B1", "name": "N",
                                           "description": None, "contact_info": None,
                                           "payment_link": None, "chats": [], "quizzes": []})
        with patch.object(bat, "BatchRepository", return_value=repo), \
             patch.object(bat, "get_db", lambda: None):
            for txt in ("My Batch", "skip", "skip", "skip"):
                m = make_creator_msg(txt, uid=uid)
                _run(bat.batch_input(MagicMock(), m))
        repo.create.assert_awaited_once()
        self.assertNotIn(uid, cs.batch_sessions)
        _clear_creator_state(uid)

    def test_batch_input_cancel_aborts(self):
        """REGRESSION: the batch wizard had no exit -- every private text was
        hijacked with no way to abort except completing all 4 steps."""
        from quizbot.creator_bot.handlers import batches as bat
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.batch_sessions[uid] = {"step": "desc", "name": "N"}
        m = make_creator_msg("cancel", uid=uid)
        with patch.object(bat, "BatchRepository") as _r, \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.batch_input(MagicMock(), m))
        self.assertNotIn(uid, cs.batch_sessions)
        self.assertIn("cancel", m.reply.await_args.args[0].lower())
        _clear_creator_state(uid)

    def test_batch_input_addchat_bad_id_retries(self):
        from quizbot.creator_bot.handlers import batches as bat
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.batch_sessions[uid] = {"step": "addchat", "bid": "B1"}
        m = make_creator_msg("notanid", uid=uid)
        with patch.object(bat, "BatchRepository", return_value=MagicMock()), \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.batch_input(MagicMock(), m))
        self.assertIn("numeric", m.reply.await_args.args[0].lower())
        # session kept for retry
        self.assertIn(uid, cs.batch_sessions)
        _clear_creator_state(uid)

    def test_batch_input_addqz_validates_quiz_exists(self):
        """REGRESSION: any text was 'added' as a quiz ID without checking the
        quiz exists -> false success + orphan batch_quizzes rows."""
        from quizbot.creator_bot.handlers import batches as bat
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.batch_sessions[uid] = {"step": "addqz", "bid": "B1"}
        m = make_creator_msg("NOPEQID", uid=uid)
        brepo = MagicMock()
        brepo.add_quiz = AsyncMock()
        brepo.get = AsyncMock(return_value={"batch_id": "B1", "name": "N",
                                            "chats": [], "quizzes": []})
        with patch.object(bat, "BatchRepository", return_value=brepo), \
             patch.object(bat, "get_db", lambda: None), \
             patch("quizbot.creator_bot.handlers.batches.QuizRepository") as qr:
            qr.return_value.get = AsyncMock(return_value=None)
            _run(bat.batch_input(MagicMock(), m))
        brepo.add_quiz.assert_not_awaited()
        self.assertIn("not found", m.reply.await_args.args[0].lower())
        _clear_creator_state(uid)

    def test_batch_cb_malformed_answered(self):
        """Malformed bat_ callbacks must be answered gracefully, not raise."""
        from quizbot.creator_bot.handlers import batches as bat
        uid = _uid()
        for bad in ("bat_", "bat", "bat_list", "bat_view", "bat_dorm_x"):
            cb = make_creator_cb(bad, uid)
            with patch.object(bat, "BatchRepository", return_value=MagicMock()), \
                 patch.object(bat, "get_db", lambda: None):
                _run(bat.batch_cb(MagicMock(), cb))  # must not raise
            cb.answer.assert_awaited()
        _clear_creator_state(uid)

    def test_batch_cb_wrong_user_rejected(self):
        from quizbot.creator_bot.handlers import batches as bat
        owner, clicker = _uid(), _uid()
        cb = make_creator_cb(f"bat_list_{owner}", clicker)
        with patch.object(bat, "BatchRepository", return_value=MagicMock()), \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.batch_cb(MagicMock(), cb))
        self.assertIn("Not yours", str(cb.answer.await_args))

    def test_batch_cb_view_ok(self):
        from quizbot.creator_bot.handlers import batches as bat
        uid = _uid()
        cb = make_creator_cb(f"bat_view_B1_{uid}", uid)
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"batch_id": "B1", "name": "B",
                                           "description": "d", "contact_info": None,
                                           "payment_link": None, "chats": [], "quizzes": []})
        with patch.object(bat, "BatchRepository", return_value=repo), \
             patch.object(bat, "get_db", lambda: None):
            _run(bat.batch_cb(MagicMock(), cb))
        cb.answer.assert_awaited()
        _clear_creator_state(uid)


# ===========================================================================
# 8. /edit /stopedit
# ===========================================================================

class EditCases(unittest.TestCase):
    def test_edit_usage(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        m = make_creator_msg("/edit")
        _run(qe.edit_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_edit_not_found(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        m = make_creator_msg("/edit NOPE")
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.edit_cmd(MagicMock(), m))
        self.assertIn("Not found", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_edit_no_permission(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        m = make_creator_msg("/edit Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 999,
                                           "questions": [], "quiz_name": "Q",
                                           "edit_permissions": []})
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None), \
             patch.object(qe.config, "OWNER_ID", 1), \
             patch.object(qe.config, "ADMIN_IDS", []):
            _run(qe.edit_cmd(MagicMock(), m))
        self.assertIn("No permission", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_edit_ok_opens_session(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        m = make_creator_msg("/edit Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": m._uid,
                                           "questions": [], "quiz_name": "Q",
                                           "timer": 30, "quiz_type": "free",
                                           "negative_marks": 0})
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.edit_cmd(MagicMock(), m))
        self.assertEqual(cs.edit_sessions[m._uid]["qid"], "Q1")
        self.assertIn("Quiz Editor", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_stopedit_no_session(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        m = make_creator_msg("/stopedit")
        _run(qe.stopedit_cmd(MagicMock(), m))
        self.assertIn("No active", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_stopedit_clears_session(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        m = make_creator_msg("/stopedit")
        cs.edit_sessions[m._uid] = {"qid": "Q1"}
        _run(qe.stopedit_cmd(MagicMock(), m))
        self.assertNotIn(m._uid, cs.edit_sessions)
        self.assertIn("stopped", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_edit_tree_settings_only_session_does_not_crash(self):
        """REGRESSION: /settings stores stg_field in edit_sessions WITHOUT a
        qid; edit_tree_cb read session['qid'] outside try -> KeyError."""
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.edit_sessions[uid] = {"stg_field": "default_text"}
        cb = make_creator_cb("main_Q1", uid)
        with patch.object(qe, "QuizRepository", return_value=MagicMock()), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.edit_tree_cb(MagicMock(), cb))  # must not raise
        cb.answer.assert_awaited()
        _clear_creator_state(uid)

    def test_edit_tree_stale_qid_rejected(self):
        """Buttons from an older /edit message must not operate on the quiz
        from a newer session (callback qid != session qid)."""
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.edit_sessions[uid] = {"qid": "Q_NEW", "page": 0, "field": None}
        cb = make_creator_cb("main_Q_OLD", uid)
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q_NEW", "quiz_name": "N",
                                           "questions": [], "timer": 30,
                                           "quiz_type": "free", "negative_marks": 0})
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.edit_tree_cb(MagicMock(), cb))
        answered = str(cb.answer.await_args)
        self.assertTrue("tale" in answered or "xpired" in answered or "again" in answered)
        _clear_creator_state(uid)

    def test_edit_rename_flow(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.edit_sessions[uid] = {"qid": "Q1", "page": 0, "field": "quiz_name"}
        m = make_creator_msg("New Name", uid=uid)
        repo = MagicMock()
        repo.update_field = AsyncMock()
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.handle_edit_text_input(MagicMock(), m))
        repo.update_field.assert_awaited_once_with("Q1", "quiz_name", "New Name")
        self.assertIn("Updated", m.reply.await_args.args[0])
        _clear_creator_state(uid)

    def test_edit_timer_invalid(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.edit_sessions[uid] = {"qid": "Q1", "page": 0, "field": "timer"}
        m = make_creator_msg("5", uid=uid)
        repo = MagicMock()
        repo.update_field = AsyncMock()
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.handle_edit_text_input(MagicMock(), m))
        repo.update_field.assert_not_awaited()
        self.assertIn("Error", m.reply.await_args.args[0])
        _clear_creator_state(uid)

    def test_edit_delete_range_confirm(self):
        from quizbot.creator_bot.handlers import quiz_editing as qe
        from quizbot.creator_bot import state as cs
        uid = _uid()
        cs.edit_sessions[uid] = {"qid": "Q1", "field": "delete_range"}
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1",
                                           "questions": [{"q": i} for i in range(10)]})
        m = make_creator_msg("1-3", uid=uid)
        with patch.object(qe, "QuizRepository", return_value=repo), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.handle_edit_text_input(MagicMock(), m))
        self.assertEqual(cs.edit_sessions[uid]["field"], "confirm_delete")
        self.assertIn("Confirm", m.reply.await_args.args[0])
        # confirm YES
        repo2 = MagicMock()
        repo2.get = AsyncMock(return_value={"qid": "Q1",
                                            "questions": [{"q": i} for i in range(10)]})
        repo2.update_field = AsyncMock()
        m2 = make_creator_msg("YES", uid=uid)
        with patch.object(qe, "QuizRepository", return_value=repo2), \
             patch.object(qe, "get_db", lambda: None):
            _run(qe.handle_edit_text_input(MagicMock(), m2))
        self.assertIn("Deleted", m2.reply.await_args.args[0])
        _clear_creator_state(uid)


# ===========================================================================
# 9. /myquizzes /del /info /search /setpromo /listquiz
# ===========================================================================

class QuizMgmtCases(unittest.TestCase):
    def test_myquizzes_empty(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/myquizzes")
        repo = MagicMock()
        repo.list_by_creator = AsyncMock(return_value=[])
        with patch.object(qm, "subscribe_gate", new=AsyncMock(return_value=False)), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.myquizzes_cmd(MagicMock(), m))
        m._pending.edit_text.assert_awaited()
        _clear_creator_state(m._uid)

    def test_myquizzes_shows_real_type_not_hardcoded_free(self):
        """REGRESSION: every row was hardcoded 'Free' even for paid quizzes."""
        from quizbot.creator_bot.handlers import quiz_management as qm
        target = SimpleNamespace(edit_text=AsyncMock())
        quizzes = [{"qid": "Q1", "quiz_name": "Paid Quiz", "quiz_type": "paid",
                    "total_participants": 3}]
        _run(qm._send_quiz_page(target, quizzes, 0, _uid()))
        txt = target.edit_text.await_args.args[0]
        self.assertNotIn("🆓 Free", txt)
        self.assertIn("paid", txt.lower())

    def test_myquizzes_search_results_are_pageable(self):
        """REGRESSION: search results were never cached, so Next/Prev always
        answered 'Expired -- run /myquizzes again'."""
        from quizbot.creator_bot.handlers import quiz_management as qm
        from quizbot.creator_bot import state as cs
        m = make_creator_msg("/myquizzes science")
        quizzes = [{"qid": f"Q{i}", "quiz_name": f"Science {i}",
                    "quiz_type": "free", "total_participants": 0} for i in range(12)]
        repo = MagicMock()
        repo.list_by_creator = AsyncMock(return_value=quizzes)
        with patch.object(qm, "subscribe_gate", new=AsyncMock(return_value=False)), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.myquizzes_cmd(MagicMock(), m))
        cached = cs.load_quiz_list_cache(m._uid)
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached["data"]), 12)
        _clear_creator_state(m._uid)

    def test_pagination_rejects_other_user(self):
        """REGRESSION: pagination had no ownership check -- anyone could page
        another user's list."""
        from quizbot.creator_bot.handlers import quiz_management as qm
        owner, clicker = _uid(), _uid()
        cb = make_creator_cb(f"next:0:{owner}", clicker)
        _run(qm.pagination_cb(MagicMock(), cb))
        self.assertIn("Not yours", str(cb.answer.await_args))

    def test_pagination_malformed_answered(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        uid = _uid()
        for bad in ("prev:", "next", "refresh", "prev:x:y"):
            cb = make_creator_cb(bad, uid)
            _run(qm.pagination_cb(MagicMock(), cb))  # must not raise
            cb.answer.assert_awaited()

    def test_del_usage(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/del")
        _run(qm.del_quiz_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_del_not_found(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/del NOPE")
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.del_quiz_cmd(MagicMock(), m))
        self.assertIn("Not found", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_del_not_owner(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/del Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 999})
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.del_quiz_cmd(MagicMock(), m))
        self.assertIn("Not authorized", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_del_ok(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/del Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": m._uid})
        repo.delete = AsyncMock()
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.del_quiz_cmd(MagicMock(), m))
        repo.delete.assert_awaited_once_with("Q1")
        self.assertIn("Deleted", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_info_usage(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/info")
        _run(qm.info_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_info_ok(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/info Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 777})
        c = MagicMock()
        c.get_users = AsyncMock(return_value=SimpleNamespace(first_name="Creator"))
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.info_cmd(c, m))
        self.assertIn("Creator", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_search_usage(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/search")
        _run(qm.search_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_search_too_short(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/search a")
        _run(qm.search_cmd(MagicMock(), m))
        self.assertIn("too short", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_search_ok(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/search science")
        repo = MagicMock()
        repo.search = AsyncMock(return_value=[
            {"qid": "Q1", "quiz_name": "Science", "total_participants": 2, "quiz_type": "free"}])
        repo.search_count = AsyncMock(return_value=1)
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None), \
             patch.object(qm, "get_runner_bot_username", new=AsyncMock(return_value="rbot")):
            _run(qm.search_cmd(MagicMock(), m))
        _clear_creator_state(m._uid)

    def test_search_more_rejects_other_user(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        from quizbot.creator_bot import state as cs
        owner, clicker = _uid(), _uid()
        cs.search_state[owner] = {"term": "science"}
        cb = make_creator_cb(f"srch_more_{owner}_5", clicker)
        _run(qm.search_more_cb(MagicMock(), cb))
        self.assertIn("Not yours", str(cb.answer.await_args))
        _clear_creator_state(owner)

    def test_setpromo_usage(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/setpromo")
        _run(qm.setpromo_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_setpromo_ok(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/setpromo Join @chan")
        repo = MagicMock()
        repo.set_promo_for_creator = AsyncMock(return_value=3)
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.setpromo_cmd(MagicMock(), m))
        repo.set_promo_for_creator.assert_awaited_once()
        _clear_creator_state(m._uid)

    def test_listquiz_wrong_chat_silent(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/listquiz", chat_id=-999, chat_type="group")
        with patch.object(qm.config, "BOT_GROUP", -1001):
            _run(qm.listquiz_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_listquiz_has_ratelimit(self):
        """REGRESSION: /listquiz had no ratelimit -- any group member could
        spam 200-message bursts (4s apart ~ 13 min of spam) repeatedly."""
        import inspect
        from quizbot.creator_bot.handlers import quiz_management as qm
        src = inspect.getsource(qm.listquiz_cmd)
        # decorated functions keep the wrapper name; check the decorator line
        # in the module source instead
        mod_src = inspect.getsource(qm)
        idx = mod_src.find("async def listquiz_cmd")
        window = mod_src[max(0, idx - 300):idx]
        self.assertIn("ratelimit", window)


# ===========================================================================
# 9b. /quiz (alias of /search) -- the 29th scoped command
# ===========================================================================

class QuizAliasCases(unittest.TestCase):
    """End-to-end alias audit: /quiz must be wired to search_cmd everywhere
    /search is -- legacy registration, bridge map, private-only, wizard
    reserved set -- with identical behaviour and no conflicting handler."""

    def test_legacy_registers_quiz_alias_private(self):
        import inspect
        from quizbot.creator_bot.handlers import quiz_management as qm
        src = inspect.getsource(qm.register)
        self.assertIn('filters.command(["search", "quiz"]) & filters.private', src)

    def test_bridge_maps_quiz_to_search_cmd_private(self):
        import inspect
        from unittest.mock import MagicMock
        from telegram.ext import CommandHandler
        from quizbot.runner_bot import creator_bridge as br
        src = inspect.getsource(br.register_creator_bridge)
        self.assertIn('"quiz": quiz_management.search_cmd', src)
        app = MagicMock()
        added = []
        app.add_handler.side_effect = lambda h, group=0: added.append(h)
        br.register_creator_bridge(app)
        qh = [h for h in added
              if isinstance(h, CommandHandler) and "quiz" in getattr(h, "commands", set())]
        self.assertEqual(len(qh), 1, "/quiz must be registered exactly once")
        self.assertEqual(repr(qh[0].filters), "filters.ChatType.PRIVATE")

    def test_no_conflicting_runner_quiz_handler(self):
        import pathlib as _pl
        root = _pl.Path(__file__).resolve().parent.parent / "quizbot" / "runner_bot"
        hits = [str(f) for f in root.rglob("*.py")
                if 'CommandHandler("quiz"' in f.read_text()
                or "CommandHandler('quiz'" in f.read_text()]
        self.assertEqual(hits, [], f"conflicting /quiz handler(s): {hits}")

    def test_quiz_reserved_from_create_wizard(self):
        from quizbot.creator_bot.handlers.quiz_creation import _RESERVED_COMMANDS
        self.assertIn("quiz", _RESERVED_COMMANDS)
        self.assertIn("search", _RESERVED_COMMANDS)

    def test_quiz_bare_shows_usage(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/quiz")
        _run(qm.search_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_quiz_short_term_rejected(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/quiz a")
        _run(qm.search_cmd(MagicMock(), m))
        self.assertIn("too short", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_quiz_term_searches_like_search(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        from quizbot.creator_bot import state as cs
        m = make_creator_msg("/quiz science")
        repo = MagicMock()
        repo.search = AsyncMock(return_value=[
            {"qid": "Q1", "quiz_name": "Science", "total_participants": 2, "quiz_type": "free"}])
        repo.search_count = AsyncMock(return_value=1)
        with patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None), \
             patch.object(qm, "get_runner_bot_username", new=AsyncMock(return_value="rbot")):
            _run(qm.search_cmd(MagicMock(), m))
        repo.search.assert_awaited_once_with("science", limit=5, offset=0)
        self.assertEqual(cs.search_state[m._uid]["term"], "science")
        m._pending.edit_text.assert_awaited()
        _clear_creator_state(m._uid)


# ===========================================================================
# 10. /whtml
# ===========================================================================

class WhtmlCases(unittest.TestCase):
    def test_usage(self):
        from quizbot.creator_bot.handlers import reports as rep
        m = make_creator_msg("/whtml")
        _run(rep.whtml_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_not_found(self):
        from quizbot.creator_bot.handlers import reports as rep
        m = make_creator_msg("/whtml NOPE")
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(rep, "QuizRepository", return_value=repo), \
             patch.object(rep, "get_db", lambda: None):
            _run(rep.whtml_cmd(MagicMock(), m))
        m._pending.edit_text.assert_awaited()
        _clear_creator_state(m._uid)

    def test_not_owner(self):
        from quizbot.creator_bot.handlers import reports as rep
        m = make_creator_msg("/whtml Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 999})
        with patch.object(rep, "QuizRepository", return_value=repo), \
             patch.object(rep, "get_db", lambda: None):
            _run(rep.whtml_cmd(MagicMock(), m))
        self.assertIn("not your quiz", str(m._pending.edit_text.await_args).lower())
        _clear_creator_state(m._uid)

    def test_ok(self):
        from quizbot.creator_bot.handlers import reports as rep
        m = make_creator_msg("/whtml Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": m._uid,
                                           "quiz_name": "Q", "questions": []})
        with patch.object(rep, "QuizRepository", return_value=repo), \
             patch.object(rep, "get_db", lambda: None), \
             patch.object(rep, "render_quiz_html",
                          new=AsyncMock(return_value=(b"<html/>", "q.html"))):
            _run(rep.whtml_cmd(MagicMock(), m))
        m.reply_document.assert_awaited()
        _clear_creator_state(m._uid)

    def test_render_failure_reported(self):
        from quizbot.creator_bot.handlers import reports as rep
        m = make_creator_msg("/whtml Q1")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": m._uid,
                                           "quiz_name": "Q", "questions": []})
        with patch.object(rep, "QuizRepository", return_value=repo), \
             patch.object(rep, "get_db", lambda: None), \
             patch.object(rep, "render_quiz_html",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            _run(rep.whtml_cmd(MagicMock(), m))
        self.assertIn("Error", str(m._pending.edit_text.await_args))
        _clear_creator_state(m._uid)


# ===========================================================================
# 11. /settings /remove /mywords /clearlist
# ===========================================================================

class SettingsWordsCases(unittest.TestCase):
    def test_settings_shows(self):
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/settings")
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"search_indexed": 1, "default_text": "",
                                           "default_text_field": "both",
                                           "quiz_defaults": {}})
        with patch.object(st, "CreatorSettingsRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.settings_cmd(MagicMock(), m))
        m.reply.assert_awaited()
        self.assertIn("Creator Settings", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_settings_cb_bad_data(self):
        from quizbot.creator_bot.handlers import settings as st
        uid = _uid()
        cb = make_creator_cb("stg_noseparator", uid)
        _run(st.settings_cb(MagicMock(), cb))
        self.assertIn("Bad data", str(cb.answer.await_args))
        _clear_creator_state(uid)

    def test_settings_cb_wrong_user(self):
        from quizbot.creator_bot.handlers import settings as st
        owner, clicker = _uid(), _uid()
        cb = make_creator_cb(f"stg_idx_{owner}", clicker)
        _run(st.settings_cb(MagicMock(), cb))
        self.assertIn("Not yours", str(cb.answer.await_args))

    def test_settings_cb_toggle_idx(self):
        from quizbot.creator_bot.handlers import settings as st
        uid = _uid()
        cb = make_creator_cb(f"stg_idx_{uid}", uid)
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"search_indexed": 1, "default_text": "",
                                           "default_text_field": "both",
                                           "quiz_defaults": {}})
        repo.update = AsyncMock()
        with patch.object(st, "CreatorSettingsRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.settings_cb(MagicMock(), cb))
        repo.update.assert_awaited_once_with(uid, search_indexed=0)
        _clear_creator_state(uid)

    def test_remove_usage(self):
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/remove")
        _run(st.remove_words_cmd(MagicMock(), m))
        self.assertIn("Word Filter", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_remove_adds_words(self):
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/remove hello world")
        repo = MagicMock()
        repo.get_or_create = AsyncMock(return_value={"remove_words": []})
        repo.update_remove_words = AsyncMock()
        with patch.object(st, "UserRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.remove_words_cmd(MagicMock(), m))
        repo.update_remove_words.assert_awaited_once_with(m._uid, ["hello", "world"])
        self.assertIn("Added", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_remove_dedups(self):
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/remove hello")
        repo = MagicMock()
        repo.get_or_create = AsyncMock(return_value={"remove_words": ["hello"]})
        repo.update_remove_words = AsyncMock()
        with patch.object(st, "UserRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.remove_words_cmd(MagicMock(), m))
        self.assertIn("already", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_mywords_empty(self):
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/mywords")
        repo = MagicMock()
        repo.get_or_create = AsyncMock(return_value={"remove_words": []})
        with patch.object(st, "UserRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.mywords_cmd(MagicMock(), m))
        self.assertIn("empty", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_mywords_lists_and_truncates(self):
        """Huge filter lists must not exceed Telegram's 4096-char limit
        (which yields a silent BadRequest via the bridge)."""
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/mywords")
        words = [f"word{i:04d}abcdefghij" for i in range(500)]
        repo = MagicMock()
        repo.get_or_create = AsyncMock(return_value={"remove_words": words})
        with patch.object(st, "UserRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.mywords_cmd(MagicMock(), m))
        txt = m.reply.await_args.args[0]
        self.assertLessEqual(len(txt), 4096)
        _clear_creator_state(m._uid)

    def test_clearlist_ok(self):
        from quizbot.creator_bot.handlers import settings as st
        m = make_creator_msg("/clearlist")
        repo = MagicMock()
        repo.update_remove_words = AsyncMock()
        with patch.object(st, "UserRepository", return_value=repo), \
             patch.object(st, "get_db", lambda: None):
            _run(st.clearlist_cmd(MagicMock(), m))
        repo.update_remove_words.assert_awaited_once_with(m._uid, [])
        self.assertIn("cleared", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)


# ===========================================================================
# 12. Bridge message-router chat-type gating
# ===========================================================================

class BridgeRouterCases(unittest.TestCase):
    def test_batch_edit_text_requires_private(self):
        import inspect
        from quizbot.runner_bot import creator_bridge as br
        src = inspect.getsource(br._creator_message_router)
        # batch/edit branches must check private like the other branches do
        self.assertIn('batch_sessions', src)
        # Heuristic: router must mention chat.type private check for batch/edit.
        # We assert the fixed code contains a private guard adjacent to batch.
        batch_idx = src.find("batch_sessions")
        window = src[max(0, batch_idx - 300):batch_idx + 500]
        self.assertIn("private", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
