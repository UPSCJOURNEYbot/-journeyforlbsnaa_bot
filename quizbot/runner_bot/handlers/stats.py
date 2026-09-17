"""Phase C UI: ``/stats`` (alias ``/xp``) -- personal XP / level / streak card.

The command is a THIN, READ-ONLY view over
:meth:`quizbot.analytics.gamification.GamificationService.get_profile`. It
never writes XP, never opens a second analytics pipeline, and never touches
another user's data.

Flow:  Telegram -> /stats (or /xp) -> stats_command -> get_profile() ->
``user_xp`` document -> formatted reply.

Guarantees (pinned by tests/test_phase_g_stats_command.py):

* **Caller-scoped.** The profile is ALWAYS keyed by
  ``update.effective_user.id`` -- the user who typed the command. In groups a
  reply-to message is never consulted, so no other member's XP/streak can ever
  be displayed or queried; the DB lookup itself is ``user_id``-scoped.
* **Zero state.** A user with no ``user_xp`` document gets a friendly
  "nothing yet" card -- no crash, no exception, no blank message.
* **Fail-soft.** Any database failure answers a generic safe error line;
  the raw exception is logged, never sent to the chat.
* **Everywhere.** Registered with no chat-type filter, so it works in private
  chats and groups alike (matching its BOTFATHER_COMMANDS.txt Block 1 entry).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from quizbot.analytics.gamification import GamificationService
from quizbot.database import get_db

logger = logging.getLogger(__name__)

#: Both menu names share this one handler (``/xp`` is a pure alias of ``/stats``).
COMMAND_NAMES = ("stats", "xp")

_BAR_WIDTH = 10


def progress_bar(percent: int, width: int = _BAR_WIDTH) -> str:
    """A compact Unicode bar; ``percent`` is clamped to 0..100."""
    pct = max(0, min(100, int(percent or 0)))
    filled = round(pct * width / 100)
    return "▰" * filled + "▱" * (width - filled)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def format_profile(p: dict) -> str:
    """Render one :meth:`GamificationService.get_profile` dict as HTML.

    Pure function: all inputs are integers/strings produced by the profile
    API (no user-controlled names), so no injection surface exists.
    """
    lp = p["level_progress"]
    earned = p["xp_earned_today"]
    cap = p["daily_cap"]
    streak = p["current_streak"]
    longest = p["longest_streak"]

    lines = ["📊 <b>Your journey profile</b>", ""]

    if not p["exists"]:
        lines += [
            "You haven't finished a quiz yet — no XP and no streak, so",
            "nothing has been lost.",
            "",
            f"🏅 Level 1 — 0/{lp['next_level_xp']} XP toward Level 2",
            f"{progress_bar(0)} 0%",
            "",
            f"⚡ Total XP: 0",
            f"📈 Today: 0/{cap} XP",
            f"🔥 Streak: 0 days (longest 0)",
            f"🎯 Quizzes completed: 0",
            "",
            "Finish any quiz to earn XP (up to 200/day) and start an IST",
            "daily streak. Try /listquiz to pick one!",
        ]
        return "\n".join(lines)

    lines += [
        f"🏅 Level {lp['level']} — "
        f"{lp['xp_in_level']}/{lp['next_level_xp']} XP toward Level {lp['level'] + 1}",
        f"{progress_bar(lp['progress_percent'])} {lp['progress_percent']}%",
        "",
        f"⚡ Total XP: {p['total_xp']}",
        f"📈 Today: {earned}/{cap} XP"
        + (
            f" ({p['xp_remaining_today']} XP left before the daily cap)"
            if p["xp_remaining_today"] > 0 and earned > 0
            else (" — daily cap reached, come back tomorrow (IST)" if earned >= cap else "")
        ),
        f"🔥 Streak: {_plural(streak, 'day')} (longest {longest})",
        f"🎯 Quizzes completed: {p['total_completions']}",
    ]
    if streak == 0 and longest > 0:
        lines += ["💡 Your streak lapsed — finish a quiz today to start over at day 1."]
    elif p["streak_alive"] and p["last_activity_day"] is not None:
        lines += ["✅ Streak safe for today — see you again tomorrow (IST)!"]
    else:
        lines += ["Finish a quiz today to keep the streak alive."]
    return "\n".join(lines)


_SAFE_ERROR = "⚠️ Could not load your stats right now. Please try again in a bit."


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/stats`` and ``/xp``: show the CALLING user's XP/level/streak card."""
    user = update.effective_user
    message = update.effective_message
    # Channel posts / anonymous-group-admin messages carry no real user;
    # there is no profile to show, so stay silent instead of guessing one.
    if user is None or isinstance(user.id, bool) or not isinstance(user.id, int):
        return
    if message is None:
        return
    try:
        profile = await GamificationService(get_db()).get_profile(user.id)
    except Exception:
        # Fail-soft: log full details, show a generic safe line. The raw
        # exception (and any internals) never reach the chat.
        logger.exception("stats_command failed for user=%s", user.id)
        try:
            await message.reply_text(_SAFE_ERROR)
        except Exception:  # pragma: no cover - even the error reply failing is non-fatal
            logger.exception("stats_command error-reply failed for user=%s", user.id)
        return
    await message.reply_text(format_profile(profile), parse_mode=ParseMode.HTML)


def register(application: Application) -> None:
    """Register ``/stats`` + ``/xp`` (one handler, both names, all chats)."""
    application.add_handler(CommandHandler(COMMAND_NAMES, stats_command))
    # Startup log marker: after a deploy/restart, grep the service logs for
    # this line to confirm the RUNNING process actually has the /stats+ /xp
    # handlers (the BotFather menu alone proves nothing about the runtime).
    logger.info("Registered commands: /stats, /xp (gamification profile card)")
