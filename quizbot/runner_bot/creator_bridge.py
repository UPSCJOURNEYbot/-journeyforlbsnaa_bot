"""Bridge the legacy Pyrogram Creator handlers into the single PTB poller.

Telegram allows only one long-polling consumer per bot token. The project
historically had a Pyrogram Creator bot and a PTB Runner bot. This module
keeps the Creator implementation intact while adapting PTB updates to the
small Pyrogram-like surface those handlers use, so ONE bot token can serve
both roles through ONE polling client.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
from types import SimpleNamespace
from typing import Any

from telegram import InlineKeyboardButton as PTBInlineKeyboardButton
from telegram import InlineKeyboardMarkup as PTBInlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from telegram.ext.filters import BaseFilter

from quizbot.shared import config


# ---------------------------------------------------------------------------
# Telegram object adapters
# ---------------------------------------------------------------------------

def _markup(markup):
    if markup is None:
        return None
    rows = []
    for row in getattr(markup, "inline_keyboard", []) or []:
        out = []
        for b in row:
            kwargs = {}
            if getattr(b, "url", None):
                kwargs["url"] = b.url
            elif getattr(b, "callback_data", None):
                kwargs["callback_data"] = b.callback_data
            elif getattr(b, "switch_inline_query", None) is not None:
                kwargs["switch_inline_query"] = b.switch_inline_query
            elif getattr(b, "switch_inline_query_current_chat", None) is not None:
                kwargs["switch_inline_query_current_chat"] = b.switch_inline_query_current_chat
            elif getattr(b, "web_app", None):
                from telegram import WebAppInfo
                wa = b.web_app
                kwargs["web_app"] = WebAppInfo(url=getattr(wa, "url", ""))
            elif getattr(b, "login_url", None):
                kwargs["login_url"] = b.login_url
            else:
                kwargs["callback_data"] = getattr(b, "callback_data", "") or "noop"
            out.append(PTBInlineKeyboardButton(getattr(b, "text", "Button"), **kwargs))
        rows.append(out)
    return PTBInlineKeyboardMarkup(rows)


class _SentMessage:
    def __init__(self, bot, message):
        self._bot = bot
        self._message = message
        self.id = message.message_id
        self.message_id = message.message_id
        self.chat = message.chat
        self.from_user = message.from_user
        self.text = message.text
        self.caption = message.caption
        self.reply_markup = message.reply_markup
        self.document = message.document
        # PTB exposes photos as a list of PhotoSize objects; Pyrogram's
        # Message.photo surface used by the Creator code is a single object.
        self.photo = message.photo[-1] if message.photo else None
        self.video = message.video
        self.poll = message.poll

    async def edit_text(self, text, reply_markup=None, **kwargs):
        msg = await self._bot.edit_message_text(
            chat_id=self._message.chat_id,
            message_id=self._message.message_id,
            text=text,
            reply_markup=_markup(reply_markup),
            parse_mode=ParseMode.MARKDOWN,
            **{k: v for k, v in kwargs.items() if k in {"disable_web_page_preview"}},
        )
        self._message = msg
        self.text = getattr(msg, "text", None)
        self.caption = getattr(msg, "caption", None)
        self.reply_markup = getattr(msg, "reply_markup", None)
        return self

    async def reply(self, text, reply_markup=None, **kwargs):
        msg = await self._bot.send_message(
            chat_id=self._message.chat_id, text=text, reply_markup=_markup(reply_markup),
            parse_mode=ParseMode.MARKDOWN,
        )
        return _SentMessage(self._bot, msg)

    async def edit_caption(self, caption, reply_markup=None, **kwargs):
        msg = await self._bot.edit_message_caption(
            chat_id=self._message.chat_id, message_id=self._message.message_id,
            caption=caption, reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN,
        )
        self._message = msg
        return self

    async def reply_document(self, document, file_name=None, caption=None, reply_markup=None, **kwargs):
        if isinstance(document, io.BytesIO):
            document.seek(0)
        msg = await self._bot.send_document(
            chat_id=self._message.chat_id, document=document, filename=file_name,
            caption=caption, reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN,
        )
        return _SentMessage(self._bot, msg)

    async def delete(self):
        return await self._bot.delete_message(self._message.chat_id, self._message.message_id)

    async def copy(self, chat_id):
        return await self._bot.copy_message(
            chat_id=chat_id,
            from_chat_id=self._message.chat_id,
            message_id=self._message.message_id,
        )


class BridgeMessage:
    """Pyrogram-shaped message backed by python-telegram-bot."""
    def __init__(self, bot, message):
        self._bot = bot
        self._message = message
        self.id = message.message_id
        self.chat = message.chat
        self.from_user = message.from_user
        self.text = message.text
        self.caption = message.caption
        self.reply_markup = message.reply_markup
        self.reply_to_message = BridgeMessage(bot, message.reply_to_message) if message.reply_to_message else None
        self.document = message.document
        # PTB exposes photos as a list of PhotoSize objects; Pyrogram's
        # Message.photo surface used by the Creator code is a single object.
        self.photo = message.photo[-1] if message.photo else None
        self.video = message.video
        self.poll = message.poll
        self.message_id = message.message_id
        self.date = message.date
        self.command = (message.text or "").split()

    async def edit_text(self, text, reply_markup=None, **kwargs):
        msg = await self._bot.edit_message_text(
            chat_id=self.chat.id, message_id=self.id, text=text,
            reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN,
        )
        self._message = msg
        self.text = getattr(msg, "text", None)
        self.reply_markup = getattr(msg, "reply_markup", None)
        return self

    async def edit_caption(self, caption, reply_markup=None, **kwargs):
        msg = await self._bot.edit_message_caption(
            chat_id=self.chat.id, message_id=self.id, caption=caption,
            reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN,
        )
        self._message = msg
        self.caption = getattr(msg, "caption", None)
        self.reply_markup = getattr(msg, "reply_markup", None)
        return self

    async def reply(self, text, reply_markup=None, **kwargs):
        msg = await self._bot.send_message(
            chat_id=self.chat.id,
            text=text,
            reply_markup=_markup(reply_markup),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=kwargs.get("disable_web_page_preview", False),
        )
        return _SentMessage(self._bot, msg)

    async def reply_text(self, text, **kwargs):
        return await self.reply(text, **kwargs)

    async def reply_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        msg = await self._bot.send_photo(
            chat_id=self.chat.id,
            photo=photo,
            caption=caption,
            reply_markup=_markup(reply_markup),
            parse_mode=ParseMode.MARKDOWN,
        )
        return _SentMessage(self._bot, msg)

    async def reply_document(self, document, file_name=None, caption=None, reply_markup=None, **kwargs):
        if isinstance(document, io.BytesIO):
            document.seek(0)
        msg = await self._bot.send_document(
            chat_id=self.chat.id,
            document=document,
            filename=file_name,
            caption=caption,
            reply_markup=_markup(reply_markup),
            parse_mode=ParseMode.MARKDOWN,
        )
        return _SentMessage(self._bot, msg)

    async def delete(self):
        return await self._bot.delete_message(self.chat.id, self.id)

    async def copy(self, chat_id):
        return await self._bot.copy_message(chat_id=chat_id, from_chat_id=self.chat.id, message_id=self.id)


class BridgeCallback:
    def __init__(self, bot, query):
        self._bot = bot
        self._query = query
        self.id = query.id
        self.data = query.data
        self.from_user = query.from_user
        self.message = BridgeMessage(bot, query.message) if query.message else None

    async def answer(self, text=None, show_alert=False, **kwargs):
        return await self._query.answer(text=text, show_alert=show_alert)


class BridgeInlineQuery:
    def __init__(self, bot, query):
        self._bot = bot
        self._query = query
        self.id = query.id
        self.query = query.query
        self.from_user = query.from_user

    async def answer(self, results, **kwargs):
        # Pyrogram result objects are converted to PTB InlineQueryResultArticle.
        from telegram import InlineQueryResultArticle, InputTextMessageContent
        out = []
        for r in results:
            content = getattr(r, "input_message_content", None)
            text = getattr(content, "message_text", "") if content else ""
            out.append(InlineQueryResultArticle(
                id=str(getattr(r, "id", "0")),
                title=getattr(r, "title", "Result"),
                description=getattr(r, "description", None),
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                ),
                reply_markup=_markup(getattr(r, "reply_markup", None)),
            ))
        return await self._query.answer(out, cache_time=kwargs.get("cache_time", 0), is_personal=True)


class BridgeClient:
    def __init__(self, bot):
        self.bot = bot

    async def get_me(self):
        return await self.bot.get_me()

    async def get_users(self, user_id):
        return await self.bot.get_chat(user_id)

    async def get_chat_member(self, chat_id, user_id):
        return await self.bot.get_chat_member(chat_id, user_id)

    async def ban_chat_member(self, chat_id, user_id):
        return await self.bot.ban_chat_member(chat_id, user_id)

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        return await self.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=_markup(reply_markup),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=kwargs.get("disable_web_page_preview", False),
        )

    async def send_photo(self, chat_id, photo, caption="", reply_markup=None, **kwargs):
        return await self.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption,
                                         reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN)

    async def send_video(self, chat_id, video, caption="", reply_markup=None, **kwargs):
        return await self.bot.send_video(chat_id=chat_id, video=video, caption=caption,
                                         reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN)

    async def send_document(self, chat_id, document, file_name=None, caption="", reply_markup=None, **kwargs):
        return await self.bot.send_document(chat_id=chat_id, document=document, filename=file_name,
                                            caption=caption, reply_markup=_markup(reply_markup), parse_mode=ParseMode.MARKDOWN)

    async def copy_message(self, chat_id, from_chat_id, message_id):
        return await self.bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)

    async def download_media(self, file_id, in_memory=True):
        f = await self.bot.get_file(file_id)
        data = await f.download_as_bytearray()
        return io.BytesIO(bytes(data))


# PTB update -> creator handler adapter.
def _wrap_message(update, context):
    return BridgeMessage(context.bot, update.effective_message)


def _wrap_callback(update, context):
    return BridgeCallback(context.bot, update.callback_query)


def _wrap_inline(update, context):
    return BridgeInlineQuery(context.bot, update.inline_query)


def _call(fn, obj, context):
    import inspect
    async def runner(update, context):
        try:
            await fn(BridgeClient(context.bot), obj(update, context))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Creator bridge handler failed: %s", getattr(fn, "__name__", fn))
    return runner


async def _creator_callback_router(update, context):
    import logging
    logger = logging.getLogger(__name__)
    from quizbot.creator_bot import state
    from quizbot.creator_bot.handlers import batches, quiz_creation, quiz_editing, quiz_management, settings
    data = update.callback_query.data or ""
    uid = update.effective_user.id
    cb = BridgeCallback(context.bot, update.callback_query)
    client = BridgeClient(context.bot)

    async def run():
        if data.startswith("cws_"):
            return await quiz_creation.creation_wizard_cb(client, cb)
        if data.startswith("bat_"):
            return await batches.batch_cb(client, cb)
        if data.startswith("stg_"):
            return await settings.settings_cb(client, cb)
        if data.startswith(("prev:", "next:", "refresh:")):
            return await quiz_management.pagination_cb(client, cb)
        if data.startswith("srch_more_"):
            return await quiz_management.search_more_cb(client, cb)
        # /edit tree callbacks are meaningful only while that user is editing.
        edit_prefixes = ("main_", "set_", "qmgr_", "view_", "next_", "prev_", "eq_", "replace_",
                         "delq_", "delrange_", "ename_", "etimer_", "etype_", "eneg_", "add_",
                         "exp_", "shuf_", "tshufq_", "tshufo_", "perms_", "addperm_", "remperm_",
                         "epromo_", "close_", "page_info")
        if uid in state.edit_sessions and data.startswith(edit_prefixes):
            return await quiz_editing.edit_tree_cb(client, cb)
        return None

    try:
        await run()
    except Exception:
        logger.exception("Creator callback failed: %s", data)
        try:
            await cb.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


async def _creator_inline(update, context):
    from quizbot.creator_bot.handlers.inline import inline_query_handler
    await inline_query_handler(BridgeClient(context.bot), BridgeInlineQuery(context.bot, update.inline_query))


class _CreatorStateFilter(BaseFilter):
    """Match only messages belonging to an active Creator workflow.

    This is critical in the single-bot architecture: the bridge must NOT
    consume ordinary text/documents, otherwise Runner features such as
    /podcast can be shadowed.
    """

    def filter(self, update):
        user = getattr(update, "effective_user", None)
        if not user:
            return False
        from quizbot.creator_bot import state
        uid = user.id
        return (
            uid in state.quiz_creation
            or uid in state.batch_sessions
            or uid in state.edit_sessions
        )


async def _creator_message_router(update, context):
    """Route only active Creator-workflow messages."""
    if not update.effective_message or not update.effective_user:
        return
    from quizbot.creator_bot import state
    uid = update.effective_user.id
    msg = update.effective_message
    bridge = BridgeMessage(context.bot, msg)
    client = BridgeClient(context.bot)

    # Batch/edit sessions must receive their next text input.
    if uid in state.batch_sessions and msg.text and not msg.text.startswith("/"):
        from quizbot.creator_bot.handlers.batches import batch_input
        await batch_input(client, bridge)
        return
    if uid in state.edit_sessions and msg.text and not msg.text.startswith("/"):
        from quizbot.creator_bot.handlers.quiz_editing import handle_edit_text_input
        await handle_edit_text_input(client, bridge)
        return

    # Creation wizard accepts documents, photos, polls and free text.
    if uid in state.quiz_creation and msg.chat.type == "private":
        from quizbot.creator_bot.handlers.quiz_creation import handle_document, handle_photo, handle_creation_message
        if msg.document:
            await handle_document(client, bridge)
            return
        if msg.photo:
            await handle_photo(client, bridge)
            return
        if msg.text and not msg.text.startswith("/"):
            await handle_creation_message(client, bridge)
            return
        if msg.poll:
            await handle_creation_message(client, bridge)
            return


def register_creator_bridge(application):
    """Register all Creator commands/callbacks on the single PTB Application."""
    from quizbot.creator_bot.handlers import admin, ai_keys, auth, batches, quiz_creation, quiz_editing, quiz_management, reports, settings

    # Commands that are shared by Runner have intentionally been left to Runner.
    command_map = {
        "help": admin.help_cmd,
        "features": admin.features_cmd,
        "limit": admin.limit_cmd,
        "gcast": admin.gcast_cmd,
        "stopcast": admin.stopcast_cmd,
        "statses": admin.statses_cmd,
        "testapi": admin.testapi_cmd,
        "leaders": admin.leaders_cmd,
        "aspirants": admin.leaders_cmd,
        "setkey": ai_keys.setkey_cmd,
        "mykeys": ai_keys.mykeys_cmd,
        "delkey": ai_keys.delkey_cmd,
        "add": auth.add_auth_cmd,
        "rem": auth.rem_auth_cmd,
        "remall": auth.remall_auth_cmd,
        "auth": auth.auth_cmd,
        "removeuser": auth.removeuser_cmd,
        "batch": batches.batch_cmd,
        "createbatch": batches.createbatch_cmd,
        "searchbatch": batches.searchbatch_cmd,
        "create": quiz_creation.create_cmd,
        "done": quiz_creation.done_cmd,
        "cancel": quiz_creation.cancel_cmd,
        "edit": quiz_editing.edit_cmd,
        "stopedit": quiz_editing.stopedit_cmd,
        "myquizzes": quiz_management.myquizzes_cmd,
        "del": quiz_management.del_quiz_cmd,
        "delall": quiz_management.delall_cmd,
        "convertall": quiz_management.convertall_cmd,
        "info": quiz_management.info_cmd,
        "search": quiz_management.search_cmd,
        "quiz": quiz_management.search_cmd,
        "setpromo": quiz_management.setpromo_cmd,
        "ban": quiz_management.ban_cmd,
        "listquiz": quiz_management.listquiz_cmd,
        "whtml": reports.whtml_cmd,
        "testseries": reports.testseries_cmd,
        "tsr": reports.testseries_cmd,
        "mocktest": reports.testseries_cmd,
        "settings": settings.settings_cmd,
        "remove": settings.remove_words_cmd,
        "mywords": settings.mywords_cmd,
        "clearlist": settings.clearlist_cmd,
    }
    # Legacy Pyrogram registration marks /testseries (+aliases) private-only
    # (see creator_bot/handlers/reports.py::register). Enforce the same here
    # so group chats can never trigger (or receive) someone's test-series PDF.
    _private_only = frozenset({"testseries", "tsr", "mocktest"})
    for cmd, fn in command_map.items():
        if cmd in _private_only:
            application.add_handler(CommandHandler(
                cmd, _call(fn, _wrap_message, application),
                filters=filters.ChatType.PRIVATE), group=0)
        else:
            application.add_handler(CommandHandler(cmd, _call(fn, _wrap_message, application)), group=0)

    # Creator callbacks must have priority over all Runner callback handlers.
    # In PTB only the first matching handler in a group runs, so keep these in
    # a negative-priority group. This is especially important for cws_*
    # (the /create settings wizard), which must never be swallowed by another
    # callback handler.
    application.add_handler(
        CallbackQueryHandler(_creator_callback_router, pattern=r"^cws_"),
        group=-2,
    )
    application.add_handler(
        CallbackQueryHandler(_creator_callback_router,
            pattern=r"^(bat_|stg_|srch_more_|(?:prev|next|refresh):)"),
        group=-2,
    )
    application.add_handler(
        CallbackQueryHandler(_creator_callback_router,
            pattern=r"^(?:main_|set_|qmgr_|view_|next_|prev_|eq_|replace_|delq_|delrange_|ename_|etimer_|etype_|eneg_|add_|exp_|shuf_|tshufq_|tshufo_|perms_|addperm_|remperm_|epromo_|close_|page_info)"),
        group=-2,
    )
    application.add_handler(InlineQueryHandler(_creator_inline), group=0)
    # Only intercept messages when the user is actually inside a Creator
    # workflow. Otherwise Runner message handlers (podcast/pdfquiz/etc.) must
    # be allowed to process the update normally. Group -1 gives active Creator
    # sessions priority over Runner's generic text handlers.
    application.add_handler(
        MessageHandler(_CreatorStateFilter(), _creator_message_router),
        group=-1,
    )
