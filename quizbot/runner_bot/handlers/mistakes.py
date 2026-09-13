"""Phase D: ``/mistakes`` mistake-revision command (private DM feature).

Reuses the Phase B non-destructive mistake history + content snapshots and the
existing private/DM quiz engine -- there is no new quiz engine, analytics
pipeline or XP path. The flow is:

1. ``/mistakes`` shows a user-scoped menu (Smart / Repeated / All / By topic),
   every button carrying the authenticated user id and only modes that have
   real data.
2. Selecting a practice mode builds a bounded (~10) revision set from
   :class:`quizbot.analytics.mistake_revision.MistakeRevisionService` and
   launches it through the ordinary ad-hoc DM quiz machinery
   (``start_private_quiz``). Completion flows through the single canonical
   ``AnalyticsService.record_completion(source="dm")`` boundary, so Phase C
   XP/streaks apply once and revision answers fold back to origin mistakes.

Security: every DB read is scoped by ``from_user.id``; every callback embeds
that id and validates it server-side, allow-lists the mode, and re-derives a
topic index before use. Malformed/stale/forged callbacks fail with a generic
safe alert and never disclose whether another user's data exists.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from quizbot.analytics import mistake_revision as mr
from quizbot.database import get_db

from ..telegram_utils import esc, safe_send_message

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "mst:"
REVISION_TIMER_SECONDS = 45
_QUESTION_PREVIEW = 160   # bounded text preview per All-mode row


# ---------------------------------------------------------------------------
# Callback encoding (compact; values re-validated server-side on arrival)
# ---------------------------------------------------------------------------

def _cb(action: str, user_id: int, token: str = "") -> str:
    return f"{CALLBACK_PREFIX}{action}:{user_id}" + (f":{token}" if token else "")


def _parse_cb(data: Optional[str]) -> Optional[tuple[str, int, str]]:
    """Return ``(action, uid, token)`` or None for any malformed payload."""
    try:
        if not data or not data.startswith(CALLBACK_PREFIX):
            return None
        parts = data.split(":")
        # mst : action : uid [: token]
        if len(parts) < 3:
            return None
        action = parts[1]
        uid = int(parts[2])
        token = parts[3] if len(parts) > 3 else ""
    except (ValueError, IndexError, AttributeError):
        return None
    return action, uid, token


# ---------------------------------------------------------------------------
# Menus / rendering
# ---------------------------------------------------------------------------

def _menu_keyboard(ov: dict) -> InlineKeyboardMarkup:
    uid = ov["user_id"]
    rows: list[list[InlineKeyboardButton]] = []
    if ov["open_groups"]:
        rows.append([
            InlineKeyboardButton("\U0001F9E0 Smart revision",
                                 callback_data=_cb("smart", uid)),
            InlineKeyboardButton(f"\U0001F501 Repeated ({len(ov['repeated_groups'])})",
                                 callback_data=_cb("rep", uid))
            if ov["repeated_groups"] else
            InlineKeyboardButton("\U0001F501 Repeated (0)",
                                 callback_data=_cb("repnone", uid)),
        ])
    if ov["groups"]:
        rows.append([InlineKeyboardButton(
            f"\U0001F4DA All mistakes ({ov['content_groups']})",
            callback_data=_cb("all", uid, "0"))])
    if ov["topics"]:
        rows.append([InlineKeyboardButton(
            f"\U0001F3F7\ufe0f By topic ({len(ov['topics'])})",
            callback_data=_cb("topics", uid))])
    return InlineKeyboardMarkup(rows)


def _menu_text(ov: dict) -> str:
    if ov["total_mistakes"] == 0:
        return (
            "\U0001F4DD <b>Mistake revision</b>\n\n"
            "You don't have any recorded mistakes yet. Play saved quizzes "
            "(in a group or DM) and any questions you answer incorrectly are "
            "saved here automatically, with the question text preserved even "
            "if the original quiz is later edited or deleted.\n\n"
            "Come back here with /mistakes to practise them."
        )
    lines = [
        "\U0001F4DD <b>Mistake revision</b>\n",
        f"\u2022 Open questions: <b>{ov['open_mistakes']}</b>",
        f"\u2022 Resolved: <b>{ov['resolved_mistakes']}</b>",
        f"\u2022 Distinct questions to revise: <b>{ov['content_groups']}</b>",
        "",
    ]
    if not ov["repeated_groups"] and ov["open_groups"]:
        lines.append(
            "\u2139\ufe0f Repeated-mistake practice unlocks once you miss the "
            "same question at least twice (or miss it again after correcting "
            "it)."
        )
    lines.append("Choose how you want to practise:")
    return "\n".join(lines)


def _topics_keyboard(uid: int, topics: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Two topics per row, index is a server-re-derived token, never the label.
    pair: list[InlineKeyboardButton] = []
    for idx, t in enumerate(topics):
        label = f"{t['topic'][:24]} ({t['open_count']})"
        pair.append(InlineKeyboardButton(label, callback_data=_cb("topic", uid, str(idx))))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=_cb("menu", uid))])
    return InlineKeyboardMarkup(rows)


def _all_keyboard(uid: int, page: int, pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u2b05\ufe0f Prev",
                                        callback_data=_cb("all", uid, str(page - 1))))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next \u27a1\ufe0f",
                                        callback_data=_cb("all", uid, str(page + 1))))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("\U0001F3AF Practise this page",
                                      callback_data=_cb("allgo", uid, str(page)))])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data=_cb("menu", uid))])
    return InlineKeyboardMarkup(rows)


def _all_text(items: list[dict], page: int, pages: int) -> str:
    head = f"\U0001F4DA <b>All mistakes</b> \u2014 page {page + 1}/{max(1, pages)}\n"
    body: list[str] = []
    for i, it in enumerate(items, start=1):
        text = it.get("question") or "\u2753 Question text unavailable for this legacy entry"
        text = text.replace("\n", " ").strip()
        if len(text) > _QUESTION_PREVIEW:
            text = text[:_QUESTION_PREVIEW] + "\u2026"
        status = "\u274c open" if it.get("open") else "\u2705 resolved"
        repeat = f"\u00d7{it.get('wrong', 0)}"
        topic = f" \u00b7 {esc(it['topic'])}" if it.get("topic") else ""
        body.append(f"{i}. {esc(text)}\n   <i>{status}{topic} \u00b7 wrong {repeat}</i>")
    return head + "\n".join(body) if body else head + "Nothing on this page."


# ---------------------------------------------------------------------------
# Revision launch
# ---------------------------------------------------------------------------

async def _launch(
    ctx: ContextTypes.DEFAULT_TYPE, user_id: int, mode: str,
    *, topic: Optional[str] = None, page: Optional[int] = None,
) -> str:
    """Build + start a bounded revision session via the DM engine. Returns a
    short status string the caller can surface."""
    db = get_db()
    revision = mr.MistakeRevisionService(db)

    # A single quiz session may be active in this chat; don't clobber it.
    from ..state import session_mgr
    if session_mgr.get(user_id) is not None:
        return ("\u26a0\ufe0f A quiz is already active here. Send /stop first, "
                "then start your revision again.")

    built = await revision.build_revision(
        user_id, mode, topic=topic, size=mr.REVISION_SIZE, page=page
    )
    if built["size"] == 0:
        if mode == mr.MODE_REPEATED:
            return ("\u2139\ufe0f Not enough repeated mistakes yet. Repeated "
                    "practice needs questions missed at least twice (or missed "
                    "again after a correct answer). Try Smart revision.")
        if mode == mr.MODE_TOPIC:
            return "\u2139\ufe0f No open questions for that topic right now."
        return ("\u2139\ufe0f You don't have any open questions to revise right "
                "now. New mistakes appear here as you play.")

    questions = built["questions"]
    # Hidden per-question provenance (canonical index aligned). The play
    # engine ignores the private key; it survives option shuffling and is read
    # back at the canonical analytics boundary to fold results to origin rows.
    for q, origins in zip(questions, built["origins_by_index"]):
        q["_revision_origins"] = origins

    qid = f"RV{int(time.time() * 1000)}{secrets.token_hex(2)}"
    quiz_obj = {
        "question_set_id": qid,
        "quiz_name": "Mistake Revision",
        "questions": questions,
        "timer": REVISION_TIMER_SECONDS,
        "negative_marking": 0,
        "correct_mark": 1,
        # Option shuffling is fine: the engine records display_order and the
        # canonical analytics path maps answers back. Question order is not
        # shuffled in DM play, so canonical index == origins list index.
        "shuffle_options": True,
        "shuffle_options_count": 0,
        "shuffle": False,
        "show_explanation": True,
        "sections": [],
        "promo_message": None,
        "quiz_type": "free",
        "creator_id": user_id,
        # Revision is an ad-hoc private quiz: analytics source dm -> Phase C
        # XP/streaks run once through the normal eligible path.
        "analytics_source": "dm",
        "_revision_session": True,
    }

    # Lazy import keeps this module light and avoids import cycles; mirrors
    # the setup-wizard's ad-hoc DM launch.
    from .quiz_play import start_private_quiz
    await start_private_quiz(user_id, ctx, questions, quiz_obj, qid)
    scope = f" ({topic})" if topic else ""
    return (f"\U0001F4DD Starting <b>{mode}</b> revision{scope} with "
            f"<b>{built['size']}</b> question"
            f"{'s' if built['size'] != 1 else ''}. Answer them to update your "
            f"mistake history \u2014 XP and streak apply as usual."
            + (f"\n\u26a0\ufe0f {built['excluded']} saved question(s) could not "
               f"be shown (the original quiz and its snapshot were unavailable)."
               if built["excluded"] else ""))


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def mistakes_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/mistakes`` entry point. Mistake history is personal: revision only
    runs in the user's private DM with the bot."""
    try:
        user = update.effective_user
        msg = update.effective_message
        if user is None or msg is None:
            return
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            await msg.reply_text(
                "\U0001F512 Mistake revision is personal. Open a private chat "
                "with me and send /mistakes there.",
                do_quote=False,
            )
            return
        ov = await mr.MistakeRevisionService(get_db()).overview(user.id)
        await safe_send_message(
            ctx, user.id, _menu_text(ov), parse_mode=ParseMode.HTML,
            reply_markup=_menu_keyboard(ov),
        )
    except Exception:
        logger.exception("mistakes_command failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await safe_send_message(
                ctx, update.effective_user.id,
                "\u274c Couldn't load your mistake history. Please try again.")
        except Exception:
            pass


async def _safe_edit(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """edit_text with HTML; fall back to a fresh message if editing fails
    (e.g. identical content / message too old)."""
    try:
        await query.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        try:
            await query.message.reply_html(
                text, reply_markup=keyboard, do_quote=False)
        except Exception:
            pass


async def mistakes_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parsed = _parse_cb(getattr(query, "data", None))
        user = update.effective_user
        if parsed is None or user is None:
            await query.answer("\u274c Invalid action", show_alert=True)
            return
        action, uid, token = parsed

        # Forgeable callback_data: the embedded owner must equal the caller.
        # Never disclose whether uid exists elsewhere.
        if uid != user.id:
            await query.answer("\u274c Not your revision menu", show_alert=True)
            return

        # Re-fetch authoritative state for every action (no trust in buttons).
        service = mr.MistakeRevisionService(get_db())
        ov = await service.overview(uid)

        # --- navigation / read-only views ---------------------------------
        if action == "menu":
            await query.answer()
            await _safe_edit(query, _menu_text(ov), _menu_keyboard(ov))
            return

        if action == "repnone":
            await query.answer(
                "Repeated practice unlocks after missing the same question twice.",
                show_alert=True)
            return

        if action == "topics":
            await query.answer()
            if not ov["topics"]:
                await _safe_edit(query, _menu_text(ov), _menu_keyboard(ov))
                return
            text = ("\U0001F3F7\ufe0f <b>Revise by topic</b>\n\n"
                    "Topics are taken only from your own saved mistakes. Pick "
                    "one:")
            await _safe_edit(query, text, _topics_keyboard(uid, ov["topics"]))
            return

        if action == "topic":
            # Re-derive the topic from the stored list using the index token;
            # a forged/stale index is simply rejected.
            try:
                idx = int(token)
            except ValueError:
                await query.answer("\u274c Invalid topic", show_alert=True)
                return
            topics = await service.topics(uid)
            if not (0 <= idx < len(topics)):
                await query.answer("\u274c That topic is no longer available",
                                   show_alert=True)
                return
            topic = topics[idx]["topic"]
            await query.answer("Building revision\u2026")
            status = await _launch(ctx, uid, mr.MODE_TOPIC, topic=topic)
            await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
            return

        if action in ("all", "allgo"):
            try:
                page = max(0, int(token)) if token else 0
            except ValueError:
                await query.answer("\u274c Invalid page", show_alert=True)
                return
            if action == "all":
                browse = await service.browse_all(uid, page)
                if browse["total"] == 0:
                    await query.answer()
                    await _safe_edit(query, _menu_text(ov), _menu_keyboard(ov))
                    return
                await query.answer()
                await _safe_edit(query,
                           _all_text(browse["items"], page, browse["pages"]),
                           _all_keyboard(uid, page, browse["pages"]))
                return
            # Practise exactly the page the user is viewing (bounded).
            await query.answer("Building revision\u2026")
            status = await _launch(ctx, uid, mr.MODE_ALL, page=page)
            await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
            return

        # --- practice launches --------------------------------------------
        mode_map = {"smart": mr.MODE_SMART, "rep": mr.MODE_REPEATED}
        if action in mode_map:
            await query.answer("Building revision\u2026")
            status = await _launch(ctx, uid, mode_map[action])
            await safe_send_message(ctx, uid, status, parse_mode=ParseMode.HTML)
            return

        # Unknown action inside a well-formed payload.
        await query.answer("\u274c Invalid action", show_alert=True)
    except Exception:
        logger.exception("mistakes_callback failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await query.answer("\u274c Something went wrong. Please try again.",
                               show_alert=True)
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("mistakes", mistakes_command))
    application.add_handler(
        CallbackQueryHandler(mistakes_callback, pattern=r"^mst:")
    )
