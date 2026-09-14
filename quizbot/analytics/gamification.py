"""Phase C gamification: XP, formula-based levels and daily IST streaks.

This module hooks into the ONE canonical completion boundary
(:meth:`quizbot.analytics.service.AnalyticsService.record_completion`). It does
not create a second analytics pipeline: it consumes the canonical, already
scored question events that Phase B produces.

Design properties (see the Phase C specification)
--------------------------------------------------
* **Fail-soft.** The analytics layer calls :meth:`GamificationService.on_completion`
  inside a ``try/except``; any failure here is logged and can never stop quiz
  completion, result/HTML/PDF generation, Mini App completion or scheduled
  completion.
* **Exactly once, cross-process.** Two *independent* service instances (no
  shared lock, no shared ``asyncio`` loop, no Redis) processing the same
  attempt credit it exactly once. Two mechanisms compose:

    1. a Mongo **unique** ``xp_ledger`` row ``(user_id, event_key)`` is inserted
       *first* (``status="reserved"``) -- a durable claim/idempotency record;
    2. the credit itself is a single conditional document update on
       ``user_xp`` (optimistic concurrency / compare-and-swap) whose filter
       pins the document version (``rev``), the daily-counter watermark and an
       ``applied_<event>`` membership guard, so a stale reservation can never be
       applied twice.

  Every CAS iteration re-reads a *fresh* ``user_xp`` document and replans from
  scratch -- it never reuses a stale reservation. In-process ``asyncio`` locks
  are deliberately NOT used for correctness, so separate processes are safe.
* **Hard 200 XP / IST-day cap.** The awarded amount is recomputed from the fresh
  watermark and the update filter pins that exact watermark, hence the invariant
  ``xp_earned_today <= 200`` is enforced by the atomic document write itself.
  Under-award is acceptable; over-award is impossible. On contention exhaustion
  the service fails closed (the reservation stays un-credited).
* **Crash/orphan safe.** A crash after the ledger insert but before the credit
  is recovered exactly once (the ``applied_*`` guard is absent -> re-drive the
  CAS). A crash after the credit but before the ledger is flipped to
  ``committed`` is detected through the same guard (present -> never credit
  again). The transient ``applied_*`` marker is removed right after the ledger
  commits, so ``user_xp`` stays O(1) in size; a crashed marker is self-healing
  on the next replay. Genuinely ambiguous old streaks are abandoned (bounded,
  documented under-award) rather than double-paid.
* **Lightweight.** A fixed, O(1) number of indexed document operations per
  completion; no Redis/Celery/worker/second poller.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from quizbot.analytics.metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    OUTCOME_SKIPPED,
    normalize_difficulty,
    resolve_metadata,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / vocabulary
# ---------------------------------------------------------------------------

# Eligible provenance (mirrors AnalyticsService source vocabulary).
QUALIFYING_SOURCES = frozenset({
    "group", "dm", "miniapp", "scheduled", "aiquiz", "pdfquiz", "mix",
})
# Explicitly non-qualifying: "backfill", "unknown" and "pollquiz" are absent
# from QUALIFYING_SOURCES and are therefore rejected.

DAILY_XP_CAP = 200
BASE_COMPLETION_XP = 10
CORRECT_XP = 2
PACE_SECONDS_PER_QUESTION = 3
DIFFICULTY_XP = {"moderate": 1, "hard": 2, "extreme": 3}

# IST is a fixed +05:30 offset (India observes no DST), so a fixed tzinfo is
# exact and avoids any dependency on the host tz database.
IST = timezone(timedelta(hours=5, minutes=30))
_UTC = timezone.utc

# Ledger lifecycle.
EVENT_ATTEMPT = "attempt"
EVENT_STREAK = "streak"
STATUS_RESERVED = "reserved"
STATUS_COMMITTED = "committed"
STATUS_ABANDONED = "abandoned"
_TERMINAL_STATUSES = frozenset({STATUS_COMMITTED, STATUS_ABANDONED})

# Bound on optimistic-CAS replan attempts before failing closed.
_MAX_CAS_TRIES = 50

# Sentinel returned by a planner when fresh state shows the work is done.
_SKIP = object()


# ---------------------------------------------------------------------------
# Time handling (Part 14)
# ---------------------------------------------------------------------------

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utc_now_iso() -> str:
    """Same naive-UTC ``"%Y-%m-%d %H:%M:%S"`` format the rest of the app stores
    (kept as a string for Mongo, matching ``repositories._now_iso``)."""
    return datetime.now(_UTC).strftime("%Y-%m-%d %H:%M:%S")


def _coerce_to_utc_datetime(value: Any) -> datetime:
    """Normalise the accepted inputs to a timezone-aware UTC datetime.

    Accepts: timezone-aware/naive :class:`datetime`, :class:`date`, ISO-8601
    strings with ``+05:30``/``-08:00``/``Z`` offsets, the app's existing naive
    UTC ``"YYYY-MM-DD HH:MM:SS"`` format and date-only strings.

    Naive timestamps are interpreted as UTC (the historical app format).
    Anything unparseable falls back to the current time rather than raising
    (preserves the existing fail-soft fallback behaviour at the boundary).
    """
    if value is None:
        return datetime.now(_UTC)

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        s = value.strip()
        # A bare day key is already a local day; midnight UTC converts to the
        # same IST calendar date (00:00 UTC == 05:30 IST), so return it directly.
        if _DATE_ONLY_RE.match(s):
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_UTC)
        iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            try:
                # Existing app format: naive UTC.
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                logger.warning("unparseable timestamp %r; falling back to now", value)
                return datetime.now(_UTC)
    else:
        logger.warning("unparseable timestamp %r; falling back to now", value)
        return datetime.now(_UTC)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)  # naive == UTC by app convention
    return dt.astimezone(_UTC)


def local_day_key(value: Any = None) -> str:
    """Return the IST calendar day (``"YYYY-MM-DD"``) for a timestamp."""
    return _coerce_to_utc_datetime(value).astimezone(IST).date().isoformat()


def _day_index(day_key: str) -> date:
    return datetime.strptime(day_key, "%Y-%m-%d").date()


def day_diff(later_day: str, earlier_day: str) -> int:
    """Whole IST calendar-day difference ``later - earlier``."""
    return (_day_index(later_day) - _day_index(earlier_day)).days


# ---------------------------------------------------------------------------
# Levels (Part 9) -- deterministic quadratic
# ---------------------------------------------------------------------------
#
# Approved anchors: L1=0, L2=50, L3=110, L4=180. The deltas 50,60,70 have a
# constant second difference of 10, giving the closed form
#
#     threshold(n) = 5 n^2 + 35 n - 40          (n >= 1)
#
# which reproduces every anchor (L4: 5*16 + 35*4 - 40 = 180) and continues
# deterministically (L5=260, L6=350, ...).


def level_threshold(level: int) -> int:
    n = int(level)
    if n < 1:
        n = 1
    return 5 * n * n + 35 * n - 40


def level_for_xp(total_xp: int) -> int:
    """Highest level whose threshold is <= ``total_xp`` (L1 starts at 0)."""
    xp = max(0, int(total_xp or 0))
    level = 1
    while level_threshold(level + 1) <= xp:
        level += 1
    return level


# ---------------------------------------------------------------------------
# Streaks (Part 10) -- pure transition
# ---------------------------------------------------------------------------


def next_streak_state(
    last_activity_day: Optional[str],
    current_streak: int,
    longest_streak: int,
    event_day: str,
) -> tuple[int, int, bool]:
    """Compute ``(new_current, new_longest, changed)`` for activity on
    ``event_day``. Same-day activity leaves the streak unchanged; a consecutive
    day increments it; any gap resets to 1."""
    cur = max(0, int(current_streak or 0))
    longest = max(0, int(longest_streak or 0))

    if last_activity_day == event_day:
        # Same day -- unchanged (a first-ever same-day edge still maps to 1).
        new = cur if cur > 0 else 1
    elif last_activity_day is None:
        new = 1  # first ever activity
    else:
        gap = day_diff(event_day, last_activity_day)
        new = cur + 1 if gap == 1 else 1  # consecutive +1, otherwise reset

    changed = new != cur
    return new, max(longest, new), changed


def streak_bonus_xp(streak_after: int) -> int:
    """5 + 2*(streak-1), capped at 25 XP."""
    s = max(1, int(streak_after or 1))
    return min(25, 5 + 2 * (s - 1))


# ---------------------------------------------------------------------------
# XP scoring (Part 5) -- pure
# ---------------------------------------------------------------------------


def canonical_events_from_results(
    question_results: Optional[list[dict]],
    questions: Optional[list[dict]] = None,
    sections: Optional[list[dict]] = None,
) -> list[dict]:
    """Project Phase B canonical question results into the minimal per-question
    rows scoring needs: ``{"outcome", "difficulty", "time_taken"}``. Used only
    when the caller did not already supply the enriched Phase B event rows."""
    out: list[dict] = []
    for qr in question_results or []:
        outcome = qr.get("outcome")
        if outcome not in (OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED):
            continue
        q_index = int(qr["q_index"])
        difficulty = None
        if isinstance(questions, list) and 0 <= q_index < len(questions):
            _, _, _, difficulty, _ = resolve_metadata(
                questions[q_index], sections, q_index
            )
        out.append({
            "outcome": outcome,
            "difficulty": normalize_difficulty(difficulty),
            "time_taken": qr.get("time_taken"),
        })
    return out


def score_quiz_xp(events: Optional[list[dict]]) -> dict:
    """Pure XP computation for one completion from canonical per-question rows.

    Returns a breakdown. ``gross_xp`` is the amount earned *before* the daily
    cap (the service clamps it). Rules:

      * all-skip / zero answered  -> 0 XP (no base either);
      * base completion           -> 10 XP (only when >=1 answered);
      * per correct answer        -> +2;
      * per ANSWERED question, its difficulty -> moderate +1 / hard +2 /
        extreme +3 (skipped and missing-difficulty add 0);
      * pacing: when timing telemetry exists, if the total answered time is
        under ``answered * 3`` seconds, all *answer* XP (correct + difficulty)
        is withheld as anti-rapid-guessing protection; the completion base is
        retained. No telemetry -> pacing cannot be evaluated -> not withheld.
    """
    answered = [
        e for e in (events or [])
        if e.get("outcome") in (OUTCOME_CORRECT, OUTCOME_INCORRECT)
    ]
    n_answered = len(answered)
    n_correct = sum(1 for e in answered if e.get("outcome") == OUTCOME_CORRECT)
    difficulty_xp = sum(
        DIFFICULTY_XP.get(normalize_difficulty(e.get("difficulty")), 0)
        for e in answered
    )
    correct_xp = CORRECT_XP * n_correct

    if n_answered == 0:
        return {
            "answered": 0, "correct": 0, "base_xp": 0, "correct_xp": 0,
            "difficulty_xp": 0, "pacing_ok": True, "answer_xp": 0,
            "gross_xp": 0,
        }

    base_xp = BASE_COMPLETION_XP
    pacing_ok = True
    measured = [
        float(e.get("time_taken"))
        for e in answered
        if isinstance(e.get("time_taken"), (int, float))
    ]
    if measured:  # only enforce pacing where the runtime genuinely timed answers
        elapsed = sum(max(0.0, t) for t in measured)
        pacing_ok = elapsed >= n_answered * PACE_SECONDS_PER_QUESTION

    answer_xp = (correct_xp + difficulty_xp) if pacing_ok else 0
    return {
        "answered": n_answered,
        "correct": n_correct,
        "base_xp": base_xp,
        "correct_xp": correct_xp if pacing_ok else 0,
        "difficulty_xp": difficulty_xp if pacing_ok else 0,
        "pacing_ok": pacing_ok,
        "answer_xp": answer_xp,
        "gross_xp": base_xp + answer_xp,
    }


def completion_eligible(
    *,
    source: Optional[str],
    finalize: bool,
    total_questions: int,
    backfilled: bool,
) -> tuple[bool, Optional[str]]:
    """Part 4 gate. Returns ``(eligible, reason)``."""
    if not finalize:
        return False, "not_finalized"
    if backfilled:
        return False, "backfilled"
    if source not in QUALIFYING_SOURCES:
        return False, f"ineligible_source:{source}"
    if total_questions <= 0:
        return False, "zero_question_completion"
    return True, None


# ---------------------------------------------------------------------------
# Index bootstrap (Part 15) -- shared by Database startup and tests
# ---------------------------------------------------------------------------


async def ensure_gamification_indexes(db: Any) -> None:
    """Idempotent unique/supporting indexes. Safe to run on every startup.

    Collections use Motor's mapping API (``db[name]``), not the app
    wrapper's ``.collection()`` method: startup hands this function the RAW
    MotorDatabase (Database._ensure_indexes), on which ``db.collection("x")``
    resolves to the collection literally named "collection" and raises
    'MotorCollection object is not callable' (the production startup crash).
    Mapping access works on raw Motor, the app Database wrapper, and the
    in-memory test doubles.
    """
    ledger = db["xp_ledger"]
    users = db["user_xp"]

    # Unique event identity: an (user, event) is awarded at most once.
    await ledger.create_index(
        [("user_id", 1), ("event_key", 1)],
        unique=True, name="uniq_xp_user_event",
    )
    await ledger.create_index(
        [("user_id", 1), ("event_type", 1), ("status", 1)],
        name="xp_user_type_status",
    )
    await ledger.create_index(
        [("user_id", 1), ("local_day", 1)], name="xp_user_local_day",
    )
    await ledger.create_index([("created_at", 1)], name="xp_created_at")

    await users.create_index("user_id", unique=True, name="uniq_user_xp_user")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GamificationError(RuntimeError):
    """Base class for gamification errors (always swallowed upstream)."""


class GamificationContention(GamificationError):
    """CAS exhausted its retries; the caller fails closed (no over-award)."""


class GamificationService:
    """XP / level / streak writer.

    Construct one per process/boundary exactly like :class:`AnalyticsService`.
    Correctness relies solely on Mongo atomic document writes + unique indexes,
    so independent instances (and separate OS processes) are safe.
    """

    def __init__(self, db: Any = None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        # Mapping API — works for the app Database wrapper AND a raw
        # MotorDatabase (see ensure_gamification_indexes docstring).
        self.ledger = db["xp_ledger"]
        self.users = db["user_xp"]

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _require_user(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("gamification requires a stable integer user_id")
        return user_id

    async def _ensure_user(self, user_id: int) -> None:
        """Lazy, idempotent, non-destructive user_xp bootstrap. Never resets an
        existing document (upsert only sets on insert)."""
        now = _utc_now_iso()
        try:
            await self.users.update_one(
                {"user_id": user_id},
                {"$setOnInsert": {
                    "user_id": user_id,
                    "total_xp": 0,
                    "xp_earned_today": 0,
                    "xp_day": None,
                    "total_completions": 0,
                    "current_level": 1,
                    "current_streak": 0,
                    "longest_streak": 0,
                    "last_activity_day": None,
                    "rev": 0,
                    "created_at": now,
                    "updated_at": now,
                }},
                upsert=True,
            )
        except DuplicateKeyError:
            # Raced with a concurrent first-ever bootstrap; row now exists.
            pass

    async def _fresh_user(self, user_id: int) -> dict:
        doc = await self.users.find_one({"user_id": user_id})
        if doc is None:  # bootstrap raced/lost: ensure then read once more
            await self._ensure_user(user_id)
            doc = await self.users.find_one({"user_id": user_id})
        return doc

    @staticmethod
    def _applied(doc: dict, field: str, key: str) -> bool:
        return key in (doc.get(field) or [])

    @staticmethod
    def _pin(doc: dict, field: str, value: Any) -> dict:
        """Equality CAS pin that is lazy-migration safe.

        Once a field exists it is matched *exactly* (strict optimistic
        concurrency). A lazily-created/partial legacy document may omit
        ``rev``/counter fields entirely; an exact ``{field: value}`` filter
        would then never match in Mongo (a missing field is not ``0``),
        livelocking the CAS forever. For a missing/null field we additionally
        accept ``null``/absent -- the very first successful write always sets
        the real value (via ``$inc``/``$set``), after which strict exact
        matching applies, so OCC is not weakened beyond that first write.
        """
        if doc.get(field) is not None:
            return {field: value}
        return {field: {"$in": [value, None]}}

    async def _reserve_event(
        self, user_id: int, event_type: str, event_key: str, day: str,
        payload: dict,
    ) -> tuple[dict, bool]:
        """Insert the ledger row first (status reserved). Returns
        ``(ledger_row, inserted_now)``. On the unique-index clash the existing
        row is returned for reconciliation (never inserted twice)."""
        now = _utc_now_iso()
        row = {
            "user_id": user_id,
            "event_key": event_key,
            "event_type": event_type,
            "local_day": day,
            "status": STATUS_RESERVED,
            "awarded_xp": None,
            "created_at": now,
            "updated_at": now,
            **payload,
        }
        try:
            await self.ledger.insert_one(row)
            return row, True
        except DuplicateKeyError:
            existing = await self.ledger.find_one(
                {"user_id": user_id, "event_key": event_key}
            )
            return existing, False

    async def _commit_ledger(
        self, user_id: int, event_key: str, awarded_xp: int,
        extra: Optional[dict] = None,
    ) -> None:
        """Flip reserved -> committed, only if still reserved."""
        update = {
            "$set": {
                "status": STATUS_COMMITTED,
                "awarded_xp": int(awarded_xp),
                "updated_at": _utc_now_iso(),
                **(extra or {}),
            }
        }
        await self.ledger.update_one(
            {"user_id": user_id, "event_key": event_key,
             "status": STATUS_RESERVED},
            update,
        )

    async def _abandon_ledger(
        self, user_id: int, event_key: str, reason: str,
    ) -> None:
        await self.ledger.update_one(
            {"user_id": user_id, "event_key": event_key,
             "status": STATUS_RESERVED},
            {"$set": {"status": STATUS_ABANDONED, "abandon_reason": reason,
                      "updated_at": _utc_now_iso()}},
        )

    async def _release_guard(self, field: str, user_id: int, key: str) -> None:
        """Best-effort removal of the transient exactly-once guard marker."""
        try:
            await self.users.update_one(
                {"user_id": user_id}, {"$pull": {field: key}}
            )
        except Exception:  # pragma: no cover - cleanup never fatal
            logger.debug("guard release failed for %s %s", field, key)

    # -- CAS planners (pure; given a freshly-read document) ----------------

    @staticmethod
    def _day_base(doc: dict, day: str) -> tuple[object, int, bool]:
        """Return ``(pin_xp_day, xp_earned_today, is_rollover)``.

        The daily counter only ever rolls FORWARD. A live completion is stamped
        with "now", so an event whose day predates the current bucket can only
        be an out-of-order replay; it is conservatively billed against the
        current bucket rather than resetting ``xp_day`` backwards (which could
        otherwise lift the cap). Backfill (the only source of historical
        timestamps) is ineligible and never reaches here.
        """
        current = doc.get("xp_day")
        if current == day:
            return day, int(doc.get("xp_earned_today") or 0), False
        if current is None or day > current:
            return current, 0, True  # new IST day -> counter resets
        return current, int(doc.get("xp_earned_today") or 0), False

    async def _validated_cas(
        self, user_id: int, planner, *, event_key: str, guard_field: str,
    ) -> dict:
        """Run ``planner(fresh_doc)`` under optimistic concurrency.

        ``planner`` returns ``(filter_extra, update, summary)`` or the sentinel
        :data:`_SKIP` when the fresh state shows the work is already done.

        On EVERY iteration (including recovered/adopted reservations) the
        watermark is revalidated before writing: the ledger row must not be
        terminal and the transient exactly-once guard must not already carry
        this event (Part 12). Each mismatch reloads a *fresh* document and
        replans; a stale reservation is never reused, so the same ledger row can
        never be credited twice.
        """
        last_err: Optional[Exception] = None
        for _ in range(_MAX_CAS_TRIES):
            ledger_now = await self.ledger.find_one(
                {"user_id": user_id, "event_key": event_key}
            )
            if ledger_now is not None and ledger_now.get("status") in _TERMINAL_STATUSES:
                return {"applied": False, "duplicate": True}
            doc = await self._fresh_user(user_id)
            if self._applied(doc, guard_field, event_key):
                # Counter already committed this reservation; do not pay again.
                return {"applied": False, "duplicate": True}
            planned = planner(doc)
            if planned is _SKIP:
                return {"applied": False, "skipped": True}
            extra_filter, update, summary = planned
            filt = {"user_id": user_id}
            filt.update(self._pin(doc, "rev", int(doc.get("rev", 0))))
            filt.update(extra_filter)
            update.setdefault("$inc", {})["rev"] = 1
            update.setdefault("$set", {})["updated_at"] = _utc_now_iso()
            try:
                res = await self.users.update_one(filt, update)
            except Exception as exc:  # pragma: no cover - driver error
                last_err = exc
                await asyncio.sleep(0)
                continue
            if res.matched_count == 1:
                summary["applied"] = True
                return summary
            # Compare failed: a concurrent commit landed. Yield and replan from
            # a fresh reload (never reuse the reservation just computed).
            await asyncio.sleep(0)
        raise GamificationContention(
            f"CAS exhausted for user={user_id}: {last_err}"
        )

    # -- streak step -------------------------------------------------------

    def _plan_streak(self, day: str, key: str):
        def planner(doc: dict):
            # Already advanced for this day (concurrent/retried event): nothing.
            if doc.get("last_activity_day") == day:
                return _SKIP

            new_streak, longest, _ = next_streak_state(
                doc.get("last_activity_day"),
                int(doc.get("current_streak") or 0),
                int(doc.get("longest_streak") or 0),
                day,
            )
            desired = streak_bonus_xp(new_streak)
            pin_day, base, rollover = self._day_base(doc, day)
            allowed = max(0, DAILY_XP_CAP - base)
            award = min(desired, allowed)
            new_total = int(doc.get("total_xp") or 0) + award

            extra_filter: dict = {}
            extra_filter.update(self._pin(doc, "xp_day", pin_day))
            extra_filter.update(
                self._pin(doc, "last_activity_day", doc.get("last_activity_day"))
            )
            extra_filter["applied_streaks"] = {"$ne": key}
            if not rollover:
                extra_filter.update(self._pin(doc, "xp_earned_today", base))
            set_fields = {
                "current_streak": new_streak,
                "longest_streak": longest,
                "last_activity_day": day,
                "current_level": level_for_xp(new_total),
            }
            inc = {"total_xp": award}
            if rollover:
                set_fields["xp_day"] = day
                set_fields["xp_earned_today"] = award
            else:
                inc["xp_earned_today"] = award
            update = {"$set": set_fields, "$inc": inc,
                      "$addToSet": {"applied_streaks": key}}
            summary = {
                "kind": EVENT_STREAK, "event_key": key, "day": day,
                "streak": new_streak, "desired_bonus": desired,
                "bonus": award, "capped": award < desired,
                "longest_streak": longest,
            }
            return extra_filter, update, summary
        return planner

    async def _apply_streak(self, user_id: int, day: str) -> Optional[dict]:
        key = f"streak:{day}"
        ledger_row, inserted = await self._reserve_event(
            user_id, EVENT_STREAK, key, day, {}
        )
        status = ledger_row.get("status")
        if status in _TERMINAL_STATUSES:
            # Already settled today (bonus paid or deliberately not). Heal any
            # transient guard left by a crash between ledger-commit and pull.
            await self._release_guard("applied_streaks", user_id, key)
            return None

        # Out-of-order guard: an event for a day older than the user's latest
        # activity can never advance the streak. Abandon its reservation
        # (bounded under-award) rather than regress current/longest streaks.
        fresh = await self._fresh_user(user_id)
        last_day = fresh.get("last_activity_day")
        if last_day is not None and day < last_day:
            await self._abandon_ledger(user_id, key, "out_of_order_day")
            return None

        # Reserved orphan reconciliation -----------------------------------
        if not inserted:
            today = local_day_key()
            if day != today:
                # Old, unsettled streak day after time has moved on: we cannot
                # retroactively pay it without corrupting a newer streak or the
                # present-day cap. Abandon -> bounded under-award, never double
                # pay, preserves any next-day streak already recorded.
                await self._abandon_ledger(user_id, key, "stale_streak_day")
                return None
            doc = await self._fresh_user(user_id)
            if doc.get("last_activity_day") == day or self._applied(
                doc, "applied_streaks", key
            ):
                # Credit already landed (crash after credit, before commit).
                # Never pay again; the exact post-cap amount is unrecoverable
                # from here so it is recorded as 0 with a recovery note.
                await self._commit_ledger(user_id, key, 0,
                                          {"note": "recovered_after_credit"})
                await self._release_guard("applied_streaks", user_id, key)
                return None

        result = await self._validated_cas(
            user_id, self._plan_streak(day, key),
            event_key=key, guard_field="applied_streaks",
        )
        if result.get("duplicate") or result.get("skipped"):
            # A concurrent process owns (or already finished) this day's streak;
            # it is responsible for committing the ledger and releasing the
            # guard. We must not overwrite its awarded amount.
            return None

        # Our CAS applied the credit: we own the commit + guard release.
        await self._commit_ledger(user_id, key, result["bonus"])
        await self._release_guard("applied_streaks", user_id, key)
        return result

    # -- attempt step ------------------------------------------------------

    def _plan_attempt(self, day: str, key: str, gross: int):
        def planner(doc: dict):
            pin_day, base, rollover = self._day_base(doc, day)
            allowed = max(0, DAILY_XP_CAP - base)
            award = min(int(gross), allowed)
            tc = int(doc.get("total_completions") or 0)
            new_total = int(doc.get("total_xp") or 0) + award

            extra_filter: dict = {}
            extra_filter.update(self._pin(doc, "xp_day", pin_day))
            extra_filter.update(self._pin(doc, "total_completions", tc))
            extra_filter["applied_attempts"] = {"$ne": key}
            if not rollover:
                extra_filter.update(self._pin(doc, "xp_earned_today", base))
            set_fields = {"current_level": level_for_xp(new_total)}
            inc = {"total_xp": award, "total_completions": 1}
            if rollover:
                set_fields["xp_day"] = day
                set_fields["xp_earned_today"] = award
            else:
                inc["xp_earned_today"] = award
            update = {"$set": set_fields, "$inc": inc,
                      "$addToSet": {"applied_attempts": key}}
            summary = {
                "kind": EVENT_ATTEMPT, "event_key": key, "day": day,
                "gross": int(gross), "awarded": award,
                "capped": award < int(gross),
                "total_completions": tc + 1,
                "total_xp": new_total,
                "level": level_for_xp(new_total),
            }
            return extra_filter, update, summary
        return planner

    async def _apply_attempt(
        self, user_id: int, attempt_id: str, day: str, gross: int,
    ) -> dict:
        key = f"attempt:{attempt_id}"
        ledger_row, inserted = await self._reserve_event(
            user_id, EVENT_ATTEMPT, key, day, {"gross_xp": int(gross)}
        )
        status = ledger_row.get("status")
        if status in _TERMINAL_STATUSES:
            # Already settled for this attempt -> exactly once. Heal any
            # transient guard a crash could leave in place.
            await self._release_guard("applied_attempts", user_id, key)
            return {"duplicate": True, "event_key": key,
                    "status": "duplicate_attempt"}

        if not inserted:
            # Adopted reservation. The applied-guard is the authority: if our
            # key is present, the completion credit already landed (crash after
            # credit, before ledger commit) -> never credit again. Settle the
            # orphan and report duplicate.
            doc = await self._fresh_user(user_id)
            if self._applied(doc, "applied_attempts", key):
                await self._commit_ledger(
                    user_id, key, 0, {"note": "recovered_after_credit"}
                )
                await self._release_guard("applied_attempts", user_id, key)
                return {"duplicate": True, "event_key": key,
                        "status": "duplicate_attempt"}
            # else: credit never landed (crash after insert, before credit) ->
            # fall through and apply exactly once via the validated CAS.

        result = await self._validated_cas(
            user_id, self._plan_attempt(day, key, gross),
            event_key=key, guard_field="applied_attempts",
        )
        if result.get("duplicate"):
            # A concurrent process handling the same attempt won the CAS; it
            # owns the ledger commit and guard release. Never credit twice.
            return {"duplicate": True, "event_key": key,
                    "status": "duplicate_attempt"}

        # Our CAS is the single committed credit for this attempt.
        await self._commit_ledger(
            user_id, key, result["awarded"], {"gross_xp": int(gross)}
        )
        await self._release_guard("applied_attempts", user_id, key)
        return result

    # -- public boundary ---------------------------------------------------

    async def on_completion(
        self,
        *,
        user_id: int,
        attempt_id: str,
        source: Optional[str],
        question_results: Optional[list[dict]],
        questions: Optional[list[dict]] = None,
        sections: Optional[list[dict]] = None,
        finalize: bool = True,
        backfilled: bool = False,
        at: Optional[str] = None,
        enriched_events: Optional[list[dict]] = None,
    ) -> dict:
        """Award XP + maintain streak for one canonical completion.

        Idempotent per ``attempt_id`` and per IST day for streaks. Returns a
        summary; ineligible completions return ``{"eligible": False, ...}`` with
        no writes. Raises only on exhausted contention/fatal driver error; the
        analytics boundary swallows everything so gamification is fail-soft.
        """
        user_id = self._require_user(user_id)
        if not attempt_id or not isinstance(attempt_id, str):
            return {"eligible": False, "reason": "missing_attempt_id"}

        total_questions = (
            len(questions) if isinstance(questions, list) and questions
            else len(question_results or [])
        )
        ok, reason = completion_eligible(
            source=source, finalize=finalize,
            total_questions=total_questions, backfilled=backfilled,
        )
        if not ok:
            return {"eligible": False, "reason": reason}

        events = (
            enriched_events if enriched_events is not None
            else canonical_events_from_results(question_results, questions, sections)
        )
        score = score_quiz_xp(events)
        day = local_day_key(at)

        await self._ensure_user(user_id)

        # Idempotency gate BEFORE touching the streak: a replay of an attempt
        # that has already settled (terminal ledger) is not meaningful
        # activity -- it must never advance a new day's streak or count twice,
        # even if a retry happens to cross an IST midnight boundary.
        attempt_key = f"attempt:{attempt_id}"
        prior = await self.ledger.find_one(
            {"user_id": user_id, "event_key": attempt_key}
        )
        if prior is not None and prior.get("status") in _TERMINAL_STATUSES:
            await self._release_guard("applied_attempts", user_id, attempt_key)
            return {"eligible": True, "duplicate": True,
                    "reason": "duplicate_attempt", "day": day, "streak": None}

        # The FIRST qualifying completion of an IST day owns the streak event.
        streak_summary: Optional[dict] = None
        try:
            streak_summary = await self._apply_streak(user_id, day)
        except GamificationContention:
            # Never let streak contention block the primary completion credit;
            # the reserved streak ledger is recovered on a later attempt or
            # abandoned once its day has passed (bounded under-award).
            logger.warning(
                "streak CAS exhausted for user=%s day=%s (fail closed)",
                user_id, day,
            )

        attempt_summary = await self._apply_attempt(
            user_id, attempt_id, day, score["gross_xp"]
        )

        if attempt_summary.get("duplicate"):
            return {"eligible": True, "duplicate": True,
                    "reason": "duplicate_attempt", "day": day,
                    "streak": streak_summary}

        final = await self._fresh_user(user_id)
        return {
            "eligible": True,
            "duplicate": False,
            "day": day,
            "score": score,
            "streak": streak_summary,
            "attempt": attempt_summary,
            "total_xp": int(final.get("total_xp") or 0),
            "xp_earned_today": int(final.get("xp_earned_today") or 0),
            "current_level": int(final.get("current_level") or 1),
            "current_streak": int(final.get("current_streak") or 0),
            "longest_streak": int(final.get("longest_streak") or 0),
            "total_completions": int(final.get("total_completions") or 0),
        }
