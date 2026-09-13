"""Phase E tests -- /weakquiz: per-user weak-topic ranking, bounded existing-
question selection, DM-engine delivery, canonical analytics/mistake/XP
feedback, security and performance bounds.

Self-contained in-memory Motor-like fake: projection-aware finds, unique
indexes, atomic single-document updates (Phase D style) plus a small Mongo
aggregation interpreter ($match/$group/$sort/$facet/$project/$unwind/$limit)
adapted from the Phase B test fake. No network or real MongoDB.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from quizbot.analytics import weak_practice as wp
from quizbot.analytics.gamification import ensure_gamification_indexes
from quizbot.analytics.metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    OUTCOME_SKIPPED,
    TOPIC_SOURCE_QUESTION,
    TOPIC_SOURCE_SECTION,
)
from quizbot.analytics.service import AnalyticsService
from quizbot.database import MistakeRepository, QuizRepository

# ---------------------------------------------------------------------------
# In-memory fake
# ---------------------------------------------------------------------------

def _get_path(doc, path):
    if isinstance(path, str):
        parts = path.split(".")
    else:
        parts = list(path)
    cur = doc
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _set_path(doc, path, value):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _path_values(doc, parts):
    """All terminal values for a dotted path, descending into arrays
    (Mongo array-matching semantics). Missing -> [None]."""
    if not parts:
        return [doc]
    key = parts[0]
    rest = parts[1:]
    if not isinstance(doc, dict) or key not in doc:
        return [None]
    v = doc[key]
    if isinstance(v, list):
        out = []
        for el in v:
            if rest:
                out.extend(_path_values(el, rest) if isinstance(el, dict) else [None])
            else:
                out.append(el)
        return out
    return _path_values(v, rest) if rest else [v]


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
                if field in val:
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
        if key == "$nor":
            if any(_matches(doc, s) for s in cond):
                return False
            continue
        if any(not _value_match(v, cond) for v in _path_values(doc, key.split("."))):
            # Mongo matches if ANY array element satisfies; _path_values also
            # yields [None] for a missing path, which must still satisfy a
            # $exists:false / $eq:None condition.
            if not any(_value_match(v, cond) for v in _path_values(doc, key.split("."))):
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
        self.upserted_id = inserted_id


def _sort_value(x, field):
    v = _get_path(x, field)
    if v is None:
        return (1, 0, 0.0, "")
    if isinstance(v, bool):
        return (0, 0, float(v), "")
    if isinstance(v, (int, float)):
        return (0, 0, float(v), "")
    if isinstance(v, str):
        return (0, 1, 0.0, v)
    return (0, 2, 0.0, str(v))


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, keys, direction=1):
        if isinstance(keys, str):
            keys = [(keys, direction)]
        for field, d in reversed(keys):
            self._docs.sort(key=lambda x, f=field: _sort_value(x, f), reverse=d < 0)
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        if n is not None:
            self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for d in list(self._docs):
                yield copy.deepcopy(d)
        return gen()


# -- minimal aggregation expression interpreter (Phase-B-style) ------------

def _truthy(v):
    return bool(v)


def _eval(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        if expr == "$$REMOVE":
            return None
        return _get_path(doc, expr[1:])
    if isinstance(expr, list):
        return [_eval(e, doc) for e in expr]
    if isinstance(expr, dict):
        ops = [k for k in expr if k.startswith("$")]
        if not ops:
            return {k: _eval(v, doc) for k, v in expr.items()}
        op = ops[0]
        args = expr[op]
        if op == "$cond":
            if isinstance(args, dict):
                cond, then, other = args["if"], args["then"], args["else"]
            else:
                cond, then, other = args
            return _eval(then if _truthy(_eval(cond, doc)) else other, doc)
        if op == "$ifNull":
            v = _eval(args[0], doc)
            return v if v is not None else _eval(args[1], doc)

        def _cmp(how):
            a, b = _eval(args[0], doc), _eval(args[1], doc)
            try:
                if a is None or b is None:
                    return False
                return how(a, b)
            except TypeError:
                return False
        if op == "$eq":
            return _eval(args[0], doc) == _eval(args[1], doc)
        if op == "$ne":
            return _eval(args[0], doc) != _eval(args[1], doc)
        if op == "$gte":
            return _cmp(lambda a, b: a >= b)
        if op == "$gt":
            return _cmp(lambda a, b: a > b)
        if op == "$lte":
            return _cmp(lambda a, b: a <= b)
        if op == "$lt":
            return _cmp(lambda a, b: a < b)
        if op == "$in":
            return _eval(args[0], doc) in _eval(args[1], doc)
        if op == "$and":
            return all(_truthy(_eval(a, doc)) for a in args)
        if op == "$or":
            return any(_truthy(_eval(a, doc)) for a in args)
        if op == "$not":
            return not _truthy(_eval(args, doc))
        raise AssertionError(f"unsupported expr op {op}")
    return expr


def _hashable(v):
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple((k, _hashable(x)) for k, x in v.items())
    return v


def _group(rows, spec):
    id_spec = spec["_id"]
    id_is_expression = isinstance(id_spec, dict) and any(
        str(k).startswith("$") for k in id_spec)
    buckets = {}

    def id_key(doc):
        if id_spec is None:
            return None
        if id_is_expression:
            return _hashable(_eval(id_spec, doc))
        if isinstance(id_spec, dict):
            return tuple((k, _hashable(_eval(v, doc))) for k, v in id_spec.items())
        return _hashable(_eval(id_spec, doc))

    def id_value(key):
        if id_spec is None:
            return None
        if id_is_expression:
            return key
        if isinstance(id_spec, dict):
            return {k: v for k, v in key}
        return key

    for doc in rows:
        buckets.setdefault(id_key(doc), []).append(doc)

    out = []
    for key, docs in buckets.items():
        row = {"_id": id_value(key)}
        for field, acc in spec.items():
            if field == "_id":
                continue
            op, arg = next(iter(acc.items()))
            if op == "$sum":
                if isinstance(arg, int):
                    row[field] = len(docs) * arg
                else:
                    row[field] = sum(
                        (v if isinstance(v, (int, float)) else 0)
                        for v in (_eval(arg, d) for d in docs))
            elif op == "$addToSet":
                vals = []
                for d in docs:
                    v = _eval(arg, d)
                    if v is not None and _hashable(v) not in [_hashable(x) for x in vals]:
                        vals.append(v)
                row[field] = vals
            elif op == "$min":
                vals = [v for v in (_eval(arg, d) for d in docs) if v is not None]
                row[field] = min(vals) if vals else None
            elif op == "$max":
                vals = [v for v in (_eval(arg, d) for d in docs) if v is not None]
                row[field] = max(vals) if vals else None
            elif op == "$first":
                row[field] = _eval(arg, docs[0])
            elif op == "$last":
                row[field] = _eval(arg, docs[-1])
            else:
                raise AssertionError(f"unsupported group op {op}")
        out.append(row)
    return out


def _run_pipeline(rows, pipeline):
    for stage in pipeline:
        op, spec = next(iter(stage.items()))
        if op == "$match":
            rows = [r for r in rows if _matches(r, spec)]
        elif op == "$sort":
            for field, direction in reversed(list(spec.items())):
                rows.sort(key=lambda r, f=field: _sort_value(r, f),
                          reverse=direction < 0)
        elif op == "$skip":
            rows = rows[spec:]
        elif op == "$limit":
            rows = rows[:spec]
        elif op == "$facet":
            rows = [{name: _run_pipeline(copy.deepcopy(rows), sub)
                     for name, sub in spec.items()}]
        elif op == "$group":
            rows = _group(rows, spec)
        elif op == "$project":
            projected = []
            for r in rows:
                nr = {}
                for k, v in spec.items():
                    if v == 0:
                        continue
                    if v == 1:
                        # Mongo inclusion form: copy the existing (possibly
                        # dotted) field, not the literal integer 1.
                        vals = _path_values(r, k.split("."))
                        if vals:
                            nr[k] = vals[0]
                    elif isinstance(v, str) and v.startswith("$"):
                        nr[k] = _get_path(r, v[1:])
                    elif isinstance(v, dict):
                        val = _eval(v, r)
                        if val is not None or v.get("$ifNull"):
                            nr[k] = val
                    else:
                        nr[k] = v
                projected.append(nr)
            rows = projected
        elif op == "$unwind":
            if isinstance(spec, str):
                path = spec[1:]
                idx_field = None
            else:
                path = spec["path"][1:]
                idx_field = spec.get("includeArrayIndex")
            new_rows = []
            for r in rows:
                arr = _get_path(r, path)
                if isinstance(arr, list):
                    for i, el in enumerate(arr):
                        nr = copy.deepcopy(r)
                        _set_path(nr, path, el)
                        if idx_field:
                            nr[idx_field] = i
                        new_rows.append(nr)
            rows = new_rows
        elif op == "$count":
            rows = [{spec: len(rows)}]
        else:
            raise AssertionError(f"unsupported stage {op}")
    return rows


class FakeCollection:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self.docs = []
        self.indexes = []
        self._seq = 0
        self.queries = 0

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

    def _project(self, doc, projection):
        if not projection:
            return doc
        out = {}
        for k, flag in projection.items():
            if k == "_id":
                if flag and "_id" in doc:
                    out["_id"] = doc["_id"]
            elif flag:
                vals = _path_values(doc, k.split("."))
                v = vals[0] if vals else None
                if v is not None:
                    out[k] = v
        return out

    async def find_one(self, filt=None, sort=None, projection=None):
        self.queries += 1
        rows = [d for d in self.docs if _matches(d, filt or {})]
        if sort:
            rows = list(FakeCursor(rows).sort(sort)._docs)
        return copy.deepcopy(self._project(rows[0], projection)) if rows else None

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

    async def aggregate(self, pipeline):
        self.queries += 1
        rows = [copy.deepcopy(d) for d in self.docs]
        for r in _run_pipeline(rows, pipeline):
            yield r


class FakeDB:
    def __init__(self):
        self._cols = {}

    def collection(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name, self)
        return self._cols[name]


def new_db():
    db = FakeDB()
    return db


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def qr(i, outcome, selected=None, correct=None, time=10):
    return {"q_index": i, "selected": selected or [],
            "correct_option_id": correct or [], "correct_option": correct or [],
            "outcome": outcome, "time_taken": time}


def question(text, opts=None, correct=0, **analytics):
    q = {"question": text, "options": opts or ["a", "b"],
         "correct_option_id": correct}
    meta = {k: v for k, v in analytics.items() if v is not None}
    if meta:
        q["analytics"] = meta
    return q


def polity_quiz(qid="qA", creator=111, n_wrong_topic=8, quiz_type="free",
                with_expl=False):
    """Stored quiz covering Polity/Judiciary (weak) with enough questions to
    span multiple sessions."""
    qs = []
    for i in range(n_wrong_topic):
        q = question(f"Judiciary question {i}",
                     [f"opt{i}a", f"opt{i}b"], 0,
                     subject="Polity", topic="Judiciary", difficulty="hard")
        if with_expl:
            q["explanation"] = f"Because {i}."
        qs.append(q)
    qs.append(question("Parliament question", ["x", "y"], 0,
                       subject="Polity", topic="Parliament",
                       difficulty="moderate"))
    qs.append(question("No meta question", ["x", "y"], 1))
    return qs


async def seed(db, qid, questions, creator=111, name=None, quiz_type="free",
               sections=None):
    await QuizRepository(db).create(
        creator, name or qid, questions, qid=qid, quiz_type=quiz_type,
        sections=sections or [])
    return questions


def service(db):
    return AnalyticsService(db)


async def complete(db, *, user, attempt, qid, results, questions,
                   source="group", persisted=True, sections=None, at=None,
                   finalize=True):
    return await service(db).record_completion(
        user_id=user, attempt_id=attempt, qid=qid, quiz_name=qid,
        question_results=results, source=source, quiz_persisted=persisted,
        questions=questions, sections=sections or [], finalize=finalize, at=at)


def iso(days_ago=0):
    dt = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def seed_topic_performance(db, user, qid, questions, *,
                                  correct_idx, wrong_idx, sections=None,
                                  at_days_ago=2, attempt_prefix="a"):
    """Play one stored quiz attempt with given correct/wrong indices."""
    results = []
    for i in wrong_idx:
        results.append(qr(i, OUTCOME_INCORRECT, [9]))
    for i in correct_idx:
        q = questions[i]
        cid = q["correct_option_id"]
        sel = cid if isinstance(cid, list) else [cid]
        results.append(qr(i, OUTCOME_CORRECT, sel))
    await complete(db, user=user, attempt=f"{attempt_prefix}-{qid}-{at_days_ago}-{len(wrong_idx)}",
                   qid=qid, results=sorted(results, key=lambda r: r["q_index"]),
                   questions=questions, sections=sections, at=iso(at_days_ago))


# ===========================================================================
# PURE MODEL
# ===========================================================================

class PureModelTests(unittest.TestCase):
    def test_01_no_history_and_02_insufficient(self):
        b = wp.normalize_rollup({"correct": 0, "incorrect": 0})
        self.assertEqual(b["answered"], 0)
        self.assertFalse(wp.is_eligible(b))
        # Four answers, one wrong -> below both evidence floors.
        b = wp.normalize_rollup({"correct": 3, "incorrect": 1, "topic": "T",
                                 "subject": "S"})
        self.assertFalse(wp.is_eligible(b))

    def test_03_single_wrong_never_weak(self):
        for n in (1, 2, 3, 4):
            b = wp.normalize_rollup(
                {"correct": n - 1, "incorrect": 1, "topic": "T"})
            self.assertFalse(wp.is_eligible(b), n)

    def test_04_five_answered_two_incorrect_qualifies_at_low_accuracy(self):
        b = wp.normalize_rollup(
            {"correct": 3, "incorrect": 2, "topic": "J", "subject": "P"})
        self.assertEqual(b["answered"], 5)
        self.assertEqual(b["accuracy_pct"], 60.0)
        self.assertTrue(wp.is_eligible(b))

    def test_05_accuracy_threshold(self):
        # 5 correct / 2 wrong = 71.43% -> above the 70% ceiling -> not weak
        hi = wp.normalize_rollup({"correct": 5, "incorrect": 2, "topic": "T"})
        self.assertFalse(wp.is_eligible(hi))
        # 4 correct / 2 wrong = 66.7% -> weak
        lo = wp.normalize_rollup({"correct": 4, "incorrect": 2, "topic": "T"})
        self.assertTrue(wp.is_eligible(lo))

    def test_06_07_wilson_sample_size_tiebreak(self):
        # Tiny perfect-failure samples are not eligible until 5 answers...
        tiny = wp.normalize_rollup({"correct": 0, "incorrect": 2, "topic": "t2"})
        self.assertFalse(wp.is_eligible(tiny))
        # ...and among eligible tied 0% topics the bigger sample ranks worse.
        small = wp.normalize_rollup({"correct": 0, "incorrect": 5, "topic": "small"})
        large = wp.normalize_rollup({"correct": 0, "incorrect": 50, "topic": "large"})
        medium = wp.normalize_rollup({"correct": 15, "incorrect": 25, "topic": "medium"})
        ranked = wp.rank_buckets([small, large, medium])
        self.assertEqual([b["topic"] for b in ranked], ["large", "small", "medium"])
        # Determinism regardless of insertion order.
        self.assertEqual(
            [b["topic"] for b in wp.rank_buckets([medium, large, small])],
            ["large", "small", "medium"])

    def test_08_same_topic_different_subjects_stay_separate(self):
        rows = [
            {"correct": 1, "incorrect": 6, "subject": "Polity", "topic": "Judiciary"},
            {"correct": 8, "incorrect": 1, "subject": "History", "topic": "Judiciary"},
        ]
        buckets = [wp.normalize_rollup(r) for r in rows]
        keys = {b["key"] for b in buckets}
        self.assertEqual(keys, {("Polity", "Judiciary"), ("History", "Judiciary")})
        weak = [b for b in buckets if wp.is_eligible(b)]
        self.assertEqual([b["subject"] for b in weak], ["Polity"])

    def test_10_subject_only_fallback(self):
        b = wp.normalize_rollup(
            {"correct": 1, "incorrect": 6, "subject": "Polity",
             "subject_only": True})
        self.assertIsNone(b["topic"])
        self.assertEqual(b["label"], "Polity \u00b7 untagged")
        self.assertTrue(wp.is_eligible(b))

    def test_section_only_mistake_groups_excluded(self):
        rows = [
            {"snapshot_id": "h1", "qid": "qA", "q_index": 0, "wrong_count": 4,
             "correct_count": 0, "status": "open", "subject": "P",
             "topic": "Section 1", "topic_source": TOPIC_SOURCE_SECTION,
             "last_wrong_at": iso(1)},
            {"snapshot_id": "h2", "qid": "qB", "q_index": 0, "wrong_count": 4,
             "correct_count": 0, "status": "open", "subject": "P",
             "topic": "Judiciary", "topic_source": TOPIC_SOURCE_QUESTION,
             "last_wrong_at": iso(1)},
        ]
        signals, groups = wp.mistake_topic_signals(rows)
        topics = {g["topic"] for g in groups}
        self.assertNotIn("Section 1", topics)
        self.assertIn("Judiciary", topics)
        self.assertEqual(signals[("P", "Judiciary")]["repeated"], 1)

    def test_13_14_15_repeated_relapse_recent_ranking(self):
        base = lambda **k: wp.normalize_rollup(
            {"correct": 2, "incorrect": 8, "answered": 10, **k})
        plain = base(topic="plain", subject="S")
        repeated = base(topic="rep", subject="S")
        repeated["repeated_mistakes"] = 3
        relapse = base(topic="rel", subject="S")
        relapse["relapse_mistakes"] = 2
        recent = base(topic="rec", subject="S")
        recent["recent_incorrect"] = 7
        ranked = wp.rank_buckets([plain, recent, relapse, repeated])
        self.assertEqual([b["topic"] for b in ranked][:2], ["rep", "rel"])
        # relapse before plain, recent before plain on later tiebreak
        self.assertLess([b["topic"] for b in ranked].index("rec"),
                        [b["topic"] for b in ranked].index("plain"))
        # reasons include real signals only
        reasons = wp.bucket_reasons(repeated)
        self.assertTrue(any("repeated" in r for r in reasons))
        rr = wp.bucket_reasons(relapse)
        self.assertTrue(
            any(("again after correcting" in r) or ("relapsed" in r) for r in rr))


# ===========================================================================
# SERVICE-LEVEL
# ===========================================================================

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        await ensure_gamification_indexes(self.db)

    async def _polity_history(self, user=1, qid="qA", n=8, quiz_type="free"):
        qs = polity_quiz(qid, n_wrong_topic=n, quiz_type=quiz_type)
        await seed(self.db, qid, qs, quiz_type=quiz_type)
        # 8 Judiciary questions wrong across two attempts; Parliament correct.
        wrong = list(range(n))
        await seed_topic_performance(
            self.db, user, qid, qs, correct_idx=[n], wrong_idx=wrong[:5],
            attempt_prefix="a1")
        await seed_topic_performance(
            self.db, user, qid, qs, correct_idx=[n], wrong_idx=wrong[5:],
            attempt_prefix="a2")
        return qs

    async def test_01_no_history_state(self):
        svc = wp.WeakPracticeService(self.db)
        ov = await svc.overview(1)
        self.assertEqual(ov["state"], "no_history")
        built = await svc.build_practice(1)
        self.assertEqual(built["state"], "no_history")

    async def test_02_insufficient_state_message_data(self):
        qs = polity_quiz("qA")
        await seed(self.db, "qA", qs)
        # only one wrong, one correct (2 answered in topic)
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[8], wrong_idx=[0])
        ov = await wp.WeakPracticeService(self.db).overview(1)
        self.assertEqual(ov["state"], "insufficient")
        self.assertFalse(ov["eligible"])

    async def test_09_section_topics_never_drive_selection(self):
        # Quiz with sections but NO explicit question metadata.
        qs = [question(f"Q{i}", ["a", "b"], 0) for i in range(8)]
        sections = [{"name": "Section 1", "question_range": [1, 4], "timer": 30},
                    {"name": "Section 2", "question_range": [5, 8], "timer": 30}]
        await seed(self.db, "qS", qs, sections=sections)
        results = [qr(i, OUTCOME_INCORRECT, [9]) for i in range(7)]
        results.append(qr(7, OUTCOME_CORRECT, [0]))
        await complete(self.db, user=1, attempt="s1", qid="qS",
                       results=results, questions=qs, sections=sections)
        ov = await wp.WeakPracticeService(self.db).overview(1)
        # Section names exist as topics in raw events but no eligible weak
        # topic and no fabricated buckets.
        self.assertFalse(any(
            b["topic"] == "Section 1" for b in ov["eligible"]))

    async def test_11_user_isolation(self):
        qs_a = await self._polity_history(user=1, qid="qA")
        # User 2 is strong at the same topic.
        results2 = []
        for i in range(8):
            results2.append(qr(i, OUTCOME_CORRECT, [0]))
        results2.append(qr(8, OUTCOME_CORRECT, [0]))
        await complete(self.db, user=2, attempt="b1", qid="qA",
                       results=results2, questions=qs_a)
        svc = wp.WeakPracticeService(self.db)
        ov1, ov2 = await svc.overview(1), await svc.overview(2)
        self.assertTrue(ov1["eligible"])
        self.assertFalse(ov2["eligible"])
        built1 = await svc.build_practice(1)
        self.assertEqual(built1["state"], "ready")
        built2 = await svc.build_practice(2)
        self.assertNotEqual(built2["state"], "ready")

    async def test_12_never_uses_cross_user_wrong_stats(self):
        # Architectural guarantee: the module never references the global
        # wrong-stats collection or QuestionStatsRepository.
        import inspect
        src = inspect.getsource(wp)
        self.assertNotIn("question_wrong_stats", src)
        self.assertNotIn("QuestionStatsRepository", src)
        # Data proof: populate global wrong stats heavily; overview unchanged.
        await self._polity_history(user=1)
        svc = wp.WeakPracticeService(self.db)
        before = await svc.overview(1)
        self.db.collection("question_wrong_stats").docs.append({
            "qid": "qA", "q_index": 0, "wrong_count": 9999, "all_users": True})
        after = await svc.overview(1)
        self.assertEqual(
            [(b["key"], b["incorrect"]) for b in before["eligible"]],
            [(b["key"], b["incorrect"]) for b in after["eligible"]])

    async def test_23_paid_quiz_never_played_is_not_in_pool(self):
        # Paid quiz the user never touched with the same weak topic.
        paid = polity_quiz("paid1", creator=999, n_wrong_topic=8)
        await seed(self.db, "paid1", paid, creator=999, quiz_type="paid")
        # User history comes from a FREE quiz with limited Judiciary Qs.
        free = polity_quiz("free1", n_wrong_topic=6)
        await seed(self.db, "free1", free)
        await seed_topic_performance(
            self.db, 1, "free1", free, correct_idx=[6],
            wrong_idx=[0, 1, 2, 3, 4, 5], attempt_prefix="f1")
        await seed_topic_performance(
            self.db, 1, "free1", free, correct_idx=[6],
            wrong_idx=[0, 1], attempt_prefix="f2")
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertEqual(built["state"], "ready")
        texts = [q["question"] for q in built["questions"]]
        self.assertTrue(all(t.startswith("Judiciary question") for t in texts))
        self.assertTrue(all(int(t.split()[-1]) < 6 for t in texts))

    async def test_19_content_dedup_across_quizzes(self):
        qs1 = [question("Same question", ["a", "b"], 0,
                        subject="Polity", topic="Judiciary", difficulty="hard")]
        qs2 = [question("Same question", ["a", "b"], 0,
                        subject="Polity", topic="Judiciary", difficulty="hard")]
        await seed(self.db, "qA", qs1)
        await seed(self.db, "qB", qs2)
        for qid, qs, att in (("qA", qs1, "a1"), ("qB", qs2, "b1")):
            res = [qr(0, OUTCOME_INCORRECT, [9])] * 3
            for k in range(3):
                await complete(self.db, user=1, attempt=f"{att}{k}", qid=qid,
                               results=[qr(0, OUTCOME_INCORRECT, [9])],
                               questions=qs)
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertEqual(built["size"], 1)  # identical content -> one item
        # and both origins are folded on answer
        origins = built["questions"][0]["_revision_origins"]
        self.assertEqual({o["qid"] for o in origins}, {"qA", "qB"})

    async def test_20_edited_quiz_uses_snapshot(self):
        qs = await self._polity_history(user=1, n=6)
        # Edit the live question in place.
        edited = copy.deepcopy(qs)
        edited[0]["question"] = "COMPLETELY DIFFERENT NEW WORDING"
        await QuizRepository(self.db).update_field("qA", "questions", edited)
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        q0 = next(q for q in built["questions"] if q["options"] == ["opt0a", "opt0b"])
        self.assertEqual(q0["question"], "Judiciary question 0")  # snapshot text

    async def test_21_deleted_quiz_with_snapshot_still_practicable(self):
        await self._polity_history(user=1, n=6)
        await QuizRepository(self.db).delete("qA")
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertGreaterEqual(built["size"], 1)
        self.assertTrue(
            any("Judiciary" in q["question"] for q in built["questions"]))

    async def test_22_deleted_without_snapshot_excluded(self):
        # Legacy mistake row: no snapshot, origin quiz absent.
        self.db.collection("user_mistakes").docs.append({
            "_id": "ghost", "user_id": 1, "qid": "gone", "q_index": 0,
            "wrong_count": 6, "correct_count": 0, "status": "open",
            "subject": "Polity", "topic": "Judiciary",
            "topic_source": TOPIC_SOURCE_QUESTION,
            "last_wrong_at": iso(1),
        })
        # Plus enough event history to qualify the topic.
        qs = polity_quiz("qA", n_wrong_topic=5)
        await seed(self.db, "qA", qs)
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[5], wrong_idx=[0, 1, 2, 3, 4])
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertEqual(built["state"], "ready")
        self.assertGreaterEqual(built["excluded"], 1)
        self.assertFalse(
            any(q["question"] == "" for q in built["questions"]))

    async def test_24_25_session_size_bounds_real_count(self):
        await self._polity_history(user=1, n=8)
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertLessEqual(built["size"], wp.PRACTICE_SIZE)
        self.assertEqual(built["size"], 8)  # only 8 exist -> real count

    async def test_26_invalid_questions_excluded(self):
        qs = []
        for i in range(7):
            qs.append(question(f"J{i}", ["a", "b"], 0,
                               subject="Polity", topic="Judiciary"))
        broken = {"question": "J broken", "options": [], "correct_option_id": 0,
                  "analytics": {"subject": "Polity", "topic": "Judiciary"}}
        qs.insert(0, broken)
        await seed(self.db, "qA", qs)
        wrong = [i for i, q in enumerate(qs) if q.get("options")]
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[], wrong_idx=wrong)
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertFalse(any(q["question"] == "J broken" for q in built["questions"]))
        self.assertGreaterEqual(built["excluded"], 1)

    async def test_subject_only_fallback_selection(self):
        # Questions carry subject but NO topic (and no sections): they form
        # the labelled 'Polity . untagged' bucket and remain practicable.
        qs = [question(f"U{i}", ["a", "b"], 0, subject="Polity")
              for i in range(6)]
        await seed(self.db, "qU", qs)
        await seed_topic_performance(
            self.db, 1, "qU", qs, correct_idx=[], wrong_idx=list(range(6)))
        svc = wp.WeakPracticeService(self.db)
        ov = await svc.overview(1)
        self.assertEqual(len(ov["eligible"]), 1)
        self.assertTrue(ov["eligible"][0]["subject_only"])
        built = await svc.build_practice(1)
        self.assertEqual(built["state"], "ready")
        self.assertTrue(all(q["question"].startswith("U")
                            for q in built["questions"]))

    async def test_other_topics_in_played_quiz_never_leak(self):
        qs = polity_quiz("qA", n_wrong_topic=6)  # + Parliament + untagged
        await seed(self.db, "qA", qs)
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[6], wrong_idx=list(range(6)))
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        texts = [q["question"] for q in built["questions"]]
        self.assertTrue(all("Judiciary" in t for t in texts))
        self.assertFalse(any("Parliament" in t or "No meta" in t for t in texts))

    async def test_t1_repeated_cap_prevents_domination(self):
        # 6 repeated (T1) questions + 4 fresh topic questions; session must
        # contain at most 4 repeated and must fill with others.
        qs = []
        for i in range(10):
            qs.append(question(f"J{i}", ["a", "b"], 0,
                               subject="Polity", topic="Judiciary"))
        await seed(self.db, "qA", qs)
        # First 6 missed twice (repeated), next 4 missed once each.
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[], wrong_idx=list(range(10)),
            attempt_prefix="a1")
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[], wrong_idx=list(range(6)),
            attempt_prefix="a2")
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertEqual(built["size"], 10)
        # Questions 6..9 (open single misses, T2) must be present because T1
        # is capped at 4.
        texts = {q["question"] for q in built["questions"]}
        self.assertTrue(any(f"J{i}" in texts for i in range(6, 10)))

    async def test_mastered_questions_deprioritised(self):
        qs = []
        for i in range(10):
            qs.append(question(f"J{i}", ["a", "b"], 0,
                               subject="Polity", topic="Judiciary"))
        await seed(self.db, "qA", qs)
        # 0..4 mastered (wrong then 2 correct each), 5..9 still open.
        for i in range(5):
            await complete(self.db, user=1, attempt=f"w{i}", qid="qA",
                           results=[qr(i, OUTCOME_INCORRECT, [9])], questions=qs)
        for i in range(5):
            await complete(self.db, user=1, attempt=f"c{i}a", qid="qA",
                           results=[qr(i, OUTCOME_CORRECT, [0])], questions=qs)
            await complete(self.db, user=1, attempt=f"c{i}b", qid="qA",
                           results=[qr(i, OUTCOME_CORRECT, [0])], questions=qs)
        for i in range(5, 10):
            await complete(self.db, user=1, attempt=f"o{i}", qid="qA",
                           results=[qr(i, OUTCOME_INCORRECT, [9])], questions=qs)
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        ordered = [q["question"] for q in built["questions"]]
        # The five still-open mistakes must all lead (order within a tie is
        # a deterministic content-key sort, so compare as a set).
        lead = {int(n[1:]) for n in ordered[:5] if n.startswith("J")}
        tail = {int(n[1:]) for n in ordered[5:] if n.startswith("J")}
        self.assertEqual(lead, {5, 6, 7, 8, 9})
        self.assertEqual(tail, {0, 1, 2, 3, 4})  # mastered only fill the tail

    async def test_16_17_18_dynamic_reselection_no_fixed_pages(self):
        qs = []
        for i in range(50):
            qs.append(question(f"J{i:02d}", ["a", "b"], 0,
                               subject="Polity", topic="Judiciary",
                               difficulty="hard"))
        await seed(self.db, "qBig", qs, creator=111)
        # All 50 wrong once in one big attempt.
        await complete(
            self.db, user=1, attempt="big1", qid="qBig",
            results=[qr(i, OUTCOME_INCORRECT, [9]) for i in range(50)],
            questions=qs)
        svc = wp.WeakPracticeService(self.db)
        s1 = await svc.build_practice(1)
        self.assertEqual(s1["size"], 10)
        # A fresh invocation with no new answers returns the SAME highest-
        # priority set -- there is no hidden page cursor.
        s1b = await svc.build_practice(1)
        self.assertEqual([q["question"] for q in s1["questions"]],
                         [q["question"] for q in s1b["questions"]])
        # Simulate answering all of session 1 correctly (dm completion with
        # provenance -> mistakes resolve), then repeat until all 50 seen.
        seen = set()
        for session in range(6):
            built = await svc.build_practice(1)
            if not built["size"]:
                break
            results = []
            for idx, q in enumerate(built["questions"]):
                seen.add(q["question"])
                cid = q["correct_option_id"]
                results.append(qr(idx, OUTCOME_CORRECT,
                                  cid if isinstance(cid, list) else [cid]))
            await complete(
                self.db, user=1, attempt=f"WK{session}", qid=f"WKx{session}",
                results=results, questions=built["questions"],
                source="dm", persisted=False)
        self.assertEqual(len(seen), 50)  # all 50 reachable across sessions

    async def test_manual_topic_index_and_multi_topic_combine(self):
        # Two weak subjects/topics; top topic has only 6 playable so auto
        # combines with the runner-up (8) to fill toward 10.
        qs1 = [question(f"J{i}", ["a", "b"], 0,
                        subject="Polity", topic="Judiciary") for i in range(6)]
        qs2 = [question(f"L{i}", ["a", "b"], 0,
                        subject="Polity", topic="Local Gov") for i in range(8)]
        await seed(self.db, "q1", qs1)
        await seed(self.db, "q2", qs2)
        await seed_topic_performance(
            self.db, 1, "q1", qs1, correct_idx=[], wrong_idx=list(range(6)))
        await seed_topic_performance(
            self.db, 1, "q2", qs2, correct_idx=[], wrong_idx=list(range(8)))
        svc = wp.WeakPracticeService(self.db)
        ov = await svc.overview(1)
        self.assertGreaterEqual(len(ov["eligible"]), 2)
        auto = await svc.build_practice(1)
        labels = {b["label"] for b in auto["buckets"]}
        self.assertEqual(len(labels), 2)
        self.assertEqual(auto["size"], 10)  # 6+8 both available -> cap 10
        # Manual index 1 targets exactly that topic.
        manual = await svc.build_practice(1, topic_index=1)
        self.assertEqual(len({b["label"] for b in manual["buckets"]}), 1)
        # Stale index rejected safely.
        stale = await svc.build_practice(1, topic_index=99)
        self.assertEqual(stale["state"], "stale_topic")

    async def test_auto_does_not_combine_when_top_topic_fills(self):
        qs1 = [question(f"J{i}", ["a", "b"], 0,
                        subject="Polity", topic="Judiciary") for i in range(10)]
        qs2 = [question(f"L{i}", ["a", "b"], 0,
                        subject="Polity", topic="Local Gov") for i in range(10)]
        await seed(self.db, "q1", qs1)
        await seed(self.db, "q2", qs2)
        for qid, qs in (("q1", qs1), ("q2", qs2)):
            await complete(self.db, user=1, attempt=f"all-{qid}", qid=qid,
                           results=[qr(i, OUTCOME_INCORRECT, [9]) for i in range(10)],
                           questions=qs)
        auto = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertEqual(len(auto["buckets"]), 1)

    async def test_adhoc_seen_questions_recovered_from_snapshots(self):
        # An AI quiz (no stored doc) with explicit topic, answered poorly.
        qs = [question(f"AI J{i}", ["a", "b"], 0,
                       subject="Polity", topic="Judiciary") for i in range(6)]
        # One stored weak question so the topic qualifies.
        stored = [question("Stored J", ["a", "b"], 0,
                           subject="Polity", topic="Judiciary")]
        await seed(self.db, "q1", stored)
        await seed_topic_performance(
            self.db, 1, "q1", stored, correct_idx=[], wrong_idx=[0])
        # 5 AI wrong + the stored one seen across attempts -> 6 answered, 6 wrong
        await complete(self.db, user=1, attempt="ai1", qid="AIabc",
                       results=[qr(i, OUTCOME_INCORRECT, [9]) for i in range(5)],
                       questions=qs, source="aiquiz", persisted=False)
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertGreaterEqual(built["size"], 5)
        # Ad-hoc question has NO fold provenance (no stored mistake origin).
        ai_q = next(q for q in built["questions"] if q["question"].startswith("AI"))
        self.assertNotIn("_revision_origins", ai_q)

        # End-to-end at the canonical boundary: answer the practice (an
        # ad-hoc question WRONG). Events/XP flow; NO mistake row may open
        # for the synthetic ad-hoc qid, and nothing is persisted as stored.
        results = []
        for i, q in enumerate(built["questions"]):
            if q["question"].startswith("AI"):
                results.append(qr(i, OUTCOME_INCORRECT, [9]))
            else:
                cid = q["correct_option_id"]
                sel = cid if isinstance(cid, list) else [cid]
                results.append(qr(i, OUTCOME_CORRECT, sel))
        out = await complete(self.db, user=1, attempt="WKadhoc", qid="WKadhoc",
                             results=results, questions=built["questions"],
                             source="dm", persisted=False)
        self.assertIsNotNone(out["gamification"])
        mistake_docs = self.db.collection("user_mistakes").docs
        self.assertFalse(any(r["qid"] in ("AIabc", "WKadhoc")
                             for r in mistake_docs))
        new_events = [e for e in self.db.collection("question_events").docs
                      if e["attempt_id"] == "WKadhoc"]
        self.assertEqual(len(new_events), built["size"])
        self.assertTrue(all(e["quiz_persisted"] is False for e in new_events))

    async def test_27_bounded_db_operations(self):
        qs = await self._polity_history(user=1, n=8)
        # 60+ played quizzes beyond the cap to prove bounded reads.
        for n in range(60):
            extra = [question(f"X{n}", ["a", "b"], 0,
                              subject="Other", topic=f"T{n % 40}")]
            await seed(self.db, f"old{n}", extra)
            await complete(self.db, user=1, attempt=f"old{n}a", qid=f"old{n}",
                           results=[qr(0, OUTCOME_CORRECT, [0])], questions=extra)
        ev = self.db.collection("question_events")
        qz = self.db.collection("quizzes")
        sn = self.db.collection("question_snapshots")
        mk = self.db.collection("user_mistakes")
        e0, q0, s0, m0 = ev.queries, qz.queries, sn.queries, mk.queries
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        de, dq, ds, dm = ev.queries-e0, qz.queries-q0, sn.queries-s0, mk.queries-m0
        self.assertEqual(built["size"], 8)
        # rollup + distinct qids + per-question perf + ad-hoc = <=4 event reads
        self.assertLessEqual(de, 4)
        # one bank aggregation + one bounded owned-qid list read + one
        # Phase D origin-quiz fetch (cached; at most PRACTICE_SIZE in total)
        self.assertLessEqual(dq, 2 + wp.PRACTICE_SIZE)
        # one snapshot $in
        self.assertLessEqual(ds, 1)
        # one bounded mistake read
        self.assertLessEqual(dm, 1)

    async def test_33_34_35_completion_feeds_analytics_fold_xp_once(self):
        await self._polity_history(user=1, n=8)
        svc = wp.WeakPracticeService(self.db)
        built = await svc.build_practice(1)
        results = []
        for idx, q in enumerate(built["questions"]):
            cid = q["correct_option_id"]
            results.append(qr(idx, OUTCOME_CORRECT,
                              cid if isinstance(cid, list) else [cid]))
        out = await complete(self.db, user=1, attempt="WK1", qid="WKx",
                             results=results, questions=built["questions"],
                             source="dm", persisted=False)
        self.assertGreaterEqual(out["revision_folded"], 1)
        self.assertIsNotNone(out["gamification"])
        xp_after = out["gamification"]["total_xp"]
        # Topic performance now shows new correct events.
        roll = await AnalyticsService(self.db).get_topic_performance(1)
        judiciary = next(r for r in roll
                         if r.get("topic") == "Judiciary"
                         and r.get("subject") == "Polity")
        self.assertGreaterEqual(judiciary["correct"], 8)
        # Replay -> no duplicate XP/fold.
        out2 = await complete(self.db, user=1, attempt="WK1", qid="WKx",
                              results=results, questions=built["questions"],
                              source="dm", persisted=False)
        self.assertTrue(out2["gamification"].get("duplicate"))
        self.assertEqual(
            self.db.collection("user_xp").docs[0]["total_xp"], xp_after)

    async def test_fresh_stored_question_feedback_and_adhoc_no_origin(self):
        # Fresh bank questions in a weak topic (never missed before) carry
        # the stored-quiz origin: a CORRECT answer must create no mistake
        # row, while events/XP flow; a WRONG answer later opens the origin.
        qs = []
        for i in range(6):
            qs.append(question(f"J{i}", ["a", "b"], 0,
                               subject="Polity", topic="Judiciary"))
        await seed(self.db, "q1", qs)
        await seed_topic_performance(
            self.db, 1, "q1", qs, correct_idx=[], wrong_idx=list(range(6)))
        # 4 unseen same-topic questions in a quiz the user OWNS.
        owned = [question(f"Fresh{i}", ["a", "b"], 0,
                          subject="Polity", topic="Judiciary") for i in range(4)]
        await seed(self.db, "own1", owned, creator=1)
        svc = wp.WeakPracticeService(self.db)
        built = await svc.build_practice(1)
        fresh = [q for q in built["questions"]
                 if q["question"].startswith("Fresh")]
        self.assertTrue(fresh)
        # Stored-bank provenance is present (single origin each).
        for q in fresh:
            self.assertEqual(len(q["_revision_origins"]), 1)
            self.assertEqual(q["_revision_origins"][0]["qid"], "own1")
        # Answer everything correctly: fresh origins must NOT create rows.
        results = [qr(i, OUTCOME_CORRECT, [0])
                   for i in range(len(built["questions"]))]
        out = await complete(self.db, user=1, attempt="WKf", qid="WKx",
                             results=results, questions=built["questions"],
                             source="dm", persisted=False)
        self.assertIsNotNone(out["gamification"])
        own_rows = [r for r in self.db.collection("user_mistakes").docs
                    if r["qid"] == "own1"]
        self.assertEqual(own_rows, [])

        # A later session: answer a FRESH question WRONG -> origin opens.
        built2 = await svc.build_practice(1)
        q0 = next(q for q in built2["questions"]
                  if q["question"].startswith("Fresh"))
        idx = built2["questions"].index(q0)
        await complete(self.db, user=1, attempt="WKw", qid="WKx",
                       results=[qr(idx, OUTCOME_INCORRECT, [9])],
                       questions=built2["questions"],
                       source="dm", persisted=False)
        rows = [r for r in self.db.collection("user_mistakes").docs
                if r["qid"] == "own1"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "open")


    async def test_shuffle_safe_fold_with_multi_correct(self):
        qs = []
        for i in range(6):
            qs.append(question(f"Multi J{i}", ["a", "b", "c"], [0, 2],
                               subject="Polity", topic="Judiciary",
                               difficulty="extreme"))
        await seed(self.db, "q1", qs)
        await seed_topic_performance(
            self.db, 1, "q1", qs, correct_idx=[], wrong_idx=list(range(6)))
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        # Simulate the DM engine permuting options of the first question:
        # canonical correct ids [0,2] display as positions [1,2].
        q0 = built["questions"][0]
        q0["options"] = ["b", "a", "c"]
        q0["correct_option_id"] = [1, 2]
        results = [qr(0, OUTCOME_CORRECT, [1, 2])]
        out = await complete(self.db, user=1, attempt="WKsh", qid="WKx",
                             results=results, questions=[q0],
                             source="dm", persisted=False)
        self.assertGreaterEqual(out["revision_folded"], 1)
        # The folded origin row resolves despite display permutation.
        rows = self.db.collection("user_mistakes").docs
        origin = next(r for r in rows if r["snapshot_id"]
                      == q0["_revision_origins"][0]["snapshot_id"])
        self.assertEqual(origin["status"], "resolved")

    async def test_concurrent_duplicate_completion_once(self):
        await self._polity_history(user=1, n=6)
        svc = wp.WeakPracticeService(self.db)
        built = await svc.build_practice(1)
        results = []
        for idx, q in enumerate(built["questions"]):
            cid = q["correct_option_id"]
            results.append(qr(idx, OUTCOME_INCORRECT, [9]))

        async def one():
            return await AnalyticsService(self.db).record_completion(
                user_id=1, attempt_id="WKRACE", qid="WKx", quiz_name="w",
                question_results=results, source="dm",
                quiz_persisted=False, questions=built["questions"],
                sections=[])
        await asyncio.gather(one(), one())
        rows = self.db.collection("user_mistakes").docs
        race = [r for r in rows if "WKRACE" in r.get("wrong_attempt_ids", [])]
        # The racing duplicate attempt is recorded at most once per row.
        self.assertTrue(all(
            r["wrong_attempt_ids"].count("WKRACE") <= 1 for r in rows))
        # Two history attempts plus exactly one of the two racing sessions.
        self.assertEqual(
            self.db.collection("user_xp").docs[0]["total_completions"], 3)

    async def test_explanation_preserved_from_live_bank(self):
        qs = polity_quiz("qA", n_wrong_topic=6, with_expl=True)
        await seed(self.db, "qA", qs)
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[6], wrong_idx=list(range(6)))
        built = await wp.WeakPracticeService(self.db).build_practice(1)
        self.assertTrue(all(q.get("explanation") for q in built["questions"]
                            if q["question"].startswith("Judiciary")))


# ===========================================================================
# HANDLER
# ===========================================================================

def _install_handler():
    captured = {}
    for name in ["admin", "ai_quiz", "mix", "mistakes", "pdf_quiz",
                 "poll_quiz", "podcast", "quiz_play", "reports",
                 "scheduling", "setup_wizard", "translation"]:
        m = types.ModuleType(f"quizbot.runner_bot.handlers.{name}")
        m.register = lambda app: None

        async def _spq(*a, **k):
            captured["args"] = a
            captured["kwargs"] = k
        if name == "quiz_play":
            m.start_private_quiz = _spq
        sys.modules[f"quizbot.runner_bot.handlers.{name}"] = m
    sys.modules.pop("quizbot.runner_bot.handlers", None)
    sys.modules.pop("quizbot.runner_bot.handlers.weakquiz", None)
    pkg = importlib.import_module("quizbot.runner_bot.handlers")
    mod = importlib.import_module("quizbot.runner_bot.handlers.weakquiz")
    return mod, pkg, captured


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, ctype="private"):
        self.type = ctype


class FakeMessage:
    def __init__(self, chat=None):
        self._chat = chat or FakeChat()
        self.replies = []
        self.edited = []

    @property
    def chat(self):
        return self._chat

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def reply_html(self, text, **kw):
        self.replies.append(text)

    async def edit_text(self, text, **kw):
        if self.edited and self.edited[-1] == text:
            raise RuntimeError("not modified")
        self.edited.append(text)


class FakeQuery:
    def __init__(self, data, uid, message):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = message
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
        cls.mod, cls.pkg, cls.captured = _install_handler()

    async def asyncSetUp(self):
        self.db = new_db()
        await ensure_gamification_indexes(self.db)
        self.captured.clear()
        self._orig = self.mod.get_db
        self.mod.get_db = lambda: self.db

    def tearDown(self):
        self.mod.get_db = self._orig

    async def _weak(self, user=1, n=8, qid="qA"):
        qs = polity_quiz(qid, n_wrong_topic=n)
        await seed(self.db, qid, qs)
        await seed_topic_performance(
            self.db, user, qid, qs, correct_idx=[n], wrong_idx=list(range(n)),
            attempt_prefix="a1")
        await seed_topic_performance(
            self.db, user, qid, qs, correct_idx=[n], wrong_idx=[0, 1],
            attempt_prefix="a2")
        return qs

    async def test_01_no_history_message(self):
        ctx = FakeCtx()
        await self.mod.weakquiz_command(FakeUpdate(1), ctx)
        self.assertIn("Play a few quizzes first", ctx.bot.sent[0][1])

    async def test_31_group_redirect(self):
        upd = FakeUpdate(1, chat_type="group")
        await self.mod.weakquiz_command(upd, FakeCtx())
        self.assertIn("private chat", upd.message.replies[0])

    async def test_02_insufficient_message(self):
        qs = polity_quiz("qA")
        await seed(self.db, "qA", qs)
        await seed_topic_performance(
            self.db, 1, "qA", qs, correct_idx=[8], wrong_idx=[0])
        ctx = FakeCtx()
        await self.mod.weakquiz_command(FakeUpdate(1), ctx)
        self.assertIn("at least 5 answered and 2 incorrect", ctx.bot.sent[0][1])

    async def test_ready_menu_lists_topics_with_real_values(self):
        await self._weak()
        ctx = FakeCtx()
        await self.mod.weakquiz_command(FakeUpdate(1), ctx)
        text, kw = ctx.bot.sent[0][1], ctx.bot.sent[0][2]
        self.assertIn("Judiciary", text)
        labels = [b.text for row in kw["reply_markup"].inline_keyboard
                  for b in row]
        self.assertTrue(any("Start Weak Quiz" in l for l in labels))
        self.assertTrue(any("Choose Weak Topic" in l for l in labels))

    async def test_28_owner_mismatch_rejected(self):
        await self._weak()
        upd = FakeUpdate(2, data=self.mod._cb("auto", 1))
        await self.mod.weakquiz_callback(upd, FakeCtx())
        self.assertIn("Not your", upd.callback_query.answers[0][0])
        self.assertNotIn("args", self.captured)

    async def test_29_malformed_callbacks(self):
        for bad in [None, "", "wk:", "wk:auto", "wk:auto:x", "other:auto:1",
                    "wk:topic:1", "wk:bogus:1"]:
            upd = FakeUpdate(1, data=bad)
            await self.mod.weakquiz_callback(upd, FakeCtx())
            self.assertIn("Invalid", upd.callback_query.answers[0][0], bad)
            self.assertNotIn("args", self.captured)

    async def test_30_stale_topic_index(self):
        await self._weak()
        upd = FakeUpdate(1, data=self.mod._cb("topic", 1, "99"))
        await self.mod.weakquiz_callback(upd, FakeCtx())
        self.assertIn("no longer available", upd.callback_query.answers[-1][0])

    async def test_32_auto_launches_dm_engine(self):
        await self._weak()
        upd = FakeUpdate(1, data=self.mod._cb("auto", 1))
        await self.mod.weakquiz_callback(upd, FakeCtx())
        self.assertIn("args", self.captured)
        chat_id, _ctx, questions, quiz, qid = self.captured["args"]
        self.assertEqual(chat_id, 1)
        self.assertTrue(qid.startswith("WK"))
        self.assertEqual(quiz["analytics_source"], "dm")
        self.assertTrue(quiz["_weak_session"])
        self.assertLessEqual(len(questions), 10)
        # Every question playable (options + valid answer).
        for q in questions:
            self.assertTrue(q["options"])
            cid = q["correct_option_id"]
            self.assertIsInstance(cid, int) if not isinstance(cid, list) \
                else self.assertTrue(all(isinstance(c, int) for c in cid))

    async def test_manual_topic_launch(self):
        # Two eligible topics; pick index 1.
        qs1 = [question(f"J{i}", ["a", "b"], 0,
                        subject="Polity", topic="Judiciary") for i in range(6)]
        qs2 = [question(f"L{i}", ["a", "b"], 0,
                        subject="Polity", topic="Local Gov") for i in range(6)]
        await seed(self.db, "q1", qs1)
        await seed(self.db, "q2", qs2)
        await seed_topic_performance(self.db, 1, "q1", qs1, correct_idx=[],
                                     wrong_idx=list(range(6)), attempt_prefix="a")
        await seed_topic_performance(self.db, 1, "q2", qs2, correct_idx=[],
                                     wrong_idx=list(range(6)), attempt_prefix="b")
        upd = FakeUpdate(1, data=self.mod._cb("topic", 1, "1"))
        ctx = FakeCtx()
        await self.mod.weakquiz_callback(upd, ctx)
        _cid, _c, questions, _q, _id = self.captured["args"]
        topics = {q.get("analytics", {}).get("topic") for q in questions}
        self.assertEqual(topics, {"Local Gov"})

    async def test_limited_pool_honest_wording(self):
        # 2 playable weak-topic questions, repeated across 3 attempts so the
        # topic is eligible (>=5 answered, >=2 wrong) but content is scarce.
        qs = polity_quiz("qL", n_wrong_topic=2)
        await seed(self.db, "qL", qs)
        for k, pref in enumerate(("l1", "l2", "l3")):
            await seed_topic_performance(
                self.db, 1, "qL", qs, correct_idx=[], wrong_idx=[0, 1],
                attempt_prefix=pref, at_days_ago=k + 1)
        upd = FakeUpdate(1, data=self.mod._cb("auto", 1))
        ctx = FakeCtx()
        await self.mod.weakquiz_callback(upd, ctx)
        _cid, _c, questions, _q, _id = self.captured["args"]
        self.assertEqual(len(questions), 2)
        text = ctx.bot.sent[-1][1]
        self.assertIn("could only find", text)
        self.assertIn("2", text)
        self.assertIn("playable", text)

    async def test_topics_view_then_back(self):
        await self._weak()
        upd = FakeUpdate(1, data=self.mod._cb("topics", 1))
        await self.mod.weakquiz_callback(upd, FakeCtx())
        self.assertTrue(any("Choose a weak topic" in e for e in upd.message.edited))
        upd2 = FakeUpdate(1, data=self.mod._cb("menu", 1))
        upd2.callback_query.message = upd.message
        await self.mod.weakquiz_callback(upd2, FakeCtx())
        self.assertTrue(any("Weak topic practice" in e for e in upd.message.edited))

    async def test_no_questions_honest_message(self):
        # Weak topic via mistakes/events but zero playable content: mistakes
        # for a deleted legacy quiz AND events only from that deleted quiz.
        for i in range(6):
            self.db.collection("question_events").docs.append({
                "user_id": 1, "attempt_id": "gone1", "question_index": i,
                "qid": "gone", "quiz_persisted": True,
                "outcome": OUTCOME_INCORRECT,
                "subject": "Polity", "topic": "Judiciary",
                "topic_source": TOPIC_SOURCE_QUESTION,
                "answered_at": iso(1), "created_at": iso(1),
            })
            self.db.collection("user_mistakes").docs.append({
                "user_id": 1, "qid": "gone", "q_index": i,
                "wrong_count": 3, "correct_count": 0, "status": "open",
                "subject": "Polity", "topic": "Judiciary",
                "topic_source": TOPIC_SOURCE_QUESTION,
                "last_wrong_at": iso(1),
            })
        upd = FakeUpdate(1, data=self.mod._cb("auto", 1))
        ctx = FakeCtx()
        await self.mod.weakquiz_callback(upd, ctx)
        text = ctx.bot.sent[-1][1]
        self.assertIn("no usable questions", text)
        self.assertNotIn("args", self.captured)

    async def test_db_failure_failsoft(self):
        def boom():
            raise RuntimeError("db down")
        self.mod.get_db = boom
        ctx = FakeCtx()
        await self.mod.weakquiz_command(FakeUpdate(1), ctx)
        self.assertIn("Something went wrong", ctx.bot.sent[0][1])

    async def test_active_session_guard(self):
        await self._weak()
        from quizbot.runner_bot.state import session_mgr
        await session_mgr.create(1, {"dummy": True})
        try:
            upd = FakeUpdate(1, data=self.mod._cb("auto", 1))
            ctx = FakeCtx()
            await self.mod.weakquiz_callback(upd, ctx)
            self.assertIn("already active", ctx.bot.sent[-1][1])
            self.assertNotIn("args", self.captured)
        finally:
            await session_mgr.delete(1)

    async def test_36_registration_once_and_namespace(self):
        app = types.SimpleNamespace(added=[])
        app.add_handler = lambda h: app.added.append(h)
        self.mod.register(app)
        self.assertEqual(len(app.added), 2)
        # wk: namespace distinct from Phase D mst:
        from telegram.ext import CommandHandler
        cmd = next(h for h in app.added if isinstance(h, CommandHandler))
        self.assertIn("weakquiz", list(cmd.commands))
        # mistakes handler module still registered in the package tuple
        from quizbot.runner_bot.handlers import mistakes
        self.assertIn(mistakes, self.pkg._MODULES)
        self.assertIn(self.mod, self.pkg._MODULES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
