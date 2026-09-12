"""PART 5 -- audit of the remaining unaudited commands (offline).

Covers (19 commands):
  /ban /convertall /delall /gcast /stopcast /statses /testapi /removeuser
  /pay /help /start /leaderboard /result /mix /pollquiz /pollstop /html /pdf
  /trans
plus sub-flows: qs_ setup wizard (/start, /mix), compare_ (/html),
channel-post path (/pollquiz, /pollstop), pending-setup lifecycle.

Already covered elsewhere (NOT re-audited): Part 4's 29 commands,
/aiquiz /schedule /viewschedule /cancelschedule /setkey /mykeys /delkey,
/testseries /tsr /mocktest /newseries, /podcast (paused),
/create /done /cancel (Part 2), /slow /fast /normal (timer suite).

Offline: no Telegram network, no MongoDB (repositories mocked).
Every test uses a fresh UID to avoid rate-limiters.
"""

from __future__ import annotations

import asyncio
import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_uids = itertools.count(970000)


def _uid() -> int:
    return next(_uids)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def make_creator_msg(text, uid=None, chat_id=None, chat_type="private",
                     reply_to_message=None):
    uid = _uid() if uid is None else uid
    chat_id = uid if chat_id is None else chat_id
    m = SimpleNamespace(
        id=1,
        from_user=SimpleNamespace(id=uid, first_name="Tester"),
        text=text,
        command=text.split(),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        reply_to_message=reply_to_message,
        reply=AsyncMock(),
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
        reply_photo=AsyncMock(),
        document=None,
        photo=None,
        poll=None,
    )
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
    return SimpleNamespace(
        id="cb1",
        from_user=SimpleNamespace(id=uid),
        data=data,
        message=msg,
        answer=AsyncMock(),
    )


def make_runner_update(text="/start", uid=None, chat_id=None,
                       chat_type="private", args=None):
    uid = _uid() if uid is None else uid
    chat_id = uid if chat_id is None else chat_id
    message = SimpleNamespace(
        chat_id=chat_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=uid, first_name="P"),
        sender_chat=None,
        text=text,
        message_id=1,
        message_thread_id=None,
    )
    update = SimpleNamespace(message=message, effective_user=message.from_user,
                             effective_chat=message.chat,
                             effective_message=message)
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="administrator"))
    bot.delete_message = AsyncMock()
    bot.send_document = AsyncMock()
    ctx = SimpleNamespace(args=args or [], bot=bot)
    return update, ctx, uid, chat_id


def make_runner_query(data, uid=None, chat_id=None):
    uid = _uid() if uid is None else uid
    chat_id = uid if chat_id is None else chat_id
    query = SimpleNamespace(
        id="q1",
        data=data,
        from_user=SimpleNamespace(id=uid, first_name="P"),
        message=SimpleNamespace(chat_id=chat_id, message_id=9,
                                message_thread_id=None),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
    ctx = SimpleNamespace(bot=MagicMock())
    return update, ctx, query


def _clear_creator_state(uid=None):
    from quizbot.creator_bot import state as cs
    cs.broadcast.active = False
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
    rs.pending_quiz_settings.clear()
    rs.channel_poll_tasks.clear()
    rs.translation_mgr.settings.clear()
    rs.rate_limiter.buckets.clear()
    try:
        rs.last_working_ai.clear()
    except Exception:
        pass


def _cancel_channel_tasks():
    from quizbot.runner_bot.state import channel_poll_tasks
    for task in list(channel_poll_tasks.values()):
        try:
            task.cancel()
        except Exception:
            pass
    channel_poll_tasks.clear()


# ===========================================================================
# 1. /help -- single-fire merged reference (B3)
# ===========================================================================

class HelpSingleFireCases(unittest.TestCase):
    def test_both_registers_yield_single_help_handler(self):
        """REGRESSION: runner help_command + bridge help_cmd both fired on
        every /help (same group, no filters) -> user got 2 messages."""
        from telegram.ext import Application, CommandHandler
        from quizbot.runner_bot.handlers import register as runner_register
        from quizbot.runner_bot.creator_bridge import register_creator_bridge
        app = (Application.builder()
               .token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build())
        runner_register(app)
        register_creator_bridge(app)
        helps = []
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler) and "help" in getattr(h, "commands", set()):
                    helps.append(h)
        self.assertEqual(len(helps), 1,
                         f"/help must be handled exactly once, got {len(helps)}")

    def test_runner_register_adds_no_help(self):
        from quizbot.runner_bot.handlers import admin as radm
        app = MagicMock()
        added = []
        app.add_handler.side_effect = lambda h, group=0: added.append(h)
        radm.register(app)
        from telegram.ext import CommandHandler
        cmds = set()
        for h in added:
            if isinstance(h, CommandHandler):
                cmds.update(getattr(h, "commands", set()))
        self.assertNotIn("help", cmds)

    def test_creator_help_covers_player_commands(self):
        """The merged /help must preserve the runner reference content."""
        from quizbot.creator_bot.handlers import admin as cadm
        for marker in ("/pause", "/result", "/mix", "/pollquiz", "/trans",
                       "/schedule", "/leaderboard", "/slow", "/html"):
            self.assertIn(marker, cadm.HELP_TEXT,
                          f"merged /help lost runner marker {marker}")
        # And the creator guide itself must be intact.
        for marker in ("/create", "/edit", "/batch", "/settings"):
            self.assertIn(marker, cadm.HELP_TEXT)


# ===========================================================================
# 2. /pollquiz + /pollstop + channel path (B1, B8)
# ===========================================================================

class PollquizPaidCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _cancel_channel_tasks()
        _clear_runner_state()

    def _quiz(self, qid="Q1", paid=True, n=3):
        return {"qid": qid, "quiz_name": "Q", "creator_id": 777,
                "quiz_type": "paid" if paid else "free",
                "questions": [{"question": f"q{i}", "options": ["a", "b"],
                               "correct_option_id": 0} for i in range(n)]}

    def test_group_path_denies_paid_without_access(self):
        """REGRESSION: check_channel_paid_access existed but was never
        called on the group/message path -> any user could broadcast any
        paid quiz's full content. (Premium gate is dormant-True.)"""
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update(
            "/pollquiz PAID1", chat_type="group", chat_id=-1001, args=["PAID1"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz("PAID1", paid=True))
        with patch.object(pq, "QuizRepository", return_value=repo), \
             patch.object(pq, "get_db", lambda: None), \
             patch.object(pq, "check_channel_paid_access",
                          new=AsyncMock(return_value="❌ Paid quiz. Nope.")), \
             patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollquiz_channel_command(update, ctx))
            ssm.assert_awaited()
            texts = " ".join(str(c) for c in ssm.await_args_list)
            self.assertIn("Paid quiz", texts)
        from quizbot.runner_bot.state import channel_poll_tasks
        self.assertNotIn(cid, channel_poll_tasks)

    def test_group_path_allows_paid_with_access(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update(
            "/pollquiz PAID1", chat_type="group", chat_id=-1002, args=["PAID1"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz("PAID1", paid=True))
        with patch.object(pq, "QuizRepository", return_value=repo), \
             patch.object(pq, "get_db", lambda: None), \
             patch.object(pq, "check_channel_paid_access",
                          new=AsyncMock(return_value=None)), \
             patch.object(pq, "safe_send_message", new=AsyncMock()):
            _run(pq.pollquiz_channel_command(update, ctx))
        from quizbot.runner_bot.state import channel_poll_tasks
        self.assertIn(cid, channel_poll_tasks)

    def test_free_quiz_starts(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update(
            "/pollquiz FREE1", chat_type="group", chat_id=-1003, args=["FREE1"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz("FREE1", paid=False))
        with patch.object(pq, "QuizRepository", return_value=repo), \
             patch.object(pq, "get_db", lambda: None), \
             patch.object(pq, "safe_send_message", new=AsyncMock()):
            _run(pq.pollquiz_channel_command(update, ctx))
        from quizbot.runner_bot.state import channel_poll_tasks
        self.assertIn(cid, channel_poll_tasks)

    def test_usage_without_arg(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update("/pollquiz", args=[])
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollquiz_channel_command(update, ctx))
            self.assertIn("Usage", ssm.await_args.args[2])

    def test_invalid_qid(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update("/pollquiz NOPE", args=["NOPE"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(pq, "QuizRepository", return_value=repo), \
             patch.object(pq, "get_db", lambda: None), \
             patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollquiz_channel_command(update, ctx))
            self.assertIn("Invalid", ssm.await_args.args[2])

    def test_already_running(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        from quizbot.runner_bot.state import channel_poll_tasks
        update, ctx, uid, cid = make_runner_update("/pollquiz Q1", args=["Q1"])
        async def _never():
            await asyncio.sleep(3600)
        loop = asyncio.new_event_loop()
        task = loop.create_task(_never())
        channel_poll_tasks[cid] = task
        try:
            with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
                _run(pq.pollquiz_channel_command(update, ctx))
                self.assertIn("already running", ssm.await_args.args[2].lower())
        finally:
            task.cancel()
            loop.close()

    def test_exception_gives_feedback(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update("/pollquiz Q1", args=["Q1"])
        with patch.object(pq, "QuizRepository", side_effect=RuntimeError("db down")), \
             patch.object(pq, "get_db", lambda: None), \
             patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollquiz_channel_command(update, ctx))
            ssm.assert_awaited()


class PollstopCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _cancel_channel_tasks()
        _clear_runner_state()

    def test_no_task_running(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update("/pollstop")
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollstop_command(update, ctx))
            self.assertIn("No poll quiz", ssm.await_args.args[2])

    def test_cancels_running_task_private(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        from quizbot.runner_bot.state import channel_poll_tasks
        update, ctx, uid, cid = make_runner_update("/pollstop")
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        channel_poll_tasks[cid] = task
        with patch.object(pq, "safe_send_message", new=AsyncMock()):
            _run(pq.pollstop_command(update, ctx))
        task.cancel.assert_called_once()

    def test_group_non_admin_denied(self):
        """REGRESSION: anyone could stop anyone's pollquiz in groups."""
        from quizbot.runner_bot.handlers import poll_quiz as pq
        from quizbot.runner_bot.state import channel_poll_tasks
        update, ctx, uid, cid = make_runner_update("/pollstop", chat_type="group",
                                                   chat_id=-1004)
        ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        channel_poll_tasks[cid] = task
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollstop_command(update, ctx))
            self.assertIn("Admin only", ssm.await_args.args[2])
        task.cancel.assert_not_called()

    def test_group_admin_ok(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        from quizbot.runner_bot.state import channel_poll_tasks
        update, ctx, uid, cid = make_runner_update("/pollstop", chat_type="supergroup",
                                                   chat_id=-1005)
        ctx.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status="administrator"))
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        channel_poll_tasks[cid] = task
        with patch.object(pq, "safe_send_message", new=AsyncMock()):
            _run(pq.pollstop_command(update, ctx))
        task.cancel.assert_called_once()

    def test_anon_admin_ok(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        from quizbot.runner_bot.state import channel_poll_tasks
        update, ctx, uid, cid = make_runner_update("/pollstop", chat_type="group",
                                                   chat_id=-1006)
        update.message.sender_chat = SimpleNamespace(id=-1006)
        update.message.from_user = None
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        channel_poll_tasks[cid] = task
        with patch.object(pq, "safe_send_message", new=AsyncMock()):
            _run(pq.pollstop_command(update, ctx))
        task.cancel.assert_called_once()

    def test_exception_gives_feedback(self):
        from quizbot.runner_bot.handlers import poll_quiz as pq
        update, ctx, uid, cid = make_runner_update("/pollstop")
        from quizbot.runner_bot.state import channel_poll_tasks
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock(side_effect=RuntimeError("boom"))
        channel_poll_tasks[cid] = task
        with patch.object(pq, "safe_send_message", new=AsyncMock()) as ssm:
            _run(pq.pollstop_command(update, ctx))
            ssm.assert_awaited()


class ChannelCommandCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _cancel_channel_tasks()
        _clear_runner_state()

    def _ch_update(self, text, chat_id=-100887766):
        msg = SimpleNamespace(text=text, message_id=5, chat_id=chat_id)
        update = SimpleNamespace(channel_post=msg, message=None)
        ctx = SimpleNamespace(bot=MagicMock())
        ctx.bot.delete_message = AsyncMock()
        return update, ctx, chat_id

    def test_pollstop_no_task(self):
        from quizbot.runner_bot.handlers import admin as radm
        update, ctx, cid = self._ch_update("/pollstop")
        with patch.object(radm, "safe_send_message", new=AsyncMock()) as ssm:
            _run(radm.handle_channel_command(update, ctx))
            self.assertIn("No poll quiz", ssm.await_args.args[2])

    def test_pollstop_cancels(self):
        from quizbot.runner_bot.handlers import admin as radm
        from quizbot.runner_bot.state import channel_poll_tasks
        update, ctx, cid = self._ch_update("/pollstop")
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        channel_poll_tasks[cid] = task
        with patch.object(radm, "safe_send_message", new=AsyncMock()):
            _run(radm.handle_channel_command(update, ctx))
        task.cancel.assert_called_once()

    def test_pollquiz_usage(self):
        from quizbot.runner_bot.handlers import admin as radm
        update, ctx, cid = self._ch_update("/pollquiz")
        with patch.object(radm, "safe_send_message", new=AsyncMock()) as ssm:
            _run(radm.handle_channel_command(update, ctx))
            self.assertIn("Usage", ssm.await_args.args[2])

    def test_pollquiz_paid_denied(self):
        from quizbot.runner_bot.handlers import admin as radm
        update, ctx, cid = self._ch_update("/pollquiz PAID9")
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value={"qid": "PAID9", "creator_id": 777,
                                            "quiz_type": "paid",
                                            "questions": [{"question": "q"}]})
        arepo = MagicMock()
        arepo.get = AsyncMock(return_value=[])
        with patch.object(radm, "QuizRepository", return_value=qrepo), \
             patch.object(radm, "AuthChatRepository", return_value=arepo), \
             patch.object(radm, "get_db", lambda: None), \
             patch.object(radm, "safe_send_message", new=AsyncMock()) as ssm:
            _run(radm.handle_channel_command(update, ctx))
            self.assertIn("Paid quiz", ssm.await_args.args[2])
        from quizbot.runner_bot.state import channel_poll_tasks
        self.assertNotIn(cid, channel_poll_tasks)

    def test_non_command_ignored(self):
        from quizbot.runner_bot.handlers import admin as radm
        update, ctx, cid = self._ch_update("hello channel")
        with patch.object(radm, "safe_send_message", new=AsyncMock()) as ssm:
            _run(radm.handle_channel_command(update, ctx))
            ssm.assert_not_awaited()


# ===========================================================================
# 3. /mix (B2)
# ===========================================================================

class MixCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def _mix_update(self, args, chat_type="private", chat_id=None):
        return make_runner_update("/mix", chat_type=chat_type,
                                  chat_id=chat_id, args=args)

    def _quiz(self, qid, n=30):
        return {"qid": qid, "quiz_name": f"Quiz {qid}", "quiz_type": "free",
                "questions": [{"question": f"{qid}q{i}", "options": ["a", "b"],
                               "correct_option_id": 0} for i in range(n)]}

    def _run_mix(self, update, ctx, repo, resolve=None):
        from quizbot.runner_bot.handlers import mix as mx
        from quizbot.runner_bot.handlers import setup_wizard as sw
        patches = [
            patch.object(mx, "QuizRepository", return_value=repo),
            patch.object(mx, "get_db", lambda: None),
            patch.object(mx, "safe_send_message", new=AsyncMock()),
            patch.object(sw, "show_correct_mark_prompt", new=AsyncMock()),
        ]
        if resolve is not None:
            patches.append(patch.object(mx, "resolve_quiz_access", resolve,
                                        create=True))
        for p in patches:
            p.start()
        try:
            _run(mx.mix_command(update, ctx))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_usage(self):
        from quizbot.runner_bot.handlers import mix as mx
        update, ctx, uid, cid = self._mix_update(["50"])
        with patch.object(mx, "safe_send_message", new=AsyncMock()) as ssm:
            _run(mx.mix_command(update, ctx))
            self.assertIn("Usage", ssm.await_args.args[2])

    def test_count_bounds(self):
        from quizbot.runner_bot.handlers import mix as mx
        for args, needle in ((["abc", "A", "B"], "number"),
                             (["10", "A", "B"], "Minimum"),
                             (["500", "A", "B"], "Maximum")):
            update, ctx, uid, cid = self._mix_update(args)
            with patch.object(mx, "safe_send_message", new=AsyncMock()) as ssm:
                _run(mx.mix_command(update, ctx))
                self.assertIn(needle, ssm.await_args.args[2])

    def test_all_invalid(self):
        from quizbot.runner_bot.handlers import mix as mx
        update, ctx, uid, cid = self._mix_update(["20", "NOPE1", "NOPE2"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        ssm = AsyncMock()
        with patch.object(mx, "QuizRepository", return_value=repo), \
             patch.object(mx, "get_db", lambda: None), \
             patch.object(mx, "safe_send_message", ssm):
            _run(mx.mix_command(update, ctx))
        texts = " ".join(str(c) for c in ssm.await_args_list)
        self.assertIn("None of the provided", texts)

    def test_premium_gate(self):
        from quizbot.runner_bot.handlers import mix as mx
        update, ctx, uid, cid = self._mix_update(["20", "A", "B"])
        with patch.object(mx, "is_premium_user", new=AsyncMock(return_value=False)), \
             patch.object(mx, "safe_send_message", new=AsyncMock()) as ssm:
            _run(mx.mix_command(update, ctx))
            self.assertIn("premium", ssm.await_args.args[2].lower())

    def test_paid_component_skipped_without_access(self):
        """REGRESSION (B2): /mix never checked quiz access -> any user
        could launder any paid quiz's questions into a mix."""
        from quizbot.runner_bot.handlers import mix as mx
        from quizbot.runner_bot import state as rs
        update, ctx, uid, cid = self._mix_update(["20", "FREE1", "PAID1"])
        repo = MagicMock()

        async def _get(qid):
            if qid == "FREE1":
                return self._quiz("FREE1")
            return {"qid": "PAID1", "quiz_name": "Paid", "quiz_type": "paid",
                    "questions": self._quiz("P")["questions"]}
        repo.get = AsyncMock(side_effect=_get)

        async def _resolve(qid, quiz, chat_id, chat_type, user_id, ctx=None):
            return (qid != "PAID1", None)

        ssm = AsyncMock()
        from quizbot.runner_bot.handlers import setup_wizard as sw
        with patch.object(mx, "QuizRepository", return_value=repo), \
             patch.object(mx, "get_db", lambda: None), \
             patch.object(mx, "safe_send_message", ssm), \
             patch.object(mx, "resolve_quiz_access", new=_resolve, create=True), \
             patch.object(sw, "show_correct_mark_prompt", new=AsyncMock()):
            _run(mx.mix_command(update, ctx))
        texts = " ".join(str(c) for c in ssm.await_args_list)
        self.assertIn("no access", texts.lower())
        self.assertIn("PAID1", texts)
        pending = rs.pending_quiz_settings.get(cid)
        self.assertIsNotNone(pending)
        got = {q["question"] for q in pending["quiz"]["questions"]}
        self.assertTrue(all(g.startswith("FREE1") for g in got),
                        "paid questions leaked into the mix")

    def test_all_denied_reports_no_access(self):
        from quizbot.runner_bot.handlers import mix as mx
        update, ctx, uid, cid = self._mix_update(["20", "PAID1", "PAID2"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "P", "quiz_name": "P",
                                           "quiz_type": "paid",
                                           "questions": self._quiz("P")["questions"]})

        async def _deny(*a, **k):
            return (False, None)

        ssm = AsyncMock()
        with patch.object(mx, "QuizRepository", return_value=repo), \
             patch.object(mx, "get_db", lambda: None), \
             patch.object(mx, "safe_send_message", ssm), \
             patch.object(mx, "resolve_quiz_access", new=_deny, create=True):
            _run(mx.mix_command(update, ctx))
        texts = " ".join(str(c) for c in ssm.await_args_list)
        self.assertIn("accessible", texts)

    def test_mix_rejected_while_setup_live(self):
        """A second /mix (or /start) must not hijack a live setup."""
        from quizbot.runner_bot.handlers import mix as mx
        from quizbot.runner_bot import state as rs
        import time as _time
        update, ctx, uid, cid = self._mix_update(["20", "A", "B"])
        rs.pending_quiz_settings[cid] = {"initiator_id": _uid(),
                                         "created_at": _time.time()}
        with patch.object(mx, "safe_send_message", new=AsyncMock()) as ssm:
            _run(mx.mix_command(update, ctx))
            self.assertIn("already in progress", ssm.await_args.args[2].lower())


# ===========================================================================
# 4. /start + qs_ setup wizard lifecycle (B4)
# ===========================================================================

class StartCmdCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def _quiz(self, qid="Q1", fixed=False):
        return {"qid": qid, "quiz_name": "Quiz", "creator_id": 111,
                "quiz_type": "free", "timer": 30,
                "questions": [{"question": "q", "options": ["a", "b"],
                               "correct_option_id": 0} for _ in range(5)],
                "fixed_settings": fixed}

    def test_bare_start_shows_welcome(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start", args=[])
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("LBSNAA", ssm.await_args.args[2])

    def test_anon_admin_gets_verify_button(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start Q1", chat_type="group",
                                                   chat_id=-101, args=["Q1"])
        update.message.sender_chat = SimpleNamespace(id=-101)
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("Anonymous admin", ssm.await_args.args[2])

    def test_rate_limited(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start Q1", args=["Q1"])
        with patch.object(qp.rate_limiter, "check", new=AsyncMock(return_value=False)), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("Too many requests", ssm.await_args.args[2])

    def test_already_running(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/start Q1", args=["Q1"])
        _run(session_mgr.create(cid, {"quiz_id": "Q9"}))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("already running", ssm.await_args.args[2].lower())

    def test_setup_already_in_progress(self):
        """REGRESSION (B4): a second /start overwrote live pending settings,
        hijacking (or bricking) the first user's wizard."""
        import time as _time
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot import state as rs
        update, ctx, uid, cid = make_runner_update("/start Q1", args=["Q1"])
        rs.pending_quiz_settings[cid] = {"initiator_id": _uid(),
                                         "quiz": {"quiz_name": "Old"},
                                         "created_at": _time.time()}
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz())
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("already in progress", ssm.await_args.args[2].lower())
        self.assertEqual(rs.pending_quiz_settings[cid]["quiz"]["quiz_name"], "Old")

    def test_expired_pending_allows_restart(self):
        import time as _time
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        update, ctx, uid, cid = make_runner_update("/start Q1", args=["Q1"])
        rs.pending_quiz_settings[cid] = {"initiator_id": _uid(),
                                         "created_at": _time.time() - 3600}
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz())
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "resolve_quiz_access",
                          new=AsyncMock(return_value=(True, None))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()), \
             patch.object(sw, "show_correct_mark_prompt", new=AsyncMock()) as scm:
            _run(qp.start_quiz(update, ctx))
            scm.assert_awaited_once()
        self.assertEqual(rs.pending_quiz_settings[cid]["initiator_id"], uid)

    def test_invalid_qid(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start NOPE", args=["NOPE"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("Invalid", ssm.await_args.args[2])

    def test_nondigit_skip_rejected(self):
        """REGRESSION: /start QID abc silently started from Q1 (typo like
        '5O' could mis-launch a whole group quiz)."""
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start Q1 abc", args=["Q1", "abc"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz())
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("skip", ssm.await_args.args[2].lower())

    def test_oversized_skip_rejected(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start Q1 99", args=["Q1", "99"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz())
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            self.assertIn("exceeds", ssm.await_args.args[2].lower())

    def test_paid_denied(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/start PAID1", args=["PAID1"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz("PAID1"))
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "resolve_quiz_access",
                          new=AsyncMock(return_value=(False, None))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.start_quiz(update, ctx))
            ssm.assert_awaited()

    def test_fixed_settings_launches_directly(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        update, ctx, uid, cid = make_runner_update("/start Q1", args=["Q1"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz(fixed=True))
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "resolve_quiz_access",
                          new=AsyncMock(return_value=(True, None))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()), \
             patch.object(sw, "_launch_quiz_from_settings", new=AsyncMock()) as ln:
            _run(qp.start_quiz(update, ctx))
            ln.assert_awaited_once()
        self.assertNotIn(cid, rs.pending_quiz_settings)

    def test_wizard_path_stores_pending_and_prompts(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        update, ctx, uid, cid = make_runner_update("/start Q1", args=["Q1"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz())
        with patch.object(qp, "QuizRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "resolve_quiz_access",
                          new=AsyncMock(return_value=(True, None))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()), \
             patch.object(sw, "show_correct_mark_prompt", new=AsyncMock()) as scm:
            _run(qp.start_quiz(update, ctx))
            scm.assert_awaited_once_with(ctx, cid)
        ps = rs.pending_quiz_settings[cid]
        self.assertEqual(ps["initiator_id"], uid)
        self.assertIn("created_at", ps)

    def test_play_payload_unwrap(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.handlers import setup_wizard as sw
        update, ctx, uid, cid = make_runner_update(
            "/start play_Q1_practice", args=["play_Q1_practice"])
        repo = MagicMock()
        repo.get = AsyncMock(return_value=self._quiz())
        with patch.object(qp, "QuizRepository", return_value=repo) as qr, \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "resolve_quiz_access",
                          new=AsyncMock(return_value=(True, None))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()), \
             patch.object(sw, "show_correct_mark_prompt", new=AsyncMock()):
            _run(qp.start_quiz(update, ctx))
            repo.get.assert_awaited_with("Q1")


class SetupWizardCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def _seed(self, cid, uid, **extra):
        import time as _time
        from quizbot.runner_bot import state as rs
        ps = {"initiator_id": uid, "quiz": {"quiz_name": "Q"},
              "chat_type": "private", "correct_mark": 1.0,
              "created_at": _time.time()}
        ps.update(extra)
        rs.pending_quiz_settings[cid] = ps
        return ps

    def test_wrong_initiator_rejected(self):
        from quizbot.runner_bot.handlers import setup_wizard as sw
        cid, owner, clicker = _uid(), _uid(), _uid()
        self._seed(cid, owner)
        update, ctx, query = make_runner_query(f"qs_cm_{cid}_2", clicker)
        _run(sw.quiz_setup_callback(update, ctx))
        query.answer.assert_awaited()
        self.assertIn("initiator", str(query.answer.await_args).lower())

    def test_expired_pending_rejected_and_pruned(self):
        """REGRESSION (B4): abandoned setups stayed live forever; stale
        buttons could launch a weeks-old setup."""
        import time as _time
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        uid = _uid()
        cid = _uid()
        rs.pending_quiz_settings[cid] = {"initiator_id": uid,
                                         "created_at": _time.time() - 3600}
        update, ctx, query = make_runner_query(f"qs_cm_{cid}_2", uid)
        _run(sw.quiz_setup_callback(update, ctx))
        query.edit_message_text.assert_awaited()
        self.assertIn("expired",
                      str(query.edit_message_text.await_args).lower())
        self.assertNotIn(cid, rs.pending_quiz_settings)

    def test_double_tap_start_launches_once(self):
        """REGRESSION (B4): pending was deleted AFTER the slow launch, so a
        double-tap spawned two quiz loops (duplicate polls/scores)."""
        from quizbot.runner_bot.handlers import setup_wizard as sw
        uid = _uid()
        cid = _uid()
        self._seed(cid, uid)
        release = asyncio.Event()
        calls = []

        async def _blocking_launch(chat_id, ctx, ps):
            calls.append(ps)
            await release.wait()

        async def _scenario():
            u1, c1, _q1 = make_runner_query(f"qs_tm_{cid}_start", uid)
            u2, c2, q2 = make_runner_query(f"qs_tm_{cid}_start", uid)
            t1 = asyncio.create_task(sw.quiz_setup_callback(u1, c1))
            await asyncio.sleep(0.05)  # t1 now blocked inside _launch
            t2 = asyncio.create_task(sw.quiz_setup_callback(u2, c2))
            await asyncio.sleep(0.05)  # t2 reaches launch / expired branch
            release.set()
            await asyncio.gather(t1, t2)
            return q2

        with patch.object(sw, "_launch_quiz_from_settings",
                          new=_blocking_launch):
            q2 = _run(_scenario())
        self.assertEqual(len(calls), 1)
        q2.edit_message_text.assert_awaited()  # 2nd tap told: expired

    def test_quickstart_double_tap_launches_once(self):
        from quizbot.runner_bot.handlers import setup_wizard as sw
        uid = _uid()
        cid = _uid()
        self._seed(cid, uid)
        prefs = MagicMock()
        prefs.get = AsyncMock(return_value={
            "updated_at": "t", "correct_mark": 1.0, "neg_mark": 0.0,
            "shuffle_q": False, "shuffle_o": False, "shuffle_o_count": 0,
            "show_explanation": False, "anti_cheat": False,
            "timer_override": None})
        release = asyncio.Event()
        calls = []

        async def _blocking_launch(chat_id, ctx, ps):
            calls.append(ps)
            await release.wait()

        async def _scenario():
            u1, c1, _q1 = make_runner_query(f"qs_qs_{cid}_go", uid)
            u2, c2, _q2 = make_runner_query(f"qs_qs_{cid}_go", uid)
            t1 = asyncio.create_task(sw.quiz_setup_callback(u1, c1))
            await asyncio.sleep(0.05)
            t2 = asyncio.create_task(sw.quiz_setup_callback(u2, c2))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(t1, t2)

        with patch.object(sw, "_launch_quiz_from_settings",
                          new=_blocking_launch), \
             patch.object(sw, "QuizPrefsRepository", return_value=prefs), \
             patch.object(sw, "get_db", lambda: None):
            _run(_scenario())
        self.assertEqual(len(calls), 1)

    def test_cancel_button_aborts(self):
        """REGRESSION (B4): the wizard had no exit; combined with the
        live-pending guard there must be a cancel path."""
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        uid = _uid()
        cid = _uid()
        self._seed(cid, uid)
        update, ctx, query = make_runner_query(f"qs_cancel_{cid}_x", uid)
        _run(sw.quiz_setup_callback(update, ctx))
        self.assertNotIn(cid, rs.pending_quiz_settings)
        query.edit_message_text.assert_awaited()
        self.assertIn("cancel",
                      str(query.edit_message_text.await_args).lower())

    def test_cancel_by_non_initiator_rejected(self):
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        cid, owner, clicker = _uid(), _uid(), _uid()
        self._seed(cid, owner)
        update, ctx, query = make_runner_query(f"qs_cancel_{cid}_x", clicker)
        _run(sw.quiz_setup_callback(update, ctx))
        self.assertIn(cid, rs.pending_quiz_settings)

    def test_malformed_qs_answered(self):
        """Malformed qs_ callbacks spun forever (query never answered)."""
        from quizbot.runner_bot.handlers import setup_wizard as sw
        uid = _uid()
        cid = _uid()
        self._seed(cid, uid)
        for bad in ("qs_", "qs_cm", f"qs_cm_{cid}", "qs_cm_nope_x"):
            update, ctx, query = make_runner_query(bad, uid)
            _run(sw.quiz_setup_callback(update, ctx))  # must not raise
            query.answer.assert_awaited()

    def test_unknown_step_answered(self):
        from quizbot.runner_bot.handlers import setup_wizard as sw
        uid = _uid()
        cid = _uid()
        self._seed(cid, uid)
        update, ctx, query = make_runner_query(f"qs_zz_{cid}_x", uid)
        _run(sw.quiz_setup_callback(update, ctx))
        query.answer.assert_awaited()

    def test_cm_step_advances(self):
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot import state as rs
        uid = _uid()
        cid = _uid()
        self._seed(cid, uid)
        update, ctx, query = make_runner_query(f"qs_cm_{cid}_2", uid)
        with patch.object(sw, "_show_neg_mark_prompt", new=AsyncMock()):
            _run(sw.quiz_setup_callback(update, ctx))
        self.assertEqual(rs.pending_quiz_settings[cid]["correct_mark"], 2.0)

    def test_pending_helper_ttl(self):
        import time as _time
        from quizbot.runner_bot import state as rs
        cid = _uid()
        rs.pending_quiz_settings[cid] = {"created_at": _time.time()}
        self.assertTrue(rs.pending_setup_live(cid))
        rs.pending_quiz_settings[cid] = {"created_at": _time.time() - 3600}
        self.assertFalse(rs.pending_setup_live(cid))
        self.assertFalse(rs.pending_setup_live(_uid()))

    def test_anon_verify_empty_qid_shows_welcome(self):
        """REGRESSION: bare /start by an anon admin edited 'Starting quiz
        setup...' then fell into a silent return."""
        from quizbot.runner_bot.handlers import setup_wizard as sw
        from quizbot.runner_bot.handlers.quiz_play import _START_WELCOME_TEXT
        uid = _uid()
        cid = -100700 + -(uid % 1000)
        update, ctx, query = make_runner_query(f"qs_anon_verify_{cid}_", uid)
        with patch.object(sw, "safe_send_message", new=AsyncMock()) as ssm:
            _run(sw.quiz_setup_callback(update, ctx))
            self.assertIn("LBSNAA", ssm.await_args.args[2])
            self.assertNotIn("Starting quiz setup",
                             str(query.edit_message_text.await_args)
                             if query.edit_message_text.await_args_list else "")


# ===========================================================================
# 5. /leaderboard + /result
# ===========================================================================

class LeaderboardCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_no_session(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/leaderboard")
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.leaderboard_command(update, ctx))
            self.assertIn("No quiz running", ssm.await_args.args[2])

    def test_group_non_admin_denied(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/leaderboard", chat_type="group",
                                                   chat_id=-1101)
        ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        _run(session_mgr.create(cid, {"quiz_id": "Q1"}))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.leaderboard_command(update, ctx))
            self.assertIn("Admin only", ssm.await_args.args[2])

    def test_admin_check_exception_gives_feedback(self):
        """REGRESSION: get_chat_member failure -> silent return."""
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/leaderboard", chat_type="group",
                                                   chat_id=-1102)
        ctx.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("tg down"))
        _run(session_mgr.create(cid, {"quiz_id": "Q1"}))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.leaderboard_command(update, ctx))
            ssm.assert_awaited()

    def test_no_answers_yet(self):
        """REGRESSION: session with zero participants -> helper returns
        silently -> user sees nothing."""
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/leaderboard")
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "participants": {},
                                      "quiz_data": {"questions": []}}))
        with patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.leaderboard_command(update, ctx))
            ssm.assert_awaited()
            self.assertIn("No answers", ssm.await_args.args[2])

    def test_private_ok(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/leaderboard")
        _run(session_mgr.create(cid, {"quiz_id": "Q1", "current_index": 2,
                                      "participants": {uid: {"name": "P", "answers": {}}},
                                      "quiz_data": {"questions": [{}, {}]}}))
        with patch.object(qp, "_send_mid_quiz_leaderboard", new=AsyncMock()) as lb, \
             patch.object(qp, "safe_send_message", new=AsyncMock()):
            _run(qp.leaderboard_command(update, ctx))
            lb.assert_awaited_once_with(cid, ctx, 2, 2)

    def test_exception_gives_feedback(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        from quizbot.runner_bot.state import session_mgr
        update, ctx, uid, cid = make_runner_update("/leaderboard")
        _run(session_mgr.create(cid, {"quiz_id": "Q1",
                                      "participants": {uid: {"answers": {}}},
                                      "quiz_data": {"questions": []}}))
        with patch.object(qp, "_send_mid_quiz_leaderboard",
                          new=AsyncMock(side_effect=RuntimeError("x"))), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.leaderboard_command(update, ctx))
            ssm.assert_awaited()


class ResultCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_no_result(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/result")
        repo = MagicMock()
        repo.latest_completed_for_user = AsyncMock(return_value=None)
        with patch.object(qp, "AttemptRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.result_command(update, ctx))
            self.assertIn("No completed", ssm.await_args.args[2])

    def test_ok_renders_fields(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/result")
        repo = MagicMock()
        repo.latest_completed_for_user = AsyncMock(return_value={
            "qid": "Q1", "quiz_name": "Quiz <b>", "total_questions": 10,
            "correct": 7, "wrong": 3, "score": 5.5, "total_time": 125,
            "time_ended": "2026-01-01"})
        with patch.object(qp, "AttemptRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.result_command(update, ctx))
            txt = ssm.await_args.args[2]
            self.assertIn("70.0%", txt)
            self.assertNotIn("<b>>", txt)  # escaped

    def test_db_exception_gives_feedback(self):
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/result")
        repo = MagicMock()
        repo.latest_completed_for_user = AsyncMock(side_effect=RuntimeError("db"))
        with patch.object(qp, "AttemptRepository", return_value=repo), \
             patch.object(qp, "get_db", lambda: None), \
             patch.object(qp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(qp.result_command(update, ctx))
            self.assertIn("Could not load", ssm.await_args.args[2])


# ===========================================================================
# 6. /trans
# ===========================================================================

class TransCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_group_non_admin_denied(self):
        from quizbot.runner_bot.handlers import translation as tr
        update, ctx, uid, cid = make_runner_update("/trans hi", chat_type="group",
                                                   chat_id=-1201, args=["hi"])
        ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
        with patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            self.assertIn("Admin only", ssm.await_args.args[2])

    def test_admin_check_exception_gives_feedback(self):
        """REGRESSION: get_chat_member failure -> silent return."""
        from quizbot.runner_bot.handlers import translation as tr
        update, ctx, uid, cid = make_runner_update("/trans hi", chat_type="group",
                                                   chat_id=-1202, args=["hi"])
        ctx.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("tg down"))
        with patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            ssm.assert_awaited()

    def test_no_arg_when_off_shows_usage(self):
        from quizbot.runner_bot.handlers import translation as tr
        update, ctx, uid, cid = make_runner_update("/trans", args=[])
        with patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            self.assertIn("Translation is OFF", ssm.await_args.args[2])

    def test_no_arg_when_on_disables(self):
        from quizbot.runner_bot.handlers import translation as tr
        from quizbot.runner_bot.state import translation_mgr
        update, ctx, uid, cid = make_runner_update("/trans", args=[])
        translation_mgr.set_language(cid, "hi")
        with patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            self.assertIn("DISABLED", ssm.await_args.args[2])
        self.assertIsNone(translation_mgr.get_language(cid))

    def test_bad_code(self):
        from quizbot.runner_bot.handlers import translation as tr
        update, ctx, uid, cid = make_runner_update("/trans xx", args=["xx"])
        with patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            self.assertIn("Unsupported", ssm.await_args.args[2])

    def test_good_code_enables(self):
        from quizbot.runner_bot.handlers import translation as tr
        from quizbot.runner_bot.state import translation_mgr
        update, ctx, uid, cid = make_runner_update("/trans HI", args=["HI"])
        with patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            self.assertIn("Hindi", ssm.await_args.args[2])
        self.assertEqual(translation_mgr.get_language(cid), "hi")

    def test_exception_gives_feedback(self):
        from quizbot.runner_bot.handlers import translation as tr
        update, ctx, uid, cid = make_runner_update("/trans hi", args=["hi"])
        with patch.object(tr.translation_mgr, "set_language",
                          side_effect=RuntimeError("x")), \
             patch.object(tr, "safe_send_message", new=AsyncMock()) as ssm:
            _run(tr.trans_command(update, ctx))
            ssm.assert_awaited()


# ===========================================================================
# 7. /html + /pdf + compare_ (+ report-settings robustness)
# ===========================================================================

class HtmlPdfCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def _toggle(self, cmd, enabled):
        from quizbot.runner_bot.handlers import reports as rp
        fn = rp.html_command if cmd == "html" else rp.pdf_command
        update, ctx, uid, cid = make_runner_update(f"/{cmd}")
        repo = MagicMock()
        repo.toggle = AsyncMock(return_value=enabled)
        with patch.object(rp, "ChatSettingsRepository", return_value=repo), \
             patch.object(rp, "get_db", lambda: None), \
             patch.object(rp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(fn(update, ctx))
        return ssm, repo, cid

    def test_html_enable_disable(self):
        ssm, repo, cid = self._toggle("html", True)
        self.assertIn("ENABLED", ssm.await_args.args[2])
        repo.toggle.assert_awaited_once_with(cid, "html")
        ssm, _, _ = self._toggle("html", False)
        self.assertIn("DISABLED", ssm.await_args.args[2])

    def test_pdf_enable_disable(self):
        ssm, repo, cid = self._toggle("pdf", True)
        self.assertIn("ENABLED", ssm.await_args.args[2])
        repo.toggle.assert_awaited_once_with(cid, "pdf")

    def test_group_non_admin_denied(self):
        from quizbot.runner_bot.handlers import reports as rp
        for fn in (rp.html_command, rp.pdf_command):
            update, ctx, uid, cid = make_runner_update("/x", chat_type="group",
                                                       chat_id=-1300 - _uid() % 997)
            ctx.bot.get_chat_member = AsyncMock(
                return_value=SimpleNamespace(status="member"))
            with patch.object(rp, "safe_send_message", new=AsyncMock()) as ssm:
                _run(fn(update, ctx))
                self.assertIn("Admin only", ssm.await_args.args[2])

    def test_admin_check_exception_denies_with_feedback(self):
        from quizbot.runner_bot.handlers import reports as rp
        update, ctx, uid, cid = make_runner_update("/html", chat_type="group",
                                                   chat_id=-1301)
        ctx.bot.get_chat_member = AsyncMock(side_effect=RuntimeError("tg down"))
        with patch.object(rp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(rp.html_command(update, ctx))
            self.assertIn("Admin only", ssm.await_args.args[2])

    def test_exception_gives_feedback(self):
        from quizbot.runner_bot.handlers import reports as rp
        for fn in (rp.html_command, rp.pdf_command):
            update, ctx, uid, cid = make_runner_update("/x")
            repo = MagicMock()
            repo.toggle = AsyncMock(side_effect=RuntimeError("db down"))
            with patch.object(rp, "ChatSettingsRepository", return_value=repo), \
                 patch.object(rp, "get_db", lambda: None), \
                 patch.object(rp, "safe_send_message", new=AsyncMock()) as ssm:
                _run(fn(update, ctx))
                ssm.assert_awaited()


class CompareCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_malformed_data_answered(self):
        """REGRESSION: 'compare_' (no qid) raised IndexError in the outer
        handler -> query never answered -> eternal spinner."""
        from quizbot.runner_bot.handlers import reports as rp
        for bad in ("compare_", "compare"):
            update, ctx, query = make_runner_query(bad)
            with patch.object(rp, "get_db", lambda: None):
                _run(rp.compare_results(update, ctx))  # must not raise
            query.answer.assert_awaited()

    def test_bad_chat_id_answered(self):
        from quizbot.runner_bot.handlers import reports as rp
        update, ctx, query = make_runner_query("compare_Q1_xx")
        with patch.object(rp, "get_db", lambda: None):
            _run(rp.compare_results(update, ctx))
        query.answer.assert_awaited()

    def test_disabled_settings(self):
        from quizbot.runner_bot.handlers import reports as rp
        update, ctx, query = make_runner_query("compare_Q1_5")
        csrepo = MagicMock()
        csrepo.get = AsyncMock(return_value={"html_enabled": False})
        with patch.object(rp, "ChatSettingsRepository", return_value=csrepo), \
             patch.object(rp, "get_db", lambda: None):
            _run(rp.compare_results(update, ctx))
        self.assertIn("disabled", str(query.answer.await_args).lower())

    def test_legacy_settings_without_keys_proceed(self):
        """REGRESSION: settings docs predating html_enabled raised
        KeyError -> spinner. Missing keys must default to off-check pass."""
        from quizbot.runner_bot.handlers import reports as rp
        update, ctx, query = make_runner_query("compare_Q1_5")
        csrepo = MagicMock()
        csrepo.get = AsyncMock(return_value={"chat_id": 5})  # legacy: no keys
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value=None)
        with patch.object(rp, "ChatSettingsRepository", return_value=csrepo), \
             patch.object(rp, "QuizRepository", return_value=qrepo), \
             patch.object(rp, "get_db", lambda: None), \
             patch.object(rp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(rp.compare_results(update, ctx))  # must not raise
            ssm.assert_awaited()  # reached the quiz lookup stage
            self.assertIn("not found", ssm.await_args.args[2].lower())

    def test_no_attempts(self):
        from quizbot.runner_bot.handlers import reports as rp
        update, ctx, query = make_runner_query("compare_Q1")
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value={"qid": "Q1"})
        arepo = MagicMock()
        arepo.list_completed = AsyncMock(return_value=[])
        with patch.object(rp, "QuizRepository", return_value=qrepo), \
             patch.object(rp, "AttemptRepository", return_value=arepo), \
             patch.object(rp, "ChatSettingsRepository",
                          return_value=MagicMock()), \
             patch.object(rp, "get_db", lambda: None), \
             patch.object(rp, "safe_send_message", new=AsyncMock()) as ssm:
            _run(rp.compare_results(update, ctx))
            self.assertIn("No completed", ssm.await_args.args[2])

    def test_ok_sends_document(self):
        from quizbot.runner_bot.handlers import reports as rp
        update, ctx, query = make_runner_query("compare_Q1")
        ctx.bot.send_document = AsyncMock()
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value={"qid": "Q1"})
        arepo = MagicMock()
        arepo.list_completed = AsyncMock(return_value=[{"user_id": 1}])
        with patch.object(rp, "QuizRepository", return_value=qrepo), \
             patch.object(rp, "AttemptRepository", return_value=arepo), \
             patch.object(rp, "ChatSettingsRepository",
                          return_value=MagicMock()), \
             patch.object(rp, "get_db", lambda: None), \
             patch.object(rp, "render_analysis_html",
                          new=AsyncMock(return_value=(b"<html/>", "a.html"))):
            _run(rp.compare_results(update, ctx))
        ctx.bot.send_document.assert_awaited_once()


class RecordAttemptRobustnessCases(unittest.TestCase):
    def setUp(self):
        _clear_runner_state()

    def tearDown(self):
        _clear_runner_state()

    def test_legacy_chat_settings_do_not_break_reports(self):
        """REGRESSION: chat_settings docs without html/pdf keys raised
        KeyError inside _record_attempt_and_report, killing end-of-quiz
        reporting (and confusing 'Error generating results')."""
        from quizbot.runner_bot.handlers import quiz_play as qp
        update, ctx, uid, cid = make_runner_update("/x")
        qrepo = MagicMock()
        qrepo.get = AsyncMock(return_value=None)  # ad-hoc quiz: skip persist
        csrepo = MagicMock()
        csrepo.get = AsyncMock(return_value={"chat_id": cid})  # legacy keys
        quiz_data = {"question_set_id": "QX", "questions": []}
        with patch.object(qp, "QuizRepository", return_value=qrepo), \
             patch.object(qp, "get_db", lambda: None):
            # ChatSettingsRepository is imported lazily inside the fn
            import quizbot.database as dbmod
            with patch.object(dbmod, "ChatSettingsRepository",
                              return_value=csrepo):
                _run(qp._record_attempt_and_report(
                    ctx, cid, quiz_data, [], chat_title="T",
                    protect_type=False, thread_id=None))  # must not raise


# ===========================================================================
# 8. /gcast + /stopcast (B5)
# ===========================================================================

class GcastCases(unittest.TestCase):
    def tearDown(self):
        from quizbot.creator_bot import state as cs
        cs.broadcast.active = False

    def _msg(self, text="/gcast", uid=None, reply_to_message=None):
        return make_creator_msg(text, uid=uid,
                                reply_to_message=reply_to_message)

    def _replied_text(self, text="hello"):
        return SimpleNamespace(text=text, photo=None, video=None, document=None,
                               caption=None, reply_markup=None,
                               copy=AsyncMock())

    def test_non_owner_silent(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = self._msg()
        with patch.object(adm.config, "OWNER_ID", 1), \
             patch.object(adm.config, "ADMIN_IDS", []):
            _run(adm.gcast_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_no_reply_usage(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = self._msg()
        with patch.object(adm.config, "OWNER_ID", m._uid):
            _run(adm.gcast_cmd(MagicMock(), m))
        self.assertIn("Reply to", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_active_guard(self):
        from quizbot.creator_bot.handlers import admin as adm
        from quizbot.creator_bot import state as cs
        m = self._msg(reply_to_message=self._replied_text())
        cs.broadcast.active = True
        try:
            with patch.object(adm.config, "OWNER_ID", m._uid):
                _run(adm.gcast_cmd(MagicMock(), m))
            self.assertIn("already active", m.reply.await_args.args[0].lower())
        finally:
            _clear_creator_state(m._uid)

    def test_happy_path_counts(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = self._msg(reply_to_message=self._replied_text("hi"))
        c = MagicMock()
        c.send_message = AsyncMock()
        repo = MagicMock()
        repo.get_all = AsyncMock(return_value=[{"chat_id": 1}, {"chat_id": 2}])
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "UserRepository", return_value=repo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.gcast_cmd(c, m))
        self.assertEqual(c.send_message.await_count, 2)
        self.assertIn("2/2", str(m._pending.edit_text.await_args))
        from quizbot.creator_bot import state as cs
        self.assertFalse(cs.broadcast.active)
        _clear_creator_state(m._uid)

    def test_get_all_exception_resets_active(self):
        """REGRESSION (B5): any mid-run exception left broadcast.active True
        forever, blocking every future /gcast until restart."""
        from quizbot.creator_bot.handlers import admin as adm
        from quizbot.creator_bot import state as cs
        m = self._msg(reply_to_message=self._replied_text("hi"))
        repo = MagicMock()
        repo.get_all = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "UserRepository", return_value=repo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.gcast_cmd(MagicMock(), m))  # must not raise
        self.assertFalse(cs.broadcast.active)
        _clear_creator_state(m._uid)

    def test_corrupt_user_doc_does_not_abort_run(self):
        """REGRESSION (B5): u['chat_id'] KeyError sat OUTSIDE the per-user
        try -> one corrupt doc aborted the whole broadcast."""
        from quizbot.creator_bot.handlers import admin as adm
        from quizbot.creator_bot import state as cs
        m = self._msg(reply_to_message=self._replied_text("hi"))
        c = MagicMock()
        c.send_message = AsyncMock()
        repo = MagicMock()
        repo.get_all = AsyncMock(return_value=[{"nope": 1}, {"chat_id": 2}])
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "UserRepository", return_value=repo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.gcast_cmd(c, m))
        c.send_message.assert_awaited_once()  # second user still served
        self.assertIn("1/2", str(m._pending.edit_text.await_args))
        self.assertFalse(cs.broadcast.active)
        _clear_creator_state(m._uid)

    def test_progress_edit_failure_tolerated(self):
        from quizbot.creator_bot.handlers import admin as adm
        from quizbot.creator_bot import state as cs
        m = self._msg(reply_to_message=self._replied_text("hi"))
        m._pending.edit_text = AsyncMock(side_effect=RuntimeError("deleted"))
        c = MagicMock()
        c.send_message = AsyncMock()
        repo = MagicMock()
        repo.get_all = AsyncMock(return_value=[{"chat_id": 1}] * 101)
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "UserRepository", return_value=repo), \
             patch.object(adm, "get_db", lambda: None), \
             patch("asyncio.sleep", new=AsyncMock()):
            _run(adm.gcast_cmd(c, m))  # must not raise
        self.assertEqual(c.send_message.await_count, 101)
        self.assertFalse(cs.broadcast.active)
        _clear_creator_state(m._uid)

    def test_markdown_send_failure_falls_back_to_copy(self):
        """REGRESSION (B5): source text with Markdown-special chars made
        EVERY typed send fail (bridge forces Markdown parse). Copy
        preserves entities server-side and must be the fallback."""
        from quizbot.creator_bot.handlers import admin as adm
        replied = self._replied_text("hello_world *x*")
        m = self._msg(reply_to_message=replied)
        c = MagicMock()
        c.send_message = AsyncMock(side_effect=RuntimeError("Can't parse entities"))
        repo = MagicMock()
        repo.get_all = AsyncMock(return_value=[{"chat_id": 1}])
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "UserRepository", return_value=repo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.gcast_cmd(c, m))
        replied.copy.assert_awaited_once_with(1)
        self.assertIn("1/1", str(m._pending.edit_text.await_args))
        _clear_creator_state(m._uid)


class StopcastCases(unittest.TestCase):
    def test_non_owner_silent(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/stopcast")
        with patch.object(adm.config, "OWNER_ID", 1), \
             patch.object(adm.config, "ADMIN_IDS", []):
            _run(adm.stopcast_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_no_active(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/stopcast")
        with patch.object(adm.config, "OWNER_ID", m._uid):
            _run(adm.stopcast_cmd(MagicMock(), m))
        self.assertIn("No active", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_stops_active(self):
        from quizbot.creator_bot.handlers import admin as adm
        from quizbot.creator_bot import state as cs
        m = make_creator_msg("/stopcast")
        cs.broadcast.active = True
        with patch.object(adm.config, "OWNER_ID", m._uid):
            _run(adm.stopcast_cmd(MagicMock(), m))
        self.assertFalse(cs.broadcast.active)
        self.assertIn("stopped", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)


# ===========================================================================
# 9. /statses + /testapi
# ===========================================================================

class StatsesCases(unittest.TestCase):
    def test_ok(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/statses")
        urepo = MagicMock()
        urepo.stats = AsyncMock(return_value={"total_users": 10, "premium_users": 2})
        qrepo = MagicMock()
        qrepo.stats = AsyncMock(return_value={"total_quizzes": 5, "paid_quizzes": 1,
                                              "free_quizzes": 4})
        with patch.object(adm, "UserRepository", return_value=urepo), \
             patch.object(adm, "QuizRepository", return_value=qrepo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.statses_cmd(MagicMock(), m))
        txt = str(m._pending.edit_text.await_args)
        self.assertIn("10", txt)
        _clear_creator_state(m._uid)

    def test_exception_is_generic(self):
        """REGRESSION: /statses is open to ALL users but printed raw
        exception text (DB topology/hostnames on outages)."""
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/statses")
        urepo = MagicMock()
        urepo.stats = AsyncMock(side_effect=RuntimeError("mongo-prod-01:27017 down"))
        with patch.object(adm, "UserRepository", return_value=urepo), \
             patch.object(adm, "get_db", lambda: None):
            _run(adm.statses_cmd(MagicMock(), m))
        txt = str(m._pending.edit_text.await_args)
        self.assertNotIn("mongo-prod-01", txt)
        self.assertIn("Error", txt)
        _clear_creator_state(m._uid)


class TestapiCases(unittest.TestCase):
    def test_non_owner_silent(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/testapi")
        with patch.object(adm.config, "OWNER_ID", 1):
            _run(adm.testapi_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_ok(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/testapi")
        db = MagicMock()
        db.db.command = AsyncMock(return_value={"ok": 1})
        urepo = MagicMock()
        urepo.stats = AsyncMock(return_value={"total_users": 3})
        qrepo = MagicMock()
        qrepo.stats = AsyncMock(return_value={"total_quizzes": 4})
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "UserRepository", return_value=urepo), \
             patch.object(adm, "QuizRepository", return_value=qrepo), \
             patch.object(adm, "get_db", return_value=db):
            _run(adm.testapi_cmd(MagicMock(), m))
        self.assertIn("OK", str(m._pending.edit_text.await_args))
        _clear_creator_state(m._uid)

    def test_fail_reported(self):
        from quizbot.creator_bot.handlers import admin as adm
        m = make_creator_msg("/testapi")
        with patch.object(adm.config, "OWNER_ID", m._uid), \
             patch.object(adm, "get_db", side_effect=RuntimeError("down")):
            _run(adm.testapi_cmd(MagicMock(), m))
        self.assertIn("FAILED", str(m._pending.edit_text.await_args))
        _clear_creator_state(m._uid)


# ===========================================================================
# 10. /delall + /convertall + /ban (B6, B7)
# ===========================================================================

class DelallCases(unittest.TestCase):
    def test_non_owner_silent(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/delall")
        with patch.object(qm.config, "OWNER_ID", 1), \
             patch.object(qm.config, "ADMIN_IDS", []):
            _run(qm.delall_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_no_confirm_shows_preview_only(self):
        """REGRESSION (B6): one bare /delall wiped the whole platform with
        no confirmation. Must preview + require CONFIRM."""
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/delall")
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[{"qid": "Q1"}, {"qid": "Q2"}])
        repo.delete = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.delall_cmd(MagicMock(), m))
        repo.delete.assert_not_awaited()
        self.assertIn("CONFIRM", m.reply.await_args.args[0])

    def test_confirm_deletes_all(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/delall CONFIRM")
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[{"qid": "Q1"}, {"qid": "Q2"}])
        repo.delete = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.delall_cmd(MagicMock(), m))
        self.assertEqual(repo.delete.await_count, 2)
        self.assertIn("2", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_item_failure_tolerated(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/delall confirm")
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[{"qid": "Q1"}, {"qid": "Q2"}])
        repo.delete = AsyncMock(side_effect=[None, RuntimeError("x")])
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.delall_cmd(MagicMock(), m))  # must not raise
        self.assertEqual(repo.delete.await_count, 2)
        self.assertIn("failed", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_list_failure_reported(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/delall CONFIRM")
        repo = MagicMock()
        repo.list_all = AsyncMock(side_effect=RuntimeError("db down"))
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.delall_cmd(MagicMock(), m))
        m.reply.assert_awaited()
        _clear_creator_state(m._uid)


class ConvertallCases(unittest.TestCase):
    def test_non_owner_silent(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/convertall")
        with patch.object(qm.config, "OWNER_ID", 1), \
             patch.object(qm.config, "ADMIN_IDS", []):
            _run(qm.convertall_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_no_confirm_shows_preview_only(self):
        """Paid->free is lossy (original paid set unrecoverable): same
        CONFIRM discipline as /delall."""
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/convertall")
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[
            {"qid": "Q1", "quiz_type": "paid"}, {"qid": "Q2", "quiz_type": "free"}])
        repo.update_field = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.convertall_cmd(MagicMock(), m))
        repo.update_field.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_confirm_converts_paid_only(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/convertall CONFIRM")
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[
            {"qid": "Q1", "quiz_type": "paid"}, {"qid": "Q2", "quiz_type": "free"}])
        repo.update_field = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.convertall_cmd(MagicMock(), m))
        repo.update_field.assert_awaited_once_with("Q1", "quiz_type", "free")
        _clear_creator_state(m._uid)

    def test_item_failure_tolerated(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/convertall CONFIRM")
        repo = MagicMock()
        repo.list_all = AsyncMock(return_value=[
            {"qid": "Q1", "quiz_type": "paid"}, {"qid": "Q2", "quiz_type": "paid"}])
        repo.update_field = AsyncMock(side_effect=[None, RuntimeError("x")])
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.convertall_cmd(MagicMock(), m))
        self.assertEqual(repo.update_field.await_count, 2)
        _clear_creator_state(m._uid)


class BanCases(unittest.TestCase):
    def test_non_owner_silent(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban Q1")
        with patch.object(qm.config, "OWNER_ID", 1), \
             patch.object(qm.config, "ADMIN_IDS", []):
            _run(qm.ban_cmd(MagicMock(), m))
        m.reply.assert_not_awaited()
        _clear_creator_state(m._uid)

    def test_usage(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban")
        with patch.object(qm.config, "OWNER_ID", m._uid):
            _run(qm.ban_cmd(MagicMock(), m))
        self.assertIn("Usage", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_not_found(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban NOPE")
        repo = MagicMock()
        repo.get = AsyncMock(return_value=None)
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.ban_cmd(MagicMock(), m))
        self.assertIn("Not found", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_no_confirm_shows_preview_only(self):
        """REGRESSION (B7): /ban irreversibly deletes a creator's whole
        catalogue with no confirmation."""
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban Q1")
        c = MagicMock()
        c.ban_chat_member = AsyncMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 555})
        repo.list_by_creator = AsyncMock(return_value=[{"qid": "Q1"}])
        repo.delete = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.ban_cmd(c, m))
        c.ban_chat_member.assert_not_awaited()
        repo.delete.assert_not_awaited()
        self.assertIn("CONFIRM", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_confirm_bans_and_deletes(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban Q1 CONFIRM")
        c = MagicMock()
        c.ban_chat_member = AsyncMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 555})
        repo.list_by_creator = AsyncMock(return_value=[{"qid": "Q1"}, {"qid": "Q2"}])
        repo.delete = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm.config, "CHANNEL_ID", -10042), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.ban_cmd(c, m))
        c.ban_chat_member.assert_awaited_once_with(-10042, 555)
        self.assertEqual(repo.delete.await_count, 2)
        _clear_creator_state(m._uid)

    def test_unset_channel_warns_but_deletes(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban Q1 CONFIRM")
        c = MagicMock()
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 555})
        repo.list_by_creator = AsyncMock(return_value=[{"qid": "Q1"}])
        repo.delete = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm.config, "CHANNEL_ID", 0), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.ban_cmd(c, m))
        texts = " ".join(str(call) for call in m.reply.await_args_list)
        self.assertIn("CHANNEL_ID", texts)
        repo.delete.assert_awaited_once()
        _clear_creator_state(m._uid)

    def test_ban_failure_still_deletes(self):
        from quizbot.creator_bot.handlers import quiz_management as qm
        m = make_creator_msg("/ban Q1 CONFIRM")
        c = MagicMock()
        c.ban_chat_member = AsyncMock(side_effect=RuntimeError("not admin"))
        repo = MagicMock()
        repo.get = AsyncMock(return_value={"qid": "Q1", "creator_id": 555})
        repo.list_by_creator = AsyncMock(return_value=[{"qid": "Q1"}])
        repo.delete = AsyncMock()
        with patch.object(qm.config, "OWNER_ID", m._uid), \
             patch.object(qm.config, "CHANNEL_ID", -10042), \
             patch.object(qm, "QuizRepository", return_value=repo), \
             patch.object(qm, "get_db", lambda: None):
            _run(qm.ban_cmd(c, m))
        repo.delete.assert_awaited_once()
        _clear_creator_state(m._uid)


# ===========================================================================
# 11. /removeuser
# ===========================================================================

class RemoveuserCases(unittest.TestCase):
    def test_non_owner(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/removeuser 5")
        with patch.object(au.config, "OWNER_ID", 1), \
             patch.object(au.config, "ADMIN_IDS", []):
            _run(au.removeuser_cmd(MagicMock(), m))
        self.assertIn("Owner only", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_bad_format(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/removeuser")
        with patch.object(au.config, "OWNER_ID", m._uid):
            _run(au.removeuser_cmd(MagicMock(), m))
        self.assertIn("Format", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_bad_int(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/removeuser abc")
        with patch.object(au.config, "OWNER_ID", m._uid):
            _run(au.removeuser_cmd(MagicMock(), m))
        self.assertIn("Invalid user_id", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)

    def test_non_positive_rejected(self):
        from quizbot.creator_bot.handlers import auth as au
        for txt in ("/removeuser -5", "/removeuser 0"):
            m = make_creator_msg(txt)
            with patch.object(au.config, "OWNER_ID", m._uid), \
                 patch.object(au, "revoke_premium", new=AsyncMock()) as rv:
                _run(au.removeuser_cmd(MagicMock(), m))
                rv.assert_not_awaited()
                self.assertIn("positive", m.reply.await_args.args[0].lower())
                _clear_creator_state(m._uid)

    def test_ok(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/removeuser 123")
        c = MagicMock()
        c.send_message = AsyncMock()
        with patch.object(au.config, "OWNER_ID", m._uid), \
             patch.object(au, "revoke_premium", new=AsyncMock()) as rv:
            _run(au.removeuser_cmd(c, m))
            rv.assert_awaited_once_with(123)
        self.assertIn("removed", m.reply.await_args.args[0].lower())
        _clear_creator_state(m._uid)

    def test_revoke_exception_reported(self):
        from quizbot.creator_bot.handlers import auth as au
        m = make_creator_msg("/removeuser 123")
        with patch.object(au.config, "OWNER_ID", m._uid), \
             patch.object(au, "revoke_premium",
                          new=AsyncMock(side_effect=RuntimeError("db down"))):
            _run(au.removeuser_cmd(MagicMock(), m))
        self.assertIn("Could not", m.reply.await_args.args[0])
        _clear_creator_state(m._uid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
