"""Phase H: ``/revise`` -- spaced-repetition revision of saved mistakes.

``/revise`` is the scheduling face of the mistakes the user already has. The
schedule itself is a projection of the Phase B/D mistake history
(:mod:`quizbot.analytics.srs`), so this module only:

1. shows what is due today (bounded, deterministic, caller-scoped),
2. starts a bounded session through the ordinary DM engine -- carrying Phase D
   ``_revision_origins`` provenance, so answers fold back into the origin rows
   AND advance the schedule,
3. explains the box ladder honestly when nothing is due yet.

Security mirrors ``/mistakes``: the feature is personal (private chat only),
every callback embeds the caller id and is validated against it, and both
actions are allow-listed.
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

from quizbot.analytics.srs import (
    BOX_LABELS,
    SrsService,
    SRS_SESSION_SIZE,
)
from quizbot.database import get_db

from .. import practice
from ..telegram_utils import esc, safe_send_message

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "rev:"
LOAD_ERROR = "❌ Couldn't load your revision schedule. Please try again."


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

def _box_line(box: int, count: int) -> str:
    label = BOX_LABELS.get(int(box), "scheduled")
    return f"• Box {box} — {label}: <b>{count}</b> card{'s' if count != 1 else ''}"


def card_text(ov: dict) -> str:
    """The ``/revise`` card, from :meth:`SrsService.overview`."""
    if ov["total_cards"] == 0:
        return (
            "📚 <b>Spaced revision</b>\n\n"
            "You have no saved mistakes yet, so there is nothing to schedule.\n\n"
            "Play any quiz — every question you get wrong becomes a revision "
            "card. Cards start in <b>box 0</b> (due immediately) and move up "
            "the ladder each time you answer them correctly:\n"
            "• correct → next box (1 → 3 → 7 → 16 → 35 days)\n"
            "• wrong → straight back to box 0\n"
            "• skipped → unchanged (still due)\n\n"
            "Then come back with /revise."
        )
    lines = [
        "📚 <b>Spaced revision</b>",
        "",
        f"• Due today: <b>{ov['due_count']}</b>",
        f"• Scheduled later: <b>{ov['scheduled_count']}</b>",
        f"• Cards: <b>{ov['total_cards']}</b> (reviews logged: {ov['reviews']})",
    ]
    if ov["next_due_day"]:
        lines.append(f"• Next due: <b>{ov['next_due_day']}</b>")
    lines.append("")
    lines.append("<b>Ladder</b>")
    for box in sorted(ov["boxes"]):
        lines.append(_box_line(box, ov["boxes"][box]))
    if ov["due_topics"]:
        focus = ", ".join(
            f"{esc(t['topic'])} ({t['count']})" for t in ov["due_topics"])
        lines += ["", f"🎯 Focus today: {focus}"]
    if ov["due_count"]:
        lines += ["", f"Ready to revise <b>{min(ov['due_count'], SRS_SESSION_SIZE)}</b> "
                      "question(s) now."]
    else:
        lines += ["", "Nothing is due right now — you can still practise ahead "
                      "of schedule without breaking the ladder."]
    return "\n".join(lines)


def schedule_text(ov: dict) -> str:
    """Plain-language explanation of the boxes (the ``📊 Schedule`` view)."""
    lines = ["📊 <b>How your schedule works</b>", ""]
    for box in sorted(BOX_LABELS):
        count = int(ov["boxes"].get(box, 0))
        lines.append(_box_line(box, count))
    lines += [
        "",
        f"Cards in the ladder: <b>{sum(ov['boxes'].values())}</b>",
        f"Lapses (answered wrong after a promotion): <b>{ov['total_lapses']}</b>",
        "",
        "Intervals: box 0 → due now, then 1, 3, 7, 16 and 35 days. A wrong "
        "answer sends a card back to box 0; a skipped question changes "
        "nothing.",
        "The schedule is rebuilt from your own answer history, so it always "
        "matches what you actually did.",
    ]
    return "\n".join(lines)


def keyboard(ov: dict) -> Optional[InlineKeyboardMarkup]:
    uid = ov["user_id"]
    rows: list[list[InlineKeyboardButton]] = []
    if ov["due_count"]:
        size = min(ov["due_count"], SRS_SESSION_SIZE)
        rows.append([InlineKeyboardButton(
            f"▶️ Revise {size} due now", callback_data=_cb("go", uid, "due"))])
    if ov["total_cards"]:
        rows.append([InlineKeyboardButton(
            "🔁 Practise ahead", callback_data=_cb("go", uid, "ahead"))])
    rows.append([InlineKeyboardButton("📊 Schedule", callback_data=_cb("info", uid)),
                 InlineKeyboardButton("🔄 Refresh", callback_data=_cb("menu", uid))])
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

async def _launch(ctx: ContextTypes.DEFAULT_TYPE, user_id: int,
                  include_ahead: bool) -> str:
    """Build + start the due (or ahead-of-schedule) session."""
    if practice.busy(user_id):
        return practice.BUSY_TEXT
    built = await SrsService(get_db()).build_revision(
        user_id, include_ahead=include_ahead)
    if built["size"] == 0:
        return ("ℹ️ Nothing is due right now. Come back when cards fall due, "
                "or use /buildtest for a free-practice test.")
    started, _ = await practice.launch(
        ctx, user_id, built["questions"],
        quiz_name=("SRS revision (due)" if not include_ahead
                   else "SRS revision (ahead)"),
        origins_by_index=built["origins_by_index"], marker="_srs_session",
    )
    if not started:
        return "❌ Could not start the revision session. Please try again."
    label = "due" if not include_ahead else "scheduled + due"
    note = ""
    if built["excluded"]:
        note = (f"\n⚠️ {built['excluded']} card(s) could not be shown (their "
                "quiz and snapshot were both unavailable).")
    return (
        f"📚 Starting <b>{label}</b> revision with <b>{built['size']}</b> "
        f"question{'s' if built['size'] != 1 else ''}.{note}\n"
        "Answer them to move cards up the ladder — wrong answers come back "
        "sooner. XP and your streak apply as usual."
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def revise_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/revise`` [now|ahead] -- personal, so private chats only."""
    try:
        user = update.effective_user
        msg = update.effective_message
        if user is None or msg is None:
            return
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            await msg.reply_text(
                "🔒 Spaced revision is personal. Open a private chat with me "
                "and send /revise there.",
                do_quote=False,
            )
            return
        args = [a.strip().lower() for a in (getattr(ctx, "args", None) or []) if a.strip()]
        if args and args[0] in ("now", "due", "ahead", "all"):
            status = await _launch(ctx, user.id, args[0] in ("ahead", "all"))
            await safe_send_message(ctx, user.id, status, parse_mode=ParseMode.HTML)
            return
        overview = await SrsService(get_db()).overview(user.id)
        await safe_send_message(
            ctx, user.id, card_text(overview), parse_mode=ParseMode.HTML,
            reply_markup=keyboard(overview),
        )
    except Exception:
        logger.exception("revise_command failed for %s",
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


async def revise_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parsed = _parse_cb(getattr(query, "data", None))
        user = update.effective_user
        if parsed is None or user is None:
            await query.answer("❌ Invalid action", show_alert=True)
            return
        action, uid, token = parsed
        if uid != user.id:
            await query.answer("❌ Not your revision menu", show_alert=True)
            return
        if action not in ("menu", "info", "go"):
            await query.answer("❌ Invalid action", show_alert=True)
            return

        overview = await SrsService(get_db()).overview(uid)
        if action == "menu":
            await query.answer()
            await _safe_edit(query, card_text(overview), keyboard(overview))
            return
        if action == "info":
            await query.answer()
            await _safe_edit(query, schedule_text(overview),
                             InlineKeyboardMarkup([[
                                 InlineKeyboardButton(
                                     "⬅️ Back", callback_data=_cb("menu", uid))]]))
            return
        # action == "go" -- token is re-validated, never trusted as data
        if token not in ("due", "ahead"):
            await query.answer("❌ Invalid action", show_alert=True)
            return
        await query.answer("Building revision…")
        status = await _launch(ctx, uid, token == "ahead")
        await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("revise_callback failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await query.answer("❌ Something went wrong. Please try again.",
                               show_alert=True)
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("revise", revise_command))
    application.add_handler(CallbackQueryHandler(revise_callback, pattern=r"^rev:"))
    logger.info("Registered commands: /revise (spaced repetition)")
