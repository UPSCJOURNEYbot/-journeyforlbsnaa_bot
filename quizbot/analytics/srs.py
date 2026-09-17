"""Phase H: spaced-repetition scheduling (SRS) for mistake revision.

This module turns the EXISTING non-destructive mistake history into a review
schedule. It adds:

* **no new collection, no new index, no new write path.** The schedule is a
  pure *projection* of data Phase B/D already store: every mistake row keeps a
  bounded, append-only ``revision_history`` timeline (``{"at", "outcome",
  "attempt_id"?}``) plus ``wrong_count`` / ``correct_count`` / ``status`` /
  ``last_wrong_at`` / ``last_correct_at``. Replaying that timeline through the
  box ladder below yields the card's box, its last review day and its due day.
  Because the answers themselves already flow through the single canonical
  ``AnalyticsService.record_completion(source="dm")`` boundary (Phase D
  revision launch), *answering a revision question is all it takes to advance
  the schedule* -- nothing else has to be written.
* **no second queue or quiz engine.** Due work is selected by
  :class:`SrsService` and handed to the ordinary private DM engine exactly
  like ``/mistakes`` does, carrying the same ``_revision_origins`` provenance
  so results fold back into the origin rows (and hence into this schedule).

Box ladder
----------
Boxes and their review intervals (days)::

    box:          0     1     2     3     4     5
    interval:  today   1     3     7    16    35

A freshly recorded mistake starts in box 0 (due immediately -- you just got it
wrong). A correct review promotes the card one box; a wrong review demotes it
straight back to box 0 and counts a lapse; a *skipped* question changes
nothing (it stays due) -- skipping must never be rewarded with a longer
interval.

Time
----
All scheduling is done in IST calendar days (the same ``local_day_key`` the
Phase C streak/XP layer uses), so "due today" means the same thing everywhere
in the bot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from quizbot.analytics import mistake_revision as mr
from quizbot.analytics.gamification import local_day_key
from quizbot.database.repositories import MistakeRepository

logger = logging.getLogger(__name__)

#: Review intervals per box, in IST days. Box 0 is "learn it now".
SRS_INTERVALS: tuple[int, ...] = (0, 1, 3, 7, 16, 35)
MAX_BOX = len(SRS_INTERVALS) - 1

#: Bounded per-session revision size (same ceiling as Phase D).
SRS_SESSION_SIZE = mr.REVISION_SIZE

#: Bounded candidate window (never unbounded work on the 1 CPU / 2 GB VPS).
SRS_CANDIDATE_CAP = MistakeRepository.REVISION_CANDIDATE_CAP

BOX_LABELS: dict[int, str] = {
    0: "learning now",
    1: "in 1 day",
    2: "in 3 days",
    3: "in 7 days",
    4: "in 16 days",
    5: "in 35 days",
}


# ---------------------------------------------------------------------------
# Pure state machine
# ---------------------------------------------------------------------------

def next_box(box: int, outcome: Optional[str]) -> int:
    """Box transition for one review outcome.

    ``correct``   -> +1 box (capped), ``incorrect`` -> box 0, anything else
    (``skipped``/unknown/``None``) leaves the box unchanged -- an unanswered
    question must never be scored as effort.
    """
    try:
        current = int(box or 0)
    except (TypeError, ValueError):
        current = 0
    current = max(0, min(MAX_BOX, current))
    if outcome == "correct":
        return min(MAX_BOX, current + 1)
    if outcome == "incorrect":
        return 0
    return current


def interval_days(box: int) -> int:
    """Review interval for a box (never raises; clamped into range)."""
    try:
        idx = int(box or 0)
    except (TypeError, ValueError):
        idx = 0
    return SRS_INTERVALS[max(0, min(MAX_BOX, idx))]


def add_days(day_key: str, days: int) -> str:
    """``day_key`` (``"YYYY-MM-DD"``, IST) plus ``days``."""
    base = datetime.strptime(day_key, "%Y-%m-%d").date()
    from datetime import timedelta
    return (base + timedelta(days=int(days))).isoformat()


def day_diff(later_day: str, earlier_day: str) -> int:
    """Whole-day difference ``later - earlier`` (both ``"YYYY-MM-DD"``)."""
    later = datetime.strptime(later_day, "%Y-%m-%d").date()
    earlier = datetime.strptime(earlier_day, "%Y-%m-%d").date()
    return (later - earlier).days


def card_from_history(
    history: Optional[list[dict]],
    *,
    today: str,
    wrong_count: int = 0,
    correct_count: int = 0,
    status: Optional[str] = None,
    last_wrong_at: Any = None,
    last_correct_at: Any = None,
) -> dict:
    """Replay one mistake row's timeline into its current SRS card.

    Pure and total: malformed entries are ignored, an empty/legacy history
    falls back to a box-0 card (immediately due), and the result is always a
    well-formed dict. Returns::

        {"box", "interval_days", "due_day", "last_review_day", "reps",
         "lapses", "overdue_days", "due_today", "reviews"}
    """
    entries = _ordered_entries(history)

    # Legacy rows (written before the revision history existed, or rows that
    # were never revised): synthesise the minimal timeline the row itself
    # proves. A resolved row with a recorded correct answer has at least one
    # successful review; anything else is still in box 0.
    if not entries:
        if int(correct_count or 0) > 0 and status == MistakeRepository.STATUS_RESOLVED:
            entries = [{
                "at": last_correct_at,
                "outcome": "correct",
                "_synthetic": True,
            }]
        elif int(wrong_count or 0) > 0 or last_wrong_at is not None:
            entries = [{
                "at": last_wrong_at,
                "outcome": "incorrect",
                "_synthetic": True,
            }]

    box = 0
    reps = 0
    lapses = 0
    last_review_day: Optional[str] = None

    for entry in entries:
        outcome = entry.get("outcome")
        box = next_box(box, outcome)
        if outcome == "correct":
            reps += 1
        elif outcome == "incorrect":
            lapses += 1
        elif outcome == "skipped":
            continue  # a skip leaves the schedule exactly where it was
        day = _entry_day(entry.get("at"))
        if day and (last_review_day is None or day > last_review_day):
            last_review_day = day

    interval = interval_days(box)
    if last_review_day is None:
        # Never actually reviewed: due right now (this is the box-0 rule).
        due_day = today
    else:
        due_day = add_days(last_review_day, interval)

    overdue = day_diff(today, due_day)
    return {
        "box": box,
        "interval_days": interval,
        "due_day": due_day,
        "last_review_day": last_review_day,
        "reps": reps,
        "lapses": lapses,
        "reviews": len(entries),
        "overdue_days": max(0, overdue),
        "due_today": due_day <= today,
    }


def _ordered_entries(history: Optional[list[dict]]) -> list[dict]:
    """Chronologically ordered, well-formed history entries (stable)."""
    if not isinstance(history, list):
        return []
    indexed: list[tuple[int, dict]] = []
    for pos, raw in enumerate(history):
        if not isinstance(raw, dict):
            continue
        if raw.get("outcome") not in ("correct", "incorrect", "skipped"):
            continue
        indexed.append((pos, raw))
    # Sort by parsed timestamp; unparseable entries keep their original order
    # at the front (they are treated as "before anything timed").
    indexed.sort(key=lambda pair: (
        mr.to_epoch(pair[1].get("at")) is not None,
        mr.to_epoch(pair[1].get("at")) or 0.0,
        pair[0],
    ))
    return [raw for _, raw in indexed]


def _entry_day(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return local_day_key(value)
    except Exception:  # pragma: no cover - local_day_key is total in practice
        return None


def srs_rank_key(group: dict, today: str):
    """Deterministic due-queue ordering: most overdue first, then lowest box,
    then most-repeated mistake, then a stable content key."""
    card = group.get("srs") or {}
    return (
        -int(card.get("overdue_days") or 0),
        int(card.get("box") or 0),
        -int(group.get("wrong_max") or 0),
        group.get("key", ("q", ""))[0],
        str(group.get("key", ("q", ""))[1]),
    )


def annotate_groups(groups: list[dict], today: str) -> list[dict]:
    """Attach a fresh ``srs`` card to every Phase D content group and return
    them ordered for presentation (due first, worst first)."""
    annotated: list[dict] = []
    for group in groups:
        card = card_from_history(
            group.get("history") or group.get("revision_history"),
            today=today,
            wrong_count=group.get("wrong_total") or 0,
            correct_count=group.get("correct_total") or 0,
            status=(
                MistakeRepository.STATUS_OPEN if group.get("open")
                else MistakeRepository.STATUS_RESOLVED
            ),
            last_wrong_at=group.get("last_wrong_at"),
            last_correct_at=group.get("last_correct_at"),
        )
        annotated.append({**group, "srs": card})
    return annotated


def split_due(groups: list[dict], today: str) -> tuple[list[dict], list[dict]]:
    """``(due, scheduled_later)`` -- both deterministic and bounded by input."""
    due = [g for g in groups if g.get("srs", {}).get("due_today")]
    later = [g for g in groups if not g.get("srs", {}).get("due_today")]
    due.sort(key=lambda g: srs_rank_key(g, today))
    later.sort(key=lambda g: (g["srs"]["due_day"],) + srs_rank_key(g, today))
    return due, later


def box_histogram(groups: list[dict]) -> dict[int, int]:
    """``{box: count}`` for every box present (missing boxes omitted)."""
    out: dict[int, int] = {}
    for group in groups:
        box = int((group.get("srs") or {}).get("box") or 0)
        out[box] = out.get(box, 0) + 1
    return dict(sorted(out.items()))


def next_due_day(groups: list[dict]) -> Optional[str]:
    """Earliest future due day across cards, or None when nothing is queued."""
    days = [g["srs"]["due_day"] for g in groups if g.get("srs")]
    return min(days) if days else None


# ---------------------------------------------------------------------------
# Service (bounded reads over existing collections)
# ---------------------------------------------------------------------------

class SrsService:
    """Read-only projection of the mistake history into a review schedule.

    Deliberately does NOT write: the history is written by Phase D through the
    canonical analytics boundary, so the schedule can never drift from the
    answers the user actually gave.
    """

    def __init__(self, db: Any = None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.mistakes = MistakeRepository(db)
        self.revision = mr.MistakeRevisionService(db)

    @staticmethod
    def _require_user(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("srs requires a stable integer user_id")
        return user_id

    async def cards(self, user_id: int, now: Any = None,
                    *, limit: int = SRS_CANDIDATE_CAP) -> dict:
        """All content-group cards for one user, split into due/later."""
        user_id = self._require_user(user_id)
        today = local_day_key(now)
        rows = await self.mistakes.query_srs_rows(user_id, limit=limit)
        groups = annotate_groups(mr.group_mistakes(rows), today)
        due, later = split_due(groups, today)
        return {
            "user_id": user_id,
            "today": today,
            "groups": groups,
            "due": due,
            "later": later,
            "boxes": box_histogram(groups),
            "next_due_day": next_due_day(later) if later else None,
        }

    async def overview(self, user_id: int, now: Any = None) -> dict:
        """Compact summary for the ``/revise`` card."""
        data = await self.cards(user_id, now)
        boxes = data["boxes"]
        return {
            "user_id": data["user_id"],
            "today": data["today"],
            "total_cards": len(data["groups"]),
            "due_count": len(data["due"]),
            "scheduled_count": len(data["later"]),
            "learning_count": int(boxes.get(0, 0)),
            "boxes": boxes,
            "next_due_day": data["next_due_day"],
            "due_topics": _top_topics(data["due"]),
            "total_lapses": sum(int(g["srs"]["lapses"]) for g in data["groups"]),
            "reviews": sum(int(g["srs"]["reviews"]) for g in data["groups"]),
        }

    async def build_revision(
        self, user_id: int, now: Any = None, *,
        size: int = SRS_SESSION_SIZE, include_ahead: bool = False,
    ) -> dict:
        """Build a ready-to-play revision session of DUE cards (or, with
        ``include_ahead``, the whole queue ordered by due-ness -- never more
        than ``size`` questions)."""
        data = await self.cards(user_id, now)
        pool = data["due"] if not include_ahead else data["due"] + data["later"]
        ordered = list(pool)[: max(1, int(size))]
        built = await self.revision.build_from_groups(
            user_id, ordered, mode="srs", now=now,
        )
        built["due_count"] = len(data["due"])
        built["include_ahead"] = bool(include_ahead)
        built["next_due_day"] = data["next_due_day"]
        built["cards"] = [
            {
                "topic": g.get("topic"),
                "box": g["srs"]["box"],
                "due_day": g["srs"]["due_day"],
                "overdue_days": g["srs"]["overdue_days"],
                "reasons": mr.smart_reasons(g, mr._now_epoch(now)),
            }
            for g in ordered
        ]
        return built


def _top_topics(due: list[dict], limit: int = 3) -> list[dict]:
    """Bounded ``[{topic, count}]`` for the due queue (deterministic)."""
    counts: dict[str, int] = {}
    for group in due:
        topic = group.get("topic")
        if not topic:
            continue
        counts[topic] = counts.get(topic, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"topic": topic, "count": count} for topic, count in ordered[:limit]]
