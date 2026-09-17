"""Phase H: daily study reminders (opt-in, IST, one per day).

The reminder is a *nudge*, not a second analytics pipeline:

* the decision to speak is derived from data that already exists -- due SRS
  cards (Phase H :mod:`quizbot.analytics.srs`) and the Phase C XP/streak
  profile -- so a reminder can never claim something the user's own records do
  not show;
* it is **at-most-once per IST day** per user (``last_sent_day`` is written
  *before* the message is attempted), so a restart, a retry or two overlapping
  ticks can never spam a user;
* it is **opt-in** (``enabled`` defaults to False), **bounded** (a tick
  considers at most :data:`REMINDER_BATCH_LIMIT` users), and **quiet**: a user
  with nothing due and a safe streak is skipped rather than pinged;
* delivery is fail-soft. A blocked/deleted chat disables that user's reminder
  instead of retrying every tick forever.

The pure half (time parsing, tick selection, message composition) is testable
without a database or a clock.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from quizbot.analytics.gamification import IST, GamificationService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

DEFAULT_TIME = "20:00"          # IST
DEFAULT_CONTENT = "both"
CONTENT_DUE = "due"
CONTENT_STREAK = "streak"
CONTENT_BOTH = "both"
CONTENTS = (CONTENT_DUE, CONTENT_STREAK, CONTENT_BOTH)

#: One-tap presets offered by /remind (IST evening/early-morning study slots).
TIME_PRESETS = ("06:00", "07:30", "20:00", "21:00", "22:00")

#: A tick considers at most this many users (1 CPU / 2 GB VPS).
REMINDER_BATCH_LIMIT = 200

#: How late a missed slot may still fire (a long outage must not spam the
#: whole day's backlog at once).
MAX_LATENESS_MINUTES = 180

MINUTES_PER_DAY = 24 * 60


def _now_ist(now: Any = None) -> datetime:
    """Timezone-aware IST ``datetime`` for the supplied instant (or "now")."""
    if now is None:
        return datetime.now(IST)
    if isinstance(now, datetime):
        return (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST)
    # Strings go through the shared canonical coercion (ISO, app format, ...).
    from quizbot.analytics.gamification import _coerce_to_utc_datetime
    return _coerce_to_utc_datetime(now).astimezone(IST)


# ---------------------------------------------------------------------------
# Pure: settings validation
# ---------------------------------------------------------------------------

def normalize_time(value: Any) -> Optional[str]:
    """``"7:5"``/``"07:05"``/``"705"``/``"7"`` -> ``"07:05"`` (None if invalid).

    Accepts the shapes a human actually types; refuses anything ambiguous
    (``"25:00"``, ``""``, ``"later"``) so a bad value can never silently
    schedule a 3 a.m. ping.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return f"{value.hour:02d}:{value.minute:02d}"
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(".", ":").replace(" ", "")
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2:
            return None
    elif text.isdigit() and len(text) in (3, 4):
        parts = [text[:-2], text[-2:]]
    elif text.isdigit():
        parts = [text, "0"]
    else:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def normalize_content(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in CONTENTS else DEFAULT_CONTENT


def minutes_of_day(value: Any) -> Optional[int]:
    """``"20:00"`` -> 1200 (None when unparseable)."""
    normalized = normalize_time(value)
    if not normalized:
        return None
    hour, minute = normalized.split(":")
    return int(hour) * 60 + int(minute)


def default_settings(user_id: int) -> dict:
    """The settings a user has before ever touching ``/remind``."""
    return {
        "user_id": user_id,
        "enabled": False,
        "time": DEFAULT_TIME,
        "content": DEFAULT_CONTENT,
        "last_sent_day": None,
        "last_sent_at": None,
    }


# ---------------------------------------------------------------------------
# Pure: tick selection + message
# ---------------------------------------------------------------------------

def is_due_tick(settings: dict, now: Any = None, *,
                max_lateness_minutes: int = MAX_LATENESS_MINUTES) -> bool:
    """Should this user's reminder fire at ``now``?"""
    if not settings or not settings.get("enabled"):
        return False
    target = minutes_of_day(settings.get("time"))
    if target is None:
        return False
    now_dt = _now_ist(now)
    today = now_dt.date().isoformat()
    if settings.get("last_sent_day") == today:
        return False  # at most once per IST day
    now_minutes = now_dt.hour * 60 + now_dt.minute
    delta = now_minutes - target
    return 0 <= delta <= max(0, int(max_lateness_minutes))


def worth_sending(state: dict, content: str) -> bool:
    """False when there is genuinely nothing to say (never spam)."""
    content = normalize_content(content)
    due = int(state.get("due_count") or 0)
    if content == CONTENT_DUE:
        return due > 0
    if content == CONTENT_STREAK:
        return bool(state.get("streak_at_risk"))
    # CONTENT_BOTH: speak when there is real work pending OR a streak to save.
    # A user who is already done for the day is never pinged.
    return due > 0 or bool(state.get("streak_at_risk"))


def compose_message(state: dict, settings: Optional[dict] = None) -> str:
    """The reminder text (HTML). Only recorded facts are ever printed."""
    name = state.get("first_name") or "there"
    due = int(state.get("due_count") or 0)
    streak = int(state.get("current_streak") or 0)
    xp_today = int(state.get("xp_earned_today") or 0)
    next_due = state.get("next_due_day")
    lines = [f"⏰ <b>Daily study reminder</b>, {name}!", ""]

    if due > 0:
        lines.append(
            f"📚 <b>{due}</b> saved question{'s are' if due != 1 else ' is'} "
            "due for revision today."
        )
        topics = state.get("due_topics") or []
        if topics:
            labels = ", ".join(
                f"{t['topic']} ({t['count']})" for t in topics[:3]
            )
            lines.append(f"• Focus: {labels}")
        lines.append("Run /revise to clear the queue (it adapts to how well "
                     "you do).")
    elif next_due:
        lines.append(f"✅ Nothing is due today. Your next revision is on "
                     f"<b>{next_due}</b>.")

    if streak > 0 and state.get("streak_at_risk"):
        lines.append(
            f"🔥 Your <b>{streak}-day</b> streak is not safe yet — one quiz "
            "today keeps it alive."
        )
    elif streak > 0 and state.get("streak_alive_today"):
        lines.append(f"🔥 Streak safe for today: <b>{streak} day"
                     f"{'s' if streak != 1 else ''}</b>.")
    else:
        lines.append("Start a new streak today — finish any quiz to begin.")

    if xp_today == 0:
        lines.append("⚡ No XP earned yet today (up to 200 XP/day).")
    else:
        lines.append(f"⚡ {xp_today} XP earned today.")

    lines += [
        "",
        "Quick actions: /revise · /buildtest · /pyq · /stats",
        "Turn this off anytime with /remind off.",
    ]
    return "\n".join(lines)


def plan_tick(rows: list[dict], now: Any = None, *,
              max_lateness_minutes: int = MAX_LATENESS_MINUTES) -> list[dict]:
    """Bounded, deterministic list of settings rows that should fire now."""
    planned = [
        row for row in (rows or [])
        if isinstance(row, dict) and is_due_tick(
            row, now, max_lateness_minutes=max_lateness_minutes)
    ]
    planned.sort(key=lambda r: (minutes_of_day(r.get("time")) or 0,
                                str(r.get("user_id"))))
    return planned


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ReminderService:
    """Settings, state assembly and delivery for daily reminders."""

    def __init__(self, db: Any = None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        from quizbot.database.repositories import ReminderRepository
        self.repo = ReminderRepository(db)
        self.gamification = GamificationService(db)

    @staticmethod
    def _require_user(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("reminders require a stable integer user_id")
        return user_id

    # -- settings ----------------------------------------------------------

    async def get_settings(self, user_id: int) -> dict:
        user_id = self._require_user(user_id)
        stored = await self.repo.get(user_id)
        if not stored:
            return default_settings(user_id)
        merged = default_settings(user_id)
        merged.update({k: stored.get(k, merged[k]) for k in merged})
        merged["enabled"] = bool(stored.get("enabled"))
        return merged

    async def update_settings(
        self, user_id: int, *, enabled: Optional[bool] = None,
        time: Any = None, content: Any = None,
    ) -> dict:
        """Validate + persist a settings change (upsert on ``user_id``).

        Invalid input raises ``ValueError`` -- callers show the user a usage
        line instead of silently storing a wrong time.
        """
        user_id = self._require_user(user_id)
        fields: dict[str, Any] = {}
        if enabled is not None:
            fields["enabled"] = bool(enabled)
        if time is not None:
            normalized = normalize_time(time)
            if normalized is None:
                raise ValueError("invalid reminder time (use HH:MM, 24-hour IST)")
            fields["time"] = normalized
        if content is not None:
            if str(content).strip().lower() not in CONTENTS:
                raise ValueError("invalid reminder content")
            fields["content"] = normalize_content(content)
        if fields:
            await self.repo.upsert(user_id, fields)
        return await self.get_settings(user_id)

    async def disable(self, user_id: int) -> None:
        """Opt out (used by the user AND when a chat is unreachable)."""
        await self.repo.upsert(self._require_user(user_id), {"enabled": False})

    async def mark_sent(self, user_id: int, now: Any = None) -> None:
        now_dt = _now_ist(now)
        await self.repo.upsert(self._require_user(user_id), {
            "last_sent_day": now_dt.date().isoformat(),
            "last_sent_at": now_dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
        })

    # -- state -------------------------------------------------------------

    async def build_state(self, user_id: int, now: Any = None) -> dict:
        """Everything a reminder may talk about, read from existing records."""
        user_id = self._require_user(user_id)
        now_dt = _now_ist(now)
        today = now_dt.date().isoformat()
        profile = await self.gamification.get_profile(user_id, at=now_dt.isoformat())
        due_count = 0
        due_topics: list[dict] = []
        next_due_day = None
        try:
            from quizbot.analytics.srs import SrsService
            overview = await SrsService(self.db).overview(user_id, now_dt)
            due_count = int(overview.get("due_count") or 0)
            due_topics = list(overview.get("due_topics") or [])
            next_due_day = overview.get("next_due_day")
        except Exception:
            logger.exception("reminder: SRS lookup failed for user=%s", user_id)

        last_activity_day = profile.get("last_activity_day")
        streak = int(profile.get("current_streak") or 0)
        return {
            "user_id": user_id,
            "today": today,
            "due_count": due_count,
            "due_topics": due_topics,
            "next_due_day": next_due_day,
            "current_streak": streak,
            "longest_streak": int(profile.get("longest_streak") or 0),
            "streak_alive": bool(profile.get("streak_alive")),
            "streak_alive_today": last_activity_day == today,
            "streak_at_risk": (
                last_activity_day != today and (
                    streak > 0 or int(profile.get("total_completions") or 0) > 0)
            ),
            "xp_earned_today": int(profile.get("xp_earned_today") or 0),
            "total_xp": int(profile.get("total_xp") or 0),
            "level": int(profile.get("current_level") or 1),
        }

    # -- delivery ----------------------------------------------------------

    async def collect(self, now: Any = None, *,
                      limit: int = REMINDER_BATCH_LIMIT) -> list[dict]:
        """Messages that should go out at ``now`` (state already assembled).

        The daily guard is written first, so an exception while sending can
        lose at most one day's reminder -- never produce a duplicate.
        """
        now_dt = _now_ist(now)
        today = now_dt.date().isoformat()
        rows = await self.repo.list_enabled(limit=limit)
        planned = plan_tick(rows, now_dt)
        out: list[dict] = []
        for settings in planned:
            user_id = int(settings["user_id"])
            try:
                state = await self.build_state(user_id, now_dt)
            except Exception:
                logger.exception("reminder: state build failed for user=%s", user_id)
                continue
            if not worth_sending(state, settings.get("content")):
                # Nothing to report: mark the day as handled so the same user
                # is not re-evaluated (and never pinged) on later ticks.
                await self.mark_sent(user_id, now_dt)
                continue
            await self.mark_sent(user_id, now_dt)
            out.append({
                "user_id": user_id,
                "text": compose_message(state, settings),
                "state": state,
                "settings": settings,
                "day": today,
            })
        return out

    async def deliver(self, bot: Any, now: Any = None, *,
                      limit: int = REMINDER_BATCH_LIMIT) -> dict:
        """Send every planned reminder once. Fail-soft per user."""
        sent = failed = skipped = 0
        for item in await self.collect(now, limit=limit):
            try:
                await bot.send_message(
                    chat_id=item["user_id"], text=item["text"],
                    parse_mode="HTML", disable_web_page_preview=True,
                )
                sent += 1
            except Exception as exc:
                failed += 1
                text = str(exc).lower()
                if "blocked" in text or "chat not found" in text \
                        or "deactivated" in text:
                    # Unreachable users must not be retried forever; the
                    # reminder is simply switched off.
                    try:
                        await self.disable(item["user_id"])
                        skipped += 1
                    except Exception:
                        logger.exception(
                            "reminder: could not disable user=%s",
                            item["user_id"])
                else:
                    logger.warning("reminder: send failed for user=%s: %s",
                                   item["user_id"], exc)
        if sent or failed or skipped:
            logger.info("Reminders: sent=%d failed=%d disabled=%d", sent, failed, skipped)
        return {"sent": sent, "failed": failed, "disabled": skipped}
