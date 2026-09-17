"""Phase H: ``/remind`` -- opt-in daily study reminders (IST).

The command is the *only* user-facing surface of the reminder system:

* ``/remind``            -- settings card + a truthful preview of what today's
                            reminder would say, plus one-tap controls;
* ``/remind on|off``     -- toggle;
* ``/remind HH:MM``      -- set the IST time (24-hour, ``7:30`` works too);
* ``/remind due|streak|both`` -- choose what the reminder may talk about.

Reminders are personal and are therefore set up in a private chat. The actual
sending happens on the shared APScheduler tick
(:class:`quizbot.analytics.reminders.ReminderService`), never from a handler.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from quizbot.analytics.reminders import (
    CONTENT_BOTH,
    CONTENT_DUE,
    CONTENT_STREAK,
    CONTENTS,
    ReminderService,
    TIME_PRESETS,
    compose_message,
    normalize_content,
    normalize_time,
)
from quizbot.database import get_db

from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "rmd:"
LOAD_ERROR = "❌ Couldn't load your reminder settings. Please try again."

CONTENT_LABELS = {
    CONTENT_DUE: "📚 Due revisions only",
    CONTENT_STREAK: "🔥 Streak only",
    CONTENT_BOTH: "📚🔥 Both",
}


def _cb(action: str, user_id: int, token: str = "") -> str:
    return f"{CALLBACK_PREFIX}{action}:{user_id}" + (f":{token}" if token else "")


def _parse_cb(data: Optional[str]) -> Optional[tuple[str, int, str]]:
    try:
        if not data or not data.startswith(CALLBACK_PREFIX):
            return None
        parts = data.split(":")
        if len(parts) < 3:
            return None
        uid = int(parts[2])
        token = parts[3] if len(parts) > 3 else ""
    except (ValueError, IndexError, AttributeError):
        return None
    return parts[1], uid, token


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

def settings_text(settings: dict, state: Optional[dict] = None,
                  *, preview: bool = False) -> str:
    lines = [
        "⏰ <b>Daily reminders</b>",
        "",
        f"• Status: <b>{'ON' if settings['enabled'] else 'OFF'}</b>",
        f"• Time: <b>{settings['time']}</b> IST (fixed +05:30, no DST)",
        f"• Content: <b>{CONTENT_LABELS[normalize_content(settings['content'])]}</b>",
    ]
    if settings.get("last_sent_day"):
        lines.append(f"• Last sent: {settings['last_sent_day']}")
    if state and state.get("due_count"):
        lines.append(f"• Due for revision now: <b>{state['due_count']}</b>")
    lines += [
        "",
        "Reminders are sent at most once per IST day, only when there is "
        "something real to say (due cards or a streak to save), and never in "
        "any other chat. Turn them off anytime with /remind off.",
    ]
    if preview and state and settings["enabled"]:
        lines += ["", "——— <b>Today's reminder would look like this</b> ———", "",
                  compose_message(state, settings)]
    elif not settings["enabled"]:
        lines += ["", "Tap <b>Turn on</b> to start receiving them."]
    return "\n".join(lines)


def keyboard(settings: dict) -> InlineKeyboardMarkup:
    uid = settings["user_id"]
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton("🔔 Turn on" if not settings["enabled"] else "🔕 Turn off",
                             callback_data=_cb("off" if settings["enabled"] else "on", uid)),
    ])
    time_row: list[InlineKeyboardButton] = []
    for preset in TIME_PRESETS:
        mark = "✅ " if preset == settings["time"] else ""
        time_row.append(InlineKeyboardButton(
            f"{mark}{preset}", callback_data=_cb("t", uid, preset)))
        if len(time_row) == 3:
            rows.append(time_row)
            time_row = []
    if time_row:
        rows.append(time_row)
    content_row: list[InlineKeyboardButton] = []
    for content in CONTENTS:
        mark = "✅ " if normalize_content(settings["content"]) == content else ""
        content_row.append(InlineKeyboardButton(
            f"{mark}{CONTENT_LABELS[content].split(' ', 1)[1]}",
            callback_data=_cb("c", uid, content)))
    rows.append(content_row)
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def remind_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/remind [on|off|HH:MM|due|streak|both]`` (private chat)."""
    try:
        user = update.effective_user
        msg = update.effective_message
        if user is None or msg is None:
            return
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            await msg.reply_text(
                "🔒 Reminders are personal. Open a private chat with me and "
                "send /remind there.",
                do_quote=False,
            )
            return

        service = ReminderService(get_db())
        settings = await service.get_settings(user.id)
        args = [a.strip().lower() for a in (getattr(ctx, "args", None) or []) if a.strip()]
        if args:
            first = args[0]
            if first in ("on", "off"):
                settings = await service.update_settings(
                    user.id, enabled=(first == "on"))
            elif first in CONTENTS:
                settings = await service.update_settings(
                    user.id, content=first)
            else:
                parsed = normalize_time(first)
                if parsed is None:
                    await safe_send_message(
                        ctx, user.id,
                        "Usage: <code>/remind on|off|HH:MM|due|streak|both</code>\n"
                        "Example: <code>/remind 07:30</code> (IST, 24-hour).",
                        parse_mode=ParseMode.HTML)
                    return
                settings = await service.update_settings(
                    user.id, time=parsed, enabled=True)

        state = None
        if settings["enabled"]:
            try:
                state = await service.build_state(user.id)
            except Exception:
                logger.exception("remind: preview state failed for user=%s", user.id)
        await safe_send_message(
            ctx, user.id, settings_text(settings, state, preview=True),
            parse_mode=ParseMode.HTML, reply_markup=keyboard(settings),
        )
    except Exception:
        logger.exception("remind_command failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await safe_send_message(ctx, update.effective_user.id, LOAD_ERROR)
        except Exception:
            pass


async def _safe_edit(query, text: str, markup) -> None:
    markup = markup if markup is not None else InlineKeyboardMarkup([])
    try:
        await query.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=markup)
    except Exception:
        try:
            await query.message.reply_html(text, reply_markup=markup, do_quote=False)
        except Exception:
            pass


async def remind_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parsed = _parse_cb(getattr(query, "data", None))
        user = update.effective_user
        if parsed is None or user is None:
            await query.answer("❌ Invalid action", show_alert=True)
            return
        action, uid, token = parsed
        if uid != user.id:
            await query.answer("❌ Not your reminder menu", show_alert=True)
            return
        if action not in ("on", "off", "t", "c", "menu"):
            await query.answer("❌ Invalid action", show_alert=True)
            return

        service = ReminderService(get_db())
        if action == "on":
            settings = await service.update_settings(uid, enabled=True)
        elif action == "off":
            settings = await service.update_settings(uid, enabled=False)
        elif action == "t":
            parsed_time = normalize_time(token)
            if parsed_time is None:
                await query.answer("❌ Invalid time", show_alert=True)
                return
            settings = await service.update_settings(uid, time=parsed_time)
        elif action == "c":
            if token not in CONTENTS:
                await query.answer("❌ Invalid content", show_alert=True)
                return
            settings = await service.update_settings(uid, content=token)
        else:  # menu (refresh)
            settings = await service.get_settings(uid)

        state = None
        if settings["enabled"]:
            try:
                state = await service.build_state(uid)
            except Exception:
                logger.exception("remind: preview state failed for user=%s", uid)
        await query.answer()
        await _safe_edit(query, settings_text(settings, state, preview=True),
                         keyboard(settings))
    except Exception:
        logger.exception("remind_callback failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await query.answer("❌ Something went wrong. Please try again.",
                               show_alert=True)
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("remind", remind_command))
    application.add_handler(
        CallbackQueryHandler(remind_callback, pattern=r"^rmd:"))
    logger.info("Registered commands: /remind (daily study reminders)")
