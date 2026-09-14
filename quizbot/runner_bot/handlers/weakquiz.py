"""Phase E: ``/weakquiz`` weak-topic targeted practice (private DM).

Identifies the CURRENT user's genuinely weak topics exclusively from that
user's own ``question_events`` + ``user_mistakes`` (never cross-user stats),
then assembles a bounded (<= 10) practice quiz from existing questions in
quizzes the user has played or owns and plays it through the EXISTING DM
quiz engine (``start_private_quiz``). No question generation, no new quiz
engine, no second analytics/XP path.

Security mirrors Phase D exactly: DM only, every callback embeds the owner
id and is validated against ``from_user.id``, actions are allow-listed, the
topic choice is a server-side integer index re-derived on arrival (never a
client-supplied topic string/qid), and failures are fail-soft with generic
alerts that never disclose another user's data.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from quizbot.analytics import weak_practice as wp
from quizbot.database import get_db

from ..telegram_utils import esc, safe_send_message

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "wk:"
WEAK_TIMER_SECONDS = 45

_NO_HISTORY = (
    "\U0001f3af <b>Weak topic practice</b>\n\n"
    "Play a few quizzes first \u2014 I build your weak topics from your own "
    "answers. Nothing to practise yet."
)
_INSUFFICIENT = (
    "\U0001f3af <b>Weak topic practice</b>\n\n"
    "I need at least 5 answered and 2 incorrect answers in a topic before I "
    "can call it weak. I won't judge a topic from a single question.\n\n"
    "{closest}"
    "Answer a few more questions and try again."
)
_NO_WEAK = (
    "\U0001f3af <b>Weak topic practice</b>\n\n"
    "No weak topics yet. Every topic with enough data is currently at or "
    "above the weakness threshold. Keep practising \u2014 I keep watching."
)
_LOAD_ERROR = (
    "Something went wrong while loading your weak topics. Please try again."
)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

def _cb(action: str, user_id: int, token: str = "") -> str:
    return f"{CALLBACK_PREFIX}{action}:{user_id}" + (f":{token}" if token else "")


def _parse_cb(data: Optional[str]) -> Optional[tuple[str, int, str]]:
    try:
        if not data or not data.startswith(CALLBACK_PREFIX):
            return None
        parts = data.split(":")
        if len(parts) < 3:
            return None
        action = parts[1]
        uid = int(parts[2])
        token = parts[3] if len(parts) > 3 else ""
    except (ValueError, IndexError, AttributeError):
        return None
    return action, uid, token


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _menu_text(overview: dict) -> str:
    lines = ["\U0001f3af <b>Weak topic practice</b>\n"]
    lines.append(
        "These are your weakest topics from <b>your own answers</b> "
        "(accuracy = correct \u00f7 answered; skipped questions don't count):\n"
    )
    for i, b in enumerate(overview["eligible"][:8], start=1):
        reasons = ", ".join(b.get("reasons", [])[:3])
        lines.append(
            f"{i}. <b>{esc(b['label'])}</b> \u2014 {b['accuracy_pct']:.0f}% "
            f"\u00b7 {b['answered']} answered\n   <i>{esc(reasons)}</i>"
        )
    lines.append("\nStart an auto-targeted quiz, or pick a topic yourself.")
    return "\n".join(lines)


def _menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f3af Start Weak Quiz",
                              callback_data=_cb("auto", uid))],
        [InlineKeyboardButton("\U0001f4da Choose Weak Topic",
                              callback_data=_cb("topics", uid))],
    ])


def _topics_keyboard(uid: int, eligible: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for idx, b in enumerate(eligible[:20]):
        acc = "?" if b["accuracy_pct"] is None else f"{b['accuracy_pct']:.0f}%"
        label = f"{b['label'][:22]} \u2014 {acc} \u2014 {b['answered']}a"
        pair.append(InlineKeyboardButton(
            label, callback_data=_cb("topic", uid, str(idx))))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back",
                                      callback_data=_cb("menu", uid))])
    return InlineKeyboardMarkup(rows)


def _insufficient_text(overview: dict) -> str:
    closest = ""
    if overview.get("buckets"):
        most = max(overview["buckets"],
                   key=lambda b: (b["answered"], b["incorrect"]))
        if most["answered"]:
            closest = (
                f"Closest so far: <b>{esc(most['label'])}</b> "
                f"({most['answered']} answered, {most['incorrect']} incorrect).\n\n"
            )
    return _INSUFFICIENT.format(closest=closest)


# ---------------------------------------------------------------------------
# Launch (reuses the existing DM quiz engine)
# ---------------------------------------------------------------------------

async def _launch(
    ctx: ContextTypes.DEFAULT_TYPE, user_id: int,
    topic_index: Optional[int],
) -> str:
    db = get_db()
    service = wp.WeakPracticeService(db)

    from ..state import session_mgr
    if session_mgr.get(user_id) is not None:
        return ("\u26a0\ufe0f A quiz is already active here. Send /stop first, "
                "then start your weak-topic practice again.")

    built = await service.build_practice(user_id, topic_index=topic_index)
    state = built["state"]
    if state == "no_history":
        return _NO_HISTORY
    if state == "insufficient":
        return _INSUFFICIENT.format(closest="")
    if state in ("no_weak", "stale_topic"):
        if state == "stale_topic":
            return "\u2139\ufe0f That weak topic is no longer available. Please reopen /weakquiz."
        return _NO_WEAK
    if state == "no_questions":
        labels = ", ".join(esc(b["label"]) for b in built.get("buckets", []))
        return (f"\u2139\ufe0f <b>{labels or 'That topic'}</b> is currently weak, "
                "but there are no usable questions available from quizzes "
                "you've used. Nothing was fabricated \u2014 play more quizzes "
                "that cover it.")

    questions = built["questions"]
    qid = f"WK{int(time.time() * 1000)}{secrets.token_hex(2)}"
    quiz_obj = {
        "question_set_id": qid,
        "quiz_name": "Weak Topic Practice",
        "questions": questions,
        "timer": WEAK_TIMER_SECONDS,
        "negative_marking": 0,
        "correct_mark": 1,
        # DM play does not shuffle question order (canonical index aligns
        # with the Phase D provenance); option shuffling is display-mapped
        # back to canonical ids at the result boundary.
        "shuffle_options": True,
        "shuffle_options_count": 0,
        "shuffle": False,
        "show_explanation": True,
        "sections": [],
        "promo_message": None,
        "quiz_type": "free",
        "creator_id": user_id,
        "analytics_source": "dm",
        "_weak_session": True,
    }

    from .quiz_play import start_private_quiz
    await start_private_quiz(user_id, ctx, questions, quiz_obj, qid)

    topics = ", ".join(f"<b>{esc(b['label'])}</b>" for b in built["buckets"])
    note = f"Targeting {topics}. "
    if 0 < built["size"] < wp.PRACTICE_SIZE:
        # Honest Case-4 wording: never pad to 10 with fabricated questions.
        labels = " / ".join(esc(b["label"]) for b in built["buckets"])
        note += (
            f"<b>{labels}</b> "
            f"{'are' if len(built['buckets']) > 1 else 'is'} currently weak, "
            f"but I could only find <b>{built['size']}</b> playable "
            f"question{'s' if built['size'] != 1 else ''} from quizzes "
            "you've used.\n"
        )
    if built.get("excluded"):
        note += (f"\n\u26a0\ufe0f {built['excluded']} saved question(s) could "
                 "not be shown (the original quiz and its snapshot were "
                 "unavailable). ")
    return (
        f"\U0001f3af Starting weak-topic practice with <b>{built['size']}</b> "
        f"question{'s' if built['size'] != 1 else ''}. {note}"
        "Your answers update your analytics, mistakes, XP and streak as "
        "usual. Run /weakquiz again anytime \u2014 each session is re-ranked "
        "from your latest performance."
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def weakquiz_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        msg = update.effective_message
        if user is None or msg is None:
            return
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            await msg.reply_text(
                "Please open a private chat with the bot to use /weakquiz.",
                do_quote=False,
            )
            return
        overview = await wp.WeakPracticeService(get_db()).overview(user.id)
        if overview["state"] == "no_history":
            text, kb = _NO_HISTORY, None
        elif overview["state"] == "insufficient":
            text, kb = _insufficient_text(overview), None
        elif overview["state"] == "no_weak":
            text, kb = _NO_WEAK, None
        else:
            text, kb = _menu_text(overview), _menu_keyboard(user.id)
        await safe_send_message(
            ctx, user.id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        logger.exception("weakquiz_command failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await safe_send_message(
                ctx, update.effective_user.id, _LOAD_ERROR)
        except Exception:
            pass


async def _safe_edit(query, text: str,
                     keyboard: Optional[InlineKeyboardMarkup]) -> None:
    # An explicit empty markup removes a stale inline keyboard; passing None
    # to editMessageText would otherwise leave the old buttons in place.
    keyboard = keyboard if keyboard is not None else InlineKeyboardMarkup([])
    try:
        await query.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        try:
            await query.message.reply_html(
                text, reply_markup=keyboard, do_quote=False)
        except Exception:
            pass


async def weakquiz_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parsed = _parse_cb(getattr(query, "data", None))
        user = update.effective_user
        if parsed is None or user is None:
            await query.answer("\u274c Invalid action", show_alert=True)
            return
        action, uid, token = parsed
        if uid != user.id:
            await query.answer("\u274c Not your weak-topic menu", show_alert=True)
            return
        if action not in ("menu", "auto", "topics", "topic"):
            await query.answer("\u274c Invalid action", show_alert=True)
            return

        overview = await wp.WeakPracticeService(get_db()).overview(uid)

        if action == "menu":
            await query.answer()
            if overview["state"] == "no_history":
                await _safe_edit(query, _NO_HISTORY, None)
            elif overview["state"] == "insufficient":
                await _safe_edit(query, _insufficient_text(overview), None)
            elif overview["state"] == "no_weak":
                await _safe_edit(query, _NO_WEAK, None)
            else:
                await _safe_edit(query, _menu_text(overview), _menu_keyboard(uid))
            return

        if action == "topics":
            await query.answer()
            if overview["state"] != "ready":
                if overview["state"] == "no_history":
                    await _safe_edit(query, _NO_HISTORY, None)
                elif overview["state"] == "insufficient":
                    await _safe_edit(query, _insufficient_text(overview), None)
                else:
                    await _safe_edit(query, _NO_WEAK, None)
                return
            await _safe_edit(
                query,
                "\U0001f4da <b>Choose a weak topic</b>\n\n"
                "Topics and values come only from your own answers:",
                _topics_keyboard(uid, overview["eligible"]),
            )
            return

        if action == "topic":
            try:
                idx = int(token)
            except ValueError:
                await query.answer("\u274c Invalid topic", show_alert=True)
                return
            if not (0 <= idx < len(overview["eligible"])):
                await query.answer("\u274c That topic is no longer available",
                                   show_alert=True)
                return
            await query.answer("Building practice\u2026")
            status = await _launch(ctx, uid, idx)
            await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
            return

        # auto
        if overview["state"] != "ready":
            await query.answer()
            if overview["state"] == "no_history":
                await safe_send_message(ctx, uid, _NO_HISTORY,
                                        parse_mode=ParseMode.HTML)
            elif overview["state"] == "no_weak":
                await safe_send_message(ctx, uid, _NO_WEAK,
                                        parse_mode=ParseMode.HTML)
            else:
                await safe_send_message(
                    ctx, uid, _insufficient_text(overview),
                    parse_mode=ParseMode.HTML)
            return
        await query.answer("Building practice\u2026")
        status = await _launch(ctx, uid, None)
        await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("weakquiz_callback failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await query.answer("\u274c Something went wrong. Please try again.",
                               show_alert=True)
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("weakquiz", weakquiz_command))
    application.add_handler(
        CallbackQueryHandler(weakquiz_callback, pattern=r"^wk:"))
