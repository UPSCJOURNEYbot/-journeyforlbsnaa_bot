"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from quizbot.shared import config
from quizbot.shared.utils import is_premium_user

from ..ai_providers import AIQUIZ_LANG_UI, generate_in_chunks, get_provider_keys
from ..state import AI_QUIZ_INFLIGHT, AI_QUIZ_SESSIONS, last_working_ai, session_mgr, tasks
from ..telegram_utils import esc, safe_send_message

logger = logging.getLogger(__name__)

# Safety valve for AI_QUIZ_INFLIGHT: generation is advertised at 30-120s, so
# 15 minutes is far beyond any legitimate run. Past that the entry is treated
# as stale and the user is allowed to retry rather than being locked out for
# the lifetime of the process.
INFLIGHT_TTL = 900.0

# Single source of truth for the difficulty / exam-style buttons: the labels
# rendered back to the user and the only callback values accepted for them.
DIFF_LABELS = {"moderate": "\U0001F7E1 Moderate", "hard": "\U0001F534 Hard", "extreme": "\U0001F480 Extreme"}
EXAM_LABELS = {"ssc": "\U0001F4CB SSC", "civil": "\U0001F3DB Civil", "oneway": "\U0001F4DD One Day"}

# The exact callback-data values each button row emits (as strings, matching
# what `_kb()` builds). They double as the allow-list in `aiquiz_callback`, so
# a forged callback_query can neither store junk in the session nor reach
# `int()`/`float()` and die silently in the catch-all handler.
TIMER_CHOICES = ("10", "15", "20", "25", "30", "45")
NEG_CHOICES = ("0", "0.25", "0.333", "0.5", "1.0")
CM_CHOICES = ("1", "2", "3", "4")
SHUFFLE_COUNT_CHOICES = ("2", "4", "all")

_STEP_VALUES = {
    "lang": frozenset(AIQUIZ_LANG_UI),
    "lang2": frozenset(AIQUIZ_LANG_UI),
    "bilingual": frozenset({"yes", "no"}),
    "diff": frozenset(DIFF_LABELS),
    "exam": frozenset(EXAM_LABELS),
    "timer": frozenset(TIMER_CHOICES),
    "neg": frozenset(NEG_CHOICES),
    "cm": frozenset(CM_CHOICES),
    "shuffle": frozenset({"q", "o", "b", "none"}),
    "shufflecount": frozenset(SHUFFLE_COUNT_CHOICES),
    "ex": frozenset({"yes", "no"}),
}


def _claim_generation(uid: int) -> bool:
    """Reserve the per-user generation slot. Returns False when this user
    already has a generation in flight.

    Contains no `await`, so under a single event loop (and PTB's
    `concurrent_updates=True`) the check-and-set is atomic -- two callbacks
    arriving together can never both claim the slot.
    """
    started = AI_QUIZ_INFLIGHT.get(uid)
    if started is not None:
        age = time.monotonic() - started
        if age < INFLIGHT_TTL:
            return False
        logger.warning("Stale /aiquiz in-flight entry for %s (%.0fs old) -- clearing it", uid, age)
    AI_QUIZ_INFLIGHT[uid] = time.monotonic()
    return True


def _release_generation(uid: int) -> None:
    AI_QUIZ_INFLIGHT.pop(uid, None)


def _retry_kb(uid: int) -> InlineKeyboardMarkup:
    """Retry / Cancel keyboard shown after a generation failure, so the user
    can try again without re-typing the whole wizard."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F504 Retry", callback_data=f"aiq_retry_{uid}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"aiq_cancel_{uid}"),
    ]])


def _kb(step: str, uid: int) -> InlineKeyboardMarkup:
    def p(s: str, v: Any) -> str:
        return f"aiq_{s}_{uid}_{v}"

    if step == "shufflecount":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("First 2", callback_data=p("shufflecount", 2)),
             InlineKeyboardButton("First 4", callback_data=p("shufflecount", 4))],
            [InlineKeyboardButton("All", callback_data=p("shufflecount", "all"))],
        ])
    if step == "count":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(str(n), callback_data=p("count", n)) for n in (5, 10, 15, 20)],
            [InlineKeyboardButton(str(n), callback_data=p("count", n)) for n in (25, 30, 40, 50)],
            [InlineKeyboardButton(str(n), callback_data=p("count", n)) for n in (60, 75, 100)],
        ])
    if step == "lang":
        items = list(AIQUIZ_LANG_UI.items())
        rows = [
            [InlineKeyboardButton(label, callback_data=p("lang", code)) for code, label in items[i:i + 2]]
            for i in range(0, len(items), 2)
        ]
        return InlineKeyboardMarkup(rows)
    if step == "bilingual":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes — Bilingual", callback_data=p("bilingual", "yes")),
             InlineKeyboardButton("❌ No — Single Lang", callback_data=p("bilingual", "no"))],
        ])
    if step == "lang2":
        sess = AI_QUIZ_SESSIONS.get(uid, {})
        lang1 = sess.get("lang", "en")
        items = [(c, l) for c, l in AIQUIZ_LANG_UI.items() if c != lang1]
        rows = [
            [InlineKeyboardButton(label, callback_data=p("lang2", code)) for code, label in items[i:i + 2]]
            for i in range(0, len(items), 2)
        ]
        return InlineKeyboardMarkup(rows)
    if step == "diff":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001F7E1 Moderate", callback_data=p("diff", "moderate"))],
            [InlineKeyboardButton("\U0001F534 Hard", callback_data=p("diff", "hard"))],
            [InlineKeyboardButton("\U0001F480 Extreme Hard", callback_data=p("diff", "extreme"))],
        ])
    if step == "exam":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001F4CB SSC Type", callback_data=p("exam", "ssc"))],
            [InlineKeyboardButton("\U0001F3DB Civil Services", callback_data=p("exam", "civil"))],
            [InlineKeyboardButton("\U0001F4DD Common One Day", callback_data=p("exam", "oneway"))],
        ])
    if step == "timer":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{t}s", callback_data=p("timer", t)) for t in (10, 15, 20)],
            [InlineKeyboardButton(f"{t}s", callback_data=p("timer", t)) for t in (25, 30, 45)],
        ])
    if step == "neg":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("None", callback_data=p("neg", "0")),
             InlineKeyboardButton("1/4", callback_data=p("neg", "0.25")),
             InlineKeyboardButton("1/3", callback_data=p("neg", "0.333"))],
            [InlineKeyboardButton("1/2", callback_data=p("neg", "0.5")),
             InlineKeyboardButton("1", callback_data=p("neg", "1.0"))],
        ])
    if step == "cm":
        return InlineKeyboardMarkup([[InlineKeyboardButton(str(n), callback_data=p("cm", n)) for n in (1, 2, 3, 4)]])
    if step == "shuffle":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001F500 Questions", callback_data=p("shuffle", "q")),
             InlineKeyboardButton("\U0001F500 Options", callback_data=p("shuffle", "o")),
             InlineKeyboardButton("\U0001F500 Both", callback_data=p("shuffle", "b"))],
            [InlineKeyboardButton("⏭ No Shuffle", callback_data=p("shuffle", "none"))],
        ])
    return InlineKeyboardMarkup([])


async def _edit(msg, text: str, kb: InlineKeyboardMarkup = None) -> None:
    """Edit the wizard message in place.

    Text reaching here is already escaped with `esc()`, but if Telegram still
    rejects the HTML the step is re-sent as plain text: a wizard that fails to
    re-render leaves the user staring at the previous step while the session
    underneath has already moved on.
    """
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except BadRequest as e:
        if "parse entities" not in str(e).lower():
            return
        logger.warning("aiquiz wizard edit rejected as HTML (%s) -- retrying as plain text", e)
        try:
            await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
    except Exception:
        pass


async def _launch_ai_quiz(
    uid: int, ctx: ContextTypes.DEFAULT_TYPE, questions: list[dict], topic: str,
    timer: int, neg: float, cm: int, shuffle_q: bool = False, shuffle_o: bool = False,
    shuffle_o_count: int = 0, chat_id: int = None, chat_type: str = "private",
    update: Any = None, show_explanation: bool = False,
    analytics_source: str = "aiquiz", analytics_topic: Optional[str] = None,
    analytics_difficulty: Optional[str] = None,
) -> None:
    """Launch a live-generated quiz in the chat the command was issued from."""
    from quizbot.analytics.metadata import attach_question_analytics

    from .quiz_play import run_group_quiz, start_private_quiz

    chat_id = chat_id or uid
    is_private = str(chat_type) in ("private", "ChatType.PRIVATE")

    # Phase B: attach only metadata genuinely collected by the wizard (the
    # declared topic and the chosen moderate/hard/extreme difficulty). PDF
    # quizzes have no declared topic, so none is fabricated.
    attach_question_analytics(
        questions,
        topic=analytics_topic,
        difficulty=analytics_difficulty,
    )

    quiz_obj = {
        "question_set_id": f"AI{int(time.time())}", "quiz_name": f"AI: {topic[:50]}",
        "questions": questions, "timer": timer, "negative_marking": neg, "correct_mark": cm,
        "shuffle": shuffle_q, "shuffle_options": shuffle_o, "shuffle_options_count": shuffle_o_count,
        "show_explanation": show_explanation, "sections": [], "promo_message": None,
        "quiz_type": "free", "creator_id": uid,
        # Provenance for the analytics layer (no stored quiz row exists).
        "analytics_source": analytics_source,
    }

    if session_mgr.get(chat_id):
        await safe_send_message(ctx, chat_id, "⚠️ A quiz is already running. /stop it first.")
        return

    if is_private:
        await start_private_quiz(chat_id, ctx, questions, quiz_obj, quiz_obj["question_set_id"])
        return

    # Capture the question permutation for canonical analytics, mirroring the
    # saved-quiz setup wizard. quiz_obj keeps the CANONICAL question order
    # (used for snapshots/metadata at the boundary); only the play list is
    # reordered, with question_order[display_pos] = canonical_index.
    question_order = None
    play_questions = questions
    if shuffle_q and not quiz_obj.get("sections"):
        question_order = list(range(len(questions)))
        random.shuffle(question_order)
        play_questions = [questions[i] for i in question_order]

    session_data = {
        "quiz_id": quiz_obj["question_set_id"], "quiz_data": quiz_obj, "current_index": 0,
        "paused": False, "polls": {}, "participants": {}, "is_private": False,
        "section_msgs": [], "modified_timer_offset": 0,
        "question_order": question_order,
    }
    await session_mgr.create(chat_id, session_data)

    class _FakeMsg:
        def __init__(self, cid: int) -> None:
            self.chat_id = cid
            self.chat = type("C", (), {"id": cid, "type": "group"})()

    class _FakeUpdate:
        def __init__(self, cid: int) -> None:
            self.message = _FakeMsg(cid)
            self._chat_id = cid

    tasks.spawn(
        run_group_quiz(chat_id, ctx, play_questions, quiz_obj, False, _FakeUpdate(chat_id), 0),
        name=f"quiz_{chat_id}_ai",
    )


async def aiquiz_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/aiquiz <topic>` -- starts the wizard."""
    chat_id = update.message.chat_id
    try:
        user_id = update.message.from_user.id
        if not await is_premium_user(user_id):
            await safe_send_message(ctx, chat_id, "❌ Premium required.")
            return
        if not ctx.args:
            await safe_send_message(
                ctx, chat_id,
                "\U0001F916 <b>AI Quiz Generator</b>\n\n"
                "Usage: <code>/aiquiz &lt;topic&gt;</code>\n"
                "Example: <code>/aiquiz Indian History</code>\n\n"
                "No DB storage — questions generated live, quiz starts immediately!\n"
                "Add your own AI key via <code>/setkey gemini AIzaSy...</code> for faster, higher-quality results.\n"
                "Without a key, a free fallback provider is used automatically.",
                parse_mode=ParseMode.HTML,
            )
            return

        topic = " ".join(ctx.args).strip()
        chat_type = update.message.chat.type
        AI_QUIZ_SESSIONS[user_id] = {"topic": topic, "step": "count", "chat_id": chat_id, "chat_type": chat_type, "user_id": user_id}
        msg = await safe_send_message(
            ctx, chat_id, f"\U0001F916 <b>AI Quiz: {esc(topic)}</b>\n\nHow many questions?",
            parse_mode=ParseMode.HTML, reply_markup=_kb("count", user_id),
        )
        if msg:
            AI_QUIZ_SESSIONS[user_id]["msg_id"] = msg.message_id
        else:
            # The prompt never reached the user (kicked from the group, flood
            # limit, ...) -- don't leave a wizard session behind that can only
            # be advanced by buttons nobody was ever shown.
            AI_QUIZ_SESSIONS.pop(user_id, None)
    except Exception as e:
        logger.error("aiquiz_command error: %s", e, exc_info=True)
        try:
            await safe_send_message(ctx, chat_id, "❌ Could not start the AI quiz. Please try again.")
        except Exception:
            pass


async def aiquiz_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all `aiq_`-prefixed callbacks."""
    try:
        query = update.callback_query
        await query.answer()
        data = query.data
        parts = data.split("_", 3)
        if len(parts) < 3:
            return
        step, uid_s = parts[1], parts[2]
        value = parts[3] if len(parts) > 3 else None
        uid = int(uid_s)

        if query.from_user.id != uid:
            await query.answer("❌ Not your session", show_alert=True)
            return

        # callback_data is forgeable -- any client can post an arbitrary
        # callback_query for a message it can see -- so every step's value is
        # checked against the exact set its own buttons emit before it is
        # stored or echoed back into an HTML message.
        allowed = _STEP_VALUES.get(step)
        if allowed is not None and value not in allowed:
            await query.answer("❌ Invalid choice", show_alert=True)
            return

        sess = AI_QUIZ_SESSIONS.get(uid)
        if not sess:
            _release_generation(uid)
            await query.message.edit_text("❌ Session expired. /aiquiz again")
            return

        msg = query.message
        # The topic is free text typed after /aiquiz and is echoed into every
        # HTML step below; unescaped, a single "<" makes Telegram reject the
        # whole message and the wizard silently freezes on the previous step.
        t = esc(sess["topic"])

        if step == "count":
            try:
                count_val = int(value)
            except (TypeError, ValueError):
                await query.answer("❌ Invalid question count", show_alert=True)
                return
            if not (1 <= count_val <= 100):
                await query.answer("❌ Choose between 1–100 questions", show_alert=True)
                return
            sess["count"] = count_val
            await _edit(msg, f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB Questions: {count_val}\n\nLanguage?", _kb("lang", uid))

        elif step == "lang":
            sess["lang"] = value
            lbl = esc(AIQUIZ_LANG_UI.get(value, value))
            await _edit(
                msg,
                f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB {sess['count']} | \U0001F310 {lbl}\n\n"
                f"\U0001F310 <b>Bilingual mode?</b>\nShow questions in 2 languages (lang1 / lang2 format)",
                _kb("bilingual", uid),
            )

        elif step == "bilingual":
            if value == "yes":
                sess["bilingual"] = True
                lbl = esc(AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "English"))
                await _edit(
                    msg, f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB {sess['count']} | \U0001F310 {lbl} + ?\n\n\U0001F310 <b>Select 2nd language:</b>",
                    _kb("lang2", uid),
                )
            else:
                sess["bilingual"] = False
                sess["lang2"] = None
                lbl = esc(AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "English"))
                await _edit(msg, f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB {sess['count']} | \U0001F310 {lbl}\n\nDifficulty?", _kb("diff", uid))

        elif step == "lang2":
            sess["lang2"] = value
            l1 = esc(AIQUIZ_LANG_UI.get(sess.get("lang", "en"), "Lang1"))
            l2 = esc(AIQUIZ_LANG_UI.get(value, value))
            await _edit(msg, f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB {sess['count']} | \U0001F310 {l1} / {l2}\n\nDifficulty?", _kb("diff", uid))

        elif step == "diff":
            sess["diff"] = value
            dlbl = esc(DIFF_LABELS.get(value, value))
            await _edit(msg, f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB {sess['count']} | {dlbl}\n\nExam style?", _kb("exam", uid))

        elif step in ("exam", "retry"):
            # Claim the slot *before* touching the session: a second tap that
            # loses the race must not be able to overwrite the exam style the
            # winning generation is about to be told to use.
            if not _claim_generation(uid):
                await query.answer("⏳ Questions are still being generated — please wait.", show_alert=True)
                return

            started = False
            try:
                # "retry" re-runs generation with the choices already
                # collected, so a provider hiccup costs one tap instead of the
                # whole wizard.
                if step == "exam":
                    sess["exam"] = value
                if not sess.get("exam"):
                    await _edit(msg, f"\U0001F916 <b>AI Quiz: {t}</b>\n\nExam style?", _kb("exam", uid))
                    return
                elbl = esc(EXAM_LABELS.get(sess["exam"], sess["exam"]))

                has_gemini = bool(await get_provider_keys(uid, "gemini"))
                has_groq = bool(await get_provider_keys(uid, "groq"))
                has_openrouter = bool(await get_provider_keys(uid, "openrouter"))
                has_default_or = bool(config.OPENROUTER_DEFAULT_KEYS)
                if has_gemini:
                    ai_label = "Gemini"
                elif has_groq:
                    ai_label = "Groq"
                elif has_openrouter:
                    ai_label = "OpenRouter (your key)"
                elif has_default_or:
                    ai_label = "OpenRouter (shared key)"
                else:
                    ai_label = "Pollinations (free fallback)"

                bilingual_info = ""
                if sess.get("bilingual") and sess.get("lang2"):
                    l1 = esc(AIQUIZ_LANG_UI.get(sess.get("lang", "en"), ""))
                    l2 = esc(AIQUIZ_LANG_UI.get(sess["lang2"], ""))
                    bilingual_info = f"\n\U0001F310 <b>Bilingual:</b> {l1} / {l2}"

                # Empty keyboard: the exam/retry buttons would otherwise stay
                # attached for the whole 30-120s generation and invite a
                # second (now rejected, but pointless) tap.
                await _edit(
                    msg,
                    f"\U0001F916 <b>AI Quiz: {t}</b>\n\U0001F4CB {sess['count']} | {elbl} | \U0001F916 {ai_label}"
                    f"{bilingual_info}\n\n⏳ Generating questions in chunks of 25...\nThis may take 30–120 seconds.",
                    InlineKeyboardMarkup([]),
                )
                tasks.spawn(_aiquiz_generate_flow(uid, ctx, msg, sess), name=f"aiquiz_gen_{uid}")
                started = True
            finally:
                if not started:
                    # Nothing took ownership of the slot -- release it here so
                    # the user is never locked out of a retry.
                    _release_generation(uid)

        elif step == "cancel":
            AI_QUIZ_SESSIONS.pop(uid, None)
            _release_generation(uid)
            await _edit(msg, f"❌ <b>AI Quiz cancelled.</b>\n\nRun <code>/aiquiz {t}</code> to start again.", InlineKeyboardMarkup([]))

        elif step == "timer":
            sess["timer"] = int(value)
            await _edit(msg, f"✅ Timer: {value}s\n\n➖ <b>Negative marking?</b>", _kb("neg", uid))

        elif step == "neg":
            sess["neg"] = float(value)
            await _edit(msg, f"✅ Negative: {value}\n\n✅ <b>Correct mark per question?</b>", _kb("cm", uid))

        elif step == "cm":
            sess["cm"] = int(value)
            await _edit(msg, f"✅ Correct mark: +{value}\n\n\U0001F500 <b>Shuffle?</b>", _kb("shuffle", uid))

        elif step == "shuffle":
            sess["shuffle_q"] = value in ("q", "b")
            sess["shuffle_o"] = value in ("o", "b")
            slbl = {"q": "Questions", "o": "Options", "b": "Both", "none": "None"}.get(value, "None")
            if sess["shuffle_o"]:
                await _edit(msg, f"✅ Shuffle: {slbl}\n\n\U0001F500 <b>Shuffle how many options?</b>", _kb("shufflecount", uid))
            else:
                sess["shuffle_o_count"] = 0
                await _edit(
                    msg, f"✅ Shuffle: {slbl}\n\n\U0001F4A1 Show explanation after each question?",
                    InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Yes", callback_data=f"aiq_ex_{uid}_yes"),
                        InlineKeyboardButton("❌ No (default)", callback_data=f"aiq_ex_{uid}_no"),
                    ]]),
                )

        elif step == "shufflecount":
            sess["shuffle_o_count"] = 0 if value == "all" else int(value)
            label = "All" if value == "all" else f"First {value}"
            await _edit(
                msg, f"✅ Options shuffle range: {label}\n\n\U0001F4A1 Show explanation after each question?",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes", callback_data=f"aiq_ex_{uid}_yes"),
                    InlineKeyboardButton("❌ No (default)", callback_data=f"aiq_ex_{uid}_no"),
                ]]),
            )

        elif step == "ex":
            sess["show_explanation"] = value == "yes"
            questions = sess.get("questions", [])
            neg_str = "None" if sess["neg"] == 0 else f"-{sess['neg']}"
            slbl = "Both" if (sess.get("shuffle_q") and sess.get("shuffle_o")) else "Questions" if sess.get("shuffle_q") else "Options" if sess.get("shuffle_o") else "None"
            await _edit(
                msg,
                f"\U0001F916 <b>AI Quiz Ready!</b>\n\n\U0001F4CC <b>Topic:</b> {t}\n\U0001F4CB <b>Questions:</b> {len(questions)}\n"
                f"⏱ <b>Timer:</b> {sess['timer']}s\n✅ <b>Correct:</b> +{sess['cm']}\n➖ <b>Negative:</b> {neg_str}\n"
                f"\U0001F500 <b>Shuffle:</b> {slbl}\n\U0001F4A1 <b>Explanation:</b> {'✅ Yes' if sess['show_explanation'] else '❌ No'}\n\n▶️ Starting quiz now...",
                InlineKeyboardMarkup([]),
            )
            chat_id = sess.get("chat_id", uid)
            chat_type = sess.get("chat_type", "private")
            show_expl = sess.get("show_explanation", False)
            AI_QUIZ_SESSIONS.pop(uid, None)
            await asyncio.sleep(1)
            await _launch_ai_quiz(
                uid, ctx, questions, sess["topic"], sess["timer"], sess["neg"], sess["cm"],
                sess.get("shuffle_q", False), sess.get("shuffle_o", False),
                shuffle_o_count=sess.get("shuffle_o_count", 0), chat_id=chat_id,
                chat_type=chat_type, update=update, show_explanation=show_expl,
                analytics_source="aiquiz",
                analytics_topic=sess.get("topic"),
                analytics_difficulty=sess.get("diff"),
            )
    except Exception as e:
        logger.error("aiquiz_callback error: %s", e, exc_info=True)


async def _aiquiz_generate_flow(uid: int, ctx: ContextTypes.DEFAULT_TYPE, msg: Any, sess: dict) -> None:
    """Background task: generate questions, then move to timer settings.

    Owns the per-user in-flight slot claimed by `aiquiz_callback` and always
    releases it in `finally`, so a success, an empty result, an exception or a
    cancellation all leave the user able to generate again.
    """
    try:
        topic = sess["topic"]
        count = sess["count"]
        lang = sess.get("lang", "en")
        diff = sess.get("diff", "moderate")
        exam = sess["exam"]
        lang2 = sess.get("lang2")

        questions = await generate_in_chunks(uid, topic, count, lang, diff, exam, msg, bilingual_lang2=lang2)

        # The user may have tapped Cancel, or started a fresh /aiquiz, while
        # this was running. Either way the stored session is no longer *this*
        # one, so drop the result instead of resurrecting a wizard the user
        # has already left behind.
        if AI_QUIZ_SESSIONS.get(uid) is not sess:
            logger.info("aiquiz session for %s was replaced during generation -- discarding %d question(s)",
                        uid, len(questions))
            return

        if not questions:
            # Keep the collected choices and offer Retry/Cancel: an empty
            # result is a provider problem, not a bad topic, and making the
            # user re-type /aiquiz + re-pick 5 settings is a dead end.
            last_working_ai.pop(uid, None)
            AI_QUIZ_SESSIONS[uid] = sess
            await _edit(
                msg,
                f"❌ <b>No questions came back for:</b> {esc(topic)}\n\n"
                "The provider returned an empty result. Check your AI key or try a "
                "different topic.\nAdd a key: <code>/setkey gemini YOUR_KEY</code>",
                _retry_kb(uid),
            )
            return

        got = len(questions)
        shortfall_note = f"\n⚠️ Got {got}/{count} — starting with available questions." if got < count else ""
        bilingual_tag = ""
        if lang2:
            l1 = esc(AIQUIZ_LANG_UI.get(lang, lang))
            l2 = esc(AIQUIZ_LANG_UI.get(lang2, lang2))
            bilingual_tag = f"\n\U0001F310 <b>Bilingual:</b> {l1} / {l2}"

        # Everything below is model output: question text and options routinely
        # contain "<", ">" and "&" (maths, HTML-ish snippets, "A & B"), which
        # Telegram rejects outright in parse_mode=HTML. Truncate first, escape
        # second, so a cut can never split an entity.
        preview = (
            f"✅ <b>{got} questions generated!</b>{shortfall_note}\n\n"
            f"\U0001F4CC <b>Topic:</b> {esc(topic)}{bilingual_tag}\n<b>Preview (first 3):</b>\n"
        )
        for i, q in enumerate(questions[:3], 1):
            cid = q["correct_option_id"]
            cids = cid if isinstance(cid, list) else [cid]
            qtext = str(q["question"])
            preview += f"\n<b>Q{i}.</b> {esc(qtext[:80])}{'...' if len(qtext) > 80 else ''}\n"
            for j, opt in enumerate(q["options"]):
                mark = "✅" if j in cids else "▪️"
                otext = str(opt)
                preview += f"  {mark} {esc(otext[:45])}{'...' if len(otext) > 45 else ''}\n"
        preview += "\n⏱ <b>Timer per question?</b>"

        sess["questions"] = questions
        AI_QUIZ_SESSIONS[uid] = sess
        await _edit(msg, preview, _kb("timer", uid))
    except Exception as e:
        logger.error("_aiquiz_generate_flow error: %s", e, exc_info=True)
        if AI_QUIZ_SESSIONS.get(uid) is sess:
            sess.pop("questions", None)
            AI_QUIZ_SESSIONS[uid] = sess
            await _edit(msg, f"❌ <b>Generation error:</b> {esc(str(e)[:200])}", _retry_kb(uid))
    finally:
        _release_generation(uid)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("aiquiz", aiquiz_command))
    application.add_handler(CallbackQueryHandler(aiquiz_callback, pattern="^aiq_"))
