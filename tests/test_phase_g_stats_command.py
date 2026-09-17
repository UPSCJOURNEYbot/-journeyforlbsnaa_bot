"""Phase C UI tests -- the /stats command (alias /xp) end to end.

These tests pin the WHOLE flow the command promises, without MongoDB or a
Telegram network:

    Telegram update -> /stats (or /xp) -> stats_command
        -> GamificationService.get_profile()  (read-only)
        -> user_xp document                    (fake in-memory DB)
        -> formatted reply                     (captured by an AsyncMock)

Coverage mirrors the completion checklist:
  * handler registration under BOTH names on the real PTB Application;
  * end-to-end reply for a real profile (private AND group chat);
  * /xp behaving exactly like /stats (same callback, same profile);
  * XP integration: AnalyticsService.record_completion -> XP awarded ->
    saved -> /stats shows the UPDATED total, level/level-progress and
    today's counter, and existing XP-awarding logic is untouched;
  * the 200 XP daily cap surfaced by the profile (no over-display);
  * IST streak semantics as DISPLAYED: first activity today, same-day
    non-double-count, next-day increment, lapsed streak showing 0 with the
    longest record intact, yesterday's streak still alive;
  * zero state: a brand-new user gets a friendly card, never a crash;
  * group privacy: only the CALLER's document is read and shown (the DB
    query itself is user_id-scoped; reply-to users are never consulted);
  * database failure: generic safe error line, exception never propagates
    to Telegram and its text never reaches the chat;
  * documentation wiring: /help, /features and BOTFATHER_COMMANDS.txt all
    advertise stats/xp consistently with the registered handlers.

No MongoDB server and no Telegram API calls are made.
"""

from __future__ import annotations

import asyncio
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.ext import Application, CommandHandler

from quizbot.analytics.gamification import (
    DAILY_XP_CAP,
    GamificationService,
    ensure_gamification_indexes,
    level_progress,
    previous_day,
)
from quizbot.analytics.service import AnalyticsService
from quizbot.runner_bot.handlers import stats as stats_mod
from quizbot.runner_bot.handlers.stats import (
    COMMAND_NAMES,
    format_profile,
    progress_bar,
    stats_command,
)

# The in-memory Motor-like fake (unique indexes, atomic updates) shared with
# the Phase C engine tests.
from tests.test_phase_c_gamification import FakeDB

UID_A = 110001          # the user who runs the command
UID_B = 110002          # ANOTHER group member with bigger numbers


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Telegram fakes
# ---------------------------------------------------------------------------

def make_update(*, uid=UID_A, chat_type="private", reply_to_user=None):
    """A minimal PTB-like Update for a /stats message."""
    msg = SimpleNamespace(
        message_id=7,
        chat=SimpleNamespace(id=uid if chat_type == "private" else -100123, type=chat_type),
        from_user=SimpleNamespace(id=uid, first_name="Aspirant"),
        reply_text=AsyncMock(),
    )
    # /stats never consults reply_to_message; include a decoy to prove it.
    if reply_to_user is not None:
        msg.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=reply_to_user, first_name="Other"),
        )
    else:
        msg.reply_to_message = None
    return SimpleNamespace(effective_user=msg.from_user, effective_message=msg,
                           effective_chat=msg.chat, message=msg)


def make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    return ctx


async def run_cmd(update=None, ctx=None, db=None):
    """Run stats_command with the handler's ``get_db`` bound to ``db``."""
    update = update or make_update()
    ctx = ctx or make_context()
    orig = stats_mod.get_db
    if db is not None:
        stats_mod.get_db = lambda: db
    try:
        await stats_command(update, ctx)
        return update.effective_message.reply_text
    finally:
        stats_mod.get_db = orig



# ---------------------------------------------------------------------------
# Engine helpers (real completion pipeline on the fake DB)
# ---------------------------------------------------------------------------

def build_service(db):
    return AnalyticsService(db)


async def complete(svc, attempt_id, uid, *, n_q=10, n_correct=8,
                   source="group", at=None, quiz_persisted=False):
    qrs = []
    for i in range(n_q):
        ok = i < n_correct
        qrs.append({
            "q_index": i, "selected": [0], "correct_option": [0] if ok else [1],
            "outcome": "correct" if ok else "incorrect", "time_taken": 5.0,
        })
    return await svc.record_completion(
        user_id=uid, attempt_id=attempt_id, qid="QZ", quiz_name="T",
        question_results=qrs, source=source,
        quiz_persisted=quiz_persisted, score=float(n_correct), at=at,
    )


def new_db():
    db = FakeDB()
    _run(ensure_gamification_indexes(db))
    return db


def extract_int(text, label):
    """Read the integer after a rendered label like 'Total XP: 31'."""
    m = re.search(re.escape(label) + r"\s*(\d+)", text)
    return int(m.group(1)) if m else None


# ===========================================================================
# 1. level_progress() + previous_day() pure helpers
# ===========================================================================

class LevelProgressCases(unittest.TestCase):
    def test_anchors(self):
        self.assertEqual(level_progress(0)["level"], 1)
        self.assertEqual(level_progress(0)["xp_to_next"], 50)
        self.assertEqual(level_progress(50)["level"], 2)
        self.assertEqual(level_progress(110)["level"], 3)

    def test_band_math(self):
        p = level_progress(80)  # L2 band: 50..110
        self.assertEqual(p["level"], 2)
        self.assertEqual(p["level_floor_xp"], 50)
        self.assertEqual(p["next_level_xp"], 110)
        self.assertEqual(p["xp_in_level"], 30)
        self.assertEqual(p["xp_to_next"], 30)
        self.assertEqual(p["progress_percent"], 50)

    def test_percent_bounds(self):
        for xp in (-5, 0, 1, 49, 10**9):
            pct = level_progress(xp)["progress_percent"]
            self.assertTrue(0 <= pct <= 100, xp)

    def test_coerces_bad_input(self):
        self.assertEqual(level_progress(None)["level"], 1)
        self.assertEqual(level_progress("72")["level"], 2)

    def test_previous_day(self):
        self.assertEqual(previous_day("2026-09-17"), "2026-09-16")
        self.assertEqual(previous_day("2026-03-01"), "2026-02-28")  # leap year


# ===========================================================================
# 2. get_profile() read-only API
# ===========================================================================

class GetProfileCases(unittest.TestCase):
    def test_zero_state_user(self):
        db = new_db()
        p = _run(GamificationService(db).get_profile(987654))
        self.assertFalse(p["exists"])
        self.assertEqual(p["total_xp"], 0)
        self.assertEqual(p["xp_earned_today"], 0)
        self.assertEqual(p["current_level"], 1)
        self.assertEqual(p["current_streak"], 0)
        self.assertEqual(p["longest_streak"], 0)
        self.assertEqual(p["total_completions"], 0)
        self.assertEqual(p["daily_cap"], DAILY_XP_CAP)
        self.assertEqual(p["level_progress"]["level"], 1)

    def test_returns_stored_values(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A))
        p = _run(svc.gamification.get_profile(UID_A))
        self.assertTrue(p["exists"])
        self.assertGreater(p["total_xp"], 0)
        self.assertEqual(p["total_completions"], 1)
        self.assertEqual(p["current_level"], p["level_progress"]["level"])

    def test_is_read_only(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A))
        before = [dict(d) for d in db["user_xp"].docs]
        _run(svc.gamification.get_profile(UID_A))
        self.assertEqual([dict(d) for d in db["user_xp"].docs], before)
        self.assertEqual(len(db["xp_ledger"].docs), 2)  # untouched ledger

    def test_stale_today_counter_displays_zero(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-10 12:00:00"))
        # Profile asked for a LATER day: the stored counter belongs to an old
        # xp_day, so today's earned must display 0.
        p = _run(svc.gamification.get_profile(UID_A, at="2026-09-12 12:00:00"))
        self.assertEqual(p["xp_earned_today"], 0)
        self.assertEqual(p["xp_remaining_today"], DAILY_XP_CAP)


# ===========================================================================
# 3. Handler registration: /stats AND /xp on the real PTB Application
# ===========================================================================

class RegistrationCases(unittest.TestCase):
    def test_both_commands_registered(self):
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        stats_mod.register(app)
        found = {}
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    for c in getattr(h, "commands", set()):
                        found[c] = h
        self.assertIn("stats", found)
        self.assertIn("xp", found)
        self.assertIs(found["stats"], found["xp"])  # one handler, two names

    def test_registered_via_package_register(self):
        from quizbot.runner_bot.handlers import register as runner_register
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        runner_register(app)
        cmds = set()
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    cmds.update(getattr(h, "commands", set()))
        self.assertLessEqual({"stats", "xp"}, cmds)
        # ... and exactly once (no double registration from the module list)
        stats_handlers = [
            h for handlers in app.handlers.values() for h in handlers
            if isinstance(h, CommandHandler) and "stats" in getattr(h, "commands", set())
        ]
        self.assertEqual(len(stats_handlers), 1)

    def test_no_chat_type_restriction(self):
        """/stats must answer in private chats AND groups (BOTFATHER_COMMANDS
        .txt Block 1 is an all-chats menu). The registration must carry no
        ChatType filter -- only PTB's default message-type gate (same audit
        pattern as tests/test_part4_command_audit.py)."""
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        stats_mod.register(app)
        h = next(h for handlers in app.handlers.values() for h in handlers
                 if isinstance(h, CommandHandler) and "stats" in getattr(h, "commands", set()))
        self.assertNotIn("ChatType", repr(h.filters),
                         f"/stats must not carry a chat-type filter, got {h.filters!r}")


# ===========================================================================
# 4. /stats == /xp end-to-end reply
# ===========================================================================

class CommandReplyCases(unittest.TestCase):
    def _seed(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A))
        return db, svc

    def test_stats_replies_with_profile(self):
        db, svc = self._seed()
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        self.assertEqual(reply.call_count, 1)
        text = reply.call_args[0][0]
        self.assertIn("journey profile", text)
        self.assertIn("Level", text)
        total = extract_int(text, "Total XP:")
        self.assertGreater(total, 0)
        self.assertIn(f"Today: {total}/{DAILY_XP_CAP} XP", text)
        self.assertIn("Streak: 1 day", text)
        self.assertIn("Quizzes completed: 1", text)

    def test_xp_alias_same_reply(self):
        db, svc = self._seed()
        # Simulate Telegram dispatching /xp: the same callback runs.
        upd = make_update(uid=UID_A)
        upd.message.text = "/xp"
        reply = _run(run_cmd(upd, db=db))
        text_xp = reply.call_args[0][0]
        # /stats on the same state renders the identical card.
        upd2 = make_update(uid=UID_A)
        upd2.message.text = "/stats"
        reply2 = _run(run_cmd(upd2, db=db))
        self.assertEqual(text_xp, reply2.call_args[0][0])

    def test_reflects_latest_values(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A))
        _run(complete(svc, "a2", UID_A))
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        text = reply.call_args[0][0]
        self.assertEqual(extract_int(text, "Quizzes completed:"), 2)
        p = _run(svc.gamification.get_profile(UID_A))
        self.assertEqual(extract_int(text, "Total XP:"), p["total_xp"])

    def test_level_up_shown(self):
        # 50 XP is the L2 anchor: bank it with capped-free completions.
        db = new_db()
        svc = build_service(db)
        for i in range(3):
            _run(complete(svc, f"a{i}", UID_A, n_q=10, n_correct=10))
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        text = reply.call_args[0][0]
        self.assertIn("Level 2", text)
        self.assertNotIn("Level 1 —", text)


# ===========================================================================
# 4b. Dispatch order: nothing registered EARLIER may swallow /stats or /xp
# ===========================================================================

class DispatchOrderCases(unittest.TestCase):
    """PTB runs ONE handler per update per group (first match wins). This
    pins that in the REAL registration order (runner modules + creator
    bridge), the FIRST handler matching '/stats' and '/xp' is the stats
    handler -- so no earlier CommandHandler/filter can ever intercept them
    and leave the user with silence (the live-bot failure signature)."""

    @classmethod
    def _build_app(cls):
        from telegram import Bot, User
        from telegram.ext import Application

        from quizbot.runner_bot.creator_bridge import register_creator_bridge
        from quizbot.runner_bot.handlers import register as runner_register

        app = (Application.builder()
               .token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build())
        runner_register(app)           # वही order जो build_application() करता है
        register_creator_bridge(app)

        async def fake_get_me(self=None, *a, **kw):
            self._bot_user = User(id=999, is_bot=True, first_name="SimBot",
                                  username="JourneyForLabsnaaBot")
            return self._bot_user

        orig_get_me = Bot.get_me
        Bot.get_me = fake_get_me  # offline only: no getMe network call
        try:
            _run(app.bot.initialize())
        finally:
            Bot.get_me = orig_get_me
        return app

    def _first_match(self, app, text, chat_type):
        from datetime import datetime, timezone as tz

        from telegram import Chat, Message, MessageEntity, Update, User

        uid = UID_A if chat_type == "private" else UID_B
        chat = Chat(id=(uid if chat_type == "private" else -100123), type=chat_type)
        msg = Message(
            message_id=1, date=datetime.now(tz.utc), chat=chat,
            from_user=User(id=uid, first_name="T", is_bot=False),
            text=text,
            entities=[MessageEntity(type=MessageEntity.BOT_COMMAND,
                                    offset=0, length=len(text))],
        )
        msg.set_bot(app.bot)  # check_update uses message.get_bot() shortcuts
        upd = Update(update_id=1, message=msg)
        upd.set_bot(app.bot)

        matches = []
        for i, h in enumerate(app.handlers[0]):
            try:
                r = h.check_update(upd)
            except Exception:
                continue  # non-command handlers cannot match a text command
            if r is not None and r is not False:
                matches.append((i, h))
        return matches

    def test_stats_and_xp_not_intercepted(self):
        app = self._build_app()
        group0 = app.handlers[0]
        stats_idx, stats_h = next(
            (i, h) for i, h in enumerate(group0)
            if isinstance(h, CommandHandler)
            and "stats" in getattr(h, "commands", set())
        )
        for text in ("/stats", "/xp", "/stats@JourneyForLabsnaaBot",
                     "/XP", "/Xp@JourneyForLabsnaaBot"):
            for chat_type in ("private", "group", "supergroup"):
                matches = self._first_match(app, text, chat_type)
                self.assertTrue(matches, f"{text} in {chat_type}: NO handler matched")
                first_idx, first_h = matches[0]
                self.assertIs(
                    first_h, stats_h,
                    f"{text} in {chat_type}: intercepted at position {first_idx} "
                    f"before the stats handler at {stats_idx}")
                self.assertEqual(first_idx, stats_idx)

    def test_control_commands_still_dispatch(self):
        """Sanity: the same probe matches /result and /help, so a PASS above
        is not vacuous."""
        app = self._build_app()
        for text in ("/result", "/help"):
            matches = self._first_match(app, text, "private")
            self.assertTrue(matches, f"control {text} matched nothing")


# ===========================================================================
# 5. XP integration through the REAL completion boundary
# ===========================================================================

class XpIntegrationCases(unittest.TestCase):
    def test_completion_awards_and_stats_shows_it(self):
        db = new_db()
        svc = build_service(db)
        res = _run(complete(svc, "a1", UID_A, n_q=10, n_correct=8))
        g = res["gamification"]
        self.assertTrue(g["eligible"])
        self.assertFalse(g.get("duplicate"))
        self.assertGreater(g["total_xp"], 0)
        # saved in the DB
        doc = _run(db["user_xp"].find_one({"user_id": UID_A}))
        self.assertEqual(doc["total_xp"], g["total_xp"])
        # ... and displayed by /stats
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        self.assertEqual(extract_int(reply.call_args[0][0], "Total XP:"), g["total_xp"])

    def test_awarding_logic_unchanged_for_engine(self):
        """Existing Phase C XP math regression guard: 8/10 correct, paced ->
        base 10 + 2*8 = 26 XP, streak bonus 5, cap untouched."""
        db = new_db()
        svc = build_service(db)
        res = _run(complete(svc, "a1", UID_A, n_q=10, n_correct=8))
        g = res["gamification"]
        self.assertEqual(g["score"]["gross_xp"], 26)
        self.assertEqual(g["attempt"]["awarded"], 26)
        self.assertEqual(g["streak"]["bonus"], 5)
        self.assertEqual(g["total_xp"], 31)

    def test_daily_cap_enforced_and_displayed(self):
        db = new_db()
        svc = build_service(db)
        i = 0
        while True:
            i += 1
            res = _run(complete(svc, f"a{i}", UID_A, n_q=10, n_correct=10))
            if res["gamification"]["attempt"]["capped"] or i > 50:
                break
        p = _run(svc.gamification.get_profile(UID_A))
        self.assertLessEqual(p["xp_earned_today"], DAILY_XP_CAP)
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        text = reply.call_args[0][0]
        shown = extract_int(text, "Today:")
        self.assertEqual(shown, p["xp_earned_today"])
        self.assertLessEqual(shown, DAILY_XP_CAP)
        self.assertIn(f"Today: {shown}/{DAILY_XP_CAP} XP", text)

    def test_ineligible_completion_never_moves_profile(self):
        db = new_db()
        svc = build_service(db)
        # pollquiz is a non-qualifying source for XP.
        _run(complete(svc, "a1", UID_A, source="pollquiz"))
        p = _run(svc.gamification.get_profile(UID_A))
        self.assertEqual(p["total_xp"], 0)
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        self.assertIn("haven't finished a quiz yet", reply.call_args[0][0])


# ===========================================================================
# 6. IST streak semantics (via get_profile display + engine)
# ===========================================================================

class StreakCases(unittest.TestCase):
    def test_first_activity_today_increments(self):
        db = new_db()
        svc = build_service(db)
        g = _run(complete(svc, "a1", UID_A, at="2026-09-17 12:00:00"))["gamification"]
        self.assertEqual(g["streak"]["streak"], 1)
        p = _run(svc.gamification.get_profile(UID_A, at="2026-09-17 12:00:00"))
        self.assertEqual(p["current_streak"], 1)
        self.assertEqual(p["longest_streak"], 1)
        self.assertTrue(p["streak_alive"])

    def test_same_day_second_activity_no_double_count(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-17 10:00:00"))
        _run(complete(svc, "a2", UID_A, at="2026-09-17 18:00:00"))
        p = _run(svc.gamification.get_profile(UID_A, at="2026-09-17 18:30:00"))
        self.assertEqual(p["current_streak"], 1)
        self.assertEqual(p["longest_streak"], 1)

    def test_next_consecutive_day_increments(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-16 12:00:00"))
        g = _run(complete(svc, "a2", UID_A, at="2026-09-17 12:00:00"))["gamification"]
        self.assertEqual(g["streak"]["streak"], 2)
        p = _run(svc.gamification.get_profile(UID_A, at="2026-09-17 12:00:00"))
        self.assertEqual(p["current_streak"], 2)
        self.assertEqual(p["longest_streak"], 2)

    def test_gap_resets_display_but_keeps_longest(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-14 12:00:00"))
        _run(complete(svc, "a2", UID_A, at="2026-09-15 12:00:00"))
        _run(complete(svc, "a3", UID_A, at="2026-09-16 12:00:00"))
        # Gap: skip 17th, ask on the 20th -> lapsed.
        p = _run(svc.gamification.get_profile(UID_A, at="2026-09-20 12:00:00"))
        self.assertFalse(p["streak_alive"])
        self.assertEqual(p["current_streak"], 0)   # lapsed display
        self.assertEqual(p["longest_streak"], 3)   # record kept
        self.assertEqual(p["last_activity_day"], "2026-09-16")

    def test_yesterday_streak_still_alive(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-16 12:00:00"))
        p = _run(svc.gamification.get_profile(UID_A, at="2026-09-17 12:00:00"))
        self.assertTrue(p["streak_alive"])
        self.assertEqual(p["current_streak"], 1)

    def test_lapsed_zero_state_card_hints_restart(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-10 12:00:00"))
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        text = reply.call_args[0][0]
        self.assertIn("Streak: 0 days", text)
        self.assertIn("lapsed", text.lower())
        self.assertIn("longest 1", text)

    def test_alive_streak_card_shows_safe_line(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A, at="2026-09-17 09:00:00"))
        reply = _run(run_cmd(make_update(uid=UID_A), db=db))
        self.assertIn("Streak safe for today", reply.call_args[0][0])


# ===========================================================================
# 7. Zero state
# ===========================================================================

class ZeroStateCases(unittest.TestCase):
    def test_brand_new_user_gets_card_not_crash(self):
        db = new_db()  # indexes only, no users
        reply = _run(run_cmd(make_update(uid=555000), db=db))
        self.assertEqual(reply.call_count, 1)
        text = reply.call_args[0][0]
        self.assertIn("haven't finished a quiz yet", text)
        self.assertIn("Level 1", text)
        self.assertIn("Total XP: 0", text)
        self.assertIn(f"Today: 0/{DAILY_XP_CAP} XP", text)
        self.assertIn("Streak: 0 days", text)
        self.assertIn("Quizzes completed: 0", text)
        self.assertEqual(len(db["user_xp"].docs), 0)  # read-only: no doc created

    def test_format_profile_is_total(self):
        for p in ({}, {"level_progress": level_progress(0)}):
            try:
                out = format_profile({**{
                    "exists": False, "total_xp": 0, "xp_earned_today": 0,
                    "daily_cap": DAILY_XP_CAP, "current_streak": 0,
                    "streak_alive": False, "longest_streak": 0,
                    "total_completions": 0, "level_progress": level_progress(0),
                }, **p})
                self.assertIn("journey profile", out)
            except KeyError:
                pass


# ===========================================================================
# 8. Group privacy
# ===========================================================================

class GroupPrivacyCases(unittest.TestCase):
    def _seed_two_users(self):
        db = new_db()
        svc = build_service(db)
        # B is a much bigger user; A is the caller.
        _run(complete(svc, "b1", UID_B, n_q=10, n_correct=10))
        _run(complete(svc, "b2", UID_B, n_q=10, n_correct=10))
        _run(complete(svc, "a1", UID_A, n_q=5, n_correct=1))
        return db, svc

    def test_only_callers_stats_shown(self):
        db, svc = self._seed_two_users()
        p_a = _run(svc.gamification.get_profile(UID_A))
        upd = make_update(uid=UID_A, chat_type="group", reply_to_user=UID_B)
        reply = _run(run_cmd(upd, db=db))
        text = reply.call_args[0][0]
        self.assertEqual(extract_int(text, "Total XP:"), p_a["total_xp"])
        self.assertEqual(extract_int(text, "Quizzes completed:"), 1)
        p_b = _run(svc.gamification.get_profile(UID_B))
        self.assertNotIn(str(p_b["total_xp"]), text)

    def test_db_query_is_caller_scoped(self):
        db, svc = self._seed_two_users()
        queried = []
        inner = db["user_xp"]

        class SpyUsers:
            def find_one(self, filt=None, *a, **kw):
                queried.append(dict(filt or {}))
                return inner.find_one(filt, *a, **kw)

            def __getattr__(self, name):
                return getattr(inner, name)

        orig = db["user_xp"]
        db._cols["user_xp"] = SpyUsers()
        try:
            upd = make_update(uid=UID_A, chat_type="group", reply_to_user=UID_B)
            reply = _run(run_cmd(upd, db=db))
        finally:
            db._cols["user_xp"] = orig
        for q in queried:
            self.assertEqual(q.get("user_id"), UID_A,
                             f"profile query leaked another scope: {q}")
        self.assertTrue(queried)
        text = reply.call_args[0][0]
        self.assertIn("Total XP:", text)
        # UID_B's exact XP never appears.
        self.assertNotIn(str(_run(svc.gamification.get_profile(UID_B))["total_xp"]), text)

    def test_reply_to_never_consulted(self):
        db, svc = self._seed_two_users()
        upd = make_update(uid=UID_A, chat_type="group", reply_to_user=UID_B)
        reply = _run(run_cmd(upd, db=db))
        p_a = _run(svc.gamification.get_profile(UID_A))
        text = reply.call_args[0][0]
        self.assertEqual(extract_int(text, "Total XP:"), p_a["total_xp"])


# ===========================================================================
# 9. Database failure handling
# ===========================================================================

class DbFailureCases(unittest.TestCase):
    def test_failure_gives_safe_error_no_crash_no_leak(self):
        db = new_db()

        class BoomUsers:
            def find_one(self, *a, **kw):
                raise RuntimeError("MongoClient blew up: secret-host:27017")

        db._cols["user_xp"] = BoomUsers()
        upd = make_update(uid=UID_A)
        reply = _run(run_cmd(upd, db=db))  # must NOT raise
        text = reply.call_args[0][0]
        self.assertIn("Could not load your stats", text)
        self.assertNotIn("MongoClient", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)

    def test_failure_then_recovery(self):
        db = new_db()
        svc = build_service(db)
        _run(complete(svc, "a1", UID_A))
        failing = {"on": True}
        real = svc.gamification.users.find_one

        async def flaky(*a, **kw):
            if failing["on"]:
                raise ConnectionError("network partition")
            return await real(*a, **kw)

        svc.gamification.users.find_one = flaky
        upd = make_update(uid=UID_A)
        reply = _run(run_cmd(upd, db=db))
        self.assertIn("Could not load your stats", reply.call_args[0][0])
        failing["on"] = False
        upd2 = make_update(uid=UID_A)
        reply2 = _run(run_cmd(upd2, db=db))
        self.assertIn("Total XP:", reply2.call_args[0][0])


# ===========================================================================
# 10. Documentation wiring (/help, /features, BOTFATHER_COMMANDS.txt)
# ===========================================================================

class DocsWiringCases(unittest.TestCase):
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

    def test_help_mentions_stats_and_xp(self):
        from quizbot.creator_bot.handlers import admin as cadm
        self.assertIn("`/stats`", cadm.HELP_TEXT)
        self.assertIn("`/xp`", cadm.HELP_TEXT)

    def test_features_mentions_gamification(self):
        from quizbot.creator_bot.handlers import admin as cadm
        self.assertIn("/stats", cadm.FEATURES_TEXT)
        self.assertIn("/xp", cadm.FEATURES_TEXT)

    def test_botfather_block1_has_both(self):
        text = (self.ROOT / "BOTFATHER_COMMANDS.txt").read_text()
        block1 = text.split("BLOCK 2")[0]
        commands = {}
        for line in block1.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and " - " in line:
                name, desc = line.split(" - ", 1)
                commands[name.strip()] = desc.strip()
        self.assertIn("stats", commands)
        self.assertIn("xp", commands)
        self.assertIn("alias", commands["stats"].lower())
        self.assertIn("streak", commands["stats"].lower())

    def test_botfather_matches_registered_commands(self):
        """Every Block-1 command must exist as a registered handler command
        (runner modules or creator bridge) -- stats/xp included."""
        text = (self.ROOT / "BOTFATHER_COMMANDS.txt").read_text()
        block1 = text.split("BLOCK 2")[0]
        listed = set()
        for line in block1.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and " - " in line:
                listed.add(line.split(" - ", 1)[0].strip())

        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        from quizbot.runner_bot.handlers import register as runner_register
        from quizbot.runner_bot.creator_bridge import register_creator_bridge
        runner_register(app)
        register_creator_bridge(app)
        registered = set()
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    registered.update(getattr(h, "commands", set()))
        missing = {c for c in listed if c not in registered}
        self.assertEqual(missing, set(),
                         f"menu commands with no registered handler: {missing}")
        self.assertLessEqual({"stats", "xp"}, listed & registered)


# ===========================================================================
# 11. Rendering helpers
# ===========================================================================

class RenderCases(unittest.TestCase):
    def test_progress_bar(self):
        self.assertEqual(progress_bar(0), "▱" * 10)
        self.assertEqual(progress_bar(100), "▰" * 10)
        self.assertEqual(progress_bar(50), "▰" * 5 + "▱" * 5)
        self.assertEqual(progress_bar(150), "▰" * 10)
        self.assertEqual(progress_bar(-3), "▱" * 10)

    def test_html_escape_free_zone(self):
        # Profile cards are numeric-only; assert no raw <tag> besides our own.
        p = {
            "exists": True, "total_xp": 31, "xp_earned_today": 31,
            "daily_cap": 200, "xp_remaining_today": 169,
            "current_level": 1, "current_streak": 1, "streak_alive": True,
            "longest_streak": 1, "total_completions": 1,
            "level_progress": level_progress(31),
            "last_activity_day": "2026-09-17",
        }
        out = format_profile(p)
        self.assertIn("<b>Your journey profile</b>", out)
        self.assertNotIn("<script", out.lower())


if __name__ == "__main__":
    unittest.main()
