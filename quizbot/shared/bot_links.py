"""Helpers for quiz links in the single-bot architecture.

Creator and Runner are two logical roles inside one Telegram bot. Quiz
play/deep-links therefore resolve to the same bot username used by Runner.
"""
from __future__ import annotations

import logging

import aiohttp

from . import config

logger = logging.getLogger(__name__)

_runner_username: str | None = None


async def get_runner_bot_username() -> str:
    """Return the single bot username, resolved from the configured token."""
    global _runner_username
    if _runner_username:
        return _runner_username
    token = config.RUNNER_BOT_TOKEN
    if not token:
        raise RuntimeError("RUNNER_BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{token}/getMe"
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            data = await response.json(content_type=None)
    if not data.get("ok") or not data.get("result", {}).get("username"):
        raise RuntimeError(f"Could not resolve Runner Bot username: {data}")
    _runner_username = data["result"]["username"]
    logger.info("Runner Bot resolved as @%s", _runner_username)
    return _runner_username


async def runner_start_url(qid: str) -> str:
    username = await get_runner_bot_username()
    return f"https://t.me/{username}?start={qid}"


async def runner_group_url(qid: str) -> str:
    username = await get_runner_bot_username()
    return f"https://t.me/{username}?startgroup={qid}"
