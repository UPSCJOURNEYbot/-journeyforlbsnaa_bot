#!/usr/bin/env python3
"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from quizbot.database import init_db, close_db
from quizbot.shared import config
from quizbot.shared.utils.http import close_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("launcher")

# Silence noisy third-party debug logs by default.
for noisy in ("httpx", "httpcore", "apscheduler", "pymongo"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def _run_runner_bot() -> None:
    from quizbot.runner_bot.bot import run_runner_bot

    logger.info("Starting SINGLE Telegram Bot (Creator + Runner)...")
    await run_runner_bot()


async def _run_mini_app() -> None:
    from quizbot.mini_app.server import run_mini_app_server

    logger.info("Starting Mini App server (FastAPI)...")
    await run_mini_app_server()


def _pdf_runtime_preflight() -> None:
    """Fail fast — with the EXACT cause — when the native WeasyPrint/Pango
    stack is broken, instead of booting a bot whose every quiz report
    silently degrades to the generic "rendering library is unavailable"
    message (the hidden runtime failure this preflight exists to kill).

    The check proves all three layers: the native shared libraries load
    (ctypes), WeasyPrint imports, and a tiny Devanagari page renders to a
    real PDF (pdf_reports.pdf_backend_health(probe=True) — the same
    dependency set tools/pdf_native_runtime.sh provisions).

    Only enforced when the runner bot actually starts: `--only miniapp`
    never generates quiz PDFs, so a host without the native PDF stack must
    be allowed to run the Mini App alone (the existing "PDF not needed"
    mode).
    """
    from quizbot.runner_bot import pdf_reports

    health = pdf_reports.pdf_backend_health(probe=True)
    if health["available"]:
        logger.info(
            "PDF native runtime OK (WeasyPrint %s) — %s",
            health.get("weasyprint"), health.get("detail"),
        )
        return
    logger.error(
        "PDF native runtime UNAVAILABLE — every quiz result PDF would fail "
        "with a hidden error. Refusing to start into a broken PDF backend.\n%s",
        pdf_reports.format_pdf_backend_error(health),
    )
    sys.exit(1)


async def main(only: str | None) -> None:
    problems = config.validate(bot=only or "both")
    if problems:
        for p in problems:
            logger.error("Config problem: %s", p)
        logger.error("Fix the above in your .env file (see .env.example) before starting.")
        sys.exit(1)

    # Native PDF backend preflight: prove WeasyPrint can actually render
    # (native pango/harfbuzz + a tiny real PDF) BEFORE the bot goes live.
    # Skipped for --only miniapp: the runner bot (the only quiz-PDF source)
    # does not start in that mode.
    if only in (None, "runner", "creator"):
        _pdf_runtime_preflight()

    logger.info("Connecting to MongoDB (db=%s) ...", config.MONGODB_DB_NAME)
    await init_db(config.MONGODB_URI, config.MONGODB_DB_NAME)
    logger.info("Database ready.")

    tasks: list[asyncio.Task] = []
    # ONE Telegram bot / ONE polling client. Creator + Runner handlers are
    # both registered inside the Runner PTB application.
    if only in (None, "runner", "creator"):
        tasks.append(asyncio.create_task(_run_runner_bot(), name="bot"))
    if only == "miniapp":
        tasks.append(asyncio.create_task(_run_mini_app(), name="mini_app"))
    elif only is None and config.MINI_APP_DOMAIN:
        tasks.append(asyncio.create_task(_run_mini_app(), name="mini_app"))

    stop_event = asyncio.Event()

    def _handle_signal(*_args):
        logger.info("Shutdown signal received, stopping bots...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows

    try:
        done, pending = await asyncio.wait(
            [*tasks, asyncio.ensure_future(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t.exception():
                logger.exception("A bot task crashed:", exc_info=t.exception())
    finally:
        logger.info("Shutting down...")
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_session()
        await close_db()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Advance Quiz Bot platform.")
    parser.add_argument(
        "--only", choices=["creator", "runner", "miniapp"], default=None,
        help="Run the single Telegram bot (creator/runner are logical roles); "
             "or run only the Mini App server.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.only))
    except KeyboardInterrupt:
        pass
