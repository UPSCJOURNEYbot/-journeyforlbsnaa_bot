"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from quizbot.database import AuthChatRepository, QuizRepository, get_db
from quizbot.shared import config

from ..state import channel_poll_tasks, rate_limiter, session_mgr
from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

async def handle_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/pollquiz` and `/pollstop` sent as channel posts (these
    arrive via `update.channel_post`, not `update.message`)."""
    from .poll_quiz import run_channel_pollquiz

    msg = update.channel_post
    if not msg:
        return
    text = (msg.text or "").strip()
    chat_id = msg.chat_id

    try:
        await ctx.bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass

    try:
        if text.startswith("/pollstop"):
            task = channel_poll_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
            else:
                await safe_send_message(ctx, chat_id, "⚠️ No poll quiz is running.")
            return

        if not text.startswith("/pollquiz"):
            return

        args = text.split()[1:]
        if not args:
            await safe_send_message(ctx, chat_id, "Usage: /pollquiz QUIZID")
            return

        existing = channel_poll_tasks.get(chat_id)
        if existing and not existing.done():
            await safe_send_message(ctx, chat_id, "⚠️ A poll quiz is already running. Use /pollstop to stop it.")
            return

        qid = args[0]
        quiz_repo = QuizRepository(get_db())
        quiz = await quiz_repo.get(qid)
        if not quiz:
            await safe_send_message(ctx, chat_id, "❌ Invalid QuestionSetID.")
            return

        creator_id = quiz.get("creator_id")
        if quiz.get("quiz_type") == "paid" and creator_id:
            auth_repo = AuthChatRepository(get_db())
            auth_users = await auth_repo.get(creator_id)
            if chat_id not in auth_users:
                try:
                    cinfo = await ctx.bot.get_chat(creator_id)
                    details = f"\U0001F464 {cinfo.first_name or ''}\n\U0001F4AC @{cinfo.username or 'N/A'}\n\U0001F522 ID: {cinfo.id}"
                    await safe_send_message(ctx, chat_id, f"❌ This is a paid quiz. Contact creator for access.\n\n{details}")
                except Exception:
                    await safe_send_message(ctx, chat_id, f"❌ Paid quiz. Contact creator ID {creator_id} for access.")
                return

        questions = quiz.get("questions", [])
        if not questions:
            await safe_send_message(ctx, chat_id, "❌ Quiz has no questions.")
            return

        task = asyncio.create_task(run_channel_pollquiz(ctx, chat_id, quiz, is_anonymous=True))
        channel_poll_tasks[chat_id] = task
    except Exception as e:
        logger.error("handle_channel_command error: %s", e, exc_info=True)


async def watchdog_loop() -> None:
    """Periodically clean up expired quiz sessions and rate-limiter buckets."""
    while True:
        try:
            await asyncio.sleep(config.WATCHDOG_INTERVAL)
            await session_mgr.cleanup()
            rate_limiter.cleanup()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Watchdog error: %s", e)


def register(application: Application) -> None:
    # NOTE: no runner /help here. The creator bridge owns /help (a merged
    # creator+player reference); registering a second handler would make
    # every /help reply twice (same group, no filters).
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.CHANNEL, handle_channel_command))
