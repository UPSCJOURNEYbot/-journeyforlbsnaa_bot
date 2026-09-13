"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional

from quizbot.shared.explanations import render_plain_text
from quizbot.analytics.runtime import build_miniapp_results
from quizbot.analytics.service import (
    SOURCE_MINIAPP,
    AnalyticsService,
)
from quizbot.database import (
    AttemptRepository,
    LeaderboardRepository,
    QuizRepository,
    get_db,
)
from quizbot.runner_bot.quiz_utils import (
    get_section_for_question,
    is_correct,
    section_marks,
    shuffle_options_multi_with_mapping,
)

Mode = Literal["practice", "exam"]


@dataclass
class SessionOverrides:
    """Player-chosen, attempt-scoped overrides from the mini app's
    start-options screen. Any field left None falls back to the quiz's own
    saved setting. Applying these NEVER writes back to the quiz record --
    they only shape the one in-memory session created for this attempt, so
    the creator's saved config (and anyone else's attempts) are untouched.
    """

    timer: Optional[int] = None
    negative_marks: Optional[float] = None
    shuffle_questions: Optional[bool] = None
    shuffle_options: Optional[bool] = None

# In-memory play sessions, keyed by attempt_id. Holds the per-question
# option order (post-shuffle) and per-question timing so scoring/section
# math is correct without trusting anything the client sends back except
# "which option index(es) did they pick". None of this is sensitive on its
# own (no correct answers stored in the shuffle map), so keeping it
# in-memory (not persisted) is an acceptable tradeoff, same as the bots'
# own `session_mgr` -- a play session that outlives a server restart simply
# has to be restarted by the player, same as an in-progress poll quiz would.
_sessions: dict[str, dict[str, Any]] = {}


async def start_session(
    user_id: int,
    username: str,
    qid: str,
    mode: Mode,
    overrides: Optional["SessionOverrides"] = None,
) -> Optional[dict]:
    """Create a new play session for (user_id, qid). Returns the session's
    public state (attempt_id, mode, total_questions, quiz_name) or None if
    the quiz doesn't exist / has no questions.

    `overrides`, when given, lets the player replace the quiz's saved
    timer/negative-marking/shuffle settings for THIS attempt only -- the
    quiz record in the database is never modified. A quiz with sections
    keeps its per-section marks/timers as configured (overriding those
    individually isn't offered by the start-options screen); the top-level
    override still applies to any question outside a section.
    """
    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz or not quiz.get("questions"):
        return None

    ov = overrides or SessionOverrides()

    attempt_repo = AttemptRepository(get_db())
    attempt = await attempt_repo.start(
        user_id, qid, quiz["quiz_name"], len(quiz["questions"]),
        source=SOURCE_MINIAPP, quiz_persisted=True,
    )

    # Effective per-attempt settings: player's choice if given, else the
    # quiz's own saved value. Stored on the session (not the quiz) so
    # scoring/timing for this attempt uses them without touching anyone
    # else's view of the quiz.
    effective_timer = ov.timer if ov.timer is not None else quiz.get("timer", 60)
    effective_negative_marks = (
        ov.negative_marks if ov.negative_marks is not None else quiz.get("negative_marks", 0)
    )
    do_shuffle_q = ov.shuffle_questions if ov.shuffle_questions is not None else bool(quiz.get("shuffle_questions"))
    do_shuffle_o = ov.shuffle_options if ov.shuffle_options is not None else bool(quiz.get("shuffle_options"))

    # Pre-compute the (possibly shuffled) option order for every question
    # up front, once, so repeated fetches of the same question are stable
    # within a session and shuffle correctness doesn't depend on request
    # ordering/races.
    order = list(range(len(quiz["questions"])))
    if do_shuffle_q:
        import random

        random.shuffle(order)

    per_question: dict[int, dict] = {}
    for q_idx in order:
        q = quiz["questions"][q_idx]
        options = list(q["options"])
        correct = q.get("correct_option_id")
        correct_ids = correct if isinstance(correct, list) else [correct]
        display_order: Optional[list[int]] = None
        if do_shuffle_o:
            options, correct_ids, display_order = shuffle_options_multi_with_mapping(
                options, correct_ids, 0
            )
        per_question[q_idx] = {
            "options": options,
            "correct_ids": correct_ids,
            # Phase B: display->canonical option permutation (None when
            # options weren't shuffled) for canonical analytics records.
            "display_order": display_order,
        }

    _sessions[attempt["attempt_id"]] = {
        "user_id": user_id,
        "username": username,
        "qid": qid,
        "quiz": quiz,
        "mode": mode,
        "order": order,
        "per_question": per_question,
        "answers": {},  # q_idx -> {"selected": [...], "correct": bool, "time_taken": float}
        "sent_at": {},  # q_idx -> timestamp when the question was served
        "created_at": time.time(),
        # Attempt-scoped effective settings (player overrides already
        # resolved against quiz defaults). submit_answer() uses
        # effective_negative_marks in place of the quiz's own value for any
        # question that isn't inside a section (sections keep their own
        # configured marks, same as before).
        "effective_timer": effective_timer,
        "effective_negative_marks": effective_negative_marks,
    }

    return {
        "attempt_id": attempt["attempt_id"],
        "mode": mode,
        "quiz_name": quiz["quiz_name"],
        "total_questions": len(quiz["questions"]),
        "timer": effective_timer,
        "correct_marks": quiz.get("correct_marks", 1),
        "negative_marks": effective_negative_marks,
        "has_sections": bool(quiz.get("sections")),
    }


def get_session(attempt_id: str) -> Optional[dict]:
    return _sessions.get(attempt_id)


def public_question(session: dict, position: int) -> Optional[dict]:
    """Return question `position` (0-based, in play order) with NO correct
    answer / explanation -- safe to encrypt and send to the client."""
    order = session["order"]
    if position < 0 or position >= len(order):
        return None
    q_idx = order[position]
    q = session["quiz"]["questions"][q_idx]
    pq = session["per_question"][q_idx]
    session["sent_at"][q_idx] = time.time()
    return {
        "position": position,
        "total": len(order),
        "question": q.get("question", ""),
        "options": pq["options"],
        "reply_text": q.get("reply_text"),
        "is_multi": isinstance(q.get("correct_option_id"), list),
    }


def submit_answer(session: dict, position: int, selected: list[int]) -> Optional[dict]:
    """Record and score an answer for question `position`. Returns
    {correct, correct_options, explanation, score_delta} -- safe to reveal
    only now, since the player has already committed their choice. Returns
    None if the position is invalid or was already answered (no
    double-scoring by resubmission)."""
    order = session["order"]
    if position < 0 or position >= len(order):
        return None
    q_idx = order[position]
    if q_idx in session["answers"]:
        return None  # already answered -- ignore silently, don't rescoring

    quiz = session["quiz"]
    q = quiz["questions"][q_idx]
    pq = session["per_question"][q_idx]
    correct_ids = pq["correct_ids"]

    sent_at = session["sent_at"].get(q_idx, time.time())
    time_taken = max(0.0, time.time() - sent_at)

    correct = is_correct(selected, correct_ids if len(correct_ids) > 1 else correct_ids[0])

    sections = quiz.get("sections") or []
    section = get_section_for_question(sections, q_idx)
    # A section's own configured marks always win (matches the classic
    # poll-based bot's behavior). Outside a section, use this attempt's
    # effective negative-marking value, which is the player's override if
    # they set one on the start-options screen, else the quiz's saved
    # default -- never the raw quiz value directly, so an override actually
    # takes effect.
    correct_mark, neg_mark = section_marks(
        section, quiz.get("correct_marks", 1), session.get("effective_negative_marks", quiz.get("negative_marks", 0))
    )
    score_delta = correct_mark if correct else -neg_mark

    session["answers"][q_idx] = {
        "selected": selected,
        "correct": correct,
        "score_delta": score_delta,
        "time_taken": time_taken,
    }

    # Phase F: compose structured companions into the same single
    # client-facing string (legacy explanations remain byte-identical).
    explanation = render_plain_text(
        q, display_order=pq.get("display_order")) or None
    return {
        "correct": correct,
        "correct_options": correct_ids,
        "explanation": explanation,
        "score_delta": round(score_delta, 4),
    }


async def complete_session(attempt_id: str) -> Optional[dict]:
    """Finalize the attempt: compute total score/time, write to
    quiz_attempts + leaderboard (first-attempt-only, same as the bots),
    update question_wrong_stats and user_mistakes, and clear the in-memory
    session. Returns a results summary safe to encrypt and return."""
    session = _sessions.pop(attempt_id, None)
    if session is None:
        return None

    answers = session["answers"]
    total_score = sum(a["score_delta"] for a in answers.values())
    total_time = sum(a["time_taken"] for a in answers.values())
    correct_count = sum(1 for a in answers.values() if a["correct"])
    wrong_count = len(answers) - correct_count

    qid = session["qid"]
    quiz = session["quiz"]

    # Phase B: canonical, shuffle-safe per-question results built from the
    # in-memory live session (the previous implementation discarded this
    # answer map and persisted only aggregate counts).
    question_results = build_miniapp_results(
        session["order"], session["per_question"], answers
    )

    # Single canonical write path shared with the poll-based bots: attempt
    # row (now WITH the answer map + question_results), question events,
    # non-destructive mistakes, question stats, leaderboard completion and
    # participant counter. Idempotent if the boundary is ever replayed.
    analytics = AnalyticsService(get_db())
    await analytics.record_completion(
        user_id=session["user_id"],
        attempt_id=attempt_id,
        qid=qid,
        quiz_name=quiz.get("quiz_name", qid),
        question_results=question_results,
        source=SOURCE_MINIAPP,
        quiz_persisted=True,
        questions=quiz.get("questions", []),
        sections=quiz.get("sections", []),
        score=float(total_score),
        correct=int(correct_count),
        wrong=int(wrong_count),
        total_time=float(total_time),
        current_question=len(session["order"]),
        username=session.get("username"),
    )

    rank_info = await LeaderboardRepository(get_db()).user_rank(qid, session["user_id"])

    review = None
    if session["mode"] == "exam":
        review = []
        for position, q_idx in enumerate(session["order"]):
            q = session["quiz"]["questions"][q_idx]
            pq = session["per_question"][q_idx]
            ans = answers.get(q_idx)
            review.append({
                "position": position,
                "question": q.get("question", ""),
                "options": pq["options"],
                "correct_options": pq["correct_ids"],
                "selected": ans["selected"] if ans else [],
                "correct": ans["correct"] if ans else False,
                "explanation": render_plain_text(
                    q, display_order=pq.get("display_order")) or None,
            })

    return {
        "score": round(total_score, 4),
        "correct_count": correct_count,
        "wrong_count": wrong_count,
        "unanswered": len(session["order"]) - len(answers),
        "total_questions": len(session["order"]),
        "total_time": round(total_time, 1),
        "rank": rank_info.get("rank") if rank_info else None,
        "review": review,
    }
