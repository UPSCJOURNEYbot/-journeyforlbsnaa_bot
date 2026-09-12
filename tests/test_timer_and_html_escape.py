"""
Regression tests for two Runner-bot bugs:

1. Timer offset (/slow, /fast, /normal):
   * the offset could push ``open_period`` below Telegram's 5s minimum
     (or negative) -> sendPoll BadRequest -> question skipped, or a poll
     that never closed;
   * no upper clamp -> values above 600s were rejected the same way;
   * slot-mode sections (base timer 0) picked up the offset and turned a
     non-expiring poll into a 10s one;
   * /slow -5 flipped direction, and non-numeric arguments were silently
     ignored.

2. Telegram HTML escaping: user-controlled strings (quiz name, section
   name, participant name, chat title, batch text) were interpolated raw
   into ``parse_mode=HTML`` messages. A ``<`` or ``&`` in any of them made
   Telegram reject the whole message ("Can't parse entities"), so section
   headers / leaderboards / result cards silently vanished.
"""

from __future__ import annotations

import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from quizbot.runner_bot import quiz_utils, telegram_utils
from quizbot.runner_bot.handlers import quiz_play
from quizbot.runner_bot.quiz_utils import (
    MIN_EFFECTIVE_TIMER,
    TG_OPEN_PERIOD_MAX,
    TG_OPEN_PERIOD_MIN,
    effective_poll_timer,
    parse_timer_arg,
)
from quizbot.runner_bot.telegram_utils import esc


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ─────────────────────────── effective_poll_timer ────────────────────────────

class EffectivePollTimerCases(unittest.TestCase):
    def test_plain_timer_without_offset(self):
        self.assertEqual(effective_poll_timer(30, 0), 30)
        self.assertEqual(effective_poll_timer(30), 30)

    def test_offset_applied(self):
        self.assertEqual(effective_poll_timer(30, 10), 40)
        self.assertEqual(effective_poll_timer(30, -10), 20)

    def test_negative_result_is_clamped_to_floor_not_none(self):
        # /fast 40 on a 30s quiz used to yield -10 -> ``open_period=None``
        # -> poll never closed. Now it clamps to the 10s floor.
        self.assertEqual(effective_poll_timer(30, -40), MIN_EFFECTIVE_TIMER)
        self.assertEqual(effective_poll_timer(30, -30), MIN_EFFECTIVE_TIMER)
        self.assertEqual(effective_poll_timer(12, -5), MIN_EFFECTIVE_TIMER)

    def test_floor_is_above_telegram_minimum(self):
        self.assertGreaterEqual(MIN_EFFECTIVE_TIMER, TG_OPEN_PERIOD_MIN)

    def test_upper_clamp_at_telegram_maximum(self):
        self.assertEqual(effective_poll_timer(600, 5), TG_OPEN_PERIOD_MAX)
        self.assertEqual(effective_poll_timer(3600, 0), TG_OPEN_PERIOD_MAX)
        self.assertEqual(effective_poll_timer(590, 10), 600)

    def test_zero_base_means_non_expiring_regardless_of_offset(self):
        # Slot-mode sections send with base_timer=0 -> poll must stay open
        # even after /slow or /fast was used earlier in the quiz.
        self.assertIsNone(effective_poll_timer(0, 0))
        self.assertIsNone(effective_poll_timer(0, 15))
        self.assertIsNone(effective_poll_timer(0, -15))
        self.assertIsNone(effective_poll_timer(None, 5))

    def test_garbage_values_do_not_raise(self):
        self.assertIsNone(effective_poll_timer("abc", 5))
        self.assertEqual(effective_poll_timer("30", "5"), 35)
        self.assertEqual(effective_poll_timer(30, None), 30)
        self.assertEqual(effective_poll_timer(30, "x"), 30)


class ParseTimerArgCases(unittest.TestCase):
    def test_plain_and_signed(self):
        self.assertEqual(parse_timer_arg("15"), 15)
        self.assertEqual(parse_timer_arg("+15"), 15)
        self.assertEqual(parse_timer_arg("-5"), -5)

    def test_trailing_seconds_suffix(self):
        self.assertEqual(parse_timer_arg("20s"), 20)
        self.assertEqual(parse_timer_arg("-20S"), -20)

    def test_invalid(self):
        self.assertIsNone(parse_timer_arg(None))
        self.assertIsNone(parse_timer_arg(""))
        self.assertIsNone(parse_timer_arg("abc"))
        self.assertIsNone(parse_timer_arg("1.5"))
        self.assertIsNone(parse_timer_arg("s"))


# ──────────────────────────── /slow /fast /normal ────────────────────────────

def _private_update(chat_id=555):
    chat = SimpleNamespace(id=chat_id, type="private")
    msg = SimpleNamespace(chat=chat, from_user=SimpleNamespace(id=chat_id), sender_chat=None)
    return SimpleNamespace(message=msg, effective_chat=chat, effective_user=msg.from_user)


class SlowFastNormalCases(unittest.TestCase):
    CHAT = 555

    def setUp(self):
        self.sent: list[str] = []

        async def fake_send(ctx, chat_id, text, **kw):
            self.sent.append(text)
            return None

        self._p_send = patch.object(quiz_play, "safe_send_message", side_effect=fake_send)
        self._p_send.start()
        self._p_anon = patch.object(quiz_play, "_is_anon_admin", return_value=False)
        self._p_anon.start()
        quiz_play.session_mgr.sessions[self.CHAT] = {"modified_timer_offset": 0, "quiz_id": "Q"}

    def tearDown(self):
        self._p_send.stop()
        self._p_anon.stop()
        quiz_play.session_mgr.sessions.pop(self.CHAT, None)

    def _offset(self):
        return quiz_play.session_mgr.sessions[self.CHAT]["modified_timer_offset"]

    def _ctx(self, *args):
        return SimpleNamespace(args=list(args), bot=AsyncMock())

    def test_existing_commands_registered_and_no_new_command_added(self):
        registered: list = []
        app = SimpleNamespace(add_handler=lambda h, **kw: registered.append(h))
        quiz_play.register(app)
        cmds = set()
        for h in registered:
            cmds |= set(getattr(h, "commands", ()) or ())
        self.assertTrue({"slow", "fast", "normal"} <= cmds)
        self.assertNotIn("timer", cmds)

    def test_slow_reports_running_total(self):
        _run(quiz_play.slow_quiz(_private_update(self.CHAT), self._ctx("10")))
        _run(quiz_play.fast_quiz(_private_update(self.CHAT), self._ctx("4")))
        self.assertEqual(self._offset(), 6)
        self.assertIn("total adjustment: +6s", self.sent[-1])

    def test_normal_resets_offset(self):
        quiz_play.session_mgr.sessions[self.CHAT]["modified_timer_offset"] = 25
        _run(quiz_play.normal_quiz(_private_update(self.CHAT), self._ctx()))
        self.assertEqual(self._offset(), 0)
        self.assertIn("reset", self.sent[-1].lower())

    def test_no_session(self):
        quiz_play.session_mgr.sessions.pop(self.CHAT)
        _run(quiz_play.slow_quiz(_private_update(self.CHAT), self._ctx("5")))
        self.assertIn("No quiz running", self.sent[-1])

    def test_zero_argument_rejected(self):
        _run(quiz_play.fast_quiz(_private_update(self.CHAT), self._ctx("0")))
        self.assertEqual(self._offset(), 0)
        self.assertIn("Invalid amount", self.sent[-1])

    def test_fast_negative_argument_still_speeds_up(self):
        # Previously ``/fast -10`` -> isdigit() False -> silently used 5.
        _run(quiz_play.fast_quiz(_private_update(self.CHAT), self._ctx("-10")))
        self.assertEqual(self._offset(), -10)

    def test_slow_negative_argument_still_slows_down(self):
        _run(quiz_play.slow_quiz(_private_update(self.CHAT), self._ctx("-10")))
        self.assertEqual(self._offset(), 10)

    def test_slow_fast_defaults(self):
        _run(quiz_play.slow_quiz(_private_update(self.CHAT), self._ctx()))
        self.assertEqual(self._offset(), 5)
        _run(quiz_play.fast_quiz(_private_update(self.CHAT), self._ctx()))
        self.assertEqual(self._offset(), 0)

    def test_slow_invalid_arg_rejected_instead_of_silent_default(self):
        _run(quiz_play.slow_quiz(_private_update(self.CHAT), self._ctx("lots")))
        self.assertEqual(self._offset(), 0)
        self.assertIn("Invalid amount", self.sent[-1])


class TimerWiringCases(unittest.TestCase):
    """The poll senders and the sleep loops must all go through the clamp."""

    def test_group_sender_uses_clamped_timer(self):
        src = inspect.getsource(quiz_play._send_group_question)
        self.assertIn("effective_poll_timer(base_timer", src)
        self.assertIn("open_period=timer", src)
        self.assertNotIn("max(timer, 10)", src)

    def test_private_sender_uses_clamped_timer(self):
        src = inspect.getsource(quiz_play.send_private_question)
        self.assertIn("effective_poll_timer(", src)
        self.assertIn("MIN_EFFECTIVE_TIMER", src)

    def test_run_loops_sleep_for_clamped_timer(self):
        for fn in (quiz_play._run_flat_quiz, quiz_play._run_sectioned_quiz):
            src = inspect.getsource(fn)
            self.assertIn("effective_poll_timer(", src, fn.__name__)
            self.assertNotIn('max(base_timer + session.get("modified_timer_offset", 0), 10)', src)
            self.assertNotIn('max(sec_timer + session.get("modified_timer_offset", 0), 10)', src)


# ───────────────────────────── HTML escaping ─────────────────────────────────

class EscCases(unittest.TestCase):
    def test_escapes_angle_brackets_and_ampersand(self):
        self.assertEqual(esc("<b>x</b> & y"), "&lt;b&gt;x&lt;/b&gt; &amp; y")

    def test_quotes_untouched(self):
        self.assertEqual(esc('He said "hi" & \'bye\''), 'He said "hi" &amp; \'bye\'')

    def test_none_and_non_str(self):
        self.assertEqual(esc(None), "")
        self.assertEqual(esc(42), "42")

    def test_idempotent_on_plain_text(self):
        self.assertEqual(esc("Polity & Governance"), "Polity &amp; Governance")
        self.assertEqual(esc("plain"), "plain")


class HtmlMessageEscapingCases(unittest.TestCase):
    """Drive the real handlers with hostile names and assert that what they
    hand to safe_send_message is well-formed Telegram HTML."""

    NASTY = "Sec <1> & <2>"
    NASTY_ESC = "Sec &lt;1&gt; &amp; &lt;2&gt;"

    def setUp(self):
        self.sent: list[tuple[str, dict]] = []

        async def fake_send(ctx, chat_id, text, **kw):
            self.sent.append((text, kw))
            return SimpleNamespace(message_id=1)

        self._p = patch.object(quiz_play, "safe_send_message", side_effect=fake_send)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        quiz_play.session_mgr.sessions.pop(777, None)

    def test_private_section_header_escaped(self):
        quiz_play.session_mgr.sessions[777] = {
            "paused": False, "quiz_data": {"timer": 30}, "context": SimpleNamespace(),
            "section_msgs": [], "questions": [], "current_index": 0,
        }
        with patch.object(quiz_play, "send_private_question", new=AsyncMock()):
            _run(quiz_play._private_section_start(
                777, {"question_range": (1, 5), "name": self.NASTY, "timer": 20}))
        text, kw = self.sent[0]
        self.assertIn(f"<b>{self.NASTY_ESC}</b>", text)
        self.assertNotIn(self.NASTY, text)
        self.assertEqual(kw.get("parse_mode"), "HTML")

    def test_mid_quiz_leaderboard_escapes_participant_names(self):
        quiz_play.session_mgr.sessions[777] = {
            "quiz_data": {"negative_marking": 0, "correct_mark": 1},
            "polls": {"p1": {"correct_option": [0]}},
            "participants": {
                1: {"name": "Evil <script> & Co", "answers": {"p1": {"option": 0}}},
            },
        }
        _run(quiz_play._send_mid_quiz_leaderboard(777, SimpleNamespace(), 5, 10))
        text, kw = self.sent[0]
        self.assertIn("Evil &lt;script&gt; &amp; Co", text)
        self.assertNotIn("<script>", text)

    def test_access_denied_escapes_batch_fields(self):
        batch = {
            "name": "Batch <A> & B", "description": "1 < 2 & 3 > 2",
            "payment_link": "https://pay.example/?a=1&b=2", "contact_info": "<@admin>",
        }
        _run(quiz_play._send_access_denied(SimpleNamespace(), 777, {"creator_id": 1}, batch))
        text, kw = self.sent[0]
        self.assertIn("Batch &lt;A&gt; &amp; B", text)
        self.assertIn("1 &lt; 2 &amp; 3 &gt; 2", text)
        self.assertIn("?a=1&amp;b=2", text)
        self.assertIn("&lt;@admin&gt;", text)
        # Every '<' left in the message must open a real tag.
        import re
        for m in re.finditer(r"<(/?)(\w+)", text):
            self.assertIn(m.group(2), {"b", "i", "code"})

    def test_result_command_escapes_quiz_name(self):
        attempt = {
            "total_questions": 10, "correct": 7, "wrong": 2, "score": 6.5, "total_time": 95,
            "quiz_name": "GS <Paper> & Ethics", "qid": "Q<1>", "time_ended": "2026-09-12 10:00",
        }
        repo = SimpleNamespace(latest_completed_for_user=AsyncMock(return_value=attempt))
        upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=777))
        with patch.object(quiz_play, "AttemptRepository", return_value=repo), \
             patch.object(quiz_play, "get_db", return_value=None):
            _run(quiz_play.result_command(upd, SimpleNamespace()))
        text, kw = self.sent[0]
        self.assertIn("<b>GS &lt;Paper&gt; &amp; Ethics</b>", text)
        self.assertIn("<code>Q&lt;1&gt;</code>", text)

    def test_setup_wizard_card_and_schedule_use_esc(self):
        from quizbot.runner_bot.handlers import scheduling, setup_wizard
        card_src = inspect.getsource(setup_wizard)
        self.assertIn("esc(quiz.get('quiz_name', 'Quiz'))", card_src)
        self.assertIn("esc(sec.get('name', '?'))", card_src)
        sched_src = inspect.getsource(scheduling)
        self.assertIn("esc(quiz.get('quiz_name', 'Quiz'))", sched_src)

    def test_no_raw_user_strings_left_in_bold_tags(self):
        """Guard: every ``<b>{...}</b>`` interpolation of a name/title in
        quiz_play must be wrapped in esc()."""
        import re
        src = inspect.getsource(quiz_play)
        raw = re.findall(r"<b>\{(?!esc\()([^}]*(?:name|title)[^}]*)\}</b>", src)
        self.assertEqual(raw, [], f"unescaped interpolations: {raw}")


if __name__ == "__main__":
    unittest.main()
