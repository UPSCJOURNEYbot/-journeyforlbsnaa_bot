"""Pure runtime -> canonical analytics mappers.

These helpers run AT THE RESULT BOUNDARY and consume what the live quiz
scorer already resolved. They never re-open a quiz document to recompute
correctness: the live session is the source of truth. Their only job is to
translate display-coordinate poll answers into stable canonical coordinates
and to emit a uniform question-result record across every execution path
(group, DM, Mini App; saved and ad-hoc quizzes).

A "question result" is::

    {
        "q_index": int,                 # stable question index (canonical order)
        "selected": [int, ...],         # canonical selected option ids ([] if skipped)
        "correct_option": [int, ...],   # canonical correct option ids
        "outcome": "correct"|"incorrect"|"skipped",
        "time_taken": float | None,     # seconds, only where genuinely measured
    }

Option shuffle handling
-----------------------
When options are shuffled, the live session stores a permutation
``display_order`` where ``display_order[display_position] = canonical_index``.
Telegram answers always arrive in display coordinates; these helpers convert
them back to canonical indices once, at the boundary, so analytics never
compares a shuffled selection against the unshuffled answer key.
"""

from __future__ import annotations

from typing import Any, Optional

from .metadata import OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED, as_int_list


def _is_correct(selected: list[int], correct: list[int]) -> bool:
    """Exact-set match, mirroring ``quiz_utils.is_correct`` semantics
    (multi-correct questions require the exact set)."""
    return set(int(i) for i in selected) == set(int(i) for i in correct)


def to_canonical(display_ids: Any, display_order: Optional[list[int]]) -> list[int]:
    """Map display-coordinate option ids to canonical option ids.

    With no permutation recorded (legacy sessions or unshuffled questions)
    display and canonical coordinates are identical.
    """
    ids = as_int_list(display_ids)
    if not display_order:
        return ids
    canonical: list[int] = []
    for pos in ids:
        if 0 <= pos < len(display_order):
            canonical.append(int(display_order[pos]))
    return sorted(set(canonical))


def _resolve(display_selected: list[int], display_correct: list[int],
             display_order: Optional[list[int]]) -> tuple[list[int], list[int], str]:
    selected_canonical = to_canonical(display_selected, display_order)
    correct_canonical = to_canonical(display_correct, display_order)
    # Outcome is decided by the live scorer (display coordinates), never by
    # re-comparing canonical ids against a possibly-edited stored key.
    outcome = OUTCOME_CORRECT if _is_correct(display_selected, display_correct) else OUTCOME_INCORRECT
    return selected_canonical, correct_canonical, outcome


def build_question_results(
    polls: dict[str, dict],
    participant_answers: dict[str, dict],
) -> list[dict]:
    """Build canonical question results for one participant from a finished
    poll-based session (group or DM).

    ``polls``        : session poll map as the runner built it, keyed by
                       Telegram poll id. Each entry must carry
                       ``question_index``, ``correct_option`` (display
                       coordinates) and ``sent_time``; ``display_order`` is
                       present whenever options were shuffled.
    ``participant_answers`` : that participant's answers keyed by poll id,
                       each ``{"option": [display ids], "time": float}``.

    Delivered-but-unanswered questions are emitted as ``skipped`` (they are
    never counted as incorrect). If a question was delivered more than once
    (re-send), the answered delivery wins.
    """
    by_index: dict[int, dict] = {}

    for poll_id, pinfo in (polls or {}).items():
        display_q_index = pinfo.get("question_index")
        if not isinstance(display_q_index, int):
            continue
        # Question shuffle (group flat quizzes): map the display position
        # back to the canonical stored question index.
        question_order = pinfo.get("question_order")
        if question_order and 0 <= display_q_index < len(question_order):
            q_index = int(question_order[display_q_index])
        else:
            q_index = display_q_index
        display_correct = as_int_list(pinfo.get("correct_option"))
        display_order = pinfo.get("display_order")
        answer = (participant_answers or {}).get(poll_id)

        if answer and as_int_list(answer.get("option")):
            display_selected = as_int_list(answer.get("option"))
            selected, correct, outcome = _resolve(
                display_selected, display_correct, display_order
            )
            time_taken: Optional[float] = None
            sent_time = pinfo.get("sent_time")
            answered_at = answer.get("time")
            try:
                if sent_time is not None and answered_at is not None:
                    time_taken = max(0.0, float(answered_at) - float(sent_time))
            except (TypeError, ValueError):
                time_taken = None
        else:
            # Delivered but not answered: skipped, not wrong.
            selected, correct, outcome, time_taken = [], to_canonical(
                display_correct, display_order
            ), OUTCOME_SKIPPED, None

        existing = by_index.get(q_index)
        # An answered delivery takes precedence over a skipped duplicate.
        if existing is None or (
            existing["outcome"] == OUTCOME_SKIPPED and outcome != OUTCOME_SKIPPED
        ):
            by_index[q_index] = {
                "q_index": q_index,
                "selected": selected,
                "correct_option": correct,
                "outcome": outcome,
                "time_taken": round(time_taken, 3) if time_taken is not None else None,
            }

    return [by_index[i] for i in sorted(by_index)]


def build_miniapp_results(
    order: list[int],
    per_question: dict[int, dict],
    answers: dict[int, dict],
) -> list[dict]:
    """Build canonical question results for a finished Mini App attempt.

    ``order``        : canonical question indices in play sequence (handles
                       shuffled questions).
    ``per_question`` : ``{q_index: {"correct_ids": [display ids],
                                    "display_order": [canonical positions]}}``.
    ``answers``      : ``{q_index: {"selected": [display ids],
                                    "correct": bool, "time_taken": float}}``
                       -- the correctness bool was computed live at submit
                       time and is treated as authoritative.
    """
    results: list[dict] = []
    for q_index in order or []:
        pq = (per_question or {}).get(q_index, {})
        display_order = pq.get("display_order")
        display_correct = as_int_list(pq.get("correct_ids"))
        answer = (answers or {}).get(q_index)

        if answer and as_int_list(answer.get("selected")):
            display_selected = as_int_list(answer.get("selected"))
            selected = to_canonical(display_selected, display_order)
            correct = to_canonical(display_correct, display_order)
            # Trust the runtime decision recorded at /api/answer time.
            outcome = OUTCOME_CORRECT if answer.get("correct") else OUTCOME_INCORRECT
            raw_time = answer.get("time_taken")
            try:
                time_taken = round(max(0.0, float(raw_time)), 3) if raw_time is not None else None
            except (TypeError, ValueError):
                time_taken = None
        else:
            selected, outcome, time_taken = [], OUTCOME_SKIPPED, None
            correct = to_canonical(display_correct, display_order)

        results.append({
            "q_index": int(q_index),
            "selected": selected,
            "correct_option": correct,
            "outcome": outcome,
            "time_taken": time_taken,
        })
    return results
