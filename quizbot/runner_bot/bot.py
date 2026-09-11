"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application

from quizbot.shared import config

from . import handlers
from .creator_bridge import register_creator_bridge
from .handlers.admin import watchdog_loop
from .handlers.scheduling import init_schedule_manager
from .state import tasks

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def post_init(application: Application) -> None:
    """Runs once after Application.initialize() -- starts the scheduler and
    the background watchdog task."""
    manager = init_schedule_manager(scheduler, application.bot)
    scheduler.start()
    await manager.restore()
    tasks.spawn(watchdog_loop(), name="watchdog")
    logger.info("Runner Bot post_init complete (scheduler + watchdog started).")


async def post_shutdown(application: Application) -> None:
    """Runs once during Application.shutdown() -- tears down the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Runner Bot post_shutdown complete.")


def build_application() -> Application:
    """Construct the PTB Application with every handler registered."""
    application = (
        Application.builder()
        .token(config.RUNNER_BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(application)
    # Single-bot architecture: Creator handlers are adapted onto the same PTB poller.
    register_creator_bridge(application)
    return application


async def run_runner_bot() -> None:
    """Run the single Telegram bot containing both Creator and Runner roles.

    There is exactly one PTB polling client for the bot token. Creator
    commands/workflows are bridged into this same application, preventing the
    Telegram long-polling conflict that occurs when two clients share a token.
    """
    application = build_application()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Runner Bot polling started.")

    try:
        # Block here until this task is cancelled by the launcher (run.py),
        # which happens on SIGINT/SIGTERM or if the sibling bot task dies.
        await asyncio.Future()
    finally:
        logger.info("Runner Bot shutting down...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
