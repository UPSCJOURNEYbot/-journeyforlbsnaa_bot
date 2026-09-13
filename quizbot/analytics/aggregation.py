"""Pure statistical helpers with explicit, documented denominators.

Keeping the arithmetic here (rather than inside future command handlers)
guarantees /coach, /dashboard, /report, /weakquiz and /mistakes all describe
"accuracy", "trend" and "activity" identically.

Canonical denominator rules
----------------------------
* ``answered_accuracy = correct / (correct + incorrect)`` -- SKIPPED questions
  are never silently counted as incorrect.
* ``completion_percent = correct / total_questions`` -- a separate metric,
  used when the denominator is the whole paper.
* ``avg_time`` is averaged only over answered questions with known timing.
* Every function tolerates missing/legacy fields and never divides by zero.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from .metadata import OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED


def safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Return numerator/denominator without raising on zero/None."""
    try:
        if not denominator:
            return default
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def outcome_counts(events: Iterable[dict]) -> dict[str, int]:
    """Count correct/incorrect/skipped across canonical event dicts."""
    counts = {OUTCOME_CORRECT: 0, OUTCOME_INCORRECT: 0, OUTCOME_SKIPPED: 0}
    for ev in events or []:
        outcome = ev.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    return counts


def answered_accuracy(events: Iterable[dict]) -> float:
    """correct / (correct + incorrect); skipped excluded. 0 when unanswered."""
    counts = outcome_counts(events)
    return round(
        safe_ratio(counts[OUTCOME_CORRECT], counts[OUTCOME_CORRECT] + counts[OUTCOME_INCORRECT]) * 100,
        2,
    )


def completion_percent(correct: float, total_questions: float) -> float:
    """correct / total questions (skipped and wrong both reduce this)."""
    return round(safe_ratio(correct, total_questions) * 100, 2)


def avg_question_time(events: Iterable[dict]) -> Optional[float]:
    """Mean ``time_taken`` over answered questions that have real timing."""
    total = 0.0
    n = 0
    for ev in events or []:
        if ev.get("outcome") == OUTCOME_SKIPPED:
            continue
        t = ev.get("time_taken")
        if isinstance(t, (int, float)) and t >= 0:
            total += float(t)
            n += 1
    if not n:
        return None
    return round(total / n, 2)


def topic_rollups(events: Iterable[dict]) -> list[dict]:
    """Aggregate events with identity-aware topic keys.

    Labels are NEVER synonym-merged: only identical normalised labels
    combine (matching the storage rules in :mod:`metadata`). Identity is:

    * explicit question metadata -> exact ``(subject, topic, source)`` and
      pooled ACROSS quizzes (same subject+topic may aggregate; different
      subjects, including unknown vs known, stay apart);
    * section-derived names (``topic_source == "section"``) -> the qid is
      added to the key, because "Section 1"/"Basics" in two unrelated
      quizzes must not pool. ``topic_source`` also keeps an explicit topic
      separate from a section of the same display label.
    """
    buckets: dict[tuple, dict] = {}
    for ev in events or []:
        topic = ev.get("topic")
        if not topic:
            continue
        source = ev.get("topic_source")
        scope_qid = ev.get("qid") if source == "section" else None
        key = (ev.get("subject"), topic, source, scope_qid)
        bucket = buckets.setdefault(key, {
            "subject": ev.get("subject"),
            "topic": topic,
            "topic_source": source,
            "qid": scope_qid,
            OUTCOME_CORRECT: 0, OUTCOME_INCORRECT: 0, OUTCOME_SKIPPED: 0,
            "time_total": 0.0, "timed_questions": 0, "attempts": set(),
        })
        outcome = ev.get("outcome")
        if outcome in (OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED):
            bucket[outcome] += 1
        t = ev.get("time_taken")
        if outcome != OUTCOME_SKIPPED and isinstance(t, (int, float)) and t >= 0:
            bucket["time_total"] += float(t)
            bucket["timed_questions"] += 1
        if ev.get("attempt_id"):
            bucket["attempts"].add(ev["attempt_id"])

    rows: list[dict] = []
    for bucket in buckets.values():
        answered = bucket[OUTCOME_CORRECT] + bucket[OUTCOME_INCORRECT]
        rows.append({
            "subject": bucket["subject"],
            "topic": bucket["topic"],
            "topic_source": bucket["topic_source"],
            "qid": bucket["qid"],
            "correct": bucket[OUTCOME_CORRECT],
            "incorrect": bucket[OUTCOME_INCORRECT],
            "skipped": bucket[OUTCOME_SKIPPED],
            "answered": answered,
            "accuracy_pct": round(safe_ratio(bucket[OUTCOME_CORRECT], answered) * 100, 2),
            "avg_time": round(bucket["time_total"] / bucket["timed_questions"], 2)
            if bucket["timed_questions"] else None,
            "attempt_count": len(bucket["attempts"]),
        })
    rows.sort(key=lambda r: (r["accuracy_pct"], -r["incorrect"]))
    return rows


def day_key(timestamp: Any) -> Optional[str]:
    """Return the UTC ``YYYY-MM-DD`` activity key from a stored timestamp.

    Stored timestamps are ``"%Y-%m-%d %H:%M:%S"`` UTC strings (see
    ``repositories._now_iso``); ISO strings and datetimes are also accepted.
    """
    if timestamp is None:
        return None
    if isinstance(timestamp, datetime):
        dt = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    text = str(timestamp).strip()
    if not text:
        return None
    # Stored format (and ISO) both begin with YYYY-MM-DD.
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def activity_days(timestamps: Iterable[Any]) -> list[str]:
    """Sorted distinct UTC days on which meaningful activity occurred."""
    days = {dk for ts in timestamps if (dk := day_key(ts))}
    return sorted(days)


def streak_stats(
    days: Iterable[str],
    today: Optional[str] = None,
) -> dict[str, Optional[int]]:
    """Compute current and best (longest) streak over a set of day keys.

    Phase B only EXPOSES activity data; Phase C owns XP/streak policy. The
    "current" streak counts back from today (or the latest active day when
    today is not given), with a single missed day allowed to break it.
    Returns ``{"current": int|None, "best": int}`` -- current is None when
    there is no activity at all.
    """
    ordered = sorted({d for d in days if d})
    if not ordered:
        return {"current": None, "best": 0}

    if today is None:
        ref = date.fromisoformat(ordered[-1])
    else:
        ref = date.fromisoformat(today)

    as_dates = sorted(date.fromisoformat(d) for d in ordered)
    as_set = set(as_dates)

    # Best streak over history.
    best = run = 0
    prev: Optional[date] = None
    for d in as_dates:
        if prev is not None and (d - prev).days == 1:
            run += 1
        else:
            run = 1
        best = max(best, run)
        prev = d

    # Current streak: walk backwards from the reference day.
    current = 0
    cursor = ref
    if cursor in as_set:
        while cursor in as_set:
            current += 1
            cursor = cursor.fromordinal(cursor.toordinal() - 1)
    else:
        # Allow the streak to remain "alive" if yesterday was active.
        yesterday = ref.fromordinal(ref.toordinal() - 1)
        cursor = yesterday
        while cursor in as_set:
            current += 1
            cursor = cursor.fromordinal(cursor.toordinal() - 1)

    return {"current": current or None, "best": best}
