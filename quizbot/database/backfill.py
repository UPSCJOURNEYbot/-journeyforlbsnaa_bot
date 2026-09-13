"""Phase B analytics backfill (MANUAL, opt-in, never run at startup).

Purpose
-------
Derive canonical ``question_events`` for *historical* completed attempts
that pre-date Phase B, subject to strict conservative gates. The script is:

* **additive**         -- it only inserts question_events and stamps each
  attempt with an ``analytics_backfill`` marker; it never deletes or
  rewrites scores, timestamps, status or leaderboard rows;
* **idempotent**       -- marked attempts are skipped on re-run and event
  upserts are keyed (user_id, attempt_id, question_index);
* **resumable**        -- it processes in batches with a sort + limit;
  re-running continues where the previous run stopped;
* **non-destructive**  -- no collection is dropped or truncated.

Conservative recoverability rules (nothing is invented)
-------------------------------------------------------
An attempt can be back-filled ONLY when ALL hold:

1. the stored quiz document still exists (otherwise option coordinates and
   answer keys are gone)             -> ``unrecoverable_deleted``;
2. the quiz has ``shuffle_questions`` AND ``shuffle_options`` both OFF --
   legacy answers were stored in whatever coordinates were displayed live,
   and the live permutation was never persisted
                                     -> ``unrecoverable_shuffle``;
3. every stored answer key is ``q<int>`` (group/Mini App canonical key
   shape). DM attempts stored answers keyed by the Telegram poll UUID and
   the poll_id -> question map was in-memory only
                                     -> ``unrecoverable_dm``;
4. every answer index is within the current question count -- a quiz that
   was edited/re-ordered in place cannot be mapped reliably
                                     -> ``unrecoverable_edited``;
5. timing: per-question time was never stored historically, so every
   back-filled event has ``time_taken = None`` and ``answered_at`` equal to
   the attempt's ``time_ended`` (a batch timestamp, NOT per-question
   truth). Skipped answers are NOT reconstructed historically (the
   delivered/unanswered distinction is unavailable), so only answered
   questions become events.

Back-filled events deliberately do NOT touch ``user_mistakes`` or
``question_wrong_stats``: the live boundary for historical group attempts
already wrote those at play time, and replaying them would double-count.

Run:
    python -m quizbot.database.backfill [--dry-run] [--limit N] [--batch 200]
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections import OrderedDict
from typing import Any, Optional

from quizbot.analytics.metadata import OUTCOME_CORRECT, OUTCOME_INCORRECT
from quizbot.analytics.service import AnalyticsService
from quizbot.database.db import close_db, get_db, init_db
from quizbot.database.repositories import _now_iso
from quizbot.runner_bot.quiz_utils import is_correct
from quizbot.shared import config

_QKEY_RE = re.compile(r"^q(\d+)$")

# Statuses written to attempt["analytics_backfill"]["status"].
STATUS_BACKFILLED = "backfilled"
STATUS_EMPTY = "noop_empty"
STATUS_CANONICAL = "already_canonical"
STATUS_DELETED = "unrecoverable_deleted"
STATUS_SHUFFLE = "unrecoverable_shuffle"
STATUS_DM = "unrecoverable_dm"
STATUS_EDITED = "unrecoverable_edited"
UNRECOVERABLE = {STATUS_DELETED, STATUS_SHUFFLE, STATUS_DM, STATUS_EDITED}

# Only the fields reconstruction needs -- never the whole quiz library.
_QUIZ_PROJECTION = {
    "_id": 0, "questions": 1, "sections": 1,
    "shuffle_questions": 1, "shuffle_options": 1,
}


class _BoundedQuizCache:
    """LRU cache of the (projected) quiz documents needed by one backfill
    pass. Capacity is fixed, so total quiz-document memory stays bounded
    regardless of the size of the quiz library; missing quizzes are cached
    as ``None`` so a deleted quiz is looked up at most once per run.
    """

    def __init__(self, db: Any, maxsize: int = 32) -> None:
        if maxsize < 1:
            raise ValueError("cache maxsize must be >= 1")
        self._col = db.collection("quizzes")
        self._data: "OrderedDict[str, Optional[dict]]" = OrderedDict()
        self._maxsize = maxsize
        self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    @property
    def maxsize(self) -> int:
        return self._maxsize

    async def get(self, qid: str) -> Optional[dict]:
        if qid in self._data:
            self._data.move_to_end(qid)
            return self._data[qid]
        self.misses += 1
        doc = await self._col.find_one({"qid": qid}, projection=_QUIZ_PROJECTION)
        self._data[qid] = doc
        self._data.move_to_end(qid)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)  # evict least recently used
        return doc


def legacy_question_results(quiz: dict, attempt: dict) -> tuple[Optional[list[dict]], Optional[str]]:
    """Attempt to reconstruct canonical question results from a legacy
    attempt. Returns (results, None) or (None, reason)."""
    questions = quiz.get("questions", [])
    answers = attempt.get("answers") or {}

    if quiz.get("shuffle_questions") or quiz.get("shuffle_options"):
        return None, STATUS_SHUFFLE

    results: list[dict] = []
    for key, selected in answers.items():
        m = _QKEY_RE.fullmatch(str(key))
        if not m:
            return None, STATUS_DM  # poll-UUID-keyed DM answers, no index map
        idx = int(m.group(1))
        if idx >= len(questions):
            return None, STATUS_EDITED
        q = questions[idx]
        raw_correct = q.get("correct_option_id")
        correct_ids = raw_correct if isinstance(raw_correct, list) else [raw_correct]
        sel = selected if isinstance(selected, list) else [selected]
        try:
            sel = sorted(int(i) for i in sel)
            correct_ids = sorted(int(i) for i in correct_ids)
        except (TypeError, ValueError):
            return None, STATUS_EDITED
        compared = correct_ids if len(correct_ids) > 1 else (correct_ids[0] if correct_ids else None)
        outcome = OUTCOME_CORRECT if is_correct(sel, compared) else OUTCOME_INCORRECT
        results.append({
            "q_index": idx,
            "selected": sel,
            "correct_option": correct_ids,
            "outcome": outcome,
            "time_taken": None,  # genuinely unknown for history
        })
    results.sort(key=lambda r: r["q_index"])
    return results, None


async def backfill_once(*, dry_run: bool = False, limit: Optional[int] = None,
                        batch: int = 200, db: Any = None,
                        quiz_cache_maxsize: int = 32) -> dict:
    db = db or get_db()
    attempts_col = db.collection("quiz_attempts")
    analytics = AnalyticsService(db)

    query = {"status": "completed", "analytics_backfill": {"$exists": False}}
    cursor = attempts_col.find(query).sort("time_ended", 1)
    if limit:
        cursor = cursor.limit(limit)

    summary: dict[str, int] = {}
    # Bounded LRU of PROJECTED quiz documents only; a library with millions
    # of quizzes cannot inflate this pass's memory on the 2 GB VPS.
    quiz_cache = _BoundedQuizCache(db, maxsize=quiz_cache_maxsize)

    def tally(status: str) -> None:
        summary[status] = summary.get(status, 0) + 1

    processed = 0
    async for attempt in cursor:
        processed += 1
        attempt_id = attempt["attempt_id"]
        user_id = attempt.get("user_id")
        qid = attempt.get("qid", "")

        # Already canonical (written by the Phase B live boundary).
        if isinstance(attempt.get("question_results"), list):
            status, reason, events_n = STATUS_CANONICAL, None, 0
        else:
            quiz = await quiz_cache.get(qid)
            if quiz is None:
                status, reason, events_n = STATUS_DELETED, "quiz document missing", 0
            elif not (attempt.get("answers") or {}):
                # Mini App legacy attempts persisted no answer map; there is
                # nothing to derive. Skipped questions aren't guessed.
                status, reason, events_n = STATUS_EMPTY, "no answer map stored", 0
            else:
                results, fail = legacy_question_results(quiz, attempt)
                if fail:
                    status, reason, events_n = fail, "coordinates cannot be resolved", 0
                else:
                    events_n = 0
                    if not dry_run and isinstance(user_id, int):
                        # time_ended is a batch-level timestamp only.
                        at = attempt.get("time_ended") or _now_iso()
                        out = await analytics.record_completion(
                            user_id=user_id,
                            attempt_id=attempt_id,
                            qid=qid,
                            quiz_name=attempt.get("quiz_name", qid),
                            question_results=results,
                            source="unknown",  # channel not stored historically
                            quiz_persisted=True,  # only saved quizzes were recorded pre-Phase B
                            questions=quiz.get("questions", []),
                            sections=quiz.get("sections", []),
                            score=attempt.get("score", 0),
                            correct=attempt.get("correct"),
                            wrong=attempt.get("wrong"),
                            total_time=attempt.get("total_time", 0),
                            finalize=False,       # do not re-complete/re-leaderboard
                            apply_mistakes=False,  # stats/mistakes already written live
                            backfilled=True,
                            at=at,
                        )
                        events_n = out["events_inserted"]
                    status = STATUS_BACKFILLED if not dry_run else "would_backfill"
                    reason = None

        tally(status)
        if not dry_run:
            await attempts_col.update_one(
                {"attempt_id": attempt_id},
                {"$set": {"analytics_backfill": {
                    "status": status,
                    "reason": reason,
                    "events_inserted": events_n,
                    "backfilled_at": _now_iso(),
                }}},
            )

        if processed % batch == 0:
            # Cooperative pause; safe to interrupt and resume later.
            await asyncio.sleep(0)

    return {
        "processed": processed,
        "summary": summary,
        "dry_run": dry_run,
        "quiz_cache_misses": quiz_cache.misses,
        "quiz_cache_size": len(quiz_cache),
        "quiz_cache_maxsize": quiz_cache.maxsize,
    }


async def _main(dry_run: bool, limit: Optional[int], batch: int) -> None:
    await init_db(config.MONGODB_URI, config.MONGODB_DB_NAME)
    try:
        result = await backfill_once(dry_run=dry_run, limit=limit, batch=batch)
    finally:
        await close_db()
    print("=== Phase B backfill ===")
    print(f"dry_run: {result['dry_run']}")
    print(f"processed: {result['processed']}")
    for status, n in sorted(result["summary"].items()):
        print(f"  {status}: {n}")
    unrecoverable = sum(
        n for s, n in result["summary"].items() if s in UNRECOVERABLE
    )
    print(f"unrecoverable total: {unrecoverable}")
    # Bounded-memory proof for operators: resident cache size can never
    # exceed maxsize, no matter how large the quiz library.
    print(
        "quiz cache: "
        f"{result['quiz_cache_size']}/{result['quiz_cache_maxsize']} resident, "
        f"{result['quiz_cache_misses']} distinct quizzes fetched"
    )
    if result["dry_run"]:
        print("Dry run only -- no documents were changed. Re-run without --dry-run to apply.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase B analytics backfill")
    parser.add_argument("--dry-run", action="store_true", help="classify only; write nothing")
    parser.add_argument("--limit", type=int, default=None, help="max attempts this run")
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()
    asyncio.run(_main(args.dry_run, args.limit, args.batch))


if __name__ == "__main__":
    main()
