"""Phase H: ``/pyq`` -- previous-year-question practice.

The command practises questions that are ALREADY in the bot's bank and carry a
year: either explicit metadata (``question["analytics"]["year"]``) or an
unambiguous marker in the question text (``(2023)``, ``UPSC 2021``,
``Prelims 2019``, ``PYQ 2018``). A bare number is never treated as a year, and
nothing is ever invented -- a bank with no tagged question says exactly that
and points at ``/buildtest`` instead.

The bank reader is shared with ``/buildtest``
(:mod:`quizbot.analytics.question_bank`): access-scoped, content-deduplicated
and bounded. Sessions run through the ordinary DM engine with the canonical
``analytics_source="dm"`` marker, so answers land in analytics/XP/streak and
mistakes exactly like any other quiz.
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

from quizbot.analytics.question_bank import DEFAULT_SIZE, QuestionBankService
from quizbot.database import get_db

from .. import practice
from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "pyq:"
LOAD_ERROR = "❌ Couldn't read the question bank. Please try again."
ALL_YEARS = "all"


def _cb(action: str, user_id: int, token: str = "") -> str:
    return f"{CALLBACK_PREFIX}{action}:{user_id}" + (f":{token}" if token else "")


def _parse_cb(data: Optional[str]) -> Optional[tuple[str, int, list[str]]]:
    """``pyq:<action>:<uid>[:<token>...]`` -> ``(action, uid, tokens)``."""
    try:
        if not data or not data.startswith(CALLBACK_PREFIX):
            return None
        parts = data.split(":")
        if len(parts) < 3:
            return None
        uid = int(parts[2])
        tokens = [p for p in parts[3:] if p]
    except (ValueError, IndexError, AttributeError):
        return None
    return parts[1], uid, tokens


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

def _year_label(year: int, count: int) -> str:
    return f"📅 {year} ({count})"


def menu_text(index: dict, *, size: int = DEFAULT_SIZE, unseen_only: bool = False) -> str:
    if index["questions"] == 0:
        return (
            "📅 <b>Previous-year questions</b>\n\n"
            "I couldn't find any playable question in your bank yet. The bank "
            "is built from quizzes you created, quizzes you have played and "
            "public quizzes.\n\n"
            "Once those exist, tag a question with its year — either metadata "
            "(<code>analytics.year</code>) or simply a marker in the text "
            "like <i>(2023)</i>, <i>UPSC 2021</i> or <i>PYQ 2018</i> — and "
            "/pyq will pick it up."
        )
    if not index["years"]:
        return (
            "📅 <b>Previous-year questions</b>\n\n"
            f"I have <b>{index['questions']}</b> playable question(s) in your "
            "bank, but none of them carries a year yet, so there is nothing "
            "truthful to show as a PYQ set.\n\n"
            "<b>How a question gets a year</b>\n"
            "• metadata: <code>analytics.year = 2023</code> on the question, or\n"
            "• text marker: <i>(2023)</i>, <i>UPSC 2021</i>, <i>Prelims 2019</i>, "
            "<i>PYQ 2018</i>.\n\n"
            "Meanwhile /buildtest can assemble a custom test from everything "
            "in your bank."
        )
    lines = [
        "📅 <b>Previous-year questions</b>",
        "",
        f"• Bank: <b>{index['questions']}</b> question(s) from "
        f"{index['quizzes_scanned']} quiz(zes)",
        f"• Tagged with a year: <b>{index['questions'] - index['untagged']}</b>",
        f"• Unseen by you: <b>{index['unseen']}</b>",
        "",
        "<b>Pick a year</b> (each set is ≤ "
        f"{size} questions, unseen first):",
    ]
    if unseen_only:
        lines += ["", "🔎 Unseen-only filter is ON."]
    if index["truncated"]:
        lines += ["", "ℹ️ The bank scan hit its bound, so only the most recent "
                      "quizzes are included."]
    return "\n".join(lines)


def keyboard(index: dict, *, size: int = DEFAULT_SIZE,
             unseen_only: bool = False) -> Optional[InlineKeyboardMarkup]:
    uid = index["user_id"]
    if not index["years"]:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎯 Build a custom test",
                                 callback_data="bt:menu:%d" % uid)]])
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    # The unseen flag travels inside every year token, and must be set at
    # construction time: telegram's InlineKeyboardButton is immutable.
    flag = "1" if unseen_only else "0"
    for year in index["years"][:12]:
        count = int(index["year_counts"].get(str(year), 0))
        pair.append(InlineKeyboardButton(
            _year_label(year, count),
            callback_data=_cb("year", uid, f"{year}:{flag}")))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton("📚 All years",
                             callback_data=_cb("year", uid, f"{ALL_YEARS}:{flag}")),
        InlineKeyboardButton(
            "🔎 Unseen: ON" if unseen_only else "🔎 Unseen only",
            callback_data=_cb("unseen", uid, "0" if unseen_only else "1")),
    ])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Launch (pure-ish; DB reads through the shared bank service)
# ---------------------------------------------------------------------------

async def launch_year(ctx: ContextTypes.DEFAULT_TYPE, user_id: int,
                      year_token: str, *, size: int = DEFAULT_SIZE,
                      unseen_only: bool = False) -> str:
    """Build + start one PYQ set. ``year_token`` is re-validated here."""
    if practice.busy(user_id):
        return practice.BUSY_TEXT
    if year_token == ALL_YEARS:
        year = None
    else:
        try:
            year = int(year_token)
        except (TypeError, ValueError):
            return "❌ Invalid year."
    bank = QuestionBankService(get_db())
    built = await bank.build_pyq(
        user_id, year, size=size, unseen_only=unseen_only,
        seed=f"{user_id}:pyq:{year}:{unseen_only}")
    if built["size"] == 0:
        if built["reason"] == "no_year":
            years = ", ".join(str(y) for y in built["available_years"][:8]) or "none"
            return (f"ℹ️ No PYQ set for <b>{year}</b> in your bank. Available "
                    f"years: {years}.")
        if built["reason"] == "no_pyq":
            return ("ℹ️ No question in your bank is tagged with a year yet, so "
                    "there is nothing to practise as a PYQ set. /buildtest can "
                    "still assemble a custom test.")
        return ("ℹ️ No PYQ matched that filter"
                + (" with unseen-only on" if unseen_only else "")
                + ". Try another year or /buildtest.")
    title = f"PYQ {year}" if year else "PYQ (all years)"
    started, _ = await practice.launch(
        ctx, user_id, built["questions"], quiz_name=title,
        show_explanation=True, marker="_pyq_session",
    )
    if not started:
        return "❌ Could not start the PYQ session. Please try again."
    note = ""
    if built["size"] < size:
        note = ("\nℹ️ Only this many matching questions exist, so the set is "
                "shorter — nothing was padded.")
    return (
        f"📅 Starting <b>{title}</b> with <b>{built['size']}</b> question"
        f"{'s' if built['size'] != 1 else ''}"
        f"{' (unseen first)' if unseen_only else ''}.{note}\n"
        "Answers update your analytics, mistakes, XP and streak as usual — "
        "and any question you miss becomes an SRS card for /revise."
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def pyq_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/pyq`` [year|all|list] -- DM practice, so private chats only."""
    try:
        user = update.effective_user
        msg = update.effective_message
        if user is None or msg is None:
            return
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            await msg.reply_text(
                "🔒 PYQ practice is personal. Open a private chat with me and "
                "send /pyq there.",
                do_quote=False,
            )
            return
        args = [a.strip().lower() for a in (getattr(ctx, "args", None) or []) if a.strip()]
        if args and (args[0].isdigit() or args[0] in ("all", ALL_YEARS)):
            token = args[0] if args[0].isdigit() else ALL_YEARS
            status = await launch_year(ctx, user.id, token)
            await safe_send_message(ctx, user.id, status, parse_mode=ParseMode.HTML)
            return
        index = await QuestionBankService(get_db()).index(user.id)
        await safe_send_message(
            ctx, user.id, menu_text(index), parse_mode=ParseMode.HTML,
            reply_markup=keyboard(index),
        )
    except Exception:
        logger.exception("pyq_command failed for %s",
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


async def pyq_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parsed = _parse_cb(getattr(query, "data", None))
        user = update.effective_user
        if parsed is None or user is None:
            await query.answer("❌ Invalid action", show_alert=True)
            return
        action, uid, tokens = parsed
        if uid != user.id:
            await query.answer("❌ Not your PYQ menu", show_alert=True)
            return
        if action not in ("menu", "year", "unseen"):
            await query.answer("❌ Invalid action", show_alert=True)
            return

        index = await QuestionBankService(get_db()).index(uid)
        if action == "menu":
            await query.answer()
            await _safe_edit(query, menu_text(index), keyboard(index))
            return
        if action == "unseen":
            # The toggle is re-derived from the token; anything else means off.
            unseen = bool(tokens and tokens[0] == "1")
            await query.answer()
            await _safe_edit(query, menu_text(index, unseen_only=unseen),
                             keyboard(index, unseen_only=unseen))
            return
        # action == "year": allow-listed, re-validated below
        year_token = tokens[0] if tokens else ALL_YEARS
        unseen = bool(tokens[1:2] and tokens[1] == "1")
        if year_token != ALL_YEARS and (
            not year_token.isdigit() or int(year_token) not in index["years"]
        ):
            await query.answer("❌ That year is no longer available", show_alert=True)
            return
        await query.answer("Building PYQ set…")
        status = await launch_year(ctx, uid, year_token, unseen_only=unseen)
        await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("pyq_callback failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await query.answer("❌ Something went wrong. Please try again.",
                               show_alert=True)
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("pyq", pyq_command))
    application.add_handler(CallbackQueryHandler(pyq_callback, pattern=r"^pyq:"))
    logger.info("Registered commands: /pyq (previous-year questions)")
