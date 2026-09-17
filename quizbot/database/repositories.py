"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo import ReturnDocument, UpdateOne

from quizbot.analytics.metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    normalize_question,
)

from .db import Database


def _now_iso() -> str:
    """Kept as a plain ISO-format string (not a native datetime) so every
    existing `datetime.strptime(row["...at"], "%Y-%m-%d %H:%M:%S")` call
    elsewhere in the codebase (there are a few, e.g. AttemptRepository's
    own elapsed-time math below, and is_premium's expiry check) keeps
    working unmodified -- storing a plain string field in Mongo is just as
    natural as storing one in a SQLite TEXT column."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clean(doc: Optional[dict]) -> Optional[dict]:
    """Strip Mongo's own `_id` (an ObjectId, not JSON/caller-friendly) from
    a document before handing it back to a caller. Every collection has its
    own business-key field (qid, attempt_id, chat_id, ...) that callers
    already use instead, so `_id` itself is never meaningful to them."""
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


class UserRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("users")

    async def get_or_create(self, chat_id: int) -> dict:
        row = await self.col.find_one({"chat_id": chat_id})
        if row is None:
            doc = {
                "chat_id": chat_id,
                "remove_words": [],
                "is_premium": False,
                "premium_until": None,
                "language": "en",
                "created_at": _now_iso(),
                "last_active": _now_iso(),
            }
            await self.col.insert_one(doc)
            row = doc
        else:
            await self.col.update_one(
                {"chat_id": chat_id}, {"$set": {"last_active": _now_iso()}}
            )
            row["last_active"] = _now_iso()
        return _clean(row)

    async def get(self, chat_id: int) -> Optional[dict]:
        row = await self.col.find_one({"chat_id": chat_id})
        return _clean(row)

    async def get_all(self, limit: int = 1000, offset: int = 0) -> list[dict]:
        cursor = self.col.find().sort("_id", -1).skip(offset).limit(limit)
        return [_clean(r) async for r in cursor]

    async def update_remove_words(self, chat_id: int, remove_words: list[str]) -> None:
        await self.get_or_create(chat_id)
        await self.col.update_one(
            {"chat_id": chat_id}, {"$set": {"remove_words": remove_words}}
        )

    async def is_premium(self, chat_id: int) -> bool:
        row = await self.col.find_one(
            {"chat_id": chat_id}, {"is_premium": 1, "premium_until": 1}
        )
        if row is None or not row.get("is_premium"):
            return False
        if row.get("premium_until") is None:
            return True  # permanent premium
        try:
            expiry = datetime.strptime(row["premium_until"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return False
        return expiry > datetime.now(timezone.utc)

    async def set_premium(self, chat_id: int, days: Optional[int] = 30) -> Optional[str]:
        """Grant/extend premium and return the resulting expiry timestamp.

        Paid renewals extend an already-active subscription instead of
        overwriting its remaining time. Permanent premium (days=None) remains
        permanent. The calculation is done from the later of the existing
        expiry and now, so expired subscriptions start from the current time.
        """
        await self.get_or_create(chat_id)
        if days is None:
            premium_until = None
        else:
            row = await self.col.find_one({"chat_id": chat_id}, {"premium_until": 1})
            base = datetime.now(timezone.utc)
            existing = row.get("premium_until") if row else None
            if existing:
                try:
                    existing_dt = datetime.strptime(existing, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                    if existing_dt > base:
                        base = existing_dt
                except (TypeError, ValueError):
                    pass
            premium_until = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        await self.col.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_premium": True, "premium_until": premium_until}},
        )
        return premium_until

    async def revoke_premium(self, chat_id: int) -> None:
        await self.col.update_one(
            {"chat_id": chat_id}, {"$set": {"is_premium": False, "premium_until": None}}
        )

    async def list_active_premium(self) -> list[dict]:
        """Every user currently on active premium (permanent or unexpired),
        ordered by expiry -- mirrors the original PHP `PremiumAPI::getAllPremium`."""
        cursor = self.col.find(
            {
                "is_premium": True,
                "$or": [{"premium_until": None}, {"premium_until": {"$gt": _now_iso()}}],
            }
        ).sort([("premium_until", 1)])
        # Mongo's ascending sort already puts None values first (BSON type
        # ordering: Null < String), matching the old
        # "ORDER BY premium_until IS NULL, premium_until ASC" exactly.
        return [_clean(r) async for r in cursor]

    async def stats(self) -> dict:
        total = await self.col.count_documents({})
        premium = await self.col.count_documents(
            {
                "is_premium": True,
                "$or": [{"premium_until": None}, {"premium_until": {"$gt": _now_iso()}}],
            }
        )
        return {"total_users": total, "premium_users": premium}


class QuizRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("quizzes")

    @staticmethod
    def _new_qid() -> str:
        return uuid.uuid4().hex[:10]

    async def create(self, creator_id: int, quiz_name: str, questions: list[dict], **kwargs) -> dict:
        # Phase B: persist the OPTIONAL per-question analytics block
        # (subject/topic/subtopic/difficulty) when present. This only
        # sanitises and stores metadata the creator/importer already
        # supplied; wording, options and answers are untouched and nothing
        # is ever fabricated for questions without metadata.
        questions = [normalize_question(q) for q in questions]
        qid = kwargs.get("qid") or self._new_qid()
        doc = {
            "qid": qid,
            "creator_id": creator_id,
            "quiz_name": quiz_name,
            "questions": questions,
            "sections": kwargs.get("sections", []),
            "timer": kwargs.get("timer", 60),
            "quiz_type": kwargs.get("quiz_type", "free"),
            "negative_marks": kwargs.get("negative_marks", 0),
            "correct_marks": kwargs.get("correct_marks", 1),
            "shuffle_questions": bool(kwargs.get("shuffle_questions", False)),
            "shuffle_options": bool(kwargs.get("shuffle_options", False)),
            "show_explanation": bool(kwargs.get("show_explanation", False)),
            "html_report": bool(kwargs.get("html_report", False)),
            "pdf_report": bool(kwargs.get("pdf_report", False)),
            "fixed_settings": bool(kwargs.get("fixed_settings", False)),
            "edit_permissions": [],
            "promo_message": kwargs.get("promo_message"),
            "search_indexed": True,
            "total_participants": 0,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        await self.col.insert_one(doc)
        return await self.get(qid)

    async def get(self, qid: str) -> Optional[dict]:
        row = await self.col.find_one({"qid": qid})
        return _clean(row)

    async def update_field(self, qid: str, field: str, value: Any) -> None:
        allowed = {
            "quiz_name", "questions", "sections", "timer", "quiz_type",
            "negative_marks", "correct_marks", "shuffle_questions", "shuffle_options",
            "show_explanation", "html_report", "pdf_report", "fixed_settings",
            "edit_permissions", "promo_message", "search_indexed",
        }
        if field not in allowed:
            raise ValueError(f"Field '{field}' is not updatable")
        if field == "questions" and isinstance(value, list):
            # Same storage chokepoint as create(): creator edits can remove
            # or reorder options, so Phase F option notes must be revalidated
            # against the final option count (out-of-range notes dropped,
            # never re-pointed). Legacy questions come through unchanged.
            value = [normalize_question(q) for q in value]
        await self.col.update_one(
            {"qid": qid}, {"$set": {field: value, "updated_at": _now_iso()}}
        )

    async def delete(self, qid: str) -> None:
        await self.col.delete_one({"qid": qid})

    async def list_by_creator(self, creator_id: int, query: Optional[str] = None) -> list[dict]:
        filt: dict = {"creator_id": creator_id}
        if query:
            filt["quiz_name"] = {"$regex": _escape_regex(query), "$options": "i"}
        cursor = self.col.find(filt).sort("created_at", -1)
        return [self._light(r) async for r in cursor]

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[dict]:
        cursor = self.col.find().sort("created_at", -1).skip(offset).limit(limit)
        return [self._light(r) async for r in cursor]

    async def search(self, query: str, limit: int = 20, offset: int = 0) -> list[dict]:
        """Search publicly-indexed quizzes by name, newest-plays-first.

        Excludes a quiz both when its own `search_indexed` flag is off AND
        when its creator has since turned OFF "Search Index" in /settings
        (creator_settings.search_indexed) -- the latter is a live,
        retroactive opt-out: flipping it off in /settings immediately pulls
        every quiz that creator owns out of /search, not just future ones.
        Supports offset-based pagination so /search isn't capped at
        whatever `limit` the first page used -- callers can page through
        the full result set with repeated calls."""
        pattern = _escape_regex(query)
        pipeline = [
            {"$match": {"search_indexed": True, "quiz_name": {"$regex": pattern, "$options": "i"}}},
            {
                "$lookup": {
                    "from": "creator_settings",
                    "localField": "creator_id",
                    "foreignField": "user_id",
                    "as": "_creator_settings",
                }
            },
            {
                "$match": {
                    "$or": [
                        {"_creator_settings": {"$size": 0}},  # no settings row yet -> defaults to indexed
                        {"_creator_settings.0.search_indexed": {"$ne": False}},
                    ]
                }
            },
            {"$project": {"_creator_settings": 0}},
            {"$sort": {"total_participants": -1}},
            {"$skip": offset},
            {"$limit": limit},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        return [self._light(r) for r in rows]

    async def search_count(self, query: str) -> int:
        """Total number of quizzes /search would match for `query`, ignoring
        limit/offset -- used to show accurate "X of Y" / enable "Load more"
        without capping the searchable set."""
        pattern = _escape_regex(query)
        pipeline = [
            {"$match": {"search_indexed": True, "quiz_name": {"$regex": pattern, "$options": "i"}}},
            {
                "$lookup": {
                    "from": "creator_settings",
                    "localField": "creator_id",
                    "foreignField": "user_id",
                    "as": "_creator_settings",
                }
            },
            {
                "$match": {
                    "$or": [
                        {"_creator_settings": {"$size": 0}},
                        {"_creator_settings.0.search_indexed": {"$ne": False}},
                    ]
                }
            },
            {"$count": "n"},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        return rows[0]["n"] if rows else 0

    async def set_promo_for_creator(self, creator_id: int, promo_message: str) -> int:
        result = await self.col.update_many(
            {"creator_id": creator_id},
            {"$set": {"promo_message": promo_message, "updated_at": _now_iso()}},
        )
        return result.modified_count

    async def reassign_owner(self, old_creator_id: int, new_creator_id: int) -> None:
        await self.col.update_many(
            {"creator_id": old_creator_id}, {"$set": {"creator_id": new_creator_id}}
        )
        await self.db.collection("batches").update_many(
            {"creator_id": old_creator_id}, {"$set": {"creator_id": new_creator_id}}
        )
        await self.db.collection("auth_chats").update_many(
            {"creator_id": old_creator_id}, {"$set": {"creator_id": new_creator_id}}
        )

    async def increment_participants(self, qid: str) -> None:
        await self.col.update_one({"qid": qid}, {"$inc": {"total_participants": 1}})

    # ------------------------------------------------------------------
    # Phase E: bounded practice-candidate extraction. The qid set is
    # already restricted to the user's recently-played/owned quizzes, so
    # this never scans the whole bank; matching embedded questions and
    # projection happen server-side (no full quiz documents cross the
    # wire), and the result is capped.
    # ------------------------------------------------------------------

    async def list_qids_by_creator(
        self, creator_id: int, limit: int = 50
    ) -> list[str]:
        """Lean qid-only listing of quizzes owned by this user."""
        cursor = (
            self.col.find({"creator_id": creator_id}, {"_id": 0, "qid": 1})
            .sort("created_at", -1)
            .limit(int(limit))
        )
        return [r["qid"] async for r in cursor if r.get("qid")]

    async def topic_candidate_questions(
        self,
        qids: list[str],
        buckets: list[tuple],
        limit: int = 100,
    ) -> list[dict]:
        """Return stored questions whose explicit analytics metadata matches
        one of the chosen practice ``buckets`` (``(subject, topic)``; topic
        None selects the subject-only/'untagged' bucket).

        Output rows: ``{"qid", "q_index", "question"}``. Assignment to a
        bucket is finalized by the caller using each question's OWN
        metadata, so over-broad document-level ``$or`` pre-filters can
        never mislabel a question into the wrong topic or subject."""
        if not qids or not buckets:
            return []

        def _bucket_cond(prefix: str, subject: Optional[str],
                         topic: Optional[str]) -> Optional[dict]:
            if topic is None and not subject:
                return None
            cond: dict[str, Any] = {}
            if subject:
                cond[f"{prefix}subject"] = subject
            if topic is None:
                # Subject-only bucket: subject present, topic absent/null.
                cond["$and"] = cond.get("$and", []) + [
                    {"$or": [
                        {f"{prefix}topic": None},
                        {f"{prefix}topic": {"$exists": False}},
                    ]},
                ]
            else:
                cond[f"{prefix}topic"] = topic
            return cond

        doc_ors, elem_ors = [], []
        for subject, topic in buckets:
            # After ``$unwind "$questions"`` the field keeps the name
            # `questions` but holds a single element object; the rename to
            # `question` happens in the later $project.
            doc_c = _bucket_cond("questions.analytics.", subject, topic)
            elem_c = _bucket_cond("questions.analytics.", subject, topic)
            if doc_c:
                doc_ors.append(doc_c)
            if elem_c:
                elem_ors.append(elem_c)
        if not doc_ors or not elem_ors:
            return []
        pipeline = [
            {"$match": {"qid": {"$in": list(qids)}, "$or": doc_ors}},
            {"$unwind": {"path": "$questions", "includeArrayIndex": "q_index"}},
            {"$match": {"$or": elem_ors}},
            {"$limit": int(limit)},
            {"$project": {
                "_id": 0, "qid": 1, "q_index": 1, "question": "$questions",
            }},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        wanted = {(s, t) for s, t in buckets}
        out = []
        for r in rows:
            question = r.get("question")
            if not isinstance(question, dict):
                continue
            analytics = question.get("analytics") or {}
            subject = analytics.get("subject")
            topic = analytics.get("topic")
            # Exact bucket assignment from the question's own metadata;
            # the subject-only bucket matches explicitly (topic absent).
            key = (subject, topic)
            key_untagged = (subject, None)
            if key not in wanted and key_untagged not in wanted:
                continue
            out.append({"qid": r.get("qid"),
                        "q_index": r.get("q_index"),
                        "question": question})
            if len(out) >= int(limit):
                break
        return out

    async def stats(self) -> dict:
        total = await self.col.count_documents({})
        paid = await self.col.count_documents({"quiz_type": "paid"})
        free = await self.col.count_documents({"quiz_type": "free"})
        return {"total_quizzes": total, "paid_quizzes": paid, "free_quizzes": free}

    @staticmethod
    def _light(row: dict) -> dict:
        """Metadata only -- omit heavy `questions`/`sections` blobs for list views."""
        data = _clean(row)
        data.pop("questions", None)
        data.pop("sections", None)
        return data


class AuthChatRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("auth_chats")

    async def get(self, creator_id: int) -> list[int]:
        row = await self.col.find_one({"creator_id": creator_id})
        return row.get("auth_users", []) if row else []

    async def set(self, creator_id: int, auth_users: list[int]) -> None:
        await self.col.update_one(
            {"creator_id": creator_id},
            {
                "$set": {"auth_users": auth_users},
                "$setOnInsert": {"created_at": _now_iso()},
            },
            upsert=True,
        )

    async def add(self, creator_id: int, chat_id: int) -> list[int]:
        users = await self.get(creator_id)
        if chat_id not in users:
            users.append(chat_id)
            await self.set(creator_id, users)
        return users

    async def remove(self, creator_id: int, chat_id: int) -> list[int]:
        users = await self.get(creator_id)
        if chat_id in users:
            users.remove(chat_id)
            await self.set(creator_id, users)
        return users

    async def clear(self, creator_id: int) -> None:
        await self.set(creator_id, [])


class PaymentRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("payments")

    async def create(
        self,
        user_id: int,
        amount: int,
        plan_days: Optional[int] = None,
        token: Optional[str] = None,
        link_id: Optional[str] = None,
        plan_label: Optional[str] = None,
        expires_at: Optional[int] = None,
    ) -> dict:
        doc = {
            "user_id": user_id,
            "amount": amount,
            "status": "created",
            "payment_method": "razorpay",
            "transaction_id": None,
            "plan_days": plan_days,
            "token": token,
            "link_id": link_id,
            "plan_label": plan_label,
            "expires_at": expires_at,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        result = await self.col.insert_one(doc)
        row = await self.col.find_one({"_id": result.inserted_id})
        return _clean(row)

    async def get_by_token(self, token: str) -> Optional[dict]:
        return _clean(await self.col.find_one({"token": token}))

    async def claim_for_activation(self, token: str, user_id: int) -> Optional[dict]:
        """Atomically reserve a verified paid payment for one activation."""
        row = await self.col.find_one_and_update(
            {"token": token, "user_id": user_id, "status": "paid"},
            {"$set": {"status": "processing", "updated_at": _now_iso()}},
            return_document=ReturnDocument.AFTER,
        )
        return _clean(row)

    async def mark_activated(self, token: str, transaction_id: Optional[str] = None) -> None:
        update = {"status": "activated", "updated_at": _now_iso()}
        if transaction_id:
            update["transaction_id"] = transaction_id
        await self.col.update_one({"token": token}, {"$set": update})

    async def mark_paid(self, token: str, transaction_id: Optional[str] = None) -> None:
        update = {"status": "paid", "updated_at": _now_iso()}
        if transaction_id:
            update["transaction_id"] = transaction_id
        await self.col.update_one(
            {"token": token, "status": {"$in": ["created", "paid", "processing"]}},
            {"$set": update},
        )

    async def get_for_user(self, user_id: int) -> list[dict]:
        cursor = self.col.find({"user_id": user_id}).sort("created_at", -1)
        return [_clean(r) async for r in cursor]

    async def update_latest_status(
        self, user_id: int, status: str, transaction_id: Optional[str] = None
    ) -> Optional[dict]:
        row = await self.col.find_one({"user_id": user_id}, sort=[("created_at", -1)])
        if row is None:
            return None
        await self.col.update_one(
            {"_id": row["_id"]},
            {"$set": {"status": status, "transaction_id": transaction_id, "updated_at": _now_iso()}},
        )
        updated = await self.col.find_one({"_id": row["_id"]})
        return _clean(updated)


def _clean_key(doc: Optional[dict]) -> Optional[dict]:
    """Like _clean(), but keeps the Mongo _id around as a plain string "id"
    field -- callers (ai_keys.py, ai_providers.py) display/round-trip this
    the same way they used to display SQLite's autoincrement `id` column."""
    if doc is None:
        return None
    doc = dict(doc)
    oid = doc.pop("_id", None)
    if oid is not None:
        doc["id"] = str(oid)
    return doc


class AIKeyRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("ai_keys")

    async def list_for_user(self, user_id: int) -> list[dict]:
        cursor = self.col.find({"user_id": user_id}).sort("created_at", 1)
        return [_clean_key(r) async for r in cursor]

    async def list_for_provider(self, user_id: int, provider: str) -> list[dict]:
        cursor = self.col.find({"user_id": user_id, "provider": provider}).sort(
            [("fail_count", 1), ("created_at", 1)]
        )
        return [_clean_key(r) async for r in cursor]

    async def add(self, user_id: int, provider: str, api_key: str, label: Optional[str] = None) -> dict:
        doc = {
            "user_id": user_id,
            "provider": provider,
            "api_key": api_key,
            "label": label,
            "last_used_at": None,
            "fail_count": 0,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        result = await self.col.insert_one(doc)
        row = await self.col.find_one({"_id": result.inserted_id})
        return _clean_key(row)

    async def mark(self, key_id: Any, failed: bool) -> None:
        oid = _as_object_id(key_id)
        if failed:
            await self.col.update_one(
                {"_id": oid}, {"$inc": {"fail_count": 1}, "$set": {"updated_at": _now_iso()}}
            )
        else:
            await self.col.update_one(
                {"_id": oid},
                {"$set": {"last_used_at": _now_iso(), "fail_count": 0, "updated_at": _now_iso()}},
            )

    async def delete_by_id(self, user_id: int, key_id: Any) -> None:
        await self.col.delete_one({"_id": _as_object_id(key_id), "user_id": user_id})

    async def delete_by_provider(self, user_id: int, provider: str) -> None:
        await self.col.delete_many({"user_id": user_id, "provider": provider})

    async def delete_all(self, user_id: int) -> None:
        await self.col.delete_many({"user_id": user_id})


class AttemptRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("quiz_attempts")

    async def start(self, user_id: int, qid: str, quiz_name: str, total_questions: int,
                    source: Optional[str] = None,
                    quiz_persisted: Optional[bool] = None) -> dict:
        attempt_id = uuid.uuid4().hex
        doc = {
            "attempt_id": attempt_id,
            "user_id": user_id,
            "qid": qid,
            "quiz_name": quiz_name,
            "current_question": 0,
            "answers": {},
            "score": 0,
            "total_questions": total_questions,
            "time_started": _now_iso(),
            "time_ended": None,
            "status": "in_progress",
            "paused": False,
            "pause_time": None,
            "total_pause_duration": 0,
        }
        # Phase B provenance -- additive only, omitted when unknown so the
        # legacy document shape is unchanged for callers that don't pass it.
        if source is not None:
            doc["source"] = str(source)
        if quiz_persisted is not None:
            doc["quiz_persisted"] = bool(quiz_persisted)
        await self.col.insert_one(doc)
        return await self.get(attempt_id)

    async def ensure_started(
        self, attempt_id: str, user_id: int, qid: str, quiz_name: str,
        total_questions: int, source: Optional[str] = None,
        quiz_persisted: Optional[bool] = None,
    ) -> None:
        """Create the in-progress attempt row if it does not exist yet.

        ``$setOnInsert`` + upsert makes this idempotent: the live runner
        normally creates the row via :meth:`start` slightly earlier, and in
        that case this is a no-op; it also makes the analytics completion
        path self-contained if it is ever reached without a prior start.
        """
        now = _now_iso()
        doc = {
            "attempt_id": attempt_id,
            "user_id": user_id,
            "qid": qid,
            "quiz_name": quiz_name,
            "current_question": 0,
            "answers": {},
            "score": 0,
            "total_questions": total_questions,
            "time_started": now,
            "time_ended": None,
            "status": "in_progress",
            "paused": False,
            "pause_time": None,
            "total_pause_duration": 0,
        }
        if source is not None:
            doc["source"] = str(source)
        if quiz_persisted is not None:
            doc["quiz_persisted"] = bool(quiz_persisted)
        await self.col.update_one(
            {"attempt_id": attempt_id}, {"$setOnInsert": doc}, upsert=True
        )

    async def get(self, attempt_id: str) -> Optional[dict]:
        row = await self.col.find_one({"attempt_id": attempt_id})
        return _clean(row)

    async def update(self, attempt_id: str, **fields) -> None:
        allowed = {
            "current_question", "answers", "score", "correct", "wrong",
            "total_time", "paused",
            # Phase B canonical per-question results + provenance.
            "question_results", "skipped", "source", "quiz_persisted",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        await self.col.update_one({"attempt_id": attempt_id}, {"$set": sets})

    async def pause(self, attempt_id: str, pause: bool) -> None:
        if pause:
            await self.col.update_one(
                {"attempt_id": attempt_id},
                {"$set": {"paused": True, "pause_time": _now_iso()}},
            )
        else:
            attempt = await self.get(attempt_id)
            if attempt and attempt.get("pause_time"):
                started = datetime.strptime(attempt["pause_time"], "%Y-%m-%d %H:%M:%S")
                elapsed = int((datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds())
                await self.col.update_one(
                    {"attempt_id": attempt_id},
                    {
                        "$set": {"paused": False, "pause_time": None},
                        "$inc": {"total_pause_duration": max(elapsed, 0)},
                    },
                )
            else:
                await self.col.update_one(
                    {"attempt_id": attempt_id},
                    {"$set": {"paused": False, "pause_time": None}},
                )

    async def complete(self, attempt_id: str, score: float, username: str,
                       persist_leaderboard: bool = True) -> Optional[dict]:
        attempt = await self.get(attempt_id)
        if attempt is None:
            return None
        await self.col.update_one(
            {"attempt_id": attempt_id},
            {"$set": {"status": "completed", "score": score, "time_ended": _now_iso()}},
        )
        # The leaderboard belongs to saved quizzes only. Ad-hoc AI/mix/PDF
        # quizzes (synthetic qids, quiz_persisted=False) must never appear on
        # a saved quiz's leaderboard even though their attempts are now
        # recorded for analytics.
        if persist_leaderboard and attempt.get("quiz_persisted") is False:
            persist_leaderboard = False
        if not persist_leaderboard:
            return await self.get(attempt_id)
        started = datetime.strptime(attempt["time_started"], "%Y-%m-%d %H:%M:%S")
        elapsed = int(
            (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds()
        ) - attempt.get("total_pause_duration", 0)
        try:
            # First-attempt-only leaderboard entry: a unique index on
            # (qid, user_id) makes a duplicate insert raise
            # DuplicateKeyError, same role SQLite's UNIQUE constraint +
            # swallowed exception played before.
            await self.db.collection("leaderboard").insert_one(
                {
                    "qid": attempt["qid"],
                    "user_id": attempt["user_id"],
                    "username": username,
                    "user_name": username,
                    "score": score,
                    "total_questions": attempt["total_questions"],
                    "time_taken": max(elapsed, 0),
                    "completed_at": _now_iso(),
                }
            )
        except Exception:
            pass  # unique(qid, user_id) -- first attempt only, ignore duplicates
        return await self.get(attempt_id)

    async def latest_completed_for_user(self, user_id: int) -> Optional[dict]:
        """Return the user's most recently completed quiz attempt."""
        row = await self.col.find_one(
            {"user_id": user_id, "status": "completed"},
            sort=[("time_ended", -1)],
        )
        return _clean(row)

    async def list_completed(self, qid: str) -> list[dict]:
        """All completed attempts for a quiz, newest first (by time_ended).
        Used for the /compare_results analysis report."""
        cursor = self.col.find({"qid": qid, "status": "completed"}).sort(
            "time_ended", -1
        )
        return [_clean(r) async for r in cursor]

    # ------------------------------------------------------------------
    # Phase B user-scoped analytics reads (aggregation in MongoDB).
    # Every method requires an explicit user_id; there is no global read.
    # ------------------------------------------------------------------

    async def list_completed_for_user(
        self, user_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        cursor = (
            self.col.find({"user_id": user_id, "status": "completed"})
            .sort("time_ended", -1)
            .skip(offset)
            .limit(limit)
        )
        return [_clean(r) async for r in cursor]

    async def count_in_progress(self, user_id: int) -> int:
        return await self.col.count_documents(
            {"user_id": user_id, "status": "in_progress"}
        )

    async def user_completed_overview(self, user_id: int) -> dict:
        """Aggregate completed attempts for one user in MongoDB.

        Legacy documents without the Phase B fields are handled explicitly:
        missing ``quiz_persisted`` defaults to True (ad-hoc quizzes were not
        recorded at all before Phase B), and missing ``question_results``
        marks an attempt as pre-canonical ("legacy") so accuracy can fall
        back to the attempt-level correct/wrong counters with provenance.
        """
        pipeline = [
            {"$match": {"user_id": user_id, "status": "completed"}},
            {"$group": {
                "_id": None,
                "attempts": {"$sum": 1},
                "adhoc": {"$sum": {"$cond": [{"$eq": ["$quiz_persisted", False]}, 1, 0]}},
                "canonical": {"$sum": {"$cond": [
                    {"$eq": [{"$type": "$question_results"}, "array"]}, 1, 0]}},
                "correct_sum": {"$sum": {"$ifNull": ["$correct", 0]}},
                "wrong_sum": {"$sum": {"$ifNull": ["$wrong", 0]}},
                "time_sum": {"$sum": {"$ifNull": ["$total_time", 0]}},
                "quiz_ids": {"$addToSet": "$qid"},
                "days": {"$addToSet": {"$substrCP": [
                    {"$ifNull": ["$time_ended", ""]}, 0, 10]}},
                "first_at": {"$min": "$time_ended"},
                "last_at": {"$max": "$time_ended"},
            }},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        if not rows:
            return {
                "attempts": 0, "adhoc": 0, "canonical": 0, "legacy": 0,
                "correct_sum": 0, "wrong_sum": 0, "time_sum": 0,
                "quiz_count": 0, "activity_days": [],
                "first_at": None, "last_at": None,
            }
        row = rows[0]
        attempts = row.get("attempts", 0)
        canonical = row.get("canonical", 0)
        days = [d for d in row.get("days", []) if d]
        return {
            "attempts": attempts,
            "adhoc": row.get("adhoc", 0),
            "canonical": canonical,
            "legacy": attempts - canonical,
            "correct_sum": row.get("correct_sum", 0),
            "wrong_sum": row.get("wrong_sum", 0),
            "time_sum": row.get("time_sum", 0),
            "quiz_count": len([q for q in row.get("quiz_ids", []) if q is not None]),
            "activity_days": sorted(days),
            "first_at": row.get("first_at"),
            "last_at": row.get("last_at"),
        }

    async def daily_completed(self, user_id: int, days: int = 30) -> list[dict]:
        """Completed attempts grouped by UTC day (activity = a completed
        attempt, per the Phase C prep definition)."""
        pipeline = [
            {"$match": {"user_id": user_id, "status": "completed",
                        "time_ended": {"$gte": " "}}},
            {"$group": {
                "_id": {"$substrCP": ["$time_ended", 0, 10]},
                "attempts": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]
        out = []
        async for row in self.col.aggregate(pipeline):
            day = row.get("_id")
            if day:
                out.append({"day": day, "attempts": row.get("attempts", 0)})
        return out[-days:] if days else out


class LeaderboardRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("leaderboard")

    async def top(self, qid: str, limit: int = 10) -> list[dict]:
        cursor = (
            self.col.find({"qid": qid})
            .sort([("score", -1), ("time_taken", 1)])
            .limit(limit)
        )
        return [_clean(r) async for r in cursor]

    async def page(self, qid: str, offset: int = 0, limit: int = 200) -> list[dict]:
        # Mongo's $setWindowFields (5.0+, fully supported on Atlas incl. the
        # free M0 tier) is the aggregation-pipeline equivalent of SQL's
        # RANK() OVER (...) window function used here previously.
        pipeline = [
            {"$match": {"qid": qid}},
            {"$sort": {"score": -1, "time_taken": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"score": -1, "time_taken": 1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$skip": offset},
            {"$limit": limit},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        return [_clean(r) for r in rows]

    async def user_rank(self, qid: str, user_id: int) -> Optional[dict]:
        pipeline = [
            {"$match": {"qid": qid}},
            {
                "$setWindowFields": {
                    "sortBy": {"score": -1, "time_taken": 1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"user_id": user_id}},
            {"$limit": 1},
        ]
        rows = [r async for r in self.col.aggregate(pipeline)]
        return _clean(rows[0]) if rows else None


class QuestionStatsRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("question_wrong_stats")

    async def bulk_update_wrong_stats(self, qid: str, items: list[dict]) -> None:
        for item in items:
            q_index = item["index"]
            existing = await self.col.find_one({"qid": qid, "q_index": q_index})
            if existing:
                wrong = existing["wrong_count"] + item.get("wrong", 0)
                total = existing["total_count"] + item.get("total", 1)
                is_hard = existing["is_hard"]
                flagged_at = existing["hard_flagged_at"]
                if not is_hard and total >= 50 and (wrong / total) >= 0.20:
                    is_hard, flagged_at = True, _now_iso()
                await self.col.update_one(
                    {"qid": qid, "q_index": q_index},
                    {
                        "$set": {
                            "wrong_count": wrong,
                            "total_count": total,
                            "is_hard": is_hard,
                            "hard_flagged_at": flagged_at,
                        }
                    },
                )
            else:
                wrong = item.get("wrong", 0)
                total = item.get("total", 1)
                is_hard = bool(total >= 50 and (wrong / total) >= 0.20)
                await self.col.insert_one(
                    {
                        "qid": qid,
                        "q_index": q_index,
                        "wrong_count": wrong,
                        "total_count": total,
                        "is_hard": is_hard,
                        "hard_flagged_at": _now_iso() if is_hard else None,
                    }
                )

    async def hard_questions(self, qid: str) -> list[dict]:
        cursor = self.col.find({"qid": qid, "is_hard": True})
        return [_clean(r) async for r in cursor]


class MistakeRepository:
    """Non-destructive per-user mistake history (Phase B foundation).

    A mistake is an identity row keyed ``(user_id, qid, q_index)`` that is
    NEVER deleted:

    * ``wrong_count``        -- total times the question was answered wrong
                                (every wrong attempt adds one).
    * ``correct_count``      -- times it was answered correctly afterwards.
    * ``status``             -- ``open`` / ``resolved``; answering wrong
                                again after resolution re-opens the row
                                ("repeated mistake") without losing history.
    * ``first_wrong_at`` / ``last_wrong_at`` / ``last_correct_at``.
    * ``snapshot_id``        -- content-hash reference into
                                ``question_snapshots`` captured at first
                                wrong so later quiz edits cannot repoint the
                                mistake at a different question (the snapshot
                                text itself is stored once, not per row).
    * ``wrong_attempt_ids`` / ``correct_attempt_ids`` -- idempotency guards:
                                replaying the same attempt never double-counts.
    * ``revision_history``   -- bounded append-only timeline.

    Legacy rows written before Phase B (only ``wrong_count`` /
    ``last_wrong_at``) keep working: they are treated as ``open`` because
    their missing ``status`` is not ``"resolved"``.
    """

    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"
    HISTORY_LIMIT = 200

    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("user_mistakes")

    # ------------------------------------------------------------------ writes

    def _history_entry(self, attempt_id: Optional[str], outcome: str, at: str,
                       selected: Optional[list] = None) -> dict:
        entry = {"at": at, "outcome": outcome}
        if attempt_id:
            entry["attempt_id"] = attempt_id
        if selected:
            entry["selected_option"] = list(selected)
        return entry

    #: Bound each bulk request so a very large quiz ending cannot produce an
    #: oversized Mongo command (1 CPU / 2 GB VPS); total round trips stay
    #: O(ceil(questions / chunk)) rather than O(questions).
    _BULK_CHUNK = 500

    async def _bulk_write_chunked(self, ops: list) -> None:
        for start in range(0, len(ops), self._BULK_CHUNK):
            await self.col.bulk_write(ops[start:start + self._BULK_CHUNK], ordered=False)

    async def _fetch_existing(
        self, user_id: int, qid: str, q_indices: set[int]
    ) -> dict[int, dict]:
        if not q_indices:
            return {}
        cursor = self.col.find(
            {"user_id": user_id, "qid": qid, "q_index": {"$in": list(q_indices)}}
        )
        return {int(row["q_index"]): row async for row in cursor}

    async def apply_attempt(
        self,
        user_id: int,
        qid: str,
        attempt_id: Optional[str],
        events: list[dict],
        *,
        at: Optional[str] = None,
    ) -> int:
        """Fold canonical question events for one completed attempt into the
        user's mistake history using a fixed, bounded number of bulk round
        trips (ensure rows -> re-fetch -> apply increments) instead of one
        round trip per question.

        Semantics are unchanged from the sequential version: idempotent per
        ``attempt_id`` (the attempt-id arrays make a replayed completion a
        no-op), fully non-destructive (rows are never deleted), wrong answers
        (re-)open a row, later correct answers resolve it, and a wrong after
        resolution re-opens it. Returns the number of mistake rows touched.
        """
        at = at or _now_iso()
        wrong_events: list[dict] = []
        correct_events: list[dict] = []
        q_indices: set[int] = set()
        for ev in events:
            outcome = ev.get("outcome")
            if outcome not in (OUTCOME_CORRECT, OUTCOME_INCORRECT):
                continue  # skipped questions never become mistakes
            try:
                q_index = int(ev["question_index"])
            except (KeyError, TypeError, ValueError):
                continue
            q_indices.add(q_index)
            (wrong_events if outcome == OUTCOME_INCORRECT else correct_events).append(ev)
        if not wrong_events and not correct_events:
            return 0

        def _metadata(ev: dict) -> dict:
            return {
                "subject": ev.get("subject"),
                "topic": ev.get("topic"),
                "subtopic": ev.get("subtopic"),
                "difficulty": ev.get("difficulty"),
                "topic_source": ev.get("topic_source"),
                "snapshot_id": ev.get("snapshot_id"),
            }

        # Phase 1 -- ensure a row exists for every freshly-wrong question
        # (idempotent $setOnInsert upsert in one bulk call). Rows that already
        # exist (e.g. a repeated mistake) are matched, not duplicated.
        existing = await self._fetch_existing(user_id, qid, q_indices)
        ensure_ops: list[UpdateOne] = []
        for ev in wrong_events:
            q_index = int(ev["question_index"])
            if q_index in existing:
                continue
            # The wrong-count increment and the first revision-history entry
            # are both applied by the uniform phase-3 update below.
            ensure_ops.append(UpdateOne(
                {"user_id": user_id, "qid": qid, "q_index": q_index},
                {"$setOnInsert": {
                    "user_id": user_id,
                    "qid": qid,
                    "q_index": q_index,
                    # Counts/history are applied by the uniform phase-3
                    # update below, including for the just-inserted row.
                    "wrong_count": 0,
                    "correct_count": 0,
                    "status": self.STATUS_OPEN,
                    "first_wrong_at": at,
                    "last_wrong_at": None,
                    "last_correct_at": None,
                    "resolved_at": None,
                    "created_at": at,
                    "updated_at": at,
                    "first_seen_attempt_id": attempt_id,
                    **_metadata(ev),
                    "wrong_attempt_ids": [],
                    "correct_attempt_ids": [],
                    "revision_history": [],
                    # The phase-1 entry is pushed in phase 3, once.
                }},
                upsert=True,
            ))
        await self._bulk_write_chunked(ensure_ops)

        # Phase 2 -- re-fetch so freshly-created rows are visible; one query.
        existing = await self._fetch_existing(user_id, qid, q_indices)

        # Phase 3 -- apply the per-attempt increment/history/guard updates.
        ops: list[UpdateOne] = []
        for ev in wrong_events:
            q_index = int(ev["question_index"])
            row = existing.get(q_index)
            if row is None:
                continue  # defensive: phase 1 should have created it
            if attempt_id and attempt_id in row.get("wrong_attempt_ids", []):
                continue  # this attempt already counted -- replay no-op
            entry = self._history_entry(
                attempt_id, OUTCOME_INCORRECT, at, ev.get("selected_option"))
            sets: dict[str, Any] = {
                # A fresh wrong answer (re-)opens the row, including a
                # previously resolved one ("repeated").
                "status": self.STATUS_OPEN,
                "last_wrong_at": at,
                "updated_at": at,
            }
            # Backfill metadata/snapshot a legacy first row didn't capture;
            # never overwrite what already exists.
            for key, value in _metadata(ev).items():
                if value is not None and row.get(key) is None:
                    sets[key] = value
            update: dict[str, Any] = {
                "$inc": {"wrong_count": 1},
                "$set": sets,
                "$push": {"revision_history": {"$each": [entry],
                                               "$slice": -self.HISTORY_LIMIT}},
            }
            filt: dict[str, Any] = {"_id": row["_id"]}
            if attempt_id:
                # Server-side guard closes the check-then-act window: if a
                # concurrent pass already counted this attempt, this update
                # matches nothing.
                filt["wrong_attempt_ids"] = {"$ne": attempt_id}
                update["$addToSet"] = {"wrong_attempt_ids": attempt_id}
            ops.append(UpdateOne(filt, update))

        for ev in correct_events:
            q_index = int(ev["question_index"])
            row = existing.get(q_index)
            if row is None:
                continue  # a never-wrong question creates no mistake row
            if attempt_id and attempt_id in row.get("correct_attempt_ids", []):
                continue  # replay no-op
            entry = self._history_entry(
                attempt_id, OUTCOME_CORRECT, at, ev.get("selected_option"))
            update = {
                "$inc": {"correct_count": 1},
                "$set": {"status": self.STATUS_RESOLVED,
                         "last_correct_at": at, "updated_at": at},
                "$push": {"revision_history": {"$each": [entry],
                                               "$slice": -self.HISTORY_LIMIT}},
            }
            filt = {"_id": row["_id"]}
            if attempt_id:
                filt["correct_attempt_ids"] = {"$ne": attempt_id}
                update["$addToSet"] = {"correct_attempt_ids": attempt_id}
            ops.append(UpdateOne(filt, update))

        await self._bulk_write_chunked(ops)
        # Rows touched = rows receiving a phase-3 counting update. Every
        # phase-1 ensured row is among them (its first wrong count), so this
        # matches the sequential version's per-row "touched" accounting.
        return len(ops)

    async def record(self, user_id: int, items: list[dict]) -> None:
        """Backward-compatible shim for the old ``record`` signature used by
        earlier call sites. Keeps one-wrong-per-item semantics, never
        deletes, and assigns a unique one-shot attempt token so repeated
        calls keep incrementing as they did before.
        """
        if not items:
            return
        by_qid: dict[str, list[dict]] = {}
        for item in items:
            by_qid.setdefault(item["qid"], []).append(item)
        at = _now_iso()
        token = f"legacy-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        for qid, qitems in by_qid.items():
            events = [
                {
                    "question_index": item["index"],
                    "outcome": OUTCOME_INCORRECT,
                    "selected_option": [],
                    "snapshot_id": None,
                }
                for item in qitems
            ]
            await self.apply_attempt(user_id, qid, token, events, at=at)

    # ------------------------------------------------------------------- reads

    async def get(self, user_id: int, qid: str, q_index: int) -> Optional[dict]:
        return _clean(await self.col.find_one(
            {"user_id": user_id, "qid": qid, "q_index": q_index}
        ))

    async def list_for_user(
        self, user_id: int, limit: int = 20, status: Optional[str] = None
    ) -> list[dict]:
        filt: dict[str, Any] = {"user_id": user_id}
        if status == self.STATUS_RESOLVED:
            filt["status"] = self.STATUS_RESOLVED
        elif status == self.STATUS_OPEN:
            # Legacy rows have no status field but are open.
            filt["status"] = {"$ne": self.STATUS_RESOLVED}
        cursor = (
            self.col.find(filt).sort([("status", 1), ("last_wrong_at", -1)]).limit(limit)
        )
        return [_clean(r) async for r in cursor]

    async def list_open(self, user_id: int, limit: int = 50) -> list[dict]:
        return await self.list_for_user(user_id, limit=limit, status=self.STATUS_OPEN)

    # ------------------------------------------------------------------
    # Phase D: bounded, projected reads powering /mistakes revision.
    # These never pull the (bounded but unbounded-in-principle)
    # revision_history / attempt-id guard arrays, and never scan the whole
    # collection -- every filter is user-scoped and index-backed.
    # ------------------------------------------------------------------

    #: Only ever reason over a bounded recent window, even for a very heavy
    #: user (1 CPU / 2 GB VPS). Revision is about current weaknesses, not the
    #: user's entire lifetime.
    REVISION_CANDIDATE_CAP = 200

    _REVISION_PROJECTION = {
        "_id": 0, "user_id": 1, "qid": 1, "q_index": 1, "snapshot_id": 1,
        "subject": 1, "topic": 1, "subtopic": 1, "difficulty": 1,
        "topic_source": 1, "wrong_count": 1, "correct_count": 1, "status": 1,
        "first_wrong_at": 1, "last_wrong_at": 1, "last_correct_at": 1,
        "resolved_at": 1, "first_seen_attempt_id": 1,
    }

    # Deterministic ordering shared by every revision read: currently-open
    # first, most-recently-missed first, then stable identity tie-breakers.
    _REVISION_SORT = [
        ("status", 1), ("last_wrong_at", -1), ("qid", 1), ("q_index", 1),
    ]

    async def query_revision_rows(
        self, user_id: int, *, only_open: bool = False,
        topic: Optional[str] = None, limit: int = REVISION_CANDIDATE_CAP,
        skip: int = 0,
    ) -> list[dict]:
        """Lean, bounded, deterministic mistake rows for revision selection.

        ``only_open`` matches the existing ``(user_id, status, last_wrong_at)``
        index (legacy rows without ``status`` are open, hence ``$ne
        resolved``). ``topic`` narrows within one user's rows (real stored
        metadata only)."""
        filt: dict[str, Any] = {"user_id": user_id}
        if only_open:
            filt["status"] = {"$ne": self.STATUS_RESOLVED}
        if topic:
            filt["topic"] = topic
        cursor = (
            self.col.find(filt, self._REVISION_PROJECTION)
            .sort(self._REVISION_SORT)
            .skip(int(skip))
            .limit(int(limit))
        )
        return [_clean(r) async for r in cursor]

    #: Phase H SRS projection: the Phase D revision fields PLUS the bounded
    #: review timeline. Kept separate on purpose -- Phase D/E reads stay lean,
    #: only the schedule (which must know what was answered when) pays for
    #: `revision_history`.
    _SRS_PROJECTION = {
        **_REVISION_PROJECTION,
        "revision_history": 1,
    }

    async def query_srs_rows(
        self, user_id: int, *, limit: int = REVISION_CANDIDATE_CAP,
    ) -> list[dict]:
        """Bounded mistake rows INCLUDING their review timeline (Phase H).

        Same deterministic ordering as :meth:`query_revision_rows`, so the
        schedule and the revision menu never disagree about what exists."""
        cursor = (
            self.col.find({"user_id": user_id}, self._SRS_PROJECTION)
            .sort(self._REVISION_SORT)
            .limit(int(limit))
        )
        return [_clean(r) async for r in cursor]

    async def list_mistake_topics(self, user_id: int, limit: int = 20) -> list[dict]:
        """Distinct topics actually present in THIS user's mistake rows.

        No UPSC taxonomy is hardcoded: a topic only ever appears here because
        it is stored on one of the user's own mistake rows. Bounded by the
        candidate window; deterministic (open groups first, then label)."""
        rows = await self.query_revision_rows(user_id, limit=self.REVISION_CANDIDATE_CAP)
        facets: dict[str, dict] = {}
        for r in rows:
            topic = r.get("topic")
            if not topic:
                continue
            f = facets.setdefault(topic, {
                "topic": topic,
                "subject": r.get("subject"),
                "open_count": 0, "total_count": 0,
            })
            f["total_count"] += 1
            if r.get("status") != self.STATUS_RESOLVED:
                f["open_count"] += 1
            # Keep a subject if the first/any row for the topic provides one.
            if not f.get("subject") and r.get("subject"):
                f["subject"] = r["subject"]
        ordered = sorted(
            facets.values(),
            key=lambda f: (-f["open_count"], -f["total_count"], f["topic"]),
        )
        return ordered[:int(limit)]

    async def totals(self, user_id: int) -> dict:
        total = await self.col.count_documents({"user_id": user_id})
        resolved = await self.col.count_documents(
            {"user_id": user_id, "status": self.STATUS_RESOLVED}
        )
        return {"total": total, "resolved": resolved, "open": total - resolved}

    async def resolve(self, user_id: int, qid: str, q_index: int) -> None:
        """Mark a mistake resolved WITHOUT deleting its history."""
        await self.col.update_one(
            {"user_id": user_id, "qid": qid, "q_index": q_index},
            {"$set": {"status": self.STATUS_RESOLVED,
                      "resolved_at": _now_iso(), "updated_at": _now_iso()}},
        )


class ReminderRepository:
    """Phase H daily-reminder settings (opt-in, one row per user).

    Deliberately tiny: one document per user holding ONLY the opt-in flag, the
    IST time and the content kind, plus the at-most-once-per-day guard
    (``last_sent_day``). Everything a reminder *says* is derived from existing
    mistake/XP records -- nothing analytical is duplicated here.
    """

    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("user_reminders")

    _PROJECTION = {
        "_id": 0, "user_id": 1, "enabled": 1, "time": 1, "content": 1,
        "last_sent_day": 1, "last_sent_at": 1,
    }

    async def get(self, user_id: int) -> Optional[dict]:
        return _clean(await self.col.find_one({"user_id": user_id},
                                              projection=self._PROJECTION))

    async def upsert(self, user_id: int, fields: dict) -> None:
        """Create-or-update this user's settings row (idempotent)."""
        now = _now_iso()
        payload = {k: v for k, v in fields.items() if k in {
            "enabled", "time", "content", "last_sent_day", "last_sent_at",
        }}
        payload["updated_at"] = now
        await self.col.update_one(
            {"user_id": user_id},
            {"$set": payload, "$setOnInsert": {"user_id": user_id, "created_at": now}},
            upsert=True,
        )

    async def list_enabled(self, limit: int = 200) -> list[dict]:
        """Bounded read of opted-in rows (the scheduler's only query shape)."""
        cursor = (
            self.col.find({"enabled": True}, self._PROJECTION)
            .sort("user_id", 1)
            .limit(int(limit))
        )
        return [_clean(r) async for r in cursor]

    async def stats(self) -> dict:
        enabled = await self.col.count_documents({"enabled": True})
        total = await self.col.count_documents({})
        return {"total": total, "enabled": enabled}


class CreatorSettingsRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("creator_settings")

    async def get(self, user_id: int) -> dict:
        row = await self.col.find_one({"user_id": user_id})
        if row is None:
            doc = {
                "user_id": user_id,
                "search_indexed": True,
                "default_text": None,
                "default_text_field": "both",
                "quiz_defaults": None,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            await self.col.insert_one(doc)
            row = doc
        return _clean(row)

    async def update(self, user_id: int, **fields) -> None:
        await self.get(user_id)
        allowed = {"search_indexed", "default_text", "default_text_field", "quiz_defaults"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = _now_iso()
        await self.col.update_one({"user_id": user_id}, {"$set": sets})


class ChatSettingsRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("chat_settings")

    async def get(self, chat_id: int) -> dict:
        row = await self.col.find_one({"chat_id": chat_id})
        if row is None:
            doc = {
                "chat_id": chat_id,
                "html_enabled": False,
                "pdf_enabled": False,
                "updated_at": _now_iso(),
            }
            await self.col.insert_one(doc)
            row = doc
        return _clean(row)

    async def get_all_enabled(self) -> list[dict]:
        """Every chat with HTML or PDF reports enabled -- used to warm an
        in-memory cache at startup instead of one query per chat (mirrors
        the original PHP `ChatSettingsAPI::getAllEnabled`)."""
        cursor = self.col.find({"$or": [{"html_enabled": True}, {"pdf_enabled": True}]})
        return [_clean(r) async for r in cursor]

    async def toggle(self, chat_id: int, which: str) -> bool:
        column = f"{which}_enabled"
        if column not in ("html_enabled", "pdf_enabled"):
            raise ValueError("which must be 'html' or 'pdf'")
        current = await self.get(chat_id)
        new_value = not current[column]
        await self.col.update_one(
            {"chat_id": chat_id}, {"$set": {column: new_value, "updated_at": _now_iso()}}
        )
        return new_value

    async def set(self, chat_id: int, which: str, enabled: bool) -> None:
        column = f"{which}_enabled"
        if column not in ("html_enabled", "pdf_enabled"):
            raise ValueError("which must be 'html' or 'pdf'")
        await self.get(chat_id)
        await self.col.update_one(
            {"chat_id": chat_id}, {"$set": {column: bool(enabled), "updated_at": _now_iso()}}
        )


class QuizPrefsRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("user_quiz_prefs")

    async def get(self, chat_id: int) -> dict:
        row = await self.col.find_one({"chat_id": chat_id})
        if row is None:
            doc = {
                "chat_id": chat_id,
                "correct_mark": 1,
                "neg_mark": 0,
                "shuffle_q": False,
                "shuffle_o": False,
                "shuffle_o_count": 0,
                "show_explanation": False,
                "anti_cheat": False,
                "timer_override": None,
                "updated_at": _now_iso(),
            }
            await self.col.insert_one(doc)
            row = doc
        return _clean(row)

    async def save(self, chat_id: int, **fields) -> None:
        await self.get(chat_id)
        allowed = {
            "correct_mark", "neg_mark", "shuffle_q", "shuffle_o", "shuffle_o_count",
            "show_explanation", "anti_cheat", "timer_override",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = _now_iso()
        await self.col.update_one({"chat_id": chat_id}, {"$set": sets})


class BatchRepository:
    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("batches")
        self.access_col = db.collection("batch_access")
        self.quizzes_col = db.collection("batch_quizzes")

    @staticmethod
    def _new_batch_id() -> str:
        return uuid.uuid4().hex[:10]

    async def create(self, creator_id: int, name: str, **kwargs) -> dict:
        batch_id = self._new_batch_id()
        doc = {
            "batch_id": batch_id,
            "creator_id": creator_id,
            "name": name,
            "description": kwargs.get("description"),
            "contact_info": kwargs.get("contact_info"),
            "payment_link": kwargs.get("payment_link"),
            "created_at": _now_iso(),
        }
        await self.col.insert_one(doc)
        return await self.get(batch_id)

    async def get(self, batch_id: str) -> Optional[dict]:
        row = await self.col.find_one({"batch_id": batch_id})
        if row is None:
            return None
        data = _clean(row)
        data["chats"] = [
            r["chat_id"] async for r in self.access_col.find({"batch_id": batch_id})
        ]
        data["quizzes"] = [
            r["qid"] async for r in self.quizzes_col.find({"batch_id": batch_id})
        ]
        return data

    async def list_by_creator(self, creator_id: int) -> list[dict]:
        cursor = self.col.find({"creator_id": creator_id}).sort("created_at", -1)
        batch_ids = [r["batch_id"] async for r in cursor]
        return [await self.get(bid) for bid in batch_ids]

    async def update(self, batch_id: str, **fields) -> None:
        allowed = {"name", "description", "contact_info", "payment_link"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        await self.col.update_one({"batch_id": batch_id}, {"$set": sets})

    async def delete(self, batch_id: str) -> None:
        await self.col.delete_one({"batch_id": batch_id})
        # Mirrors the old schema's ON DELETE CASCADE on batch_access /
        # batch_quizzes -- Mongo has no native FK cascade, so it's done
        # explicitly here.
        await self.access_col.delete_many({"batch_id": batch_id})
        await self.quizzes_col.delete_many({"batch_id": batch_id})

    async def add_chat(self, batch_id: str, chat_id: int) -> None:
        await self.access_col.update_one(
            {"batch_id": batch_id, "chat_id": chat_id},
            {"$setOnInsert": {"batch_id": batch_id, "chat_id": chat_id}},
            upsert=True,
        )

    async def remove_chat(self, batch_id: str, chat_id: int) -> None:
        await self.access_col.delete_one({"batch_id": batch_id, "chat_id": chat_id})

    async def add_quiz(self, batch_id: str, qid: str) -> None:
        await self.quizzes_col.update_one(
            {"batch_id": batch_id, "qid": qid},
            {"$setOnInsert": {"batch_id": batch_id, "qid": qid}},
            upsert=True,
        )

    async def remove_quiz(self, batch_id: str, qid: str) -> None:
        await self.quizzes_col.delete_one({"batch_id": batch_id, "qid": qid})

    async def check_access(self, qid: str, chat_id: int) -> bool:
        bq = await self.quizzes_col.find_one({"qid": qid})
        if bq is None:
            return False
        row = await self.access_col.find_one({"batch_id": bq["batch_id"], "chat_id": chat_id})
        return row is not None

    async def info_for_quiz(self, qid: str) -> Optional[dict]:
        bq = await self.quizzes_col.find_one({"qid": qid})
        if bq is None:
            return None
        row = await self.col.find_one({"batch_id": bq["batch_id"]})
        return _clean(row)

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search batches by name OR description (matches the original PHP
        `BatchAPI::search`, which searched both fields)."""
        pattern = _escape_regex(query)
        cursor = (
            self.col.find(
                {
                    "$or": [
                        {"name": {"$regex": pattern, "$options": "i"}},
                        {"description": {"$regex": pattern, "$options": "i"}},
                    ]
                }
            )
            .sort("created_at", -1)
            .limit(limit)
        )
        return [_clean(r) async for r in cursor]


class PodcastKeyRepository:
    """Per-user podcast Gemini API keys, stored as encrypted blobs.

    Only ciphertext ever touches this collection -- encryption and
    decryption happen in quizbot.runner_bot.podcast_security, server-side.
    One row per Telegram user (user_id unique). Existing collections and
    documents are untouched.
    """

    def __init__(self, db: Database):
        self.db = db
        self.col = db.collection("podcast_keys")

    async def get_encrypted(self, user_id: int) -> Optional[str]:
        row = await self.col.find_one({"user_id": user_id}, {"enc_key": 1})
        return row.get("enc_key") if row else None

    async def has(self, user_id: int) -> bool:
        return await self.col.count_documents({"user_id": user_id}, limit=1) > 0

    async def save(self, user_id: int, enc_key: str) -> None:
        await self.col.update_one(
            {"user_id": user_id},
            {
                "$set": {"enc_key": enc_key, "updated_at": _now_iso()},
                "$setOnInsert": {"created_at": _now_iso()},
            },
            upsert=True,
        )

    async def delete(self, user_id: int) -> bool:
        """Permanently remove a user's stored key. Returns True if one existed."""
        res = await self.col.delete_one({"user_id": user_id})
        return res.deleted_count > 0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _escape_regex(text: str) -> str:
    """Escape regex metacharacters in user-supplied search text before using
    it in a Mongo $regex filter -- the SQL version's `LIKE ?` with a `%...%`
    wildcard had no equivalent injection risk since LIKE patterns aren't
    Python/regex syntax, but a raw string dropped into $regex here could
    let a search term with regex metacharacters (e.g. `.*`, `(`, `|`) behave
    unexpectedly or (for pathological patterns) run slowly. re.escape keeps
    the search literal, matching LIKE's plain-substring behavior."""
    import re

    return re.escape(text)


def _as_object_id(value: Any):
    """AIKeyRepository.mark/delete_by_id are called elsewhere in the
    codebase with the `id` field from a row previously returned by
    list_for_user/add -- under Mongo that field is now an ObjectId already
    (ai_keys rows no longer have a separate integer `id`; see the
    migration note in this module's docstring). Accept either an ObjectId
    already, or something coercible to one (e.g. its string form), so a
    caller holding either representation keeps working."""
    from bson import ObjectId

    if isinstance(value, ObjectId):
        return value
    return ObjectId(str(value))
