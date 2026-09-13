"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from quizbot.database import QuizRepository, get_db
from quizbot.shared.utils import is_premium_user

from ..state import session_mgr
from ..telegram_utils import esc, safe_send_message

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# How many missed-schedule notices restore() will send at startup. A long
# outage can leave dozens of stale rows; flooding every affected group at
# boot is worse than one line in the log for the overflow.
MAX_MISSED_NOTICES = 25


def _as_float(value: Any, default: float) -> float:
    """Coerce a stored quiz field to float, falling back on NULL/garbage.

    `_run()` executes hours after the admin typed /schedule and nobody is
    watching, so a single bad field must not turn a scheduled quiz into a
    stack trace.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


async def _require_group_admin(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """True when `user_id` is an administrator/creator of `chat_id`.

    Every rejection path replies: a permission check that fails silently
    (bot demoted, FloodWait, transient API error) leaves an admin staring at a
    bot that simply said nothing, with no way to tell "not allowed" from
    "broken".
    """
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        logger.warning("get_chat_member(%s, %s) failed: %s", chat_id, user_id, e)
        await safe_send_message(
            ctx, chat_id,
            "⚠️ Could not verify your admin rights (is the bot still an admin in this group?). "
            "Please try again.",
        )
        return False
    if member.status not in ("administrator", "creator"):
        await safe_send_message(ctx, chat_id, "\U0001F6AB Admin only.")
        return False
    return True


class ScheduledQuizManager:
    """Tracks pending scheduled-quiz jobs and drives their launch via the
    shared AsyncIOScheduler instance."""

    def __init__(self, scheduler: AsyncIOScheduler, bot: Any = None) -> None:
        self.scheduler = scheduler
        self.bot = bot
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self.col = get_db().collection("scheduled_quizzes")

    async def restore(self) -> None:
        """Reload pending schedules from MongoDB after a process restart.

        Rows whose time passed while the process was down can never fire
        again, so they are reported to their group and deleted -- otherwise
        the admin who was told "✅ Scheduled!" hears nothing at all and the
        dead rows pile up in the collection forever.
        """
        now = datetime.now(IST)
        missed: list[dict[str, Any]] = []
        unparsable: list[str] = []
        restored = 0
        # Nothing is deleted while this cursor is open -- mutating a
        # collection mid-scan can make the server skip or re-serve documents.
        # Rows to drop are collected here and purged once the scan is done.
        async for row in self.col.find({}):
            job_id = row.get("job_id")
            try:
                parsed = datetime.fromisoformat(row["scheduled_time"])
                # Honour whatever offset was stored; only a naive value is
                # assumed to be IST. Blindly re-localising an offset-aware
                # timestamp would shift a UTC-written row by its whole offset.
                scheduled_time = (parsed if parsed.tzinfo else IST.localize(parsed)).astimezone(IST)
            except Exception:
                logger.exception("Unparsable scheduled_time on %s -- dropping that row", job_id)
                if job_id:
                    unparsable.append(job_id)
                continue

            if scheduled_time <= now:
                missed.append({**row, "scheduled_time": scheduled_time})
                continue

            if self.bot is None:
                logger.error("Cannot restore scheduled quiz %s: no bot instance available", job_id)
                continue
            try:
                self.scheduler.add_job(
                    self._run, trigger=DateTrigger(run_date=scheduled_time),
                    args=[row["chat_id"], row["quiz_id"], _BotContext(self.bot)],
                    id=job_id, replace_existing=True,
                )
                self.jobs[job_id] = {
                    "chat_id": row["chat_id"], "quiz_id": row["quiz_id"],
                    "scheduled_time": scheduled_time, "created_by": row["created_by"],
                    "created_at": row.get("created_at", now),
                }
                restored += 1
            except Exception:
                logger.exception("Failed to restore scheduled quiz %s", job_id)

        for job_id in unparsable:
            await self._purge(job_id)

        for i, row in enumerate(missed):
            await self._purge(row.get("job_id"))
            if i >= MAX_MISSED_NOTICES:
                continue
            try:
                await safe_send_message(
                    _BotContext(self.bot), row["chat_id"],
                    f"⚠️ The scheduled quiz {row['quiz_id']} for "
                    f"{row['scheduled_time'].strftime('%I:%M %p, %d %b')} did not run: the bot was "
                    f"offline at that time.\nRe-schedule it with /schedule {row['quiz_id']} HH:MM",
                )
            except Exception:
                logger.exception("Could not tell chat %s about a missed schedule", row.get("chat_id"))

        if missed:
            logger.warning("%d scheduled quiz(es) were missed while the bot was down", len(missed))
        logger.info("Schedule restore complete: %d re-armed, %d missed and %d unparsable row(s) cleared.",
                    restored, len(missed), len(unparsable))

    async def _purge(self, job_id: str | None) -> None:
        """Drop a job row from memory + MongoDB without touching the live
        scheduler (used for rows that can never fire again)."""
        if not job_id:
            return
        try:
            self.jobs.pop(job_id, None)
            await self.col.delete_one({"job_id": job_id})
        except Exception:
            logger.exception("Could not purge scheduled-quiz row %s", job_id)


    async def add(self, chat_id: int, qid: str, scheduled_time: datetime, created_by: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
        async with self._lock:
            job_id = f"quiz_{chat_id}_{qid}_{int(scheduled_time.timestamp())}"
            # `_BotContext(ctx.bot)` rather than the command's own context: the
            # job fires hours later, and holding PTB's context object pins its
            # Update (and chat_data/user_data) in memory for that whole time.
            # It also makes a freshly scheduled job run through exactly the
            # same code path as one re-armed by restore() after a restart.
            self.scheduler.add_job(
                self._run, trigger=DateTrigger(run_date=scheduled_time),
                args=[chat_id, qid, _BotContext(ctx.bot)], id=job_id, replace_existing=True,
            )
            created_at = datetime.now(IST)
            self.jobs[job_id] = {
                "chat_id": chat_id, "quiz_id": qid, "scheduled_time": scheduled_time,
                "created_by": created_by, "created_at": created_at,
            }
            await self.col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "job_id": job_id, "chat_id": chat_id, "quiz_id": qid,
                    "scheduled_time": scheduled_time.isoformat(), "created_by": created_by,
                    "created_at": created_at.isoformat(),
                }}, upsert=True,
            )
            return job_id

    async def remove(self, job_id: str) -> bool:
        async with self._lock:
            if job_id not in self.jobs:
                return False
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
            del self.jobs[job_id]
            await self.col.delete_one({"job_id": job_id})
            return True

    async def get_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            return [{**v, "job_id": k} for k, v in self.jobs.items() if v["chat_id"] == chat_id]

    async def _run(self, chat_id: int, qid: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from .setup_wizard import _launch_quiz_from_settings

        try:
            admin_id = None
            async with self._lock:
                for jid, info in list(self.jobs.items()):
                    if info["chat_id"] == chat_id and info["quiz_id"] == qid:
                        admin_id = info["created_by"]
                        self.jobs.pop(jid, None)
                        await self.col.delete_one({"job_id": jid})
                        break
            admin_id = admin_id or chat_id

            # The chat already has a live quiz. Launching anyway would replace
            # its session dict -- every participant, answer and poll id of the
            # running quiz is lost -- and leave two quiz loops posting polls
            # into one group. /start and /aiquiz both refuse in this case;
            # the unattended path has to refuse (loudly) as well.
            if session_mgr.get(chat_id):
                logger.warning("Scheduled quiz %s skipped: chat %s already has a running quiz", qid, chat_id)
                await safe_send_message(
                    ctx, chat_id,
                    f"⚠️ Scheduled quiz {qid} did not start: another quiz is still running here.\n"
                    f"/stop it first, then start this one with /start {qid} (or /schedule it again).",
                )
                return

            quiz_repo = QuizRepository(get_db())
            quiz = await quiz_repo.get(qid)
            if not quiz:
                await safe_send_message(ctx, chat_id, f"❌ Scheduled quiz {qid} not found. Try /start {qid} manually.")
                return

            quiz["question_set_id"] = quiz["qid"]
            quiz["negative_marking"] = quiz.get("negative_marks", 0)
            quiz["correct_mark"] = quiz.get("correct_marks", 1)

            class _FakeUser:
                def __init__(self, uid: int) -> None:
                    self.id = uid
                    self.first_name = "Scheduled"
                    self.is_bot = False

            class _FakeChat:
                def __init__(self, cid: int) -> None:
                    self.id = cid
                    self.type = "group"

            class _FakeMessage:
                def __init__(self, cid: int) -> None:
                    self.chat_id = cid
                    self.chat = _FakeChat(cid)
                    self.from_user = _FakeUser(admin_id)
                    self.message_id = int(time.time())
                    self.message_thread_id = None

            class _FakeUpdate:
                def __init__(self, cid: int) -> None:
                    self.message = _FakeMessage(cid)
                    self.update_id = int(time.time())

            # Same rules as the fixed_settings branch of /start (quiz_play.py),
            # so an unattended launch scores, explains and protects exactly
            # like the admin's own manual launch of the same quiz:
            #   * correct_mark / show_explanation come from the quiz document
            #     -- hardcoding +1 and "no explanation" here silently rescored
            #     every scheduled run of a creator-configured quiz;
            #   * protect is ON everywhere except the creator's own private
            #     chat, and a scheduled quiz always runs in a group.
            # Phase B provenance (in-memory only; the stored quiz document
            # is never modified).
            quiz["analytics_source"] = "scheduled"
            ps = {
                "quiz": quiz, "skip": 0,
                "protect": chat_id != quiz.get("creator_id"),
                "chat_type": "group",
                "correct_mark": _as_float(quiz.get("correct_marks", 1), 1.0),
                "neg_mark": _as_float(quiz.get("negative_marks", 0), 0.0),
                "shuffle_q": bool(quiz.get("shuffle_questions", False)),
                "shuffle_o": bool(quiz.get("shuffle_options", False)),
                "show_explanation": bool(quiz.get("show_explanation", False)),
                "timer_override": None, "initiator_id": admin_id,
                "update": _FakeUpdate(chat_id),
            }

            await safe_send_message(
                ctx, chat_id,
                f"\U0001F3AF <b>Scheduled Quiz Starting!</b>\n\nQuiz ID: <code>{esc(qid)}</code>\n"
                f"Started at: {datetime.now(IST).strftime('%I:%M %p')}\n"
                f"⏱ Timer: {quiz.get('timer', 30)}s | Questions: {len(quiz.get('questions', []))}",
                parse_mode=ParseMode.HTML,
            )
            await _launch_quiz_from_settings(chat_id, ctx, ps)
        except Exception as e:
            logger.error("Scheduled quiz exec error: %s", e, exc_info=True)
            try:
                await safe_send_message(ctx, chat_id, f"❌ Error starting scheduled quiz {qid}: {e}\nTry /start {qid}")
            except Exception:
                pass


class _BotContext:
    """Minimal context used by restored APScheduler jobs."""
    def __init__(self, bot: Any) -> None:
        self.bot = bot


schedule_mgr: "ScheduledQuizManager | None" = None


def init_schedule_manager(scheduler: AsyncIOScheduler, bot: Any = None) -> ScheduledQuizManager:
    """Called once from bot.py after the shared scheduler is created."""
    global schedule_mgr
    schedule_mgr = ScheduledQuizManager(scheduler, bot)
    return schedule_mgr


async def schedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/schedule QUIZ_ID HH:MM` -- schedule a quiz to auto-launch today
    (or tomorrow if the time has already passed), IST."""
    chat_id = update.message.chat_id
    try:
        user_id = update.message.from_user.id
        if update.message.chat.type == ChatType.PRIVATE:
            await safe_send_message(ctx, chat_id, "❌ Groups only.")
            return

        if not await _require_group_admin(ctx, chat_id, user_id):
            return

        if not await is_premium_user(user_id):
            await safe_send_message(ctx, chat_id, "\U0001F512 Premium required for scheduling.")
            return

        if len(ctx.args) < 2:
            await safe_send_message(
                ctx, chat_id,
                "\U0001F4C5 Usage: <code>/schedule QUIZ_ID HH:MM</code>\nExample: <code>/schedule ABC123 14:30</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        qid, time_str = ctx.args[0], ctx.args[1]
        try:
            h, m = map(int, time_str.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except Exception:
            await safe_send_message(ctx, chat_id, "❌ Invalid time. Use HH:MM (24h).")
            return

        now = datetime.now(IST)
        sched_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        rolled_over = sched_time <= now
        if rolled_over:
            sched_time += timedelta(days=1)

        quiz_repo = QuizRepository(get_db())
        quiz = await quiz_repo.get(qid)
        if not quiz:
            await safe_send_message(ctx, chat_id, f"❌ Quiz {qid} not found.")
            return
        if not quiz.get("questions"):
            # Better told now than by an empty quiz firing at the scheduled
            # hour in front of the whole group.
            await safe_send_message(ctx, chat_id, f"❌ Quiz {qid} has no questions, so there is nothing to run.")
            return

        if schedule_mgr is None:
            await safe_send_message(ctx, chat_id, "❌ Scheduler is not ready. Please try again.")
            return

        # Same quiz already queued for this group at other times? Scheduling it
        # again is legitimate, but say so -- otherwise the second run looks
        # like a duplicate the bot silently accepted.
        also_at = [
            s["scheduled_time"].strftime("%I:%M %p, %d %b")
            for s in await schedule_mgr.get_for_chat(chat_id) if s["quiz_id"] == qid
        ]

        try:
            await schedule_mgr.add(chat_id, qid, sched_time, user_id, ctx)
        except Exception as e:
            logger.error("schedule_mgr.add failed: %s", e, exc_info=True)
            await safe_send_message(
                ctx, chat_id,
                "❌ Could not save the schedule (storage error). Nothing was scheduled -- please try again.",
            )
            return

        diff = sched_time - now
        hrs, rem = divmod(int(diff.total_seconds()), 3600)
        mins, _ = divmod(rem, 60)
        notes = ""
        if rolled_over:
            notes += "\nℹ️ That time had already passed today, so it is set for tomorrow."
        if also_at:
            notes += f"\nℹ️ This quiz is also scheduled for {', '.join(also_at)}."
        await safe_send_message(
            ctx, chat_id,
            f"✅ <b>Scheduled!</b>\n\n\U0001F4DD {esc(quiz.get('quiz_name', 'Quiz'))}\n"
            f"\U0001F550 {sched_time.strftime('%I:%M %p, %d %b')}\n⏱️ In {hrs}h {mins}m{notes}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("schedule_command error: %s", e, exc_info=True)
        try:
            await safe_send_message(ctx, chat_id, "❌ Could not schedule that quiz. Please try again.")
        except Exception:
            pass


async def viewschedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/viewschedule` -- lists this group's pending scheduled quizzes."""
    chat_id = update.message.chat_id
    try:
        if update.message.chat.type == ChatType.PRIVATE:
            await safe_send_message(ctx, chat_id, "❌ Groups only.")
            return

        if schedule_mgr is None:
            await safe_send_message(ctx, chat_id, "❌ Scheduler is not ready.")
            return
        schedules = await schedule_mgr.get_for_chat(chat_id)
        if not schedules:
            await safe_send_message(ctx, chat_id, "\U0001F4C5 No scheduled quizzes.")
            return

        schedules.sort(key=lambda x: x["scheduled_time"])
        now = datetime.now(IST)
        text = "\U0001F4C5 <b>Scheduled Quizzes</b>\n\n"
        for i, s in enumerate(schedules, 1):
            diff = s["scheduled_time"] - now
            if diff.total_seconds() > 0:
                h, r = divmod(int(diff.total_seconds()), 3600)
                m, _ = divmod(r, 60)
                until = f"{h}h {m}m"
            else:
                until = "Starting soon..."
            text += f"{i}. <code>{esc(s['quiz_id'])}</code>\n   \U0001F550 {s['scheduled_time'].strftime('%I:%M %p, %d %b')} (in {until})\n\n"
        await safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("viewschedule_command error: %s", e, exc_info=True)
        try:
            await safe_send_message(ctx, chat_id, "❌ Could not read the schedule list. Please try again.")
        except Exception:
            pass


async def cancelschedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cancelschedule QUIZ_ID` -- cancels every pending scheduled run of
    that quiz in this group."""
    chat_id = update.message.chat_id
    try:
        user_id = update.message.from_user.id
        if update.message.chat.type == ChatType.PRIVATE:
            await safe_send_message(ctx, chat_id, "❌ Groups only.")
            return

        if not await _require_group_admin(ctx, chat_id, user_id):
            return

        if not ctx.args:
            await safe_send_message(ctx, chat_id, "Usage: <code>/cancelschedule QUIZ_ID</code>", parse_mode=ParseMode.HTML)
            return

        qid = ctx.args[0]
        if schedule_mgr is None:
            await safe_send_message(ctx, chat_id, "❌ Scheduler is not ready.")
            return
        schedules = await schedule_mgr.get_for_chat(chat_id)
        # A quiz can legitimately be scheduled more than once (different
        # times). Cancelling only the first match and reporting success left
        # the remaining run in place -- the admin believed it was gone and the
        # quiz still fired.
        matched = [s for s in schedules if s["quiz_id"] == qid]
        if not matched:
            await safe_send_message(ctx, chat_id, f"❌ No schedule for {qid}.")
            return

        cancelled = 0
        for s in sorted(matched, key=lambda x: x["scheduled_time"]):
            if await schedule_mgr.remove(s["job_id"]):
                cancelled += 1

        if cancelled == 0:
            await safe_send_message(ctx, chat_id, f"❌ Could not cancel the schedule for {qid}. Please try again.")
        elif cancelled == 1:
            await safe_send_message(ctx, chat_id, f"✅ Schedule cancelled for {qid}.")
        else:
            await safe_send_message(ctx, chat_id, f"✅ Cancelled all {cancelled} schedules for {qid}.")
    except Exception as e:
        logger.error("cancelschedule_command error: %s", e, exc_info=True)
        try:
            await safe_send_message(ctx, chat_id, "❌ Could not cancel that schedule. Please try again.")
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("viewschedule", viewschedule_command))
    application.add_handler(CommandHandler("cancelschedule", cancelschedule_command))
