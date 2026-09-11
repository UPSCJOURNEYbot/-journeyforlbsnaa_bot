"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pyrogram import Client, filters
from pyrogram.enums import PollType
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from quizbot.database import CreatorSettingsRepository, QuizRepository, UserRepository, get_db
from quizbot.shared import config
from quizbot.shared.bot_links import get_runner_bot_username, runner_group_url, runner_start_url
from quizbot.shared.mini_app_link import mini_app_web_app_button
from quizbot.shared.utils import is_premium_user

from .. import state
from ..parsing import filter_words, parse_question_block, strip_source_noise
from ..ratelimit import ratelimit
from ..subscribe_gate import subscribe_gate
from .file_import import process_public_url, process_uploaded_file

logger = logging.getLogger(__name__)


def _poll_text(value) -> Optional[str]:
    """Unwrap a poll's question/option/explanation field to a plain str.

    Newer Pyrogram forks (Kurigram) return these as `FormattedText` objects
    (with a `.text` attribute + entities) instead of a bare `str`, so any
    downstream `re.sub`/string handling breaks with a TypeError unless we
    normalize here first. Handles both the old (str) and new (FormattedText)
    shapes, plus a plain None."""
    if value is None:
        return None
    text = getattr(value, "text", None)
    if text is not None:
        return str(text)
    return str(value)


# Commands that must always be reachable even while a quiz-creation wizard
# is in progress -- the free-text/poll catch-all handler must not swallow
# these. Mirrors the exclusion list from the original bot's message filter.
_RESERVED_COMMANDS = [
    "start", "create", "myquizzes", "edit", "info", "ban", "done", "add", "rem",
    "remall", "del", "remove", "clearlist", "mywords", "help", "cancel", "quiz",
    "search", "auth", "setpromo", "setkey", "mykeys", "delkey", "settings",
    "batch", "createbatch", "searchbatch", "stopedit", "whtml", "testseries",
    "tsr", "mocktest", "features", "gcast", "stopcast", "statses", "testapi",
    "leaders", "aspirants", "limit", "listquiz", "removeuser",
]

MIN_QUESTIONS = 10
MAX_QUESTIONS = 300
MAX_QUESTIONS_OWNER = 3000


def _gen_qid() -> str:
    import random
    import string

    return "GGN" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


@ratelimit("create")
async def create_cmd(c: Client, m: Message) -> None:
    """/create -- start a new quiz-creation session. Sends questions,
    forwarded quiz polls, or a .txt file next; finish with /done."""
    if await subscribe_gate(c, m):
        return
    uid = m.from_user.id
    if uid in state.quiz_creation:
        await m.reply("⚠️ Already creating a quiz. Use /done or /cancel.")
        return
    await m.reply("📝 **Send the quiz name.**")
    state.quiz_creation[uid] = {
        "questions": [],
        "timer": None,
        "quiz_name": None,
        "awaiting_name": True,
    }


@ratelimit("default")
async def cancel_cmd(c: Client, m: Message) -> None:
    """/cancel -- discard the quiz currently being created."""
    uid = m.from_user.id
    if uid in state.quiz_creation:
        state.quiz_creation.pop(uid, None)
        await m.reply("❌ Cancelled.")
    elif uid in state.testseries_upload:
        state.testseries_upload.pop(uid, None)
        await m.reply("❌ Test-series upload cancelled.")
    else:
        await m.reply("⚠️ Nothing to cancel.")


def _settings_kb(uid: int, step: str, choices: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"cws_{step}_{uid}_{value}")]
         for label, value in choices]
    )


async def _ask_negative_mark(c: Client, target, uid: int) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1/4 (0.25)", callback_data=f"cws_nm_{uid}_25"),
            InlineKeyboardButton("1/3 (0.333)", callback_data=f"cws_nm_{uid}_333"),
        ],
        [InlineKeyboardButton("❌ No Negative", callback_data=f"cws_nm_{uid}_0")],
    ])
    text = "➖ **Negative marking?**\n\nChoose the penalty for a wrong answer."
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


async def _ask_timer(c: Client, target, uid: int) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10s", callback_data=f"cws_tm_{uid}_10"),
            InlineKeyboardButton("20s", callback_data=f"cws_tm_{uid}_20"),
            InlineKeyboardButton("30s", callback_data=f"cws_tm_{uid}_30"),
        ],
        [
            InlineKeyboardButton("60s", callback_data=f"cws_tm_{uid}_60"),
            InlineKeyboardButton("⚙️ Custom", callback_data=f"cws_tm_{uid}_custom"),
        ],
    ])
    text = "⏱️ **Time per question?**\n\nChoose a preset or tap **Custom** and enter seconds."
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


async def _ask_explanation(c: Client, target, uid: int) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"cws_ex_{uid}_yes"),
        InlineKeyboardButton("❌ No", callback_data=f"cws_ex_{uid}_no"),
    ]])
    text = "💡 **Show explanation after each question?**"
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


async def _ask_section(c: Client, target, uid: int) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"cws_sec_{uid}_yes"),
        InlineKeyboardButton("❌ No", callback_data=f"cws_sec_{uid}_no"),
    ]])
    text = "📚 **Section?**\n\nDo you want to divide this quiz into sections?"
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


async def _ask_shuffle_q(c: Client, target, uid: int) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"cws_sq_{uid}_yes"),
        InlineKeyboardButton("❌ No", callback_data=f"cws_sq_{uid}_no"),
    ]])
    text = "🔀 **Shuffle Questions?**"
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


async def _ask_shuffle_o(c: Client, target, uid: int) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"cws_so_{uid}_yes"),
        InlineKeyboardButton("❌ No", callback_data=f"cws_so_{uid}_no"),
    ]])
    text = "🔀 **Shuffle Options?**"
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


async def _ask_report(c: Client, target, uid: int, kind: str) -> None:
    label = "HTML" if kind == "html" else "PDF"
    default = "Yes"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"cws_{kind}_{uid}_yes"),
        InlineKeyboardButton("❌ No", callback_data=f"cws_{kind}_{uid}_no"),
    ]])
    text = f"📄 **{label} Report?**\n\nDefault: **{default}**"
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.reply(text, reply_markup=kb)


@ratelimit("create")
async def done_cmd(c: Client, m: Message) -> None:
    """/done -- finish question import and start the step-by-step quiz settings wizard."""
    uid = m.from_user.id
    if uid not in state.quiz_creation:
        await m.reply("⚠️ Use /create first.")
        return

    total = len(state.quiz_creation[uid]["questions"])
    if total < MIN_QUESTIONS:
        await m.reply(f"⚠️ Need at least {MIN_QUESTIONS} questions. You have {total}.")
        return

    max_allowed = MAX_QUESTIONS_OWNER if uid == config.OWNER_ID else MAX_QUESTIONS
    if total > max_allowed:
        await m.reply(f"⚠️ Max {max_allowed} questions. You have {total}.")
        return

    ud = state.quiz_creation[uid]
    ud.update({
        "negative_marks": 0.0,
        "timer": None,
        "show_explanation": False,
        "section_wise": False,
        "sections": [],
        "shuffle_questions": False,
        "shuffle_options": False,
        "html_report": True,
        "pdf_report": True,
        "wizard_step": "negative",
    })
    await _ask_negative_mark(c, m, uid)


async def _finalize_quiz(c: Client, reply_target, uid: int, from_user_name: str) -> None:
    """Create the quiz using the explicit settings collected by the wizard."""
    is_message = hasattr(reply_target, "reply") and not hasattr(reply_target, "data")

    async def send_result(text: str, kb: Optional[InlineKeyboardMarkup] = None):
        if is_message:
            return await reply_target.reply(text, reply_markup=kb)
        try:
            return await reply_target.message.edit_text(text, reply_markup=kb)
        except Exception:
            return await reply_target.message.reply(text, reply_markup=kb)

    ud = state.quiz_creation[uid]
    quiz_name = ud["quiz_name"]
    questions = ud["questions"]
    timer = int(ud.get("timer") or 30)
    sections = ud.get("sections", [])
    settings_repo = CreatorSettingsRepository(get_db())
    saved = await settings_repo.get(uid)

    # Keep the creator's existing /settings default-text feature, but do not
    # introduce another question into this step-by-step wizard.
    default_text = saved.get("default_text")
    default_text_field = saved.get("default_text_field", "both")
    if default_text:
        for q in questions:
            if default_text_field in ("question", "both") and q.get("question"):
                q["question"] = q["question"].rstrip() + "\n" + default_text
            if default_text_field in ("explanation", "both"):
                q["explanation"] = (
                    (q.get("explanation") or "").rstrip() + "\n" + default_text
                    if q.get("explanation") else default_text
                )

    qid = _gen_qid()
    quiz_repo = QuizRepository(get_db())
    quiz = await quiz_repo.create(
        creator_id=uid,
        quiz_name=quiz_name,
        questions=questions,
        qid=qid,
        sections=sections,
        timer=timer,
        quiz_type="free",
        negative_marks=float(ud.get("negative_marks", 0.0)),
        promo_message=None,
        show_explanation=bool(ud.get("show_explanation", False)),
        shuffle_questions=bool(ud.get("shuffle_questions", False)),
        shuffle_options=bool(ud.get("shuffle_options", False)),
        html_report=bool(ud.get("html_report", True)),
        pdf_report=bool(ud.get("pdf_report", True)),
        fixed_settings=True,
    )
    state.quiz_creation.pop(uid, None)

    if not quiz:
        await send_result("⚠️ Quiz created but could not be re-fetched. Try /myquizzes.")
        return

    neg = quiz.get("negative_marks", 0)
    neg_label = "No" if not neg else ("1/4" if abs(float(neg) - .25) < .001 else "1/3")
    text = (
        "🎉 **Quiz Created!**\n\n"
        f"📝 **Name:** {quiz_name}\n"
        f"❓ **Questions:** {len(quiz['questions'])}\n"
        f"➖ **Negative marking:** {neg_label}\n"
        f"⏱️ **Time/question:** {timer}s\n"
        f"💡 **Explanation:** {'Yes' if quiz.get('show_explanation') else 'No'}\n"
        f"📚 **Sections:** {'Yes' if sections else 'No'}\n"
        f"🔀 **Shuffle questions:** {'Yes' if quiz.get('shuffle_questions') else 'No'}\n"
        f"🔀 **Shuffle options:** {'Yes' if quiz.get('shuffle_options') else 'No'}\n"
        f"📄 **HTML report:** {'Yes' if quiz.get('html_report') else 'No'}\n"
        f"📄 **PDF report:** {'Yes' if quiz.get('pdf_report') else 'No'}\n"
        f"🆔 **Quiz ID:** `{qid}`\n"
        f"🏷️ **Type:** `free`"
    )
    if sections:
        text += "\n\n**Sections:**"
        for i, sec in enumerate(sections, 1):
            text += (
                f"\n\nSection {i}: {sec['name']}\n"
                f"  Questions: {sec['question_range'][0]} to {sec['question_range'][1]}\n"
                f"  Timer: {sec.get('timer', timer)}s"
            )

    runner_username = await get_runner_bot_username()
    kb_buttons = [
        [InlineKeyboardButton("🚀 Start", url=f"https://t.me/{runner_username}?start={qid}")],
        [InlineKeyboardButton("👥 Add to Group", url=f"https://t.me/{runner_username}?startgroup={qid}")],
        [InlineKeyboardButton("🔗 Share", switch_inline_query=qid)],
    ]
    play_practice = mini_app_web_app_button(runner_username, qid, "practice", "Play (Practice)")
    play_exam = mini_app_web_app_button(runner_username, qid, "exam", "Play (Exam)")
    if play_practice and play_exam:
        kb_buttons.append([play_practice, play_exam])
    await send_result(text, InlineKeyboardMarkup(kb_buttons))

    if config.BOT_GROUP:
        try:
            announce_text = strip_source_noise(text) or text
            await c.send_message(config.BOT_GROUP, announce_text, reply_markup=InlineKeyboardMarkup(kb_buttons[:3]))
        except Exception:
            logger.debug("Failed to announce new quiz in BOT_GROUP", exc_info=True)


async def handle_document(c: Client, m: Message) -> None:
    """Handle a .txt/.json file sent while a quiz-creation session is
    active -- imports questions in bulk (see handlers/file_import.py)."""
    uid = m.from_user.id
    if uid not in state.quiz_creation:
        return
    filename = (m.document.file_name or "").lower()
    supported_ext = (".txt", ".md", ".markdown", ".json", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
    mime = (m.document.mime_type or "").lower()
    if not filename.endswith(supported_ext) and not any(x in mime for x in ("text/plain", "text/markdown", "json", "pdf", "image/")):
        await m.reply("⚠️ Supported: TXT, MD, JSON, PDF, PNG/JPG/WEBP/BMP/TIFF.")
        return

    status = await m.reply("⏳ Processing...")
    file_bytes = await c.download_media(m.document.file_id, in_memory=True)
    file_bytes.seek(0)
    content = file_bytes.read()

    user = await UserRepository(get_db()).get_or_create(uid)
    remove_words = user.get("remove_words", [])

    # Parsing (especially large/scanned PDFs) is CPU-bound: run it off the
    # event loop so the single PTB poller never appears frozen while a file
    # is being processed.
    count, error = await asyncio.to_thread(
        process_uploaded_file,
        content, m.document.file_name or "upload.txt", state.quiz_creation[uid]["questions"], remove_words
    )
    if error:
        await status.edit_text(f"❌ Error: {error}")
    elif count == 0:
        await status.edit_text(
            "⚠️ No questions could be read from this file.\n"
            "Send .txt/.json, a text-based PDF with marked answers, or an image of the questions. /done"
        )
    else:
        total = len(state.quiz_creation[uid]["questions"])
        await status.edit_text(f"✅ {count} questions processed! Total: {total}\nSend more, paste text, send an image, PDF/MD/TXT, or public AI link. /done")


async def handle_photo(c: Client, m: Message) -> None:
    """OCR a question image while creating a quiz."""
    uid = m.from_user.id
    if uid not in state.quiz_creation:
        return
    status = await m.reply("⏳ Reading image...")
    try:
        file_bytes = await c.download_media(m.photo.file_id, in_memory=True)
        file_bytes.seek(0)
        content = file_bytes.read()
        user = await UserRepository(get_db()).get_or_create(uid)
        remove_words = user.get("remove_words", [])
        count, error = process_uploaded_file(
            content, "question.jpg", state.quiz_creation[uid]["questions"], remove_words
        )
        if error:
            await status.edit_text(f"❌ {error}")
        else:
            total = len(state.quiz_creation[uid]["questions"])
            await status.edit_text(f"✅ {count} questions extracted from image. Total: {total}\nSend more or /done")
    except Exception as exc:
        logger.exception("Image import failed")
        await status.edit_text(f"❌ Image could not be processed: {exc}")


async def handle_creation_message(c: Client, m: Message) -> None:
    """Handle quiz name, imported questions and the text-entry steps of the
    creation wizard. Settings are handled by inline callbacks below."""
    uid = m.from_user.id
    if uid not in state.quiz_creation:
        return
    ud = state.quiz_creation[uid]

    # Step-by-step settings that require typed input.
    if ud.get("awaiting_timer"):
        try:
            timer = int(m.text.strip())
            if timer <= 0 or timer > 3600:
                raise ValueError
        except ValueError:
            await m.reply("⚠️ Enter seconds between 1 and 3600.")
            return
        ud["timer"] = timer
        ud.pop("awaiting_timer", None)
        await _ask_explanation(c, m, uid)
        return

    if ud.get("awaiting_section_count"):
        try:
            n = int(m.text.strip())
            if n < 2 or n > len(ud["questions"]):
                raise ValueError
        except ValueError:
            await m.reply(f"⚠️ Enter a section count from 2 to {len(ud['questions'])}.")
            return
        ud["section_count"] = n
        ud["sections"] = []
        ud["current_section"] = 1
        ud["last_range_end"] = 0
        ud.pop("awaiting_section_count", None)
        ud["awaiting_section_name"] = True
        await m.reply("📚 Section 1 name:")
        return

    if ud.get("awaiting_section_name"):
        name = m.text.strip()
        if not name:
            await m.reply("⚠️ Invalid section name.")
            return
        ud["sections"].append({"name": name})
        ud.pop("awaiting_section_name", None)
        ud["awaiting_question_range"] = True
        await m.reply(f"📚 Range for **{name}** (e.g. 1-5). Max: {len(ud['questions'])}")
        return

    if ud.get("awaiting_question_range"):
        try:
            start_q, end_q = map(int, m.text.strip().split("-"))
            total = len(ud["questions"])
            if not (1 <= start_q <= end_q <= total):
                raise ValueError
            if ud.get("last_range_end") and start_q != ud["last_range_end"] + 1:
                raise ValueError
        except ValueError:
            await m.reply("⚠️ Invalid range. Use e.g. `1-10` and keep sections continuous.")
            return
        ud["sections"][-1]["question_range"] = (start_q, end_q)
        ud.pop("awaiting_question_range", None)
        ud["last_range_end"] = end_q
        ud["awaiting_section_timer"] = True
        await m.reply("⏱️ Section timer in seconds (1-3600):")
        return

    if ud.get("awaiting_section_timer"):
        try:
            timer = int(m.text.strip())
            if timer <= 0 or timer > 3600:
                raise ValueError
        except ValueError:
            await m.reply("⚠️ Enter seconds between 1 and 3600.")
            return
        ud["sections"][-1]["timer"] = timer
        ud.pop("awaiting_section_timer", None)
        if len(ud["sections"]) < ud["section_count"]:
            ud["current_section"] += 1
            ud["awaiting_section_name"] = True
            await m.reply(f"📚 Section {ud['current_section']} name:")
            return
        # All sections are configured.
        await _ask_shuffle_q(c, m, uid)
        return

    if ud.get("awaiting_name"):
        name = m.text.strip()
        if not name:
            await m.reply("⚠️ Invalid name.")
            return
        ud["quiz_name"] = name
        ud["awaiting_name"] = False
        await m.reply(
            f"📝 **Name:** {name}\n\n"
            "Now send questions as text, TXT/MD/JSON/PDF, image, forwarded quiz polls, "
            "or public ChatGPT/DeepSeek/Gemini links.\n\n"
            "When finished, use /done."
        )
        return

    # Public AI/share links.
    import re
    urls = re.findall(r"https?://\S+", m.text or "")
    if urls and not any(ud.get(k) for k in (
        "awaiting_name", "awaiting_timer", "awaiting_section_count",
        "awaiting_section_name", "awaiting_question_range", "awaiting_section_timer"
    )):
        user = await UserRepository(get_db()).get_or_create(uid)
        remove_words = user.get("remove_words", [])
        status = await m.reply("⏳ Reading public link and extracting questions...")
        imported, errors = 0, []
        for url in urls[:3]:
            count, error = await process_public_url(url.rstrip(")]>"), ud["questions"], remove_words)
            imported += count or 0
            if error:
                errors.append(error)
        total = len(ud["questions"])
        if imported:
            await status.edit_text(f"✅ {imported} questions imported. Total: {total}\nSend more or /done")
        else:
            await status.edit_text(errors[0] if errors else "❌ No valid questions found.")
        return

    # Free-text question paste.
    if not m.text:
        return
    blocks = m.text.split("\n\n")
    reply_msg = m.reply_to_message
    reply_text = reply_msg.text if reply_msg and reply_msg.text else None
    file_id = None
    if reply_msg and reply_msg.photo and config.BOT_GROUP:
        try:
            copied = await c.copy_message(config.BOT_GROUP, reply_msg.chat.id, reply_msg.id)
            file_id = copied.photo.file_id
        except Exception:
            logger.debug("Failed to copy pasted question photo", exc_info=True)

    parsed_any = False
    for block in blocks:
        if not block.strip():
            continue
        parsed = parse_question_block(block)
        if not parsed:
            await m.reply(
                "⚠️ Invalid format.\n\nMark the correct option with a check-mark emoji, "
                "or use A) B) C) D) labels."
            )
            return
        parsed["file_id"] = file_id
        parsed["reply_text"] = reply_text
        ud["questions"].append(parsed)
        parsed_any = True

    if not parsed_any:
        await m.reply("⚠️ No valid question found.")
        return
    total = len(ud["questions"])
    await m.reply(f"✅ {total} questions saved! Send more or /done")


async def creation_wizard_cb(c: Client, cb: CallbackQuery) -> None:
    """Inline callbacks for the /create settings wizard."""
    parts = cb.data.split("_")
    # cws_<step>_<uid>_<value>
    if len(parts) < 4:
        await cb.answer("Invalid step.", show_alert=True)
        return
    step, uid_s, value = parts[1], parts[2], "_".join(parts[3:])
    try:
        uid = int(uid_s)
    except ValueError:
        await cb.answer("Invalid session.", show_alert=True)
        return
    if cb.from_user.id != uid:
        await cb.answer("❌ This setup belongs to another user.", show_alert=True)
        return
    ud = state.quiz_creation.get(uid)
    if not ud:
        await cb.answer("⚠️ Session expired. Start again with /create.", show_alert=True)
        return

    if step == "nm":
        ud["negative_marks"] = {"25": .25, "333": 1/3, "0": 0.0}.get(value, 0.0)
        ud["wizard_step"] = "timer"
        await _ask_timer(c, cb, uid)

    elif step == "tm":
        if value == "custom":
            ud["awaiting_timer"] = True
            ud["wizard_step"] = "timer_custom"
            await cb.message.edit_text("⏱️ **Enter time per question in seconds.**\n\nAllowed: 1–3600")
        else:
            ud["timer"] = int(value)
            ud["wizard_step"] = "explanation"
            await _ask_explanation(c, cb, uid)

    elif step == "ex":
        ud["show_explanation"] = value == "yes"
        ud["wizard_step"] = "section"
        await _ask_section(c, cb, uid)

    elif step == "sec":
        if value == "yes":
            ud["section_wise"] = True
            ud["wizard_step"] = "section_count"
            ud["awaiting_section_count"] = True
            await cb.message.edit_text(
                f"📚 **How many sections?**\n\nEnter 2–{len(ud['questions'])}."
            )
        else:
            ud["section_wise"] = False
            ud["sections"] = []
            ud["wizard_step"] = "shuffle_questions"
            await _ask_shuffle_q(c, cb, uid)

    elif step == "sq":
        ud["shuffle_questions"] = value == "yes"
        ud["wizard_step"] = "shuffle_options"
        await _ask_shuffle_o(c, cb, uid)

    elif step == "so":
        ud["shuffle_options"] = value == "yes"
        ud["wizard_step"] = "html"
        await _ask_report(c, cb, uid, "html")

    elif step == "html":
        ud["html_report"] = value != "no"
        ud["wizard_step"] = "pdf"
        await _ask_report(c, cb, uid, "pdf")

    elif step == "pdf":
        ud["pdf_report"] = value != "no"
        ud["wizard_step"] = "summary"
        neg = ud.get("negative_marks", 0)
        neg_label = "No" if not neg else ("1/4" if abs(neg-.25) < .001 else "1/3")
        summary = (
            "⚙️ **Quiz Settings Summary**\n\n"
            f"➖ Negative marking: **{neg_label}**\n"
            f"⏱️ Time/question: **{ud.get('timer')}s**\n"
            f"💡 Show explanation: **{'Yes' if ud.get('show_explanation') else 'No'}**\n"
            f"📚 Sections: **{'Yes' if ud.get('sections') else 'No'}**\n"
            f"🔀 Shuffle questions: **{'Yes' if ud.get('shuffle_questions') else 'No'}**\n"
            f"🔀 Shuffle options: **{'Yes' if ud.get('shuffle_options') else 'No'}**\n"
            f"📄 HTML report: **{'Yes' if ud.get('html_report') else 'No'}**\n"
            f"📄 PDF report: **{'Yes' if ud.get('pdf_report') else 'No'}**\n\n"
            "Everything looks good?"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Create Quiz", callback_data=f"cws_create_{uid}_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cws_cancel_{uid}_yes"),
        ]])
        await cb.message.edit_text(summary, reply_markup=kb)

    elif step == "create":
        if value != "yes":
            await cb.answer()
            return
        await cb.answer("⏳ Creating quiz...")
        try:
            await cb.message.edit_text("🚀 Creating quiz...")
        except Exception:
            pass
        name = cb.from_user.first_name if cb.from_user else str(uid)
        await _finalize_quiz(c, cb, uid, name)

    elif step == "cancel":
        state.quiz_creation.pop(uid, None)
        await cb.message.edit_text("❌ Quiz creation cancelled.")

    else:
        await cb.answer("Unknown step.", show_alert=True)
        return

    await cb.answer()


def in_quiz_creation_filter():
    async def func(_, __, m: Message) -> bool:
        return bool(m.from_user) and m.from_user.id in state.quiz_creation

    return filters.create(func)


def register(app: Client) -> None:
    app.on_message(filters.command("create") & filters.private)(create_cmd)
    app.on_message(filters.command("done") & filters.private)(done_cmd)
    app.on_message(filters.command("cancel") & filters.private)(cancel_cmd)
    app.on_callback_query(filters.regex(r"^cws_(nm|tm|ex|sec|sq|so|html|pdf|create|cancel)_\d+_.+$"))(creation_wizard_cb)
    app.on_message(filters.document & filters.private & in_quiz_creation_filter())(handle_document)
    app.on_message(filters.photo & filters.private & in_quiz_creation_filter())(handle_photo)
    app.on_message(
        (filters.text | filters.poll)
        & filters.private
        & in_quiz_creation_filter()
        & ~filters.command(_RESERVED_COMMANDS)
    )(handle_creation_message)
