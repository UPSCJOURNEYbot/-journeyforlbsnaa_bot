"""Canonical question-event repository (``question_events`` collection).

One record per (user, attempt, question). Writes are idempotent upserts keyed
on that triple, so replaying a completion (duplicate callback, retry,
backfill re-run) can never create duplicate analytics rows.

Every read method requires an explicit ``user_id`` -- there is deliberately
no user-less read here; cross-user aggregation belongs to the Phase F admin
layer, not this repository.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from pymongo import UpdateOne

from .metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    OUTCOME_SKIPPED,
    TOPIC_SOURCE_QUESTION,
    snapshot_content_hash,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

_EVENT_NAMESPACE = uuid.UUID("4f9f5b23-7c44-4b6a-8e9d-2f6c1a7d3011")  # fixed, arbitrary
_FIELDS_ON_INSERT = (
    "qid", "quiz_name", "source", "quiz_persisted", "subject", "topic",
    "subtopic", "difficulty", "topic_source", "selected_option",
    "correct_option", "outcome", "time_taken", "answered_at",
    "snapshot_id",
)


class QuestionSnapshotRepository:
    """Content-addressed minimal question snapshots.

    Snapshot text is stored EXACTLY ONCE per distinct question content
    (sha256 over question text + options + correct answer), instead of being
    copied onto every question_event / user_mistakes row. Events and mistake
    rows reference it by ``snapshot_id``. The collection is append-only and
    never garbage-collected, so a snapshot stays resolvable after a quiz is
    edited in place (the new content gets a new hash) or deleted entirely.
    """

    def __init__(self, db=None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.col = db.collection("question_snapshots")

    @staticmethod
    def hash_for(snapshot: Optional[dict]) -> Optional[str]:
        return snapshot_content_hash(snapshot)

    async def ensure(self, snapshots: Iterable[dict], at: Optional[str] = None) -> dict[str, dict]:
        """Idempotently store the given snapshot contents. Returns a
        ``{snapshot_id: snapshot_doc}`` map for every distinct content."""
        by_hash: dict[str, dict] = {}
        for snapshot in snapshots or []:
            sid = snapshot_content_hash(snapshot)
            if sid and sid not in by_hash:
                by_hash[sid] = {
                    "snapshot_id": sid,
                    "question": snapshot["question"],
                    "options": list(snapshot["options"]),
                    "correct_option_id": snapshot["correct_option_id"],
                    "created_at": at or _iso_now(),
                }
        if by_hash:
            ops = [
                UpdateOne(
                    {"snapshot_id": sid},
                    {"$setOnInsert": doc},
                    upsert=True,
                )
                for sid, doc in by_hash.items()
            ]
            # One bounded bulk round trip per completion; identical content
            # within the batch was already de-duplicated above.
            for start in range(0, len(ops), 500):
                await self.col.bulk_write(ops[start:start + 500], ordered=False)
        return {
            sid: {
                "question": doc["question"],
                "options": doc["options"],
                "correct_option_id": doc["correct_option_id"],
            }
            for sid, doc in by_hash.items()
        }

    async def get_many(self, snapshot_ids: Iterable[str]) -> dict[str, dict]:
        """Resolve snapshot ids to their minimal content in ONE query."""
        ids = [sid for sid in dict.fromkeys(snapshot_ids) if sid]
        if not ids:
            return {}
        out: dict[str, dict] = {}
        cursor = self.col.find({"snapshot_id": {"$in": ids}})
        async for row in cursor:
            out[row["snapshot_id"]] = {
                "question": row.get("question", ""),
                "options": row.get("options", []),
                "correct_option_id": row.get("correct_option_id"),
            }
        return out


def event_id_for(user_id: int, attempt_id: str, question_index: int) -> str:
    """Deterministic natural id for an answer event (idempotency key)."""
    return uuid.uuid5(
        _EVENT_NAMESPACE, f"question_event:{user_id}:{attempt_id}:{question_index}"
    ).hex


class QuestionEventRepository:
    def __init__(self, db=None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.col = db.collection("question_events")

    # ------------------------------------------------------------------ writes

    async def record_many(
        self,
        user_id: int,
        attempt_id: str,
        events: list[dict],
        *,
        backfilled: bool = False,
    ) -> int:
        """Idempotently upsert one user's question events for one attempt.

        Every descriptive field (including the identity snapshot) lives in
        ``$setOnInsert``: a replay matches the existing document and changes
        nothing. Returns the number of *new* event documents created.
        """
        if not events:
            return 0
        ops: list[UpdateOne] = []
        for ev in events:
            q_index = int(ev["question_index"])
            set_on_insert: dict[str, Any] = {
                "event_id": event_id_for(user_id, attempt_id, q_index),
                "user_id": user_id,
                "attempt_id": attempt_id,
                "question_index": q_index,
                "created_at": ev.get("created_at"),
                "backfilled": bool(backfilled),
            }
            for key in _FIELDS_ON_INSERT:
                if key in ev:
                    set_on_insert[key] = ev[key]
            ops.append(UpdateOne(
                {
                    "user_id": user_id,
                    "attempt_id": attempt_id,
                    "question_index": q_index,
                },
                {"$setOnInsert": set_on_insert},
                upsert=True,
            ))
        result = await self.col.bulk_write(ops, ordered=False)
        return int(getattr(result, "upserted_count", 0) or 0)

    # ------------------------------------------------------------------- reads

    async def get_overview(self, user_id: int) -> dict:
        """Faceted counts for one user: totals + answered-only timing +
        distinct quiz/attempt sets + daily activity + topic coverage.
        Aggregation runs in MongoDB; nothing is pulled into Python."""
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$facet": {
                "totals": [
                    {"$group": {
                        "_id": None,
                        "questions": {"$sum": 1},
                        "correct": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_CORRECT]}, 1, 0]}},
                        "incorrect": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_INCORRECT]}, 1, 0]}},
                        "skipped": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_SKIPPED]}, 1, 0]}},
                        "quiz_ids": {"$addToSet": "$qid"},
                        "attempt_ids": {"$addToSet": "$attempt_id"},
                        "topics": {"$addToSet": "$topic"},
                    }},
                ],
                "answered": [
                    {"$match": {"outcome": {"$in": [OUTCOME_CORRECT, OUTCOME_INCORRECT]}}},
                    {"$group": {
                        "_id": None,
                        "answered": {"$sum": 1},
                        "time_total": {"$sum": {"$cond": [{"$gte": ["$time_taken", 0]}, "$time_taken", 0]}},
                        "timed_questions": {"$sum": {"$cond": [{"$gte": ["$time_taken", 0]}, 1, 0]}},
                        "first_at": {"$min": "$answered_at"},
                        "last_at": {"$max": "$answered_at"},
                    }},
                ],
                "daily": [
                    {"$match": {"outcome": {"$in": [OUTCOME_CORRECT, OUTCOME_INCORRECT]},
                                "answered_at": {"$gte": " "}}},
                    {"$group": {
                        "_id": {"$substrCP": ["$answered_at", 0, 10]},
                        "answered": {"$sum": 1},
                        "correct": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_CORRECT]}, 1, 0]}},
                        "attempt_ids": {"$addToSet": "$attempt_id"},
                    }},
                    {"$sort": {"_id": 1}},
                ],
            }},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        if not rows:
            return {}
        row = rows[0]
        totals = (row.get("totals") or [{}])[0]
        answered = (row.get("answered") or [{}])[0]
        totals.pop("_id", None)
        answered.pop("_id", None)
        quiz_ids = [q for q in totals.pop("quiz_ids", []) if q is not None]
        attempt_ids = [a for a in totals.pop("attempt_ids", []) if a is not None]
        topics = [t for t in totals.pop("topics", []) if t]
        daily = []
        for d in row.get("daily", []):
            daily.append({
                "day": d.get("_id"),
                "answered": d.get("answered", 0),
                "correct": d.get("correct", 0),
                "attempt_count": len([a for a in d.get("attempt_ids", []) if a is not None]),
            })
        return {
            **totals,
            "quiz_count": len(set(quiz_ids)),
            "attempt_count": len(set(attempt_ids)),
            "topic_count": len(set(topics)),
            "daily": daily,
            **answered,
        }

    async def get_topic_performance(self, user_id: int) -> list[dict]:
        """Per-topic rollup with identity-aware grouping.

        Labels are never synonym-merged. Identity rules (no taxonomy is
        assumed; raw labels are preserved):

        * explicit question metadata (``topic_source="question"``) aggregates
          by exact ``(subject, topic)`` ACROSS quizzes -- same subject + same
          topic is intentionally pooled, different subjects (including one
          known and one unknown) stay in distinct buckets;
        * section-derived topics (``topic_source="section"``) are free-text
          names scoped to one quiz, so identical generic names like
          "Section 1"/"Basics"/"Mixed" in different quizzes must not pool:
          the group key additionally includes ``qid`` for those rows.
        """
        section_source = "section"
        pipeline = [
            {"$match": {"user_id": user_id, "topic": {"$ne": None}}},
            {"$group": {
                "_id": {
                    "subject": "$subject",
                    "topic": "$topic",
                    "topic_source": "$topic_source",
                    # Section names are only meaningful within their quiz;
                    # explicit metadata topics span quizzes (scope qid=null).
                    "scope_qid": {
                        "$cond": [
                            {"$eq": ["$topic_source", section_source]},
                            "$qid", None,
                        ]
                    },
                },
                "correct": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_CORRECT]}, 1, 0]}},
                "incorrect": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_INCORRECT]}, 1, 0]}},
                "skipped": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_SKIPPED]}, 1, 0]}},
                "time_total": {"$sum": {"$cond": [
                    {"$and": [{"$ne": ["$outcome", OUTCOME_SKIPPED]}, {"$gte": ["$time_taken", 0]}]},
                    "$time_taken", 0]}},
                "timed_questions": {"$sum": {"$cond": [
                    {"$and": [{"$ne": ["$outcome", OUTCOME_SKIPPED]}, {"$gte": ["$time_taken", 0]}]},
                    1, 0]}},
                "attempt_ids": {"$addToSet": "$attempt_id"},
            }},
        ]
        out: list[dict] = []
        async for row in self.col.aggregate(pipeline):
            key = row.get("_id", {})
            out.append({
                "subject": key.get("subject"),
                "topic": key.get("topic"),
                "topic_source": key.get("topic_source"),
                # Set only for quiz-scoped (section-derived) buckets; this is
                # the scope discriminator, never used to merge labels.
                "qid": key.get("scope_qid"),
                "correct": row.get("correct", 0),
                "incorrect": row.get("incorrect", 0),
                "skipped": row.get("skipped", 0),
                "time_total": round(row.get("time_total", 0.0), 2),
                "timed_questions": row.get("timed_questions", 0),
                "attempt_count": len(row.get("attempt_ids", [])),
            })
        return out

    async def get_question_performance(
        self, user_id: int, qid: Optional[str] = None, limit: int = 200,
        qids: Optional[list[str]] = None,
    ) -> list[dict]:
        """Per (quiz, question) rollup with the latest identity snapshot.

        ``qids`` (Phase E) restricts the read to a BOUNDED list of stored
        quiz ids (the user's played/owned practice pool); it is used instead
        of the single ``qid`` filter when supplied."""
        match: dict[str, Any] = {"user_id": user_id}
        if qids:
            match["qid"] = {"$in": list(qids)}
        elif qid is not None:
            match["qid"] = qid
        pipeline = [
            {"$match": match},
            {"$sort": {"created_at": 1}},
            {"$group": {
                "_id": {"qid": "$qid", "question_index": "$question_index"},
                "times_seen": {"$sum": 1},
                "correct": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_CORRECT]}, 1, 0]}},
                "incorrect": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_INCORRECT]}, 1, 0]}},
                "skipped": {"$sum": {"$cond": [{"$eq": ["$outcome", OUTCOME_SKIPPED]}, 1, 0]}},
                "last_outcome": {"$last": "$outcome"},
                "subject": {"$last": "$subject"},
                "topic": {"$last": "$topic"},
                "subtopic": {"$last": "$subtopic"},
                "difficulty": {"$last": "$difficulty"},
                "snapshot_id": {"$last": "$snapshot_id"},
                # Rows written before the content-addressed store existed
                # carried an embedded snapshot; keep carrying it through so
                # legacy history stays readable without a migration.
                "legacy_snapshot": {"$last": "$question_snapshot"},
                "last_answered_at": {"$max": "$answered_at"},
            }},
            {"$sort": {"incorrect": -1, "times_seen": -1}},
            {"$limit": limit},
        ]
        out: list[dict] = []
        async for row in self.col.aggregate(pipeline):
            key = row.get("_id", {})
            item = {
                "qid": key.get("qid"),
                "question_index": key.get("question_index"),
                "times_seen": row.get("times_seen", 0),
                "correct": row.get("correct", 0),
                "incorrect": row.get("incorrect", 0),
                "skipped": row.get("skipped", 0),
                "last_outcome": row.get("last_outcome"),
                "subject": row.get("subject"),
                "topic": row.get("topic"),
                "subtopic": row.get("subtopic"),
                "difficulty": row.get("difficulty"),
                "snapshot_id": row.get("snapshot_id"),
                "last_answered_at": row.get("last_answered_at"),
            }
            # Only surface the legacy embedded snapshot when it exists;
            # referenced snapshots are resolved by the service layer.
            if row.get("legacy_snapshot"):
                item["question_snapshot"] = row["legacy_snapshot"]
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # Phase E: bounded, user-scoped reads powering /weakquiz. Every read
    # is user-prefixed (index-backed) and capped; weak selection never
    # scans the whole events collection or the question bank.
    # ------------------------------------------------------------------

    async def distinct_played_qids(
        self, user_id: int, limit: int = 50
    ) -> list[str]:
        """Bounded set of STORED quiz ids this user has answered questions
        in, most recently answered first. Ad-hoc synthetic qids (AI/PDF/
        revision/weak sessions, ``quiz_persisted`` false) are excluded --
        they have no stored quiz document and would never match the bank."""
        pipeline = [
            {"$match": {
                "user_id": user_id,
                "quiz_persisted": True,
                "qid": {"$ne": None},
            }},
            {"$group": {
                "_id": "$qid",
                "last_answered": {"$max": "$answered_at"},
                "last_created": {"$max": "$created_at"},
            }},
            {"$sort": {"last_answered": -1, "last_created": -1, "_id": 1}},
            {"$limit": int(limit)},
        ]
        return [r["_id"] async for r in self.col.aggregate(pipeline)
                if r.get("_id")]

    async def get_practice_rollups(
        self, user_id: int, recent_cutoff: str, limit: int = 100
    ) -> dict:
        """One bounded ``$facet`` returning everything /weakquiz needs to
        rank the user's OWN topics:

        * ``topics``       -- explicit-metadata buckets keyed (subject,
          topic), pooled across quizzes (section-derived names are
          deliberately excluded -- they are quiz-scoped free text);
        * ``subject_only`` -- questions carrying a subject but no topic,
          the clearly-labelled 'Subject \u00b7 untagged' fallback;
        * ``answered``     -- total answered questions, for empty/
          insufficient-state messaging.

        Each bucket carries correct/incorrect/skipped counts, the number of
        distinct attempts, last answered time and recent incorrect answers
        (on/after ``recent_cutoff``, a lexicographically-comparable
        ``%Y-%m-%d %H:%M:%S`` UTC string)."""
        def _group(id_expr):
            return [
                {"$group": {
                    "_id": id_expr,
                    "correct": {"$sum": {"$cond": [
                        {"$eq": ["$outcome", OUTCOME_CORRECT]}, 1, 0]}},
                    "incorrect": {"$sum": {"$cond": [
                        {"$eq": ["$outcome", OUTCOME_INCORRECT]}, 1, 0]}},
                    "skipped": {"$sum": {"$cond": [
                        {"$eq": ["$outcome", OUTCOME_SKIPPED]}, 1, 0]}},
                    "attempt_ids": {"$addToSet": "$attempt_id"},
                    "last_answered_at": {"$max": "$answered_at"},
                    "recent_incorrect": {"$sum": {"$cond": [
                        {"$and": [
                            {"$eq": ["$outcome", OUTCOME_INCORRECT]},
                            {"$gte": ["$answered_at", recent_cutoff]},
                        ]},
                        1, 0]}},
                }},
                {"$limit": int(limit)},
            ]

        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$facet": {
                "topics": (
                    [{"$match": {
                        "topic": {"$ne": None},
                        "topic_source": TOPIC_SOURCE_QUESTION,
                    }}]
                    + _group({"subject": "$subject", "topic": "$topic"})
                ),
                "subject_only": (
                    [{"$match": {
                        "subject": {"$ne": None},
                        "$or": [
                            {"topic": None},
                            {"topic": {"$exists": False}},
                        ],
                    }}]
                    + _group({"subject": "$subject"})
                ),
                "answered": [
                    {"$match": {"outcome": {
                        "$in": [OUTCOME_CORRECT, OUTCOME_INCORRECT]}}},
                    {"$group": {"_id": None, "n": {"$sum": 1}}},
                ],
            }},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        if not rows:
            return {"topics": [], "subject_only": [], "answered": 0}
        row = rows[0]

        def _normalize(rows_out, *, subject_only):
            out = []
            for r in rows_out or []:
                key = r.get("_id") or {}
                subject = key.get("subject")
                out.append({
                    "subject": subject,
                    "topic": None if subject_only else key.get("topic"),
                    "subject_only": subject_only,
                    "correct": r.get("correct", 0),
                    "incorrect": r.get("incorrect", 0),
                    "skipped": r.get("skipped", 0),
                    "attempt_count": len(
                        [a for a in r.get("attempt_ids", []) if a]),
                    "recent_incorrect": r.get("recent_incorrect", 0),
                    "last_answered_at": r.get("last_answered_at"),
                })
            return out

        answered_rows = row.get("answered") or []
        answered = answered_rows[0]["n"] if answered_rows else 0
        return {
            "topics": _normalize(row.get("topics"), subject_only=False),
            "subject_only": _normalize(
                row.get("subject_only"), subject_only=True),
            "answered": answered,
        }

    async def get_seen_ad_hoc(
        self, user_id: int, buckets: list[tuple], recent_cutoff: str,
        limit: int = 100,
    ) -> list[dict]:
        """Questions the user answered in quizzes WITHOUT a stored quiz
        document (AI/PDF/mix/ad-hoc), recoverable only via their content
        snapshot. ``buckets`` is a list of ``(subject, topic)`` keys (topic
        None = subject-only). Returns one row per distinct ``snapshot_id``
        with personal history; content is resolved by the service via one
        bounded ``question_snapshots`` $in."""
        if not buckets:
            return []
        ors = []
        for subject, topic in buckets:
            cond: dict[str, Any] = {}
            if topic is None:
                cond["$or"] = [
                    {"topic": None}, {"topic": {"$exists": False}}]
            else:
                cond["topic"] = topic
            if subject:
                cond["subject"] = subject
            ors.append(cond)
        pipeline = [
            {"$match": {
                "user_id": user_id,
                "quiz_persisted": False,
                "snapshot_id": {"$ne": None},
                "outcome": {"$in": [OUTCOME_CORRECT, OUTCOME_INCORRECT]},
                "$or": ors,
            }},
            {"$sort": {"answered_at": -1}},
            {"$group": {
                "_id": "$snapshot_id",
                "subject": {"$last": "$subject"},
                "topic": {"$last": "$topic"},
                "difficulty": {"$last": "$difficulty"},
                "correct": {"$sum": {"$cond": [
                    {"$eq": ["$outcome", OUTCOME_CORRECT]}, 1, 0]}},
                "incorrect": {"$sum": {"$cond": [
                    {"$eq": ["$outcome", OUTCOME_INCORRECT]}, 1, 0]}},
                "last_outcome": {"$last": "$outcome"},
                "last_answered_at": {"$max": "$answered_at"},
                "times_seen": {"$sum": 1},
            }},
            {"$limit": int(limit)},
        ]
        out = []
        async for r in self.col.aggregate(pipeline):
            out.append({
                "snapshot_id": r.get("_id"),
                "subject": r.get("subject"),
                "topic": r.get("topic"),
                "difficulty": r.get("difficulty"),
                "correct": r.get("correct", 0),
                "incorrect": r.get("incorrect", 0),
                "last_outcome": r.get("last_outcome"),
                "last_answered_at": r.get("last_answered_at"),
                "times_seen": r.get("times_seen", 0),
            })
        return out
