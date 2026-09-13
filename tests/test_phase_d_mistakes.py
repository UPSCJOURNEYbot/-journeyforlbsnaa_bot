"""Phase D tests -- /mistakes revision: lifecycle, content dedup, snapshot
safety, Smart ranking, bounded selection, the fail-soft revision fold through
the canonical analytics boundary, callback security and performance bounds.

No MongoDB / Telegram network is required. An in-memory Motor-like DB (with
unique indexes, projection support and atomic single-document updates) backs
the repository/service tests; the Telegram handler tests use tiny fake
Update/Query/Context objects and stub the heavy sibling handler modules
(``start_private_quiz`` is captured instead of run).
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from quizbot.analytics import mistake_revision as mr
from quizbot.analytics.metadata import OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED
from quizbot.analytics.service import AnalyticsService
from quizbot.database import MistakeRepository, QuizRepository
from quizbot.database.repositories import _now_iso

# ---------------------------------------------------------------------------
# In-memory Motor-like fake (unique indexes, projections, atomic writes)
# ---------------------------------------------------------------------------

def _get_path(doc, path):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _value_match(field, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, val in cond.items():
            if op == "$ne":
                if isinstance(field, list):
                    if val in field:
                        return False
                elif field == val:
                    return False
            elif op == "$eq":
                if field != val:
                    return False
            elif op == "$in":
                if isinstance(field, list):
                    if not any(x in val for x in field):
                        return False
                elif field not in val:
                    return False
            elif op == "$nin":
                if isinstance(field, list):
                    if any(x in val for x in field):
                        return False
                elif field in val:
                    return False
            elif op == "$gte":
                if field is None or field < val:
                    return False
            elif op == "$gt":
                if field is None or field <= val:
                    return False
            elif op == "$lte":
                if field is None or field > val:
                    return False
            elif op == "$lt":
                if field is None or field >= val:
                    return False
            elif op == "$exists":
                if bool(val) != (field is not None):
                    return False
            elif op == "$regex":
                import re
                if field is None or not re.search(val, str(field)):
                    return False
            else:
                raise AssertionError(f"unsupported op {op}")
        return True
    if isinstance(field, list) and not isinstance(cond, list):
        return cond in field
    return field == cond


def _matches(doc, filt):
    for key, cond in (filt or {}).items():
        if key == "$or":
            if not any(_matches(doc, s) for s in cond):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, s) for s in cond):
                return False
            continue
        if not _value_match(_get_path(doc, key), cond):
            return False
    return True


def _apply_update(doc, update, inserting):
    for k, v in update.get("$set", {}).items():
        doc[k] = v
    if inserting:
        for k, v in update.get("$setOnInsert", {}).items():
            doc.setdefault(k, v)
    for k, v in update.get("$inc", {}).items():
        doc[k] = doc.get(k, 0) + v
    for k, v in update.get("$addToSet", {}).items():
        arr = doc.setdefault(k, [])
        vals = v["$each"] if isinstance(v, dict) and "$each" in v else [v]
        for x in vals:
            if x not in arr:
                arr.append(x)
    for k, v in update.get("$pull", {}).items():
        arr = doc.setdefault(k, [])
        if isinstance(v, dict) and "$in" in v:
            doc[k] = [x for x in arr if x not in v["$in"]]
        else:
            doc[k] = [x for x in arr if x != v]
    for k, v in update.get("$push", {}).items():
        arr = doc.setdefault(k, [])
        if isinstance(v, dict) and "$each" in v:
            arr.extend(v["$each"])
            if "$slice" in v and v["$slice"] is not None:
                arr[:] = arr[v["$slice"]:] if v["$slice"] < 0 else arr[:v["$slice"]]
        else:
            arr.append(v)


class FakeResult:
    def __init__(self, *, matched=0, modified=0, upserted_count=0, inserted_id=None,
                 deleted=0):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_count = upserted_count
        self.inserted_id = inserted_id
        self.deleted_count = deleted


def _sort_value(x, field):
    # BSON-style type-tolerant ordering (Mongo sorts mixed types, Python does
    # not): missing/null last, then numbers, strings, everything else.
    v = _get_path(x, field)
    if v is None:
        return (1, 0, 0.0)
    if isinstance(v, bool):
        return (0, 0, int(v))
    if isinstance(v, (int, float)):
        return (0, 0, float(v))
    if isinstance(v, str):
        return (0, 1, v)
    return (0, 2, str(v))


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, keys, direction=1):
        if isinstance(keys, str):
            keys = [(keys, direction)]
        for field, d in reversed(keys):
            self._docs.sort(
                key=lambda x, f=field: _sort_value(x, f),
                reverse=d < 0)
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for d in list(self._docs):
                yield copy.deepcopy(d)
        return gen()


class FakeCollection:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self.docs = []
        self.indexes = []
        self._seq = 0
        self.queries = 0  # bound-test instrumentation

    async def create_index(self, keys, unique=False, sparse=False, name=None, **kw):
        if isinstance(keys, str):
            keys = [(keys, 1)]
        self.indexes.append({"keys": keys, "unique": unique, "name": name})
        return name

    def _violates(self, candidate):
        for idx in self.indexes:
            if not idx["unique"]:
                continue
            vals = tuple(_get_path(candidate, p) for p, _ in idx["keys"])
            if any(v is None for v in vals):
                continue
            for d in self.docs:
                if tuple(_get_path(d, p) for p, _ in idx["keys"]) == vals:
                    return True
        return False

    async def insert_one(self, doc):
        self.queries += 1
        new = copy.deepcopy(doc)
        self._seq += 1
        new.setdefault("_id", f"{self.name}_{self._seq}")
        if self._violates(new):
            raise DuplicateKeyError(None, 11000, {"errmsg": "dup"})
        self.docs.append(new)
        return FakeResult(inserted_id=new["_id"])

    async def find_one(self, filt=None, sort=None, projection=None):
        self.queries += 1
        rows = [d for d in self.docs if _matches(d, filt or {})]
        if sort:
            rows = list(FakeCursor(rows).sort(sort)._docs)
        if rows:
            return self._project(copy.deepcopy(rows[0]), projection)
        return None

    def _project(self, doc, projection):
        if not projection:
            return doc
        out = {"_id": doc.get("_id")} if "_id" in projection or all(
            v == 0 for v in projection.values()) else {}
        # Inclusive projection only (the repositories use inclusion).
        for k, flag in projection.items():
            if k == "_id":
                if flag:
                    out["_id"] = doc.get("_id")
                else:
                    out.pop("_id", None)
            elif flag and k in doc:
                out[k] = doc[k]
        return out

    def find(self, filt=None, projection=None):
        self.queries += 1
        rows = [copy.deepcopy(d) for d in self.docs if _matches(d, filt or {})]
        rows = [self._project(d, projection) for d in rows]
        return FakeCursor(rows)

    async def count_documents(self, filt=None):
        self.queries += 1
        return sum(1 for d in self.docs if _matches(d, filt or {}))

    async def update_one(self, filt, update, upsert=False):
        self.queries += 1
        for d in self.docs:
            if _matches(d, filt):
                _apply_update(d, update, False)
                return FakeResult(matched=1, modified=1)
        if upsert:
            base = {k: v for k, v in filt.items()
                    if not k.startswith("$") and not (
                        isinstance(v, dict) and any(x.startswith("$") for x in v))}
            _apply_update(base, update, True)
            self._seq += 1
            base.setdefault("_id", f"{self.name}_{self._seq}")
            if self._violates(base):
                raise DuplicateKeyError(None, 11000, {"errmsg": "dup"})
            self.docs.append(base)
            return FakeResult(upserted_count=1)
        return FakeResult()

    async def update_many(self, filt, update):
        self.queries += 1
        n = 0
        for d in self.docs:
            if _matches(d, filt):
                _apply_update(d, update, False)
                n += 1
        return FakeResult(matched=n, modified=n)

    async def delete_one(self, filt):
        self.queries += 1
        for i, d in enumerate(self.docs):
            if _matches(d, filt):
                self.docs.pop(i)
                return FakeResult(deleted=1)
        return FakeResult()

    async def bulk_write(self, ops, ordered=True):
        ups = 0
        for op in ops:
            r = await self.update_one(op._filter, op._doc, upsert=bool(op._upsert))
            ups += r.upserted_count
        return types.SimpleNamespace(matched_count=0, modified_count=0,
                                     upserted_count=ups, inserted_count=0)


class FakeDB:
    def __init__(self):
        self._cols = {}

    def collection(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name, self)
        return self._cols[name]


def new_db():
    return FakeDB()


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def qr(i, outcome, selected=None, correct=None, time=10):
    return {"q_index": i, "selected": selected or [],
            "correct_option_id": correct or [], "correct_option": correct or [],
            "outcome": outcome, "time_taken": time}


def quiz_questions(*topics):
    """3 stored questions; pass per-index analytics dicts (or None)."""
    base = [
        {"question": "Q0 body", "options": ["a", "b"], "correct_option_id": 0},
        {"question": "Q1 multi", "options": ["a", "b", "c"], "correct_option_id": [0, 2]},
        {"question": "Q2 body", "options": ["a", "b"], "correct_option_id": 1},
    ]
    for i, meta in enumerate(topics):
        if meta:
            base[i] = dict(base[i], analytics=meta)
    return base


async def seed_quiz(db, qid="qA", questions=None, creator=111, name="Quiz A"):
    questions = questions or quiz_questions(
        {"topic": "Polity", "difficulty": "hard"},
        {"topic": "Polity", "difficulty": "extreme"}, None)
    await QuizRepository(db).create(creator, name, questions, qid=qid)
    return questions


def svc(db):
    return AnalyticsService(db)


async def complete(db, *, user, attempt, qid, results, questions,
                   source="group", persisted=True, finalize=True,
                   backfilled=False):
    return await svc(db).record_completion(
        user_id=user, attempt_id=attempt, qid=qid, quiz_name=qid,
        question_results=results, source=source, quiz_persisted=persisted,
        questions=questions, sections=[], finalize=finalize, backfilled=backfilled)


def mistake_rows(db, user, qid=None):
    col = db.collection("user_mistakes").docs
    return [d for d in col if d.get("user_id") == user and (qid is None or d.get("qid") == qid)]


# ===========================================================================
# PURE LOGIC
# ===========================================================================

class PureTests(unittest.TestCase):
    def test_content_identity_prefers_snapshot_then_fallback(self):
        self.assertEqual(mr.content_key({"snapshot_id": "h1", "qid": "a", "q_index": 2}),
                         ("snap", "h1"))
        self.assertEqual(mr.content_key({"snapshot_id": None, "qid": "a", "q_index": 2}),
                         ("q", "a#2"))
        self.assertEqual(mr.content_key({"qid": "b", "q_index": 0}), ("q", "b#0"))

    def test_grouping_dedups_content_and_aggregates(self):
        rows = [
            {"snapshot_id": "H", "qid": "qA", "q_index": 0, "wrong_count": 2,
             "correct_count": 0, "status": "open", "last_wrong_at": "2026-09-10 10:00:00",
             "subject": "S", "topic": "T", "difficulty": "hard"},
            {"snapshot_id": "H", "qid": "qB", "q_index": 4, "wrong_count": 1,
             "correct_count": 1, "status": "resolved", "last_wrong_at": "2026-09-09 10:00:00",
             "subject": "S", "topic": "T", "difficulty": "hard"},
        ]
        groups = mr.group_mistakes(rows)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["wrong_total"], 3)
        self.assertEqual(g["wrong_max"], 2)
        self.assertEqual(g["correct_total"], 1)
        self.assertTrue(g["open"])
        self.assertEqual(sorted((o["qid"], o["q_index"]) for o in g["origins"]),
                         [("qA", 0), ("qB", 4)])
        self.assertEqual(g["topic"], "T")
        self.assertEqual(g["difficulty"], "hard")

    def test_relapse_detection_and_legacy_open(self):
        # wrong after a prior correct -> relapse
        g = mr.group_mistakes([{"snapshot_id": "H", "qid": "q", "q_index": 0,
                               "wrong_count": 2, "correct_count": 1,
                               "status": "open"}])[0]
        self.assertTrue(g["relapse"])
        # legacy row with NO status field is treated as open
        g2 = mr.group_mistakes([{"snapshot_id": None, "qid": "q", "q_index": 1,
                                "wrong_count": 1}])[0]
        self.assertTrue(g2["open"])
        self.assertFalse(g2["relapse"])

    def test_smart_ranking_deterministic_and_single_not_priority(self):
        now = "2026-09-13 12:00:00"
        recent = "2026-09-13 11:00:00"
        single = {"key": ("snap", "a"), "open": True, "relapse": False,
                  "wrong_max": 1, "wrong_total": 1, "last_wrong_at": recent,
                  "difficulty": None}
        repeated = {"key": ("snap", "b"), "open": True, "relapse": False,
                    "wrong_max": 3, "wrong_total": 3, "last_wrong_at": recent,
                    "difficulty": None}
        relapsed = {"key": ("snap", "c"), "open": True, "relapse": True,
                    "wrong_max": 2, "wrong_total": 2, "last_wrong_at": recent,
                    "difficulty": None}
        ranked = mr.rank_groups([single, repeated, relapsed], now=now)
        keys = [g["key"][1] for g in ranked]
        # repeated (3x) outranks relapse (2x) which outranks single
        self.assertEqual(keys, ["b", "c", "a"])
        # deterministic across repeated calls
        self.assertEqual([g["key"] for g in mr.rank_groups(
            [single, repeated, relapsed], now=now)],
            [g["key"] for g in mr.rank_groups(
                [relapsed, single, repeated], now=now)])
        # single wrong never qualifies as repeated
        self.assertFalse(mr.is_repeated(single))
        self.assertTrue(mr.is_repeated(repeated))
        self.assertTrue(mr.is_repeated(relapsed))

    def test_smart_reasons_explainable(self):
        now = "2026-09-13 12:00:00"
        g = {"relapse": True, "wrong_max": 2, "difficulty": "extreme",
             "last_wrong_at": "2026-09-13 11:00:00"}
        reasons = mr.smart_reasons(g, mr._now_epoch(now))
        self.assertIn("Relapsed after correction", reasons)
        self.assertTrue(any("Repeated" in r for r in reasons))
        self.assertIn("Difficult question", reasons)
        single = mr.smart_reasons(
            {"relapse": False, "wrong_max": 1, "difficulty": None,
             "last_wrong_at": "2026-01-01 00:00:00"}, mr._now_epoch(now))
        self.assertEqual(single, ["Missed once"])

    def test_select_modes_size_cap_and_topic(self):
        mk = lambda k, n, open_, topic="T": {
            "key": ("snap", k), "open": open_, "relapse": n >= 2,
            "wrong_max": n, "wrong_total": n, "last_wrong_at": "2026-09-13 10:00:00",
            "difficulty": None, "topic": topic}
        groups = [mk(f"g{i}", (i % 4) + 1, i % 2 == 0,
                     topic="A" if i % 2 == 0 else "B") for i in range(12)]
        smart = mr.select_groups(groups, mr.MODE_SMART, size=10)
        self.assertLessEqual(len(smart), 10)
        self.assertTrue(all(g["open"] for g in smart))
        repeated = mr.select_groups(groups, mr.MODE_REPEATED, size=50)
        self.assertTrue(all(mr.is_repeated(g) and g["open"] for g in repeated))
        self.assertFalse(any(g["wrong_max"] < 2 for g in repeated))
        allsel = mr.select_groups(groups, mr.MODE_ALL, size=3)
        self.assertEqual(len(allsel), 3)
        topicA = mr.select_groups(groups, mr.MODE_TOPIC, topic="A")
        self.assertTrue(all(g["topic"] == "A" and g["open"] for g in topicA))
        with self.assertRaises(ValueError):
            mr.select_groups(groups, "bogus")

    def test_pagination(self):
        groups = [{"key": ("snap", str(i))} for i in range(23)]
        self.assertEqual(len(mr.paginate(groups, 0)), 10)
        self.assertEqual(len(mr.paginate(groups, 2)), 3)
        self.assertEqual(mr.page_count(23), 3)
        self.assertEqual(mr.page_count(0), 0)
        self.assertEqual(mr.page_count(10), 1)

    def test_epoch_tolerant(self):
        self.assertIsNone(mr.to_epoch(None))
        self.assertIsNone(mr.to_epoch("not a date"))
        self.assertIsInstance(mr.to_epoch("2026-09-13 10:00:00"), float)
        self.assertIsInstance(mr.to_epoch("2026-09-13T10:00:00Z"), float)


# ===========================================================================
# LIFECYCLE THROUGH THE CANONICAL BOUNDARY
# ===========================================================================

class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        self.questions = await seed_quiz(self.db)

    async def _wrong_once(self, attempt="a1"):
        await complete(self.db, user=1, attempt=attempt, qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1]),
                                qr(1, OUTCOME_CORRECT, [0, 2]),
                                qr(2, OUTCOME_SKIPPED)],
                       questions=self.questions)

    async def test_01_wrong_creates_mistake_skipped_does_not(self):
        await self._wrong_once()
        rows = mistake_rows(self.db, 1, "qA")
        indices = sorted(r["q_index"] for r in rows)
        self.assertEqual(indices, [0])  # only the wrong q0; q2 skipped absent
        row = rows[0]
        self.assertEqual(row["wrong_count"], 1)
        self.assertEqual(row["correct_count"], 0)
        self.assertEqual(row["status"], "open")
        self.assertTrue(row["snapshot_id"])  # content-addressed snapshot
        self.assertEqual(row["topic"], "Polity")
        self.assertEqual(row["difficulty"], "hard")

    async def test_02_repeated_wrong_increments(self):
        await self._wrong_once("a1")
        await complete(self.db, user=1, attempt="a2", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])],
                       questions=self.questions)
        row = mistake_rows(self.db, 1, "qA")[0]
        self.assertEqual(row["wrong_count"], 2)
        self.assertEqual(len(row["wrong_attempt_ids"]), 2)

    async def test_03_wrong_then_correct_resolves_keeps_history(self):
        await self._wrong_once("a1")
        await complete(self.db, user=1, attempt="a2", qid="qA",
                       results=[qr(0, OUTCOME_CORRECT, [0])],
                       questions=self.questions)
        row = mistake_rows(self.db, 1, "qA")[0]
        self.assertEqual(row["wrong_count"], 1)
        self.assertEqual(row["correct_count"], 1)
        self.assertEqual(row["status"], "resolved")
        self.assertTrue(row["last_correct_at"])
        self.assertGreaterEqual(len(row["revision_history"]), 2)  # not deleted

    async def test_04_correct_then_later_wrong_relapses(self):
        await self._wrong_once("a1")
        await complete(self.db, user=1, attempt="a2", qid="qA",
                       results=[qr(0, OUTCOME_CORRECT, [0])],
                       questions=self.questions)
        await complete(self.db, user=1, attempt="a3", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])],
                       questions=self.questions)
        row = mistake_rows(self.db, 1, "qA")[0]
        self.assertEqual(row["status"], "open")      # re-opened
        self.assertEqual(row["wrong_count"], 2)
        self.assertEqual(row["correct_count"], 1)    # history retained

    async def test_05_duplicate_completion_is_idempotent(self):
        res = [qr(0, OUTCOME_INCORRECT, [1]), qr(2, OUTCOME_INCORRECT, [0])]
        await complete(self.db, user=1, attempt="dup", qid="qA",
                       results=res, questions=self.questions)
        await complete(self.db, user=1, attempt="dup", qid="qA",
                       results=res, questions=self.questions)  # replay
        rows = {r["q_index"]: r for r in mistake_rows(self.db, 1, "qA")}
        self.assertEqual(rows[0]["wrong_count"], 1)
        self.assertEqual(rows[2]["wrong_count"], 1)
        self.assertEqual(len(rows[0]["wrong_attempt_ids"]), 1)

    async def test_06_user_isolation(self):
        await complete(self.db, user=1, attempt="a1", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])],
                       questions=self.questions)
        await complete(self.db, user=2, attempt="b1", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])],
                       questions=self.questions)
        self.assertEqual(len(mistake_rows(self.db, 1, "qA")), 1)
        self.assertEqual(len(mistake_rows(self.db, 2, "qA")), 1)
        ov1 = await mr.MistakeRevisionService(self.db).overview(1)
        ov2 = await mr.MistakeRevisionService(self.db).overview(2)
        self.assertEqual(ov1["total_mistakes"], 1)
        self.assertEqual(ov2["total_mistakes"], 1)

    async def test_07_same_content_across_quizzes_groups_and_folds_both(self):
        # qB contains the SAME q0 content (identical text/options/answer).
        qB = quiz_questions({"topic": "Polity", "difficulty": "hard"}, None, None)
        qB = [dict(qB[0])]  # only the one identical question
        qB[0]["question"] = "Q0 body"; qB[0]["options"] = ["a", "b"]
        qB[0]["correct_option_id"] = 0
        await QuizRepository(self.db).create(111, "Quiz B", qB, qid="qB")
        await complete(self.db, user=1, attempt="a1", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])],
                       questions=self.questions)
        await complete(self.db, user=1, attempt="b1", qid="qB",
                       results=[qr(0, OUTCOME_INCORRECT, [1])], questions=qB)
        rev = mr.MistakeRevisionService(self.db)
        groups = (await rev.overview(1))["groups"]
        same = [g for g in groups if g["origins"] and
                {o["qid"] for o in g["origins"]} == {"qA", "qB"}]
        self.assertEqual(len(same), 1)  # identical content grouped
        built = await rev.build_revision(1, mr.MODE_SMART)
        target = [q for q in built["questions"] if q["question"] == "Q0 body"][0]
        idx = built["questions"].index(target)
        origins = built["origins_by_index"][idx]
        self.assertEqual({o["qid"] for o in origins}, {"qA", "qB"})
        # Answer correctly in the revision -> both origin rows resolve.
        for q, o in zip(built["questions"], built["origins_by_index"]):
            q["_revision_origins"] = o
        out = await complete(self.db, user=1, attempt="RV1", qid="RVx",
                             results=[qr(idx, OUTCOME_CORRECT, [0])],
                             questions=built["questions"], source="dm",
                             persisted=False)
        self.assertEqual(out["revision_folded"], 2)
        self.assertTrue(all(r["status"] == "resolved"
                            for r in mistake_rows(self.db, 1)
                            if r["q_index"] == 0))

    async def test_08_edited_quiz_uses_immutable_snapshot(self):
        await self._wrong_once("a1")
        sid = mistake_rows(self.db, 1, "qA")[0]["snapshot_id"]
        # Edit the live question in place -> content hash changes.
        edited = copy.deepcopy(self.questions)
        edited[0]["question"] = "COMPLETELY DIFFERENT NEW QUESTION"
        await QuizRepository(self.db).update_field("qA", "questions", edited)
        built = await mr.MistakeRevisionService(self.db).build_revision(1, mr.MODE_SMART)
        q0 = [q for q in built["questions"] if q["options"] == ["a", "b"]][0]
        self.assertEqual(q0["question"], "Q0 body")  # original snapshot, not edited
        self.assertEqual(q0["correct_option_id"], 0)

    async def test_09_deleted_quiz_still_revisable_from_snapshot(self):
        await self._wrong_once("a1")
        await QuizRepository(self.db).delete("qA")
        built = await mr.MistakeRevisionService(self.db).build_revision(1, mr.MODE_SMART)
        self.assertEqual(built["size"], 1)
        q0 = built["questions"][0]
        self.assertEqual(q0["question"], "Q0 body")
        self.assertEqual(q0["correct_option_id"], 0)

    async def test_10_shuffled_options_provenance_folds_correctly(self):
        await self._wrong_once("a1")
        rev = mr.MistakeRevisionService(self.db)
        built = await rev.build_revision(1, mr.MODE_SMART)
        self.assertEqual(built["size"], 1)
        # Simulate the DM engine shuffling options: display order permutes the
        # answer index, but origins are keyed by CANONICAL index.
        question = built["questions"][0]
        question["options"] = ["b", "a"]            # display-shuffled
        question["correct_option_id"] = 1          # correct option now at display idx 1
        question["_revision_origins"] = built["origins_by_index"][0]
        # User taps the (displayed) correct option 1; canonical result maps back.
        out = await complete(self.db, user=1, attempt="RVsh", qid="RVx",
                             results=[qr(0, OUTCOME_CORRECT, [1])],
                             questions=[question], source="dm", persisted=False)
        self.assertEqual(out["revision_folded"], 1)
        self.assertEqual(mistake_rows(self.db, 1, "qA")[0]["status"], "resolved")

    async def test_11_multi_correct_mistake_lifecycle(self):
        await complete(self.db, user=1, attempt="a1", qid="qA",
                       results=[qr(1, OUTCOME_INCORRECT, [0])],  # missed one of the pair
                       questions=self.questions)
        row = [r for r in mistake_rows(self.db, 1, "qA") if r["q_index"] == 1][0]
        self.assertEqual(row["wrong_count"], 1)
        await complete(self.db, user=1, attempt="a2", qid="qA",
                       results=[qr(1, OUTCOME_CORRECT, [0, 2])],
                       questions=self.questions)
        row = [r for r in mistake_rows(self.db, 1, "qA") if r["q_index"] == 1][0]
        self.assertEqual(row["status"], "resolved")

    async def test_13_malformed_event_data_is_safe(self):
        # Bad q_index / unknown outcome must not raise nor create junk rows.
        out = await complete(self.db, user=1, attempt="a1", qid="qA",
                             results=[{"q_index": "nope", "outcome": OUTCOME_INCORRECT},
                                      {"q_index": 0, "outcome": "weird"},
                                      {"outcome": OUTCOME_INCORRECT}],
                             questions=self.questions)
        self.assertEqual(out["mistake_ops"], 0)
        self.assertEqual(mistake_rows(self.db, 1, "qA"), [])

    async def test_14_missing_topic_is_honest(self):
        q = [{"question": "No meta", "options": ["x", "y"], "correct_option_id": 0}]
        await QuizRepository(self.db).create(111, "No meta quiz", q, qid="qN")
        await complete(self.db, user=1, attempt="a1", qid="qN",
                       results=[qr(0, OUTCOME_INCORRECT, [1])], questions=q)
        ov = await mr.MistakeRevisionService(self.db).overview(1)
        self.assertEqual(ov["topics"], [])           # no fabricated topic
        built = await mr.MistakeRevisionService(self.db).build_revision(1, mr.MODE_SMART)
        self.assertEqual(built["size"], 1)
        self.assertNotIn("analytics", built["questions"][0])  # nothing fabricated

    async def test_25_normal_result_generation_and_fields_intact(self):
        out = await complete(self.db, user=1, attempt="a1", qid="qA",
                             results=[qr(0, OUTCOME_INCORRECT, [1])],
                             questions=self.questions)
        for key in ("events_inserted", "mistake_ops", "gamification",
                    "revision_folded", "skipped"):
            self.assertIn(key, out)
        self.assertEqual(out["revision_folded"], 0)  # normal quiz -> no fold

    async def test_28_revision_awards_xp_once_and_folds_once_on_replay(self):
        await self._wrong_once("a1")
        rev = await mr.MistakeRevisionService(self.db).build_revision(1, mr.MODE_SMART)
        for q, o in zip(rev["questions"], rev["origins_by_index"]):
            q["_revision_origins"] = o
        res = [qr(0, OUTCOME_CORRECT, [0])]
        o1 = await complete(self.db, user=1, attempt="RV1", qid="RVx",
                            results=res, questions=rev["questions"],
                            source="dm", persisted=False)
        xp_after = o1["gamification"]["total_xp"]
        o2 = await complete(self.db, user=1, attempt="RV1", qid="RVx",
                            results=res, questions=rev["questions"],
                            source="dm", persisted=False)
        self.assertTrue(o2["gamification"]["duplicate"])
        user = self.db.collection("user_xp").docs[0]
        self.assertEqual(user["total_xp"], xp_after)            # no duplicate XP
        # a1 (stored quiz) + exactly one revision completion
        self.assertEqual(user["total_completions"], 2)
        self.assertEqual(mistake_rows(self.db, 1, "qA")[0]["correct_count"], 1)

    async def test_29_concurrent_duplicate_revision_folds_once(self):
        await self._wrong_once("a1")
        rev = await mr.MistakeRevisionService(self.db).build_revision(1, mr.MODE_SMART)
        for q, o in zip(rev["questions"], rev["origins_by_index"]):
            q["_revision_origins"] = o
        res = [qr(0, OUTCOME_INCORRECT, [1])]

        async def one():
            return await svc(self.db).record_completion(
                user_id=1, attempt_id="RVRACE", qid="RVx", quiz_name="rev",
                question_results=res, source="dm", quiz_persisted=False,
                questions=rev["questions"], sections=[])
        outs = await asyncio.gather(one(), one())
        row = mistake_rows(self.db, 1, "qA")[0]
        self.assertEqual(row["wrong_count"], 2)       # original 1 + exactly one fold
        self.assertEqual(row["wrong_attempt_ids"].count("RVRACE"), 1)
        user = self.db.collection("user_xp").docs[0]
        # a1 (stored quiz) + exactly one of the two racing revision completions
        self.assertEqual(user["total_completions"], 2)
        self.assertEqual(user.get("total_xp", 0) > 0, True)

    async def test_30_legacy_row_compat(self):
        # Hand-written pre-Phase-B row missing all newer fields.
        self.db.collection("user_mistakes").docs.append({
            "_id": "legacy1", "user_id": 1, "qid": "qA", "q_index": 2,
            "wrong_count": 1, "last_wrong_at": "2026-01-01 00:00:00",
        })
        ov = await mr.MistakeRevisionService(self.db).overview(1)
        # legacy row (q2, snapshot_id absent) is visible via fallback identity
        keys = [g["key"] for g in ov["groups"]]
        self.assertIn(("q", "qA#2"), keys)
        # a later correct through a normal completion resolves it safely
        await complete(self.db, user=1, attempt="new1", qid="qA",
                       results=[qr(2, OUTCOME_CORRECT, [1])],
                       questions=self.questions)
        row = [r for r in mistake_rows(self.db, 1, "qA") if r["q_index"] == 2][0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["correct_count"], 1)

    async def test_quiz_deleted_mid_revision_still_folds_to_origin(self):
        await self._wrong_once("a1")
        rev = mr.MistakeRevisionService(self.db).build_revision
        built = await rev(1, mr.MODE_SMART)
        for q, o in zip(built["questions"], built["origins_by_index"]):
            q["_revision_origins"] = o
        # Origin quiz disappears AFTER the revision was handed to the player.
        await QuizRepository(self.db).delete("qA")
        out = await complete(self.db, user=1, attempt="RVmid", qid="RVx",
                             results=[qr(0, OUTCOME_CORRECT, [0])],
                             questions=built["questions"], source="dm",
                             persisted=False)
        self.assertEqual(out["revision_folded"], 1)
        row = mistake_rows(self.db, 1, "qA")[0]
        self.assertEqual(row["status"], "resolved")

    async def test_unrevisable_legacy_row_is_excluded_not_fabricated(self):
        # Pre-Phase-B row: no snapshot_id AND its quiz no longer exists.
        self.db.collection("user_mistakes").docs.append({
            "_id": "ghost1", "user_id": 1, "qid": "goneQuiz", "q_index": 0,
            "wrong_count": 1, "status": "open",
            "last_wrong_at": "2026-01-01 00:00:00",
        })
        rev = mr.MistakeRevisionService(self.db)
        ov = await rev.overview(1)
        self.assertEqual(ov["total_mistakes"], 1)          # visible in counts
        built = await rev.build_revision(1, mr.MODE_SMART)
        self.assertEqual(built["size"], 0)
        self.assertEqual(built["excluded"], 1)             # honestly reported
        self.assertEqual(built["questions"], [])           # never fabricated

    async def test_stored_quiz_never_folds_even_with_planted_key(self):
        # Confused-deputy defense: a stored quiz whose question doc carries
        # the private provenance key (e.g. crafted content) must never fold.
        questions = copy.deepcopy(self.questions)
        questions[0]["_revision_origins"] = [{"qid": "evilQuiz", "q_index": 0}]
        out = await complete(self.db, user=1, attempt="a1", qid="qA",
                             results=[qr(0, OUTCOME_INCORRECT, [1])],
                             questions=questions, persisted=True)
        self.assertEqual(out["revision_folded"], 0)
        # Only the normal stored-quiz mistake exists; nothing for evilQuiz.
        self.assertEqual(len(mistake_rows(self.db, 1, "qA")), 1)
        self.assertEqual(mistake_rows(self.db, 1, "evilQuiz"), [])

    async def test_normal_adhoc_quiz_does_not_fold_or_write_mistakes(self):        # An ad-hoc AI-style DM quiz (persisted=False) without provenance:
        # XP works, but no origin mistakes and no fold.
        out = await complete(self.db, user=1, attempt="ai1", qid="AI123",
                             results=[qr(0, OUTCOME_INCORRECT, [1])],
                             questions=self.questions, source="aiquiz",
                             persisted=False)
        self.assertEqual(out["mistake_ops"], 0)
        self.assertEqual(out["revision_folded"], 0)
        self.assertIsNotNone(out["gamification"])


# ===========================================================================
# SELECTION / BOUNDS / FAIL-SOFT AT SERVICE LEVEL
# ===========================================================================

class ServiceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_16_17_18_repeated_all_topic_modes(self):
        db = new_db()
        q = quiz_questions({"topic": "Polity", "difficulty": "hard"},
                           {"topic": "History", "difficulty": "moderate"}, None)
        await seed_quiz(db, "qA", q)
        # q0 wrong twice (repeated), q1 wrong once (single), q2 never wrong
        await complete(db, user=1, attempt="a1", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1]),
                                qr(1, OUTCOME_INCORRECT, [1])], questions=q)
        await complete(db, user=1, attempt="a2", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])], questions=q)
        rev = mr.MistakeRevisionService(db)
        rep = await rev.build_revision(1, mr.MODE_REPEATED)
        self.assertEqual([x["question"] for x in rep["questions"]], ["Q0 body"])
        allv = await rev.build_revision(1, mr.MODE_ALL)
        self.assertEqual(len(allv["questions"]), 2)
        pol = await rev.build_revision(1, mr.MODE_TOPIC, topic="Polity")
        self.assertEqual(len(pol["questions"]), 1)
        hist = await rev.build_revision(1, mr.MODE_TOPIC, topic="History")
        self.assertEqual(len(hist["questions"]), 1)
        # forged/unknown topic -> nothing, never fabricated
        ghost = await rev.build_revision(1, mr.MODE_TOPIC, topic="Geography")
        self.assertEqual(ghost["size"], 0)

    async def test_19_20_empty_and_insufficient_states(self):
        db = new_db()
        rev = mr.MistakeRevisionService(db)
        ov = await rev.overview(1)
        self.assertEqual(ov["total_mistakes"], 0)
        self.assertEqual(ov["groups"], [])
        # single mistakes -> no repeated set but smart available
        q = await seed_quiz(db, "qA")
        await complete(db, user=1, attempt="a1", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])], questions=q)
        ov = await rev.overview(1)
        self.assertEqual(ov["repeated_groups"], [])
        self.assertEqual(len(ov["open_groups"]), 1)
        rep = await rev.build_revision(1, mr.MODE_REPEATED)
        self.assertEqual(rep["size"], 0)
        smart = await rev.build_revision(1, mr.MODE_SMART)
        self.assertEqual(smart["size"], 1)

    async def test_21_pagination_bounded(self):
        db = new_db()
        # 23 distinct wrong questions across synthetic stored quizzes.
        for n in range(23):
            qid = f"q{n}"
            questions = [{"question": f"Question number {n}", "options": ["a", "b"],
                          "correct_option_id": 0,
                          "analytics": {"topic": f"T{n % 3}", "difficulty": "hard"}}]
            await QuizRepository(db).create(111, qid, questions, qid=qid)
            await complete(db, user=1, attempt=f"a{n}", qid=qid,
                           results=[qr(0, OUTCOME_INCORRECT, [1])],
                           questions=questions)
        rev = mr.MistakeRevisionService(db)
        p0 = await rev.browse_all(1, 0)
        p2 = await rev.browse_all(1, 2)
        self.assertEqual(len(p0["items"]), mr.PAGE_SIZE)
        self.assertEqual(p0["pages"], 3)
        self.assertEqual(len(p2["items"]), 3)
        # deterministic order between pages (no overlap)
        ids0 = {it["question"] for it in p0["items"]}
        ids2 = {it["question"] for it in p2["items"]}
        self.assertFalse(ids0 & ids2)

    async def test_32_db_operation_and_memory_bounds_for_large_history(self):
        db = new_db()
        # 500 wrong questions (each its own quiz/content).
        for n in range(500):
            qid = f"big{n}"
            questions = [{"question": f"Q{n}", "options": ["a", "b"],
                          "correct_option_id": 0,
                          "analytics": {"topic": f"T{n % 5}", "difficulty": "hard"}}]
            await QuizRepository(db).create(111, qid, questions, qid=qid)
            await complete(db, user=1, attempt=f"a{n}", qid=qid,
                           results=[qr(0, OUTCOME_INCORRECT, [1])],
                           questions=questions)
        mistakes = db.collection("user_mistakes")
        snapshots = db.collection("question_snapshots")
        quizzes = db.collection("quizzes")
        m0, s0, q0 = mistakes.queries, snapshots.queries, quizzes.queries
        rev = mr.MistakeRevisionService(db)
        built = await rev.build_revision(1, mr.MODE_SMART)
        dm, ds, dq = (mistakes.queries - m0, snapshots.queries - s0,
                      quizzes.queries - q0)
        self.assertLessEqual(built["size"], mr.REVISION_SIZE)  # <=10 in memory
        # Exactly ONE bounded candidate read (capped at 200), regardless of
        # the 500-row history; no per-row N+1 mistake scans.
        self.assertEqual(dm, 1)
        # Quiz lookups must be bounded by the <=10 selected distinct origins,
        # never one per 500 candidates.
        self.assertLessEqual(dq, mr.REVISION_SIZE)
        # Exactly ONE bounded snapshot $in for the <=10 selected, not 500.
        self.assertEqual(ds, 1)

    async def test_24_fold_failsoft_when_mistake_db_raises(self):
        db = new_db()
        q = await seed_quiz(db, "qA")
        await complete(db, user=1, attempt="a1", qid="qA",
                       results=[qr(0, OUTCOME_INCORRECT, [1])], questions=q)
        rev = await mr.MistakeRevisionService(db).build_revision(1, mr.MODE_SMART)
        for x, o in zip(rev["questions"], rev["origins_by_index"]):
            x["_revision_origins"] = o
        # Make the fold (user_mistakes writes) explode; completion must survive.
        orig_bulk = db.collection("user_mistakes").bulk_write
        async def boom(*a, **k):
            raise RuntimeError("mongo down")
        db.collection("user_mistakes").bulk_write = boom
        out = await complete(db, user=1, attempt="RVok", qid="RVx",
                             results=[qr(0, OUTCOME_CORRECT, [0])],
                             questions=rev["questions"], source="dm", persisted=False)
        self.assertIsNotNone(out["gamification"])  # XP still awarded
        self.assertEqual(out["revision_folded"], 0)


# ===========================================================================
# TELEGRAM HANDLER SECURITY / UX (real handler, stubbed siblings)
# ===========================================================================

def _install_handler_module():
    """Import the real mistakes.py while stubbing heavy sibling handler
    modules; capture start_private_quiz calls."""
    captured = {}
    stubs = {}
    for name in ["admin", "ai_quiz", "mix", "pdf_quiz", "poll_quiz", "podcast",
                 "reports", "scheduling", "setup_wizard", "translation"]:
        m = types.ModuleType(f"quizbot.runner_bot.handlers.{name}")
        m.register = lambda app: None
        sys.modules[f"quizbot.runner_bot.handlers.{name}"] = m
        stubs[name] = m
    qp = types.ModuleType("quizbot.runner_bot.handlers.quiz_play")

    async def start_private_quiz(chat_id, ctx, questions, quiz, qid, **kw):
        captured["called"] = True
        captured["chat_id"] = chat_id
        captured["questions"] = questions
        captured["quiz"] = quiz
        captured["qid"] = qid
    qp.start_private_quiz = start_private_quiz
    qp.register = lambda app: None
    sys.modules["quizbot.runner_bot.handlers.quiz_play"] = qp
    # Force a fresh import of the package + real mistakes module.
    sys.modules.pop("quizbot.runner_bot.handlers", None)
    sys.modules.pop("quizbot.runner_bot.handlers.mistakes", None)
    handlers = importlib.import_module("quizbot.runner_bot.handlers")
    mod = importlib.import_module("quizbot.runner_bot.handlers.mistakes")
    return mod, handlers, captured


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, ctype="private"):
        self.type = ctype
        self.id = 1


class FakeMessage:
    def __init__(self, chat=None):
        self.text_sent = []
        self.edited = []
        self.replies = []
        self._chat = chat or FakeChat()

    @property
    def chat(self):
        return self._chat

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def reply_html(self, text, **kw):
        self.replies.append(text)

    async def edit_text(self, text, **kw):
        if self.edited and self.edited[-1] == text:
            raise RuntimeError("message is not modified")
        self.edited.append(text)
        self._kw = kw

    async def answer(self, *a, **k):
        return None


class FakeQuery:
    def __init__(self, data, uid, message=None):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = message or FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeUpdate:
    def __init__(self, uid=1, data=None, chat_type="private"):
        self.user = FakeUser(uid)
        self.chat = FakeChat(chat_type)
        self.message = FakeMessage(self.chat)
        self.callback_query = FakeQuery(data, uid, self.message)

    @property
    def effective_user(self):
        return self.user

    @property
    def effective_chat(self):
        return self.chat

    @property
    def effective_message(self):
        return self.message


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return FakeMessage()


class FakeCtx:
    def __init__(self):
        self.bot = FakeBot()


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.handlers_pkg, cls.captured = _install_handler_module()

    async def asyncSetUp(self):
        self.db = new_db()
        self.captured.clear()
        # Point the handler's get_db at our fake.
        self._orig_get_db = self.mod.get_db
        self.mod.get_db = lambda: self.db

    def tearDown(self):
        self.mod.get_db = self._orig_get_db

    async def _seed_mistake(self, user=1, repeats=1, topic=None):
        questions = quiz_questions(
            {"topic": topic or "Polity", "difficulty": "hard"} if repeats else None,
            None, None)
        await seed_quiz(self.db, "qA", questions)
        for n in range(repeats):
            await complete(self.db, user=user, attempt=f"a{n}", qid="qA",
                           results=[qr(0, OUTCOME_INCORRECT, [1])],
                           questions=questions)

    async def test_19_empty_state(self):
        ctx = FakeCtx()
        await self.mod.mistakes_command(FakeUpdate(1), ctx)
        text = ctx.bot.sent[0][1]
        self.assertIn("don't have any recorded mistakes", text)
        # no actionable buttons on an empty menu
        kb = ctx.bot.sent[0][2].get("reply_markup")
        self.assertEqual(list(kb.inline_keyboard), [])

    async def test_group_chat_is_redirected_to_dm(self):
        upd = FakeUpdate(1, chat_type="group")
        await self.mod.mistakes_command(upd, FakeCtx())
        self.assertEqual(len(upd.message.replies), 1)
        self.assertIn("private chat", upd.message.replies[0])
        self.assertEqual(FakeCtx().bot.sent, [])  # no menu/data leaked via bot send

    async def test_menu_offers_only_available_modes_single_mistake(self):
        await self._seed_mistake(repeats=1)
        ctx = FakeCtx()
        await self.mod.mistakes_command(FakeUpdate(1), ctx)
        text, kw = ctx.bot.sent[0][1], ctx.bot.sent[0][2]
        labels = [b.text for row in kw["reply_markup"].inline_keyboard for b in row]
        joined = " ".join(labels)
        self.assertIn("Smart", joined)
        self.assertIn("Repeated (0)", joined)   # present but zero
        self.assertIn("All mistakes (1)", joined)

    async def test_23_unauthorized_callback_rejected_no_data(self):
        await self._seed_mistake(repeats=2)
        upd = FakeUpdate(2)  # attacker id 2 pressing user 1's smart button
        upd.callback_query.data = self.mod._cb("smart", 1)
        await self.mod.mistakes_callback(upd, FakeCtx())
        ans = upd.callback_query.answers[0]
        self.assertIn("Not your", ans[0])
        self.assertTrue(ans[1])
        self.assertNotIn("called", self.captured)  # nothing launched/leaked

    async def test_22_malformed_callbacks_fail_safely(self):
        for bad in [None, "", "mst:", "mst:smart", "mst:smart:x", "other:smart:1",
                    "mst:all:1:xy", "mst:topic:1"]:
            upd = FakeUpdate(1)
            upd.callback_query.data = bad
            await self.mod.mistakes_callback(upd, FakeCtx())
            ans = upd.callback_query.answers[0][0]
            self.assertTrue(ans and "Invalid" in ans, (bad, ans))
            self.assertNotIn("called", self.captured)

    async def test_forged_topic_index_out_of_range(self):
        await self._seed_mistake(repeats=1, topic="Polity")
        upd = FakeUpdate(1)
        upd.callback_query.data = self.mod._cb("topic", 1, "99")
        await self.mod.mistakes_callback(upd, FakeCtx())
        self.assertIn("no longer available", upd.callback_query.answers[-1][0])
        self.assertNotIn("called", self.captured)

    async def test_smart_launch_uses_dm_engine_with_provenance_and_no_persist(self):
        await self._seed_mistake(repeats=2)
        upd = FakeUpdate(1)
        upd.callback_query.data = self.mod._cb("smart", 1)
        await self.mod.mistakes_callback(upd, FakeCtx())
        self.assertTrue(self.captured["called"])
        quiz = self.captured["quiz"]
        self.assertEqual(quiz["analytics_source"], "dm")
        self.assertTrue(quiz["_revision_session"])
        for q in self.captured["questions"]:
            self.assertIn("_revision_origins", q)  # provenance survives in list
            self.assertTrue(q["_revision_origins"])

    async def test_topic_special_chars_are_escaped_in_launch_status(self):
        await self._seed_mistake(repeats=1, topic="P<G & H")
        # The topics list is re-derived server side; launch via its index.
        topics = await self.mod.mr.MistakeRevisionService(self.db).topics(1)
        idx = next(i for i, t in enumerate(topics) if t["topic"] == "P<G & H")
        upd, ctx = FakeUpdate(1), FakeCtx()
        upd.callback_query.data = self.mod._cb("topic", 1, str(idx))
        await self.mod.mistakes_callback(upd, ctx)
        self.assertTrue(self.captured["called"])
        # The follow-up status is HTML; the user-controlled topic is escaped.
        texts = [t for _, t, _ in ctx.bot.sent]
        self.assertTrue(any("P&lt;G &amp; H" in t for t in texts))
        self.assertFalse(any("P<G" in t for t in texts))

    async def test_repeated_insufficient_message(self):
        await self._seed_mistake(repeats=1)
        upd = FakeUpdate(1)
        # repnone alert
        upd.callback_query.data = self.mod._cb("repnone", 1)
        await self.mod.mistakes_callback(upd, FakeCtx())
        self.assertIn("twice", upd.callback_query.answers[-1][0])
        # actual rep launch with zero repeated -> status message, no launch
        upd2 = FakeUpdate(1); upd2.callback_query.data = self.mod._cb("rep", 1)
        await self.mod.mistakes_callback(upd2, FakeCtx())
        sent = upd2.message
        self.assertNotIn("called", self.captured)

    async def test_21_all_pagination_callbacks(self):
        # 12 distinct mistakes
        questions = None
        for n in range(12):
            qid = f"q{n}"
            qq = [{"question": f"Question {n}", "options": ["a", "b"],
                   "correct_option_id": 0,
                   "analytics": {"topic": "Polity", "difficulty": "hard"}}]
            await QuizRepository(self.db).create(111, qid, qq, qid=qid)
            await complete(self.db, user=1, attempt=f"a{n}", qid=qid,
                           results=[qr(0, OUTCOME_INCORRECT, [1])], questions=qq)
        upd = FakeUpdate(1)
        upd.callback_query.data = self.mod._cb("all", 1, "0")
        await self.mod.mistakes_callback(upd, FakeCtx())
        self.assertTrue(any("page 1" in e for e in upd.message.edited))
        upd.callback_query.data = self.mod._cb("all", 1, "1")
        await self.mod.mistakes_callback(upd, FakeCtx())
        page2_text = next(e for e in upd.message.edited if "page 2" in e)
        displayed = set(re.findall(r"^\d+\. (Question \d+)\b", page2_text, re.M))
        self.assertEqual(len(displayed), 2)  # 12 items -> page 2 has 2
        # stale/non-numeric page -> safe
        upd.callback_query.data = self.mod._cb("all", 1, "abc")
        await self.mod.mistakes_callback(upd, FakeCtx())
        self.assertIn("Invalid page", upd.callback_query.answers[-1][0])
        # "Practise this page" on page 2 launches EXACTLY those page-2 items,
        # not the global top-10.
        self.captured.clear()
        upd.callback_query.data = self.mod._cb("allgo", 1, "1")
        await self.mod.mistakes_callback(upd, FakeCtx())
        self.assertTrue(self.captured["called"])
        launched = {q["question"] for q in self.captured["questions"]}
        self.assertEqual(launched, displayed)
        self.assertEqual(len(launched), 2)

    async def test_db_failure_is_failsoft_command(self):
        def _boom():
            raise RuntimeError("db down")
        self.mod.get_db = _boom
        ctx = FakeCtx()
        await self.mod.mistakes_command(FakeUpdate(1), ctx)
        # graceful message, no raise
        self.assertTrue(any("Couldn't load" in t for _, t, _ in ctx.bot.sent))

    async def test_31_register_adds_handlers(self):
        app = types.SimpleNamespace(added=[])
        app.add_handler = lambda h: app.added.append(h)
        self.mod.register(app)
        self.assertEqual(len(app.added), 2)  # command + callback


if __name__ == "__main__":
    unittest.main(verbosity=2)
