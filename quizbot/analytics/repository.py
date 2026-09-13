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
from typing import Any, Optional

from pymongo import UpdateOne

from .metadata import OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED

_EVENT_NAMESPACE = uuid.UUID("4f9f5b23-7c44-4b6a-8e9d-2f6c1a7d3011")  # fixed, arbitrary
_FIELDS_ON_INSERT = (
    "qid", "quiz_name", "source", "quiz_persisted", "subject", "topic",
    "subtopic", "difficulty", "topic_source", "selected_option",
    "correct_option", "outcome", "time_taken", "answered_at",
    "question_snapshot",
)


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
        """Per exact (subject, topic, topic_source) rollup. Labels are never
        synonym-merged."""
        pipeline = [
            {"$match": {"user_id": user_id, "topic": {"$ne": None}}},
            {"$group": {
                "_id": {
                    "subject": "$subject",
                    "topic": "$topic",
                    "topic_source": "$topic_source",
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
                "correct": row.get("correct", 0),
                "incorrect": row.get("incorrect", 0),
                "skipped": row.get("skipped", 0),
                "time_total": round(row.get("time_total", 0.0), 2),
                "timed_questions": row.get("timed_questions", 0),
                "attempt_count": len(row.get("attempt_ids", [])),
            })
        return out

    async def get_question_performance(
        self, user_id: int, qid: Optional[str] = None, limit: int = 200
    ) -> list[dict]:
        """Per (quiz, question) rollup with the latest identity snapshot."""
        match: dict[str, Any] = {"user_id": user_id}
        if qid is not None:
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
                "snapshot": {"$last": "$question_snapshot"},
                "last_answered_at": {"$max": "$answered_at"},
            }},
            {"$sort": {"incorrect": -1, "times_seen": -1}},
            {"$limit": limit},
        ]
        out: list[dict] = []
        async for row in self.col.aggregate(pipeline):
            key = row.get("_id", {})
            out.append({
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
                "question_snapshot": row.get("snapshot"),
                "last_answered_at": row.get("last_answered_at"),
            })
        return out
