"""Phase H: ``/buildtest`` -- assemble a custom test and play it now.

The wizard is deliberately *stateless*: every screen carries its own state in
the callback data (size, source, difficulty, selected topic indices), and every
value is re-validated server-side against the freshly read bank index before it
is used. Nothing is trusted from the button, and a forged/stale payload is
rejected with a generic alert -- the same discipline as ``/mistakes`` and
``/weakquiz``.

Sources (``scope``) reuse the existing selection engines, so a custom test
never invents a second copy of them:

* ``bank`` / ``unseen`` -- the shared bounded, access-scoped bank reader;
* ``mistakes`` -- Phase D Smart revision selection (fold-back provenance);
* ``due`` -- Phase H SRS due queue;
* ``weak`` -- Phase E weak buckets, applied as a topic filter on the bank.

The assembled test runs through the ordinary DM engine with
``analytics_source="dm"`` and explanations off (test mode), so the result,
analytics, XP and streak all follow the normal path.
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

from quizbot.analytics.question_bank import (
    DEFAULT_SIZE,
    DIFFICULTIES,
    QuestionBankService,
    SCOPE_BANK,
    SCOPE_DUE,
    SCOPE_MISTAKES,
    SCOPE_UNSEEN,
    SCOPE_WEAK,
    SESSION_SIZES,
)
from quizbot.database import get_db

from .. import practice
from ..telegram_utils import esc, safe_send_message

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "bt:"
LOAD_ERROR = "❌ Couldn't read the question bank. Please try again."

DIFFICULTY_CYCLE = ("any",) + DIFFICULTIES
SCOPE_LABELS = {
    SCOPE_BANK: "📚 All bank",
    SCOPE_MISTAKES: "🧠 My mistakes",
    SCOPE_DUE: "⏳ Due revisions",
    SCOPE_WEAK: "🎯 Weak topics",
    SCOPE_UNSEEN: "🔎 Unseen only",
}
_MAX_TOPIC_BUTTONS = 8


def _enc(value: Optional[str], default: str = "-") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


def _cb(*parts) -> str:
    return CALLBACK_PREFIX + ":".join(str(p) for p in parts)


def parse_state(data: Optional[str]) -> Optional[dict]:
    """Parse + validate a callback payload. Returns None for anything that is
    not a well-formed, allow-listed wizard action."""
    try:
        if not data or not data.startswith(CALLBACK_PREFIX):
            return None
        parts = data.split(":")
        action = parts[1]
        uid = int(parts[2])
    except (ValueError, IndexError, AttributeError):
        return None

    def _int(idx: int, default: int) -> int:
        try:
            return int(parts[idx])
        except (ValueError, IndexError):
            return default

    size = _int(3, DEFAULT_SIZE)
    size = size if size in SESSION_SIZES else DEFAULT_SIZE
    scope = parts[4] if len(parts) > 4 and parts[4] in SCOPE_LABELS else SCOPE_BANK
    difficulty = (parts[5] if len(parts) > 5 and parts[5] in DIFFICULTY_CYCLE
                  else "any")
    sel_token = parts[6] if len(parts) > 6 else "-"
    selection: list[int] = []
    if sel_token not in ("", "-"):
        for chunk in sel_token.split(","):
            if chunk.isdigit():
                idx = int(chunk)
                if idx not in selection:
                    selection.append(idx)
    selection = sorted(selection)[:_MAX_TOPIC_BUTTONS]
    toggle = _int(7, -1)

    allowed = {"menu", "s", "sc", "d", "tp", "t", "go"}
    if action not in allowed:
        return None
    return {
        "action": action, "user_id": uid, "size": size, "scope": scope,
        "difficulty": difficulty, "selection": selection, "toggle": toggle,
    }


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

def _dash(value: str, empty: str = "any") -> str:
    return value if value and value != "any" else empty


def menu_text(state: dict, index: dict) -> str:
    topics = index.get("topics") or []
    chosen = [topics[i] for i in state["selection"] if 0 <= i < len(topics)]
    topic_label = ", ".join(esc(t["topic"]) for t in chosen) if chosen else "all topics"
    lines = [
        "🏗 <b>Build a test</b>",
        "",
        f"• Questions: <b>{state['size']}</b>",
        f"• Source: <b>{SCOPE_LABELS[state['scope']]}</b>",
        f"• Difficulty: <b>{_dash(state['difficulty'])}</b>",
        f"• Topics: <b>{topic_label}</b>",
        "",
        f"Bank right now: <b>{index['questions']}</b> playable question(s), "
        f"<b>{index['unseen']}</b> unseen by you.",
    ]
    if index["questions"] == 0:
        lines += ["", "Your bank is empty, so there is nothing to assemble yet. "
                      "Create or play a quiz first."]
    else:
        lines += ["", "Pick a size, source, difficulty (tap to change) and optionally "
                      "narrow the topics, then tap <b>Build &amp; start</b>."]
    return "\n".join(lines)


def menu_keyboard(state: dict, index: dict) -> InlineKeyboardMarkup:
    uid, size = state["user_id"], state["size"]
    scope, difficulty = state["scope"], state["difficulty"]
    sel = ",".join(str(i) for i in state["selection"]) or "-"
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(
            f"{'✅ ' if size == s else ''}{s}",
            callback_data=_cb("s", uid, s, scope, difficulty, sel))
        for s in SESSION_SIZES
    ])
    for scope_key in (SCOPE_BANK, SCOPE_MISTAKES, SCOPE_DUE, SCOPE_WEAK, SCOPE_UNSEEN):
        rows.append([InlineKeyboardButton(
            f"{'✅ ' if scope == scope_key else ''}{SCOPE_LABELS[scope_key]}",
            callback_data=_cb("sc", uid, size, scope_key, difficulty, sel))])
    next_difficulty = DIFFICULTY_CYCLE[
        (DIFFICULTY_CYCLE.index(difficulty) + 1) % len(DIFFICULTY_CYCLE)]
    rows.append([InlineKeyboardButton(
        f"🎚 Difficulty: {_dash(difficulty)}", 
        callback_data=_cb("d", uid, size, scope, next_difficulty, sel))])
    if index.get("topics"):
        label = ("🏷 Topics: " + str(len(state["selection"])) + " selected"
                 if state["selection"] else "🏷 Topics: all")
        rows.append([InlineKeyboardButton(
            label, callback_data=_cb("tp", uid, size, scope, difficulty, sel))])
    if index["questions"]:
        rows.append([InlineKeyboardButton(
            "▶️ Build & start",
            callback_data=_cb("go", uid, size, scope, difficulty, sel))])
    return InlineKeyboardMarkup(rows)


def topics_text(index: dict, state: dict) -> str:
    lines = [
        "🏷 <b>Filter by topic</b>",
        "",
        "Topics come from your own bank (never a hard-coded syllabus). Leave "
        "everything unselected to practise across all of them.",
        "",
    ]
    for i, topic in enumerate((index.get("topics") or [])[:_MAX_TOPIC_BUTTONS]):
        mark = "✅" if i in state["selection"] else "⬜"
        subject = topic.get("subject") or "—"
        lines.append(f"{mark} {esc(topic['topic'])} ({topic['count']}) · "
                     f"<i>{esc(subject)}</i>")
    return "\n".join(lines)


def topics_keyboard(index: dict, state: dict) -> InlineKeyboardMarkup:
    uid, size = state["user_id"], state["size"]
    scope, difficulty = state["scope"], state["difficulty"]
    sel = ",".join(str(i) for i in state["selection"])
    rows: list[list[InlineKeyboardButton]] = []
    for i, topic in enumerate((index.get("topics") or [])[:_MAX_TOPIC_BUTTONS]):
        mark = "✅" if i in state["selection"] else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {topic['topic'][:28]} ({topic['count']})",
            callback_data=_cb("t", uid, size, scope, difficulty, _enc(sel), i))])
    rows.append([InlineKeyboardButton(
        "⬅️ Done", callback_data=_cb("menu", uid, size, scope, difficulty, _enc(sel)))])
    rows.append([InlineKeyboardButton(
        "🗑 Clear topics",
        callback_data=_cb("menu", uid, size, scope, difficulty, "-"))])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

async def build_and_start(ctx: ContextTypes.DEFAULT_TYPE, state: dict,
                          index: dict) -> str:
    """Assemble from the (re-validated) state and start the session."""
    if practice.busy(state["user_id"]):
        return practice.BUSY_TEXT
    topics = [
        (index.get("topics") or [])[i]["topic"]
        for i in state["selection"]
        if 0 <= i < len(index.get("topics") or [])
    ]
    spec = {
        "size": state["size"],
        "scope": state["scope"],
        "topics": topics,
        "difficulty": None if state["difficulty"] == "any" else state["difficulty"],
        "seed": (f"{state['user_id']}:{state['scope']}:{state['size']}:"
                 f"{','.join(sorted(topics))}:{state['difficulty']}"),
    }
    built = await QuestionBankService(get_db()).build_test(
        state["user_id"], spec)
    if built["size"] == 0:
        reasons = {
            "no_mistakes": "You have no saved mistakes yet, so that source is "
                           "empty. Try the full bank.",
            "nothing_due": "Nothing is due for revision right now. Try the "
                           "full bank or wait for cards to fall due.",
            "no_bank": "Your bank is empty — create or play a quiz first.",
            "no_match": "No question matched those filters. Loosen the topic, "
                        "difficulty or unseen-only filter and try again.",
        }
        return "ℹ️ " + reasons.get(built["reason"] or "no_match",
                                   reasons["no_match"])
    started, _ = await practice.launch(
        ctx, state["user_id"], built["questions"],
        quiz_name=f"Custom test ({built['size']}Q)",
        show_explanation=False,
        origins_by_index=built["origins_by_index"], marker="_buildtest_session",
    )
    if not started:
        return "❌ Could not start the test. Please try again."
    label = SCOPE_LABELS[state["scope"]]
    note = ""
    if built["size"] < state["size"]:
        note = (f"\nℹ️ Only <b>{built['size']}</b> question(s) matched, so the "
                "test is shorter — nothing was padded or repeated.")
    return (
        f"🏗 Starting your custom test: <b>{built['size']}</b> question"
        f"{'s' if built['size'] != 1 else ''} · {label}"
        f"{' · ' + ', '.join(esc(t) for t in topics) if topics else ''}.{note}\n"
        "Explanations are hidden during the test — check /result afterwards. "
        "Missed questions become SRS cards for /revise, and XP/streak apply "
        "as usual."
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def buildtest_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/buildtest [size|scope]`` -- personal test assembly (private chats)."""
    try:
        user = update.effective_user
        msg = update.effective_message
        if user is None or msg is None:
            return
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            await msg.reply_text(
                "🔒 Custom tests are personal. Open a private chat with me and "
                "send /buildtest there.",
                do_quote=False,
            )
            return
        state = {"action": "menu", "user_id": user.id, "size": DEFAULT_SIZE,
                 "scope": SCOPE_BANK, "difficulty": "any", "selection": [],
                 "toggle": -1}
        args = [a.strip().lower() for a in (getattr(ctx, "args", None) or []) if a.strip()]
        for arg in args:            # `/buildtest 30 weak` sets both
            if arg.isdigit() and int(arg) in SESSION_SIZES:
                state["size"] = int(arg)
            elif arg in SCOPE_LABELS:
                state["scope"] = arg
        index = await QuestionBankService(get_db()).index(user.id)
        await safe_send_message(
            ctx, user.id, menu_text(state, index), parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(state, index),
        )
    except Exception:
        logger.exception("buildtest_command failed for %s",
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


async def buildtest_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        state = parse_state(getattr(query, "data", None))
        user = update.effective_user
        if state is None or user is None:
            await query.answer("❌ Invalid action", show_alert=True)
            return
        if state["user_id"] != user.id:
            await query.answer("❌ Not your test builder", show_alert=True)
            return
        index = await QuestionBankService(get_db()).index(user.id)

        if state["action"] == "tp":
            await query.answer()
            await _safe_edit(query, topics_text(index, state),
                             topics_keyboard(index, state))
            return
        if state["action"] == "t":
            topics = index.get("topics") or []
            if not (0 <= state["toggle"] < len(topics)):
                await query.answer("❌ That topic is no longer available",
                                   show_alert=True)
                return
            selection = set(state["selection"])
            selection.symmetric_difference_update({state["toggle"]})
            state["selection"] = sorted(selection)[:_MAX_TOPIC_BUTTONS]
            await query.answer()
            await _safe_edit(query, topics_text(index, state),
                             topics_keyboard(index, state))
            return
        if state["action"] == "go":
            await query.answer("Building your test…")
            status = await build_and_start(ctx, state, index)
            await safe_send_message(ctx, user.id, status, parse_mode=ParseMode.HTML)
            return
        # menu / s / sc / d -- re-render the builder with the new selection
        await query.answer()
        await _safe_edit(query, menu_text(state, index),
                         menu_keyboard(state, index))
    except Exception:
        logger.exception("buildtest_callback failed for %s",
                         getattr(update.effective_user, "id", "?"))
        try:
            await query.answer("❌ Something went wrong. Please try again.",
                               show_alert=True)
        except Exception:
            pass


def register(application: Application) -> None:
    application.add_handler(CommandHandler("buildtest", buildtest_command))
    application.add_handler(
        CallbackQueryHandler(buildtest_callback, pattern=r"^bt:"))
    logger.info("Registered commands: /buildtest (custom test builder)")
