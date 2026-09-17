"""Phase H: one launch helper for every personal practice command.

``/revise``, ``/pyq`` and ``/buildtest`` all end the same way: hand a bounded
list of questions to the EXISTING private (DM) quiz engine. Doing that
identically in three places is how "one of them quietly grew its own timer /
provenance handling" bugs start, so the launch lives here once:

* the canonical ``analytics_source="dm"`` marker is always set, so completion
  flows through the single Phase B/C boundary (analytics + XP + streak);
* ``_revision_origins`` provenance (Phase D fold-back) is attached whenever a
  builder supplies it;
* a running session (or an in-flight setup) is detected BEFORE the questions
  are fetched, so a user mid-quiz gets one clear line instead of a clobbered
  session.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Optional

from telegram.ext import ContextTypes

from .state import pending_setup_live, session_mgr
from .telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

#: Seconds per question for a personal practice session.
PRACTICE_TIMER_SECONDS = 45

BUSY_TEXT = ("⚠️ A quiz is already active here. Send /stop first, then start "
             "this practice again.")


def busy(user_id: int) -> bool:
    """True when a session or setup wizard would be clobbered."""
    try:
        return session_mgr.get(user_id) is not None or pending_setup_live(user_id)
    except Exception:  # pragma: no cover - state lookups are defensive
        return False


def build_quiz_obj(
    user_id: int, questions: list[dict], *, quiz_name: str,
    timer: int = PRACTICE_TIMER_SECONDS, show_explanation: bool = True,
    origins_by_index: Optional[list] = None, marker: Optional[str] = None,
) -> dict:
    """The ad-hoc DM quiz document (free, unshuffled questions, shuffled
    options -- option shuffling is display-mapped back at the result
    boundary, so canonical indices stay valid for provenance)."""
    if origins_by_index:
        for question, origins in zip(questions, origins_by_index):
            if origins:
                question["_revision_origins"] = origins
    qid = f"PH{int(time.time() * 1000)}{secrets.token_hex(2)}"
    quiz_obj: dict[str, Any] = {
        "question_set_id": qid,
        "quiz_name": quiz_name,
        "questions": questions,
        "timer": int(timer),
        "negative_marking": 0,
        "correct_mark": 1,
        "shuffle_options": True,
        "shuffle_options_count": 0,
        "shuffle": False,
        "show_explanation": bool(show_explanation),
        "sections": [],
        "promo_message": None,
        "quiz_type": "free",
        "creator_id": user_id,
        # Personal practice is a DM quiz: canonical analytics + XP/streak.
        "analytics_source": "dm",
    }
    if marker:
        quiz_obj[marker] = True
    return quiz_obj


async def launch(
    ctx: ContextTypes.DEFAULT_TYPE, user_id: int, questions: list[dict], *,
    quiz_name: str, timer: int = PRACTICE_TIMER_SECONDS,
    show_explanation: bool = True,
    origins_by_index: Optional[list] = None, marker: Optional[str] = None,
) -> tuple[bool, str]:
    """Start the session. Returns ``(started, status_text)``.

    Never raises: a failure is logged and reported to the caller as a safe
    line, so the command handler cannot crash the update loop.
    """
    if not questions:
        return False, "ℹ️ Nothing to practise right now."
    quiz_obj = build_quiz_obj(
        user_id, questions, quiz_name=quiz_name, timer=timer,
        show_explanation=show_explanation, origins_by_index=origins_by_index,
        marker=marker,
    )
    try:
        from .handlers.quiz_play import start_private_quiz
        await start_private_quiz(user_id, ctx, questions, quiz_obj,
                                 quiz_obj["question_set_id"])
        return True, ""
    except Exception:
        logger.exception("practice launch failed for user=%s", user_id)
        try:
            await safe_send_message(
                ctx, user_id, "❌ Could not start the session. Please try again.")
        except Exception:
            pass
        return False, ""
