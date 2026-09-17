"""Phase H tests -- daily reminders: policy, service, tick and ``/remind``.

Pure policy tests need neither clock nor database; service tests reuse the
Phase E in-memory Motor-like fake; the tick is driven through a recording fake
scheduler so no real APScheduler instance is started.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from quizbot.analytics import reminders as rm
from quizbot.analytics.reminders import ReminderService
from quizbot.database import QuizRepository
from tests.test_phase_e_weak_practice import FakeCtx, FakeUpdate, new_db, question

USER = 777
IST = ZoneInfo("Asia/Kolkata")


def at(day: str, hhmm: str) -> datetime:
    """An IST instant.

    Stored timestamps are UTC, so tests pin the zone explicitly instead of
    relying on how a naive string happens to be coerced.
    """
    year, month, date = (int(x) for x in day.split("-"))
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(year, month, date, hour, minute, tzinfo=IST)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


async def seed_quiz(db, qid="R1", n=3):
    questions = [question(f"Q{i}", ["a", "b"], 0, subject="Polity", topic="T")
                 for i in range(n)]
    await QuizRepository(db).create(9, "R quiz", questions, qid=qid,
                                    quiz_type="free", sections=[])
    return questions


async def seed_mistake(db, *, q_index=0, history=None, user=USER, qid="R1"):
    await db["user_mistakes"].insert_one({
        "user_id": user, "qid": qid, "q_index": q_index, "wrong_count": 1,
        "correct_count": 0, "status": "open", "topic": "T",
        "last_wrong_at": "2026-09-01 10:00:00",
        "revision_history": list(history or []),
    })


async def seed_xp(db, *, user=USER, total=120, streak=4, last_day="2026-09-16",
                  earned_today=0, xp_day="2026-09-17"):
    await db["user_xp"].insert_one({
        "user_id": user, "total_xp": total, "xp_earned_today": earned_today,
        "xp_day": xp_day, "current_streak": streak, "longest_streak": streak,
        "last_activity_day": last_day, "total_completions": 3, "rev": 1,
    })


# ===========================================================================
# 1. Pure settings parsing
# ===========================================================================

class TimeParsingCases(unittest.TestCase):
    def test_01_accepts_human_shapes(self):
        self.assertEqual(rm.normalize_time("7:5"), "07:05")
        self.assertEqual(rm.normalize_time("07:05"), "07:05")
        self.assertEqual(rm.normalize_time("705"), "07:05")
        self.assertEqual(rm.normalize_time("7"), "07:00")
        self.assertEqual(rm.normalize_time("19"), "19:00")
        self.assertEqual(rm.normalize_time(" 20.30 "), "20:30")

    def test_02_rejects_anything_ambiguous(self):
        for bad in ("", None, "later", "25:00", "12:99", "1:2:3", "-1", "abc"):
            self.assertIsNone(rm.normalize_time(bad), bad)

    def test_03_content_normalisation(self):
        self.assertEqual(rm.normalize_content("DUE"), rm.CONTENT_DUE)
        self.assertEqual(rm.normalize_content("streak"), rm.CONTENT_STREAK)
        self.assertEqual(rm.normalize_content("nonsense"), rm.DEFAULT_CONTENT)

    def test_04_minutes_and_defaults(self):
        self.assertEqual(rm.minutes_of_day("20:00"), 1200)
        self.assertIsNone(rm.minutes_of_day("nope"))
        defaults = rm.default_settings(USER)
        self.assertFalse(defaults["enabled"])
        self.assertEqual(defaults["time"], rm.DEFAULT_TIME)
        self.assertEqual(defaults["content"], rm.CONTENT_BOTH)
        self.assertIsNone(defaults["last_sent_day"])


# ===========================================================================
# 2. Pure tick policy
# ===========================================================================

def settings(**kw):
    base = rm.default_settings(USER)
    base.update({"enabled": True, "time": "20:00"})
    base.update(kw)
    return base


class TickCases(unittest.TestCase):
    def test_10_fires_inside_the_window(self):
        self.assertTrue(rm.is_due_tick(settings(), at("2026-09-17", "20:00")))
        self.assertTrue(rm.is_due_tick(settings(), at("2026-09-17", "20:14")))

    def test_11_does_not_fire_early(self):
        self.assertFalse(rm.is_due_tick(settings(), at("2026-09-17", "19:59")))

    def test_12_late_slots_expire(self):
        # 21:30 is 90 min late: inside the 180-min window.
        self.assertTrue(rm.is_due_tick(settings(), at("2026-09-17", "21:30")))
        # 23:59 is 239 min late: outside it.
        self.assertFalse(rm.is_due_tick(settings(), at("2026-09-17", "23:59")))

    def test_13_once_per_ist_day(self):
        sent = settings(last_sent_day="2026-09-17")
        self.assertFalse(rm.is_due_tick(sent, at("2026-09-17", "20:30")))
        # ... but a new day is a new reminder.
        self.assertTrue(rm.is_due_tick(settings(last_sent_day="2026-09-16"),
                                       at("2026-09-17", "20:05")))

    def test_14_disabled_or_invalid_time_never_fires(self):
        self.assertFalse(rm.is_due_tick(settings(enabled=False),
                                        at("2026-09-17", "20:00")))
        self.assertFalse(rm.is_due_tick(settings(time="garbage"),
                                        at("2026-09-17", "20:00")))
        self.assertFalse(rm.is_due_tick({}, at("2026-09-17", "20:00")))
        self.assertFalse(rm.is_due_tick(None, at("2026-09-17", "20:00")))

    def test_15_ist_boundary_is_respected(self):
        # 14:35 UTC == 20:05 IST -> inside the 20:00 slot.
        self.assertTrue(rm.is_due_tick(settings(), "2026-09-17T14:35:00+00:00"))


class WorthSendingCases(unittest.TestCase):
    def test_20_due_content_needs_due_cards(self):
        self.assertTrue(rm.worth_sending({"due_count": 2}, rm.CONTENT_DUE))
        self.assertFalse(rm.worth_sending({"due_count": 0}, rm.CONTENT_DUE))

    def test_21_streak_content_needs_risk(self):
        self.assertTrue(rm.worth_sending({"streak_at_risk": True}, rm.CONTENT_STREAK))
        self.assertFalse(rm.worth_sending({"streak_at_risk": False}, rm.CONTENT_STREAK))

    def test_22_both_speaks_when_anything_is_pending(self):
        self.assertTrue(rm.worth_sending({"due_count": 1, "streak_at_risk": False},
                                         rm.CONTENT_BOTH))
        self.assertTrue(rm.worth_sending({"due_count": 0, "streak_at_risk": True},
                                         rm.CONTENT_BOTH))

    def test_23_quiet_user_is_never_pinged(self):
        state = {"due_count": 0, "streak_at_risk": False, "streak_alive_today": True,
                 "current_streak": 9}
        self.assertFalse(rm.worth_sending(state, rm.CONTENT_BOTH))
        self.assertFalse(rm.worth_sending(state, rm.CONTENT_DUE))


class ComposeCases(unittest.TestCase):
    def test_30_due_message_mentions_counts_topics_and_revise(self):
        text = rm.compose_message({
            "due_count": 5, "due_topics": [{"topic": "Polity", "count": 5}],
            "current_streak": 3, "streak_at_risk": True, "xp_earned_today": 40,
        })
        self.assertIn("5", text)
        self.assertIn("Polity", text)
        self.assertIn("/revise", text)
        self.assertIn("streak", text.lower())

    def test_31_nothing_due_shows_next_date(self):
        text = rm.compose_message({
            "due_count": 0, "next_due_day": "2026-10-01",
            "current_streak": 0, "xp_earned_today": 0,
        })
        self.assertIn("2026-10-01", text)
        self.assertIn("Nothing is due today", text)

    def test_32_plan_tick_filters_and_orders(self):
        rows = [
            settings(user_id=2, time="21:00"),
            settings(user_id=1, time="20:00"),
            settings(user_id=3, enabled=False, time="20:00"),
            settings(user_id=4, time="20:00", last_sent_day="2026-09-17"),
        ]
        planned = rm.plan_tick(rows, at("2026-09-17", "21:30"))
        self.assertEqual([r["user_id"] for r in planned], [1, 2])


# ===========================================================================
# 3. Service against the fake DB
# ===========================================================================

class ReminderServiceCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        self.service = ReminderService(self.db)

    async def test_40_defaults_and_upsert(self):
        got = await self.service.get_settings(USER)
        self.assertFalse(got["enabled"])
        updated = await self.service.update_settings(
            USER, enabled=True, time="7:5", content="due")
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["time"], "07:05")
        self.assertEqual(updated["content"], rm.CONTENT_DUE)
        # One row per user, even after several updates.
        self.assertEqual(len(self.db["user_reminders"].docs), 1)
        again = await self.service.get_settings(USER)
        self.assertEqual(again["time"], "07:05")

    async def test_41_invalid_input_raises_and_stores_nothing(self):
        with self.assertRaises(ValueError):
            await self.service.update_settings(USER, time="25:00")
        with self.assertRaises(ValueError):
            await self.service.update_settings(USER, content="everything")
        self.assertEqual(self.db["user_reminders"].docs, [])

    async def test_42_build_state_from_records(self):
        await seed_quiz(self.db)
        await seed_mistake(self.db)
        await seed_xp(self.db)
        state = await self.service.build_state(USER, at("2026-09-17", "20:00"))
        self.assertEqual(state["due_count"], 1)
        self.assertEqual(state["current_streak"], 4)
        self.assertTrue(state["streak_at_risk"])       # nothing today yet
        self.assertFalse(state["streak_alive_today"])
        self.assertEqual(state["xp_earned_today"], 0)

    async def test_43_streak_safe_after_today_activity(self):
        await seed_xp(self.db, last_day="2026-09-17")
        state = await self.service.build_state(USER, at("2026-09-17", "21:00"))
        self.assertFalse(state["streak_at_risk"])
        self.assertTrue(state["streak_alive_today"])

    async def test_44_collect_marks_and_returns_messages(self):
        await seed_quiz(self.db)
        await seed_mistake(self.db)
        await seed_xp(self.db)
        await self.service.update_settings(USER, enabled=True, time="20:00")
        items = await self.service.collect(at("2026-09-17", "20:05"))
        self.assertEqual(len(items), 1)
        self.assertIn("/revise", items[0]["text"])
        row = (await self.service.get_settings(USER))
        self.assertEqual(row["last_sent_day"], "2026-09-17")

    async def test_45_second_tick_same_day_is_silent(self):
        await seed_quiz(self.db)
        await seed_mistake(self.db)
        await self.service.update_settings(USER, enabled=True, time="20:00")
        self.assertEqual(len(await self.service.collect(at("2026-09-17", "20:05"))), 1)
        self.assertEqual(await self.service.collect(at("2026-09-17", "20:20")), [])

    async def test_46_quiet_users_are_skipped_but_marked(self):
        await seed_xp(self.db, last_day="2026-09-17")     # streak safe, no due
        await self.service.update_settings(USER, enabled=True, time="20:00")
        items = await self.service.collect(at("2026-09-17", "20:05"))
        self.assertEqual(items, [])
        self.assertEqual((await self.service.get_settings(USER))["last_sent_day"],
                         "2026-09-17")

    async def test_47_disable_stops_everything(self):
        await seed_quiz(self.db)
        await seed_mistake(self.db)
        await self.service.update_settings(USER, enabled=True, time="20:00")
        await self.service.disable(USER)
        self.assertTrue(await self.service.collect(at("2026-09-17", "21:00")) == [])
        self.assertFalse((await self.service.get_settings(USER))["enabled"])

    async def test_48_deliver_sends_and_counts(self):
        await seed_quiz(self.db)
        await seed_mistake(self.db)
        await self.service.update_settings(USER, enabled=True, time="20:00")
        bot = _FakeBot()
        result = await self.service.deliver(bot, at("2026-09-17", "20:05"))
        self.assertEqual(result["sent"], 1)
        self.assertEqual(bot.sent[0][0], USER)
        self.assertIn("Daily study reminder", bot.sent[0][1])

    async def test_49_blocked_chat_is_disabled(self):
        from telegram.error import Forbidden
        await seed_quiz(self.db)
        await seed_mistake(self.db)
        await self.service.update_settings(USER, enabled=True, time="20:00")
        bot = _FakeBot(error=Forbidden("Forbidden: bot was blocked by the user"))
        result = await self.service.deliver(bot, at("2026-09-17", "20:05"))
        self.assertEqual(result["disabled"], 1)
        self.assertFalse((await self.service.get_settings(USER))["enabled"])

    async def test_50_settings_are_user_scoped(self):
        await self.service.update_settings(USER, enabled=True)
        other = await self.service.get_settings(USER + 1)
        self.assertFalse(other["enabled"])


class _FakeBot:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send_message(self, chat_id, text, **kw):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text, kw))


# ===========================================================================
# 4. Scheduler tick
# ===========================================================================

class RecordingScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger=None, **kw):
        self.jobs[kw.get("id")] = {"fn": fn, "trigger": trigger, **kw}
        return None


class TickRegistrationCases(unittest.IsolatedAsyncioTestCase):
    async def test_60_registers_one_job(self):
        from quizbot.runner_bot.handlers.reminders_tick import JOB_ID, start_reminder_tick
        scheduler = RecordingScheduler()
        with patch("quizbot.runner_bot.handlers.reminders_tick.config") as cfg:
            cfg.REMINDERS_ENABLED = True
            cfg.REMINDER_TICK_MINUTES = 15
            cfg.REMINDER_BATCH_LIMIT = 200
            job_id = start_reminder_tick(scheduler, _FakeBot())
        self.assertEqual(job_id, JOB_ID)
        job = scheduler.jobs[JOB_ID]
        self.assertTrue(job["coalesce"])
        self.assertEqual(job["max_instances"], 1)

    async def test_61_disabled_means_no_job(self):
        from quizbot.runner_bot.handlers.reminders_tick import start_reminder_tick
        scheduler = RecordingScheduler()
        with patch("quizbot.runner_bot.handlers.reminders_tick.config") as cfg:
            cfg.REMINDERS_ENABLED = False
            self.assertIsNone(start_reminder_tick(scheduler, _FakeBot()))
        self.assertEqual(scheduler.jobs, {})

    async def test_62_job_is_fail_soft(self):
        from quizbot.runner_bot.handlers import reminders_tick as tick
        scheduler = RecordingScheduler()

        class Boom:
            async def deliver(self, *a, **kw):
                raise RuntimeError("db down")

        with patch.object(tick.config, "REMINDERS_ENABLED", True), \
                patch.object(tick.config, "REMINDER_TICK_MINUTES", 15), \
                patch.object(tick.config, "REMINDER_BATCH_LIMIT", 200):
            tick.start_reminder_tick(scheduler, _FakeBot(),
                                     service_factory=lambda: Boom())
        await scheduler.jobs[tick.JOB_ID]["fn"]()   # must not raise

    async def test_63_post_init_wires_the_tick(self):
        from quizbot.runner_bot import bot as runner_bot
        self.assertTrue(hasattr(runner_bot, "post_init"))
        # The import itself is the wiring contract (post_init calls it).
        from quizbot.runner_bot.handlers.reminders_tick import start_reminder_tick
        self.assertTrue(callable(start_reminder_tick))


# ===========================================================================
# 5. /remind handler
# ===========================================================================

class RemindHandlerCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from quizbot.runner_bot.handlers import reminders as mod
        self.mod = mod
        self.db = new_db()
        self._orig = mod.get_db
        mod.get_db = lambda: self.db
        self.addCleanup(lambda: setattr(mod, "get_db", self._orig))

    async def test_70_card_shows_off_state(self):
        ctx = FakeCtx()
        await self.mod.remind_command(FakeUpdate(uid=USER), ctx)
        text = ctx.bot.sent[-1][1]
        self.assertIn("Daily reminders", text)
        self.assertIn("OFF", text)
        self.assertIn("20:00", text)

    async def test_71_text_args_enable_and_set_time(self):
        ctx = FakeCtx()
        ctx.args = ["07:30"]
        await self.mod.remind_command(FakeUpdate(uid=USER), ctx)
        self.assertIn("ON", ctx.bot.sent[-1][1])
        self.assertIn("07:30", ctx.bot.sent[-1][1])

    async def test_72_invalid_time_shows_usage(self):
        ctx = FakeCtx()
        ctx.args = ["sometime"]
        await self.mod.remind_command(FakeUpdate(uid=USER), ctx)
        self.assertIn("Usage", ctx.bot.sent[-1][1])

    async def test_73_group_chat_is_refused(self):
        update = FakeUpdate(uid=USER, chat_type="supergroup")
        await self.mod.remind_command(update, FakeCtx())
        self.assertIn("personal", update.message.replies[-1])

    async def test_74_off_argument_disables(self):
        await ReminderService(self.db).update_settings(USER, enabled=True)
        ctx = FakeCtx()
        ctx.args = ["off"]
        await self.mod.remind_command(FakeUpdate(uid=USER), ctx)
        self.assertIn("OFF", ctx.bot.sent[-1][1])

    async def test_75_callback_foreign_user_rejected(self):
        update = FakeUpdate(uid=USER, data="rmd:on:999999")
        await self.mod.remind_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])

    async def test_76_callback_time_preset_and_content(self):
        message = FakeUpdate(uid=USER).message
        await self.mod.remind_callback(
            FakeUpdate(uid=USER, data=f"rmd:t:{USER}:21:00"), FakeCtx())
        self.assertEqual((await ReminderService(self.db).get_settings(USER))["time"],
                         "21:00")
        await self.mod.remind_callback(
            FakeUpdate(uid=USER, data=f"rmd:c:{USER}:streak"), FakeCtx())
        self.assertEqual((await ReminderService(self.db).get_settings(USER))["content"],
                         rm.CONTENT_STREAK)
        self.assertIsNotNone(message)

    async def test_77_callback_invalid_time_rejected(self):
        update = FakeUpdate(uid=USER, data=f"rmd:t:{USER}:99:99")
        await self.mod.remind_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])

    async def test_78_registration(self):
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        self.mod.register(app)
        cmds, callbacks = set(), 0
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    cmds.update(getattr(h, "commands", set()))
                if isinstance(h, CallbackQueryHandler):
                    callbacks += 1
        self.assertEqual(cmds, {"remind"})
        self.assertEqual(callbacks, 1)
