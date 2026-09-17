"""Phase H: the APScheduler tick that drives daily reminders.

Kept separate from :mod:`quizbot.runner_bot.handlers.reminders` (the
``/remind`` command) because they have different lifetimes: the command is
registered per update, the tick is registered once on the shared scheduler in
``post_init``.

Design:
* one interval job, ``max_instances=1`` + ``coalesce=True`` -- a slow tick can
  never stack up behind itself on the 1 CPU VPS;
* the first run happens shortly after startup (a restart must not silently
  skip a slot; the service's lateness window decides what is still fair game);
* the job body is fail-soft: an exception is logged, never raised into the
  scheduler (APScheduler would otherwise drop the job for the process).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from apscheduler.triggers.interval import IntervalTrigger

from quizbot.analytics.reminders import ReminderService
from quizbot.shared import config

logger = logging.getLogger(__name__)

JOB_ID = "phase_h_daily_reminders"
#: Grace period before the first tick, so startup is never blocked by I/O.
FIRST_TICK_SECONDS = 120


def start_reminder_tick(
    scheduler: Any, bot: Any, *,
    interval_minutes: Optional[int] = None,
    limit: Optional[int] = None,
    service_factory: Callable[[], ReminderService] = ReminderService,
) -> Optional[str]:
    """Register the recurring reminder job. Returns its id (None if disabled).

    ``scheduler`` is any object exposing ``add_job`` (APScheduler's
    AsyncIOScheduler in production, a recording fake in tests).
    """
    if not config.REMINDERS_ENABLED:
        logger.info("Daily reminders disabled (REMINDERS_ENABLED=false).")
        return None

    minutes = int(interval_minutes or config.REMINDER_TICK_MINUTES or 15)
    minutes = max(1, minutes)

    async def _tick() -> None:
        try:
            result = await service_factory().deliver(
                bot, limit=int(limit or config.REMINDER_BATCH_LIMIT or 200))
            logger.debug("Reminder tick done: %s", result)
        except Exception:
            # A reminder must never take the bot down.
            logger.exception("Reminder tick failed")

    scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(minutes=minutes),
        id=JOB_ID,
        name="Phase H daily reminders",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        misfire_grace_time=max(60, minutes * 60),
        next_run_time=_soon(),
    )
    logger.info("Registered command-independent reminder tick every %d min.", minutes)
    return JOB_ID


def _soon():
    """First-run time: a short delay from now (lazy import keeps tests fast)."""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(seconds=FIRST_TICK_SECONDS)
