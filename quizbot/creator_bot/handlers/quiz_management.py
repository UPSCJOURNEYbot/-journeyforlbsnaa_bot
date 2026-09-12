"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from quizbot.database import QuizRepository, get_db
from quizbot.shared.bot_links import get_runner_bot_username, runner_group_url, runner_start_url
from quizbot.shared import config
from quizbot.shared.utils import is_premium_user

from .. import state
from ..ratelimit import ratelimit
from ..subscribe_gate import subscribe_gate

logger = logging.getLogger(__name__)


def _is_owner_or_admin(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS


async def _send_quiz_page(target, quizzes: list[dict], page: int, uid: int) -> None:
    """Render one page of a quiz list (used by /myquizzes and its
    prev/next pagination callbacks)."""
    page_size = config.PAGE_SIZE
    start, end = page * page_size, page * page_size + page_size
    chunk = quizzes[start:end]
    if not chunk:
        await target.edit_text("📋 No quizzes on this page.")
        return

    lines = []
    for i, q in enumerate(chunk, start=start + 1):
        qid = q.get("qid", "N/A")
        qtype = str(q.get("quiz_type", "free") or "free")
        type_icon = "🆓" if qtype.lower() == "free" else "💎"
        lines.append(
            f"**{i}. {q.get('quiz_name', 'Unnamed')[:100]}**\n"
            f"    ID: `{qid}`\n"
            f"    {type_icon} {qtype.title()}\n"
            f"    Plays: {q.get('total_participants', 0)}\n"
            f"    Edit: `/edit {qid}`\n"
            f"────────────────"
        )
    text = f"📋 **Your Quizzes (Page {page + 1})**\nTotal: {len(quizzes)}\n\n" + "\n".join(lines)

    keyboard_rows = []
    if len(quizzes) > page_size:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"prev:{page}:{uid}"))
        if end < len(quizzes):
            nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"next:{page}:{uid}"))
        nav.append(
            InlineKeyboardButton(
                f"Page {page + 1}/{(len(quizzes) + page_size - 1) // page_size}",
                callback_data="page_info",
            )
        )
        keyboard_rows.append(nav)
    keyboard_rows.append([InlineKeyboardButton("❌ Close", callback_data=f"refresh:{uid}")])
    await target.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))


@ratelimit("strict")
async def myquizzes_cmd(c: Client, m: Message) -> None:
    """/myquizzes [term] -- list (optionally search) this creator's quizzes."""
    if await subscribe_gate(c, m):
        return
    uid = m.from_user.id
    args = m.text.split(maxsplit=1)
    search_term = args[1].strip() if len(args) > 1 else None

    label = f"🔍 Searching for **{search_term}**..." if search_term else "📋 Fetching..."
    pending = await m.reply(label)

    quizzes = await QuizRepository(get_db()).list_by_creator(uid, query=search_term)
    if not quizzes:
        msg = f"🔍 No quizzes found for **{search_term}**" if search_term else "📋 No quizzes yet."
        await pending.edit_text(msg)
        return
    state.save_quiz_list_cache(uid, quizzes)
    await _send_quiz_page(pending, quizzes, 0, uid)


async def pagination_cb(c: Client, cb: CallbackQuery) -> None:
    """`prev:<page>:<uid>` / `next:<page>:<uid>` / `refresh:<uid>` -- quiz
    list pagination for /myquizzes."""
    try:
        parts = (cb.data or "").split(":")
        action = parts[0]
        uid = int(parts[-1])
        page = int(parts[1]) if action in ("prev", "next") else 0
        if action not in ("prev", "next", "refresh"):
            raise ValueError("bad action")
        if action in ("prev", "next") and len(parts) != 3:
            raise ValueError("bad shape")
        if action == "refresh" and len(parts) != 2:
            raise ValueError("bad shape")
    except (ValueError, IndexError, AttributeError):
        await cb.answer("⚠️ Bad data", show_alert=True)
        return
    if cb.from_user.id != uid:
        await cb.answer("⚠️ Not yours", show_alert=True)
        return
    if action == "refresh":
        state.clear_quiz_list_cache(uid)
        await cb.answer()
        try:
            await cb.message.delete()
        except Exception:
            pass
        return
    cached = state.load_quiz_list_cache(uid)
    if not cached:
        await cb.answer("⚠️ Expired -- run /myquizzes again", show_alert=True)
        return
    new_page = page - 1 if action == "prev" else page + 1
    await _send_quiz_page(cb.message, cached["data"], new_page, uid)
    await cb.answer()


@ratelimit("default")
async def del_quiz_cmd(c: Client, m: Message) -> None:
    """/del <quiz_id> -- delete one of this creator's own quizzes."""
    args = m.text.split()
    if len(args) != 2:
        await m.reply("⚠️ Usage: `/del <quiz_id>`")
        return
    qid, uid = args[1], m.from_user.id
    repo = QuizRepository(get_db())
    quiz = await repo.get(qid)
    if not quiz:
        await m.reply("❌ Not found.")
        return
    if quiz["creator_id"] != uid:
        await m.reply("❌ Not authorized.")
        return
    await repo.delete(qid)
    await m.reply(f"🗑️ Deleted `{qid}`.")


async def delall_cmd(c: Client, m: Message) -> None:
    """/delall CONFIRM -- (owner only) delete every quiz on the platform.
    A bare /delall only previews the blast radius; the wipe requires an
    explicit CONFIRM so a typo can never nuke the platform."""
    if not _is_owner_or_admin(m.from_user.id):
        return
    try:
        repo = QuizRepository(get_db())
        quizzes = await repo.list_all(limit=1_000_000)
        parts = (m.text or "").strip().split()
        if len(parts) < 2 or parts[1].upper() != "CONFIRM":
            await m.reply(
                f"⚠️ This will permanently delete **{len(quizzes)}** quizzes.\n"
                f"Re-run as `/delall CONFIRM` to proceed."
            )
            return
        deleted, failed = 0, 0
        for q in quizzes:
            try:
                await repo.delete(q["qid"])
                deleted += 1
            except Exception:
                logger.exception("delall_cmd: failed to delete %s", q.get("qid"))
                failed += 1
        text = f"🗑️ Deleted {deleted} quizzes."
        if failed:
            text += f" {failed} failed."
        await m.reply(text)
    except Exception:
        logger.exception("delall_cmd failed")
        await m.reply("❌ Could not complete the wipe. Check the logs and retry.")


@ratelimit("default")
async def convertall_cmd(c: Client, m: Message) -> None:
    """/convertall CONFIRM -- (owner/admin only) convert every paid quiz
    to free. Paid->free is lossy (the original paid set is unrecoverable),
    so a bare /convertall only previews; CONFIRM executes."""
    if not _is_owner_or_admin(m.from_user.id):
        return
    try:
        repo = QuizRepository(get_db())
        quizzes = await repo.list_all(limit=1_000_000)
        paid = [q for q in quizzes if q.get("quiz_type") == "paid"]
        parts = (m.text or "").strip().split()
        if len(parts) < 2 or parts[1].upper() != "CONFIRM":
            await m.reply(
                f"⚠️ This will convert **{len(paid)}** paid quizzes to free.\n"
                f"Re-run as `/convertall CONFIRM` to proceed."
            )
            return
        status = await m.reply("🔄 Converting...")
        done, failed = 0, 0
        for q in paid:
            try:
                await repo.update_field(q["qid"], "quiz_type", "free")
                done += 1
            except Exception:
                logger.exception("convertall_cmd: failed on %s", q.get("qid"))
                failed += 1
        text = f"✅ Converted {done} quizzes to free."
        if failed:
            text += f" {failed} failed."
        await status.edit_text(text)
    except Exception:
        logger.exception("convertall_cmd failed")
        await m.reply("❌ Could not complete the conversion. Check the logs and retry.")


@ratelimit("default")
async def info_cmd(c: Client, m: Message) -> None:
    """/info <quiz_id> -- show the creator of a quiz."""
    args = m.text.split()
    if len(args) < 2:
        await m.reply("⚠️ Usage: `/info <quiz_id>`")
        return
    quiz = await QuizRepository(get_db()).get(args[1])
    if not quiz:
        await m.reply("❌ Not found.")
        return
    creator_id = quiz.get("creator_id", "Unknown")
    try:
        creator = await c.get_users(creator_id)
        name = creator.first_name
    except Exception:
        name = "Unknown"
    await m.reply(f"🆔 Creator: {name} -- ID: `{creator_id}`")


_SEARCH_PAGE_SIZE = 5


async def _send_search_page(c: Client, target, uid: int, term: str, offset: int) -> None:
    """Fetches ONE page straight from the database (offset-based) instead of
    paginating a pre-fetched, size-capped snapshot -- so /search can walk
    through an arbitrarily large result set, not just the first 50."""
    repo = QuizRepository(get_db())
    chunk = await repo.search(term, limit=_SEARCH_PAGE_SIZE, offset=offset)
    total = await repo.search_count(term)
    shown = min(offset + len(chunk), total)

    if not chunk:
        text = f"🔍 No quizzes found for **{term}**."
        try:
            await target.edit_text(text)
        except Exception:
            await target.reply(text)
        return

    runner_username = await get_runner_bot_username()
    lines, buttons = [], []
    for i, q in enumerate(chunk, start=offset + 1):
        name = q.get("quiz_name", "Unnamed")[:55]
        qid = q.get("qid", "")
        plays = q.get("total_participants", 0)
        quiz_type = q.get("quiz_type", "free")
        lines.append(f"**{i}.** {name}\n    ID: `{qid}` | {plays} plays | {quiz_type}")
        url = f"https://t.me/{runner_username}?start={qid}"
        buttons.append([InlineKeyboardButton(f"{i}. {q.get('quiz_name', '?')[:28]}", url=url)])

    nav = []
    if offset + len(chunk) < total:
        next_offset = offset + _SEARCH_PAGE_SIZE
        nav.append(InlineKeyboardButton(f"⬇️ Load more ({shown}/{total})", callback_data=f"srch_more_{uid}_{next_offset}"))
    if nav:
        buttons.append(nav)

    text = f"🔍 **Search: {term}** ({shown}/{total} shown)\n\n" + "\n\n".join(lines)
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    try:
        await target.edit_text(text[:4000], reply_markup=kb)
    except Exception:
        await target.reply(text[:4000], reply_markup=kb)


@ratelimit("default")
async def search_cmd(c: Client, m: Message) -> None:
    """/search <term> (or /quiz) -- search all publicly-indexed quizzes."""
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.reply(
            "🔍 **Quiz Search**\n\nUsage: `/search <keyword>`\nExample: `/search science gk`\n\n"
            "Shows 5 results at a time -- tap Load More for the next batch."
        )
        return
    term = args[1].strip()
    if len(term) < 2:
        await m.reply("⚠️ Search term too short (min 2 chars).")
        return

    uid = m.from_user.id
    status = await m.reply(f"🔍 Searching **{term}**...")
    state.search_state[uid] = {"term": term}
    await _send_search_page(c, status, uid, term, 0)


async def search_more_cb(c: Client, cb: CallbackQuery) -> None:
    """`srch_more_<uid>_<offset>` -- paginate /search results, re-querying
    the database for each page so there's no cap on total results."""
    parts = cb.data.split("_")
    uid, offset = int(parts[2]), int(parts[3])
    if cb.from_user.id != uid:
        await cb.answer("⚠️ Not yours", show_alert=True)
        return
    session = state.search_state.get(uid)
    if not session:
        await cb.answer("⚠️ Search expired -- run /search again", show_alert=True)
        return
    await _send_search_page(c, cb.message, uid, session["term"], offset)
    await cb.answer()


@ratelimit("default")
async def setpromo_cmd(c: Client, m: Message) -> None:
    """/setpromo <text> -- set (or clear with 'none') a promo message on
    every quiz this creator owns."""
    uid = m.from_user.id
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        await m.reply(
            "📢 **Set Promo on All Your Quizzes**\n\n"
            "Usage: `/setpromo Your promo message here`\n"
            "Remove promo: `/setpromo none`"
        )
        return
    promo_raw = args[1].strip()
    promo = None if promo_raw.lower() in ("none", "no", "remove", "clear") else promo_raw

    status = await m.reply("🔄 Updating all quizzes...")
    count = await QuizRepository(get_db()).set_promo_for_creator(uid, promo or "")
    if promo:
        await status.edit_text(f"✅ Promo set on **{count}** quizzes:\n\n_{promo}_")
    else:
        await status.edit_text(f"✅ Promo removed from **{count}** quizzes.")


async def ban_cmd(c: Client, m: Message) -> None:
    """/ban <quiz_id> -- (owner/admin only) ban the quiz's creator from the
    designated channel and delete all of their quizzes."""
    if not _is_owner_or_admin(m.from_user.id):
        return
    args = m.text.split()
    if len(args) < 2:
        await m.reply("⚠️ Usage: `/ban <quiz_id>`")
        return
    repo = QuizRepository(get_db())
    quiz = await repo.get(args[1])
    if not quiz:
        await m.reply("❌ Not found.")
        return
    creator_id = quiz.get("creator_id")
    if not creator_id:
        await m.reply("❌ No creator on this quiz.")
        return
    try:
        creator_quizzes = await repo.list_by_creator(creator_id)
        parts = (m.text or "").strip().split()
        if len(parts) < 3 or parts[2].upper() != "CONFIRM":
            await m.reply(
                f"⚠️ This will ban creator `{creator_id}` from the channel and "
                f"permanently delete their **{len(creator_quizzes)}** quizzes.\n"
                f"Re-run as `/ban {args[1]} CONFIRM` to proceed."
            )
            return
        if config.CHANNEL_ID:
            try:
                await c.ban_chat_member(config.CHANNEL_ID, creator_id)
                await m.reply(f"🚫 Banned {creator_id}.")
            except Exception as exc:
                await m.reply(f"⚠️ Ban failed: {exc}")
        else:
            await m.reply("ℹ️ CHANNEL_ID not set -- skipped channel ban.")
        deleted, failed = 0, 0
        for q in creator_quizzes:
            try:
                await repo.delete(q["qid"])
                deleted += 1
            except Exception:
                logger.exception("ban_cmd: failed to delete %s", q.get("qid"))
                failed += 1
        text = f"🗑️ Deleted {deleted} quizzes by {creator_id}."
        if failed:
            text += f" {failed} failed."
        await m.reply(text)
    except Exception:
        logger.exception("ban_cmd failed")
        await m.reply("❌ Could not complete the ban. Check the logs and retry.")


@ratelimit("default")
async def listquiz_cmd(c: Client, m: Message) -> None:
    """/listquiz -- (in a designated group chat) list every quiz on the
    platform, one message per quiz."""
    if not config.BOT_GROUP or m.chat.id != config.BOT_GROUP:
        return
    quizzes = await QuizRepository(get_db()).list_all(limit=200)
    if not quizzes:
        await m.reply("📋 No quizzes.")
        return
    runner_username = await get_runner_bot_username()
    for i, q in enumerate(quizzes):
        qid = q.get("qid")
        text = (
            f"📋 **Quiz {i + 1}**\n\n"
            f"Name: `{q.get('quiz_name', 'Unnamed')}`\n"
            f"Plays: `{q.get('total_participants', 0)}`\n"
            f"Timer: `{q.get('timer', 'N/A')}s`\n"
            f"ID: `{qid}`\n"
            f"Type: `{q.get('quiz_type', 'free')}`"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start", url=f"https://t.me/{runner_username}?start={qid}")]])
        await m.reply(text, reply_markup=kb)
        await asyncio.sleep(4)


def register(app: Client) -> None:
    app.on_message(filters.command("myquizzes") & filters.private)(myquizzes_cmd)
    app.on_callback_query(filters.regex(r"^(prev|next|refresh):"))(pagination_cb)
    app.on_message(filters.command("del") & filters.private)(del_quiz_cmd)
    app.on_message(filters.command("delall") & filters.private)(delall_cmd)
    app.on_message(filters.command("convertall") & filters.private)(convertall_cmd)
    app.on_message(filters.command("info") & filters.private)(info_cmd)
    app.on_message(filters.command(["search", "quiz"]) & filters.private)(search_cmd)
    app.on_callback_query(filters.regex(r"^srch_more_"))(search_more_cb)
    app.on_message(filters.command("setpromo") & filters.private)(setpromo_cmd)
    app.on_message(filters.command("ban") & filters.private)(ban_cmd)
    app.on_message(filters.command("listquiz"))(listquiz_cmd)
