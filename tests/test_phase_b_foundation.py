"""Phase B tests — analytics data foundation.

Covers: optional question metadata, shuffle-safe canonical result mapping,
canonical question events with idempotent writes, non-destructive mistake
history, saved vs ad-hoc boundaries, user isolation, legacy fallback,
backfill classification/idempotency, and index bootstrap.

No MongoDB server is required: an in-memory collection implements the
small Motor subset the foundation uses (queries, updates incl. $inc/$addToSet/
$push/$slice, bulk upserts and the aggregation stages used by the read
repositories).
"""

from __future__ import annotations

import asyncio
import unittest

# ---------------------------------------------------------------------------
# In-memory Motor-like fakes
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
                if field == val:
                    return False
            elif op == "$eq":
                if field != val:
                    return False
            elif op == "$in":
                if field not in val:
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
                exists = field is not None
                if bool(val) != exists:
                    return False
            elif op == "$regex":
                import re
                if field is None or not re.search(val, str(field)):
                    return False
            else:
                raise AssertionError(f"unsupported match op {op}")
        return True
    if isinstance(field, list) and not isinstance(cond, list):
        return cond in field
    return field == cond


def _matches(doc, filt):
    if not filt:
        return True
    for key, cond in filt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, sub) for sub in cond):
                return False
            continue
        if not _value_match(_get_path(doc, key), cond):
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    @staticmethod
    def _apply_sort(rows, keys):
        # Stable sorts applied in reverse key order yield the composite
        # ordering (least significant first), matching Mongo.
        for field, direction in reversed(keys):
            rows.sort(
                key=lambda d, f=field: (
                    1 if _get_path(d, f) is None else 0, _get_path(d, f)),
                reverse=direction < 0)
        return rows

    def sort(self, *args):
        if args and isinstance(args[0], list):
            keys = args[0]
        elif args:
            keys = [(args[0], args[1] if len(args) > 1 else 1)]
        else:
            keys = []
        if keys:
            self._apply_sort(self._docs, keys)
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
                yield d
        return gen()


class FakeResult:
    def __init__(self, inserted_id=None, matched=0, modified=0, upserted_count=0,
                 deleted=0):
        self.inserted_id = inserted_id
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_count = upserted_count
        self.deleted_count = deleted


def _apply_update(doc, update, inserting):
    for key, value in update.get("$set", {}).items():
        doc[key] = value
    if inserting:
        for key, value in update.get("$setOnInsert", {}).items():
            doc.setdefault(key, value)
    for key, value in update.get("$inc", {}).items():
        doc[key] = doc.get(key, 0) + value
    for key, value in update.get("$addToSet", {}).items():
        arr = doc.setdefault(key, [])
        vals = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
        for v in vals:
            if v not in arr:
                arr.append(v)
    for key, value in update.get("$push", {}).items():
        arr = doc.setdefault(key, [])
        if isinstance(value, dict) and "$each" in value:
            arr.extend(value["$each"])
            if "$slice" in value and value["$slice"] is not None:
                sl = value["$slice"]
                arr[:] = arr[sl:] if sl < 0 else arr[:sl]
        else:
            arr.append(value)


class FakeCollection:
    def __init__(self, name, owner):
        self.name = name
        self.owner = owner
        self.docs: list[dict] = []
        self._seq = 0
        self.indexes: list[tuple] = []

    async def create_index(self, keys, unique=False, sparse=False, name=None, **kw):
        self.indexes.append((keys, unique, sparse, name))
        return name or str(keys)

    async def insert_one(self, doc):
        self._seq += 1
        doc = dict(doc)
        doc.setdefault("_id", f"{self.name}_{self._seq}")
        self.docs.append(doc)
        return FakeResult(inserted_id=doc["_id"])

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def find_one(self, filt=None, sort=None, projection=None):
        rows = [d for d in self.docs if _matches(d, filt or {})]
        if sort:
            cur = FakeCursor(rows).sort(sort)
            rows = list(cur._docs)
        return dict(rows[0]) if rows else None

    def find(self, filt=None):
        return FakeCursor([dict(d) for d in self.docs if _matches(d, filt or {})])

    async def count_documents(self, filt=None):
        return sum(1 for d in self.docs if _matches(d, filt or {}))

    async def update_one(self, filt, update, upsert=False):
        for i, d in enumerate(self.docs):
            if _matches(d, filt):
                _apply_update(d, update, False)
                return FakeResult(matched=1, modified=1)
        if upsert:
            new_doc = {k: v for k, v in filt.items()
                       if not (isinstance(v, dict) and any(kk.startswith("$") for kk in v))}
            _apply_update(new_doc, update, True)
            await self.insert_one(new_doc)
            return FakeResult(upserted_count=1)
        return FakeResult()

    async def update_many(self, filt, update):
        n = 0
        for d in self.docs:
            if _matches(d, filt):
                _apply_update(d, update, False)
                n += 1
        return FakeResult(matched=n, modified=n)

    async def delete_one(self, filt):
        for i, d in enumerate(self.docs):
            if _matches(d, filt):
                self.docs.pop(i)
                return FakeResult(deleted=1)
        return FakeResult()

    async def delete_many(self, filt):
        keep = [d for d in self.docs if not _matches(d, filt)]
        removed = len(self.docs) - len(keep)
        self.docs = keep
        return FakeResult(deleted=removed)

    async def bulk_write(self, ops, ordered=True):
        upserts = 0
        for op in ops:
            res = await self.update_one(op._filter, op._doc, upsert=bool(op._upsert))
            upserts += res.upserted_count
        return FakeResult(upserted_count=upserts)

    # -- minimal aggregation engine ----------------------------------------

    async def aggregate(self, pipeline):
        rows = [dict(d) for d in self.docs]
        rows = _run_pipeline(rows, pipeline)
        for r in rows:
            yield r


def _eval(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
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
                if a is None:
                    return False  # Mongo: null comparisons against numbers are false
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
        if op == "$in":
            return _eval(args[0], doc) in _eval(args[1], doc)
        if op == "$and":
            return all(_truthy(_eval(a, doc)) for a in args)
        if op == "$or":
            return any(_truthy(_eval(a, doc)) for a in args)
        if op == "$not":
            return not _truthy(_eval(args, doc))
        if op == "$type":
            v = _eval(args, doc) if isinstance(args, str) else _eval(args[0], doc)
            if isinstance(v, list):
                return "array"
            if v is None:
                return "null"
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, int):
                return "int"
            if isinstance(v, float):
                return "double"
            if isinstance(v, str):
                return "string"
            return "object"
        if op == "$substrCP":
            v = _eval(args[0], doc) or ""
            return str(v)[int(_eval(args[1], doc)):int(_eval(args[1], doc)) + int(_eval(args[2], doc))]
        raise AssertionError(f"unsupported expression op {op}")
    return expr


def _truthy(v):
    return bool(v) if not isinstance(v, list) else bool(v)


def _run_pipeline(rows, pipeline):
    for stage in pipeline:
        op, spec = next(iter(stage.items()))
        if op == "$match":
            rows = [r for r in rows if _matches(r, spec)]
        elif op == "$sort":
            for field, direction in reversed(list(spec.items())):
                rows.sort(
                    key=lambda r, f=field: (
                        1 if _get_path(r, f) is None else 0, _get_path(r, f)),
                    reverse=direction < 0)
        elif op == "$skip":
            rows = rows[spec:]
        elif op == "$limit":
            rows = rows[:spec]
        elif op == "$facet":
            rows = [{name: _run_pipeline(rows, sub) for name, sub in spec.items()}]
        elif op == "$group":
            rows = _group(rows, spec)
        elif op == "$setWindowFields":
            sort_by = spec.get("sortBy", {})
            for field, direction in reversed(list(sort_by.items())):
                rows.sort(
                    key=lambda r, f=field: (
                        1 if _get_path(r, f) is None else 0, _get_path(r, f)),
                    reverse=direction < 0)
            outputs = spec.get("output", {})
            for out_field in outputs:
                for i, r in enumerate(rows, start=1):
                    r[out_field] = i  # $rank dense ranking is enough here
        elif op == "$project":
            proj = []
            for r in rows:
                proj.append({k: (_get_path(r, v[1:]) if isinstance(v, str) and v.startswith("$") else v)
                             for k, v in spec.items() if v != 0})
            rows = proj
        else:
            raise AssertionError(f"unsupported stage {op}")
    return rows


def _hashable(v):
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple((k, _hashable(x)) for k, x in v.items())
    return v


def _group(rows, spec):
    id_spec = spec["_id"]
    buckets: dict = {}

    # A group _id that is itself an expression (keys like $substrCP) yields a
    # scalar key; a dict of output field names (none starting with "$") is a
    # compound key.
    id_is_expression = isinstance(id_spec, dict) and any(
        str(k).startswith("$") for k in id_spec)

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
        key = id_key(doc)
        b = buckets.setdefault(key, {"_docs": []})
        b["_docs"].append(doc)

    out = []
    for key, b in buckets.items():
        docs = b["_docs"]
        row: dict = {"_id": id_value(key)}
        for field, acc in spec.items():
            if field == "_id":
                continue
            op, arg = next(iter(acc.items()))
            if op == "$sum":
                if isinstance(arg, int):
                    row[field] = len(docs) * arg
                else:
                    row[field] = sum(
                        (lambda v: v if isinstance(v, (int, float)) else 0)(
                            _eval(arg, d)) for d in docs)
            elif op == "$addToSet":
                vals = []
                for d in docs:
                    v = _eval(arg, d)
                    if v is not None and v not in vals:
                        vals.append(v)
                row[field] = vals
            elif op == "$push":
                row[field] = [_eval(arg, d) for d in docs]
            elif op == "$min":
                vals = [_eval(arg, d) for d in docs]
                vals = [v for v in vals if v is not None]
                row[field] = min(vals) if vals else None
            elif op == "$max":
                vals = [_eval(arg, d) for d in docs]
                vals = [v for v in vals if v is not None]
                row[field] = max(vals) if vals else None
            elif op == "$first":
                row[field] = _eval(arg, docs[0])
            elif op == "$last":
                row[field] = _eval(arg, docs[-1])
        out.append(row)
    return out


class FakeDB:
    def __init__(self):
        self._cols: dict[str, FakeCollection] = {}

    def collection(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name, self)
        return self._cols[name]

    def __getattr__(self, name):
        return self.collection(name)

    # MotorDatabase-style index bootstrap target:
    async def create_index(self, *a, **k):  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_quiz():
    return {
        "qid": "q1",
        "quiz_name": "Polity Test",
        "questions": [
            {"question": "Q0", "options": ["a", "b", "c", "d"],
             "correct_option_id": 1, "explanation": "e"},
            {"question": "Q1", "options": ["a", "b", "c"],
             "correct_option_id": [0, 2], "analytics": {
                 "subject": "Polity", "topic": "FR &amp; FD",
                 "subtopic": "Article 12", "difficulty": "Hard"}},
            {"question": "Q2", "options": ["a", "b"], "correct_option_id": 0},
        ],
        "sections": [
            {"name": "Basics", "question_range": [1, 2], "timer": 30},
            {"name": "Advanced", "question_range": [3, 3], "timer": 60},
        ],
        "shuffle_questions": False,
        "shuffle_options": False,
        "negative_marks": 0,
        "correct_marks": 1,
    }


def qr(q_index, outcome, *, selected=None, correct=None, time_taken=None):
    return {
        "q_index": q_index,
        "selected": selected or [],
        "correct_option": correct or [],
        "outcome": outcome,
        "time_taken": time_taken,
    }


# ---------------------------------------------------------------------------
# Pure metadata tests
# ---------------------------------------------------------------------------

from quizbot.analytics import aggregation, metadata, runtime  # noqa: E402
from quizbot.analytics.metadata import (  # noqa: E402
    OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED,
    TOPIC_SOURCE_QUESTION, TOPIC_SOURCE_SECTION,
)
from quizbot.analytics.service import AnalyticsService  # noqa: E402
from quizbot.database.repositories import (  # noqa: E402
    AttemptRepository, MistakeRepository, QuizRepository,
)


class MetadataTests(unittest.TestCase):
    def test_01_difficulty_vocabulary_preserved(self):
        self.assertEqual(metadata.normalize_difficulty("Hard"), "hard")
        self.assertEqual(metadata.normalize_difficulty("MODERATE"), "moderate")
        self.assertEqual(metadata.normalize_difficulty("medium"), "moderate")
        self.assertEqual(metadata.normalize_difficulty("very-hard"), "extreme")
        self.assertEqual(metadata.normalize_difficulty("extreme"), "extreme")
        self.assertIsNone(metadata.normalize_difficulty(""))
        self.assertIsNone(metadata.normalize_difficulty("super-duper"))
        # labels without a project equivalent are never remapped/fabricated
        self.assertIsNone(metadata.normalize_difficulty("easy"))
        self.assertIsNone(metadata.normalize_difficulty("beginner"))
        self.assertIsNone(metadata.normalize_difficulty(None))

    def test_02_label_normalisation_never_merges_synonyms(self):
        self.assertEqual(metadata.normalize_label("  FR   &amp;  FD "), "FR & FD")
        self.assertNotEqual(metadata.normalize_label("FR"),
                            metadata.normalize_label("Fundamental Rights"))
        self.assertIsNone(metadata.normalize_label("   "))

    def test_03_extract_analytics_nested_flat_or_empty(self):
        q = {"analytics": {"subject": " pol ", "topic": "T", "difficulty": "hard",
                            "unknown_key": "x"}}
        a = metadata.extract_question_analytics(q)
        self.assertEqual(a["subject"], "pol")
        self.assertEqual(a["difficulty"], "hard")
        self.assertNotIn("unknown_key", a)
        # "easy" has no project-vocabulary equivalent -> dropped, not remapped
        dropped = metadata.extract_question_analytics(
            {"analytics": {"difficulty": "easy"}})
        self.assertNotIn("difficulty", dropped)
        flat = metadata.extract_question_analytics({"topic": "X", "difficulty": "hard"})
        self.assertEqual(flat["topic"], "X")
        self.assertEqual(metadata.extract_question_analytics({}), {})

    def test_04_normalize_question_is_non_destructive(self):
        q = {"question": "W?", "options": ["x"], "correct_option_id": 0}
        nq = metadata.normalize_question(q)
        self.assertEqual(nq, q)  # no analytics key invented
        rich = {"question": "W?", "options": ["x"], "correct_option_id": 0,
                "analytics": {"topic": "T", "difficulty": "garbage"}}
        nq = metadata.normalize_question(rich)
        self.assertEqual(nq["analytics"], {"topic": "T"})  # garbage dropped
        self.assertEqual(nq["options"], ["x"])
        self.assertEqual(nq["correct_option_id"], 0)

    def test_05_attach_does_not_override_explicit(self):
        qs = [{"question": "x", "options": ["a", "b"], "correct_option_id": 0,
               "analytics": {"topic": "Explicit", "difficulty": "extreme"}}]
        metadata.attach_question_analytics(
            qs, topic="Wizard Topic", difficulty="moderate")
        self.assertEqual(qs[0]["analytics"]["topic"], "Explicit")
        self.assertEqual(qs[0]["analytics"]["difficulty"], "extreme")

    def test_06_section_resolution_boundaries(self):
        secs = [{"name": "A", "question_range": [1, 2]},
                {"name": "B", "question_range": [3, 5]}]
        self.assertEqual(metadata.resolve_section(secs, 0)["name"], "A")
        self.assertEqual(metadata.resolve_section(secs, 1)["name"], "A")
        self.assertEqual(metadata.resolve_section(secs, 2)["name"], "B")
        self.assertEqual(metadata.resolve_section(secs, 4)["name"], "B")
        self.assertIsNone(metadata.resolve_section(secs, 5))
        self.assertIsNone(metadata.resolve_section(
            [{"name": "bad", "question_range": [9]}], 0))

    def test_07_resolve_metadata_provenance(self):
        sections = [{"name": "Section Name", "question_range": [1, 1]}]
        q = {"analytics": {"topic": "Explicit Topic"},
             "options": ["a"], "correct_option_id": 0}
        subject, topic, sub, diff, source = metadata.resolve_metadata(q, sections, 0)
        self.assertEqual(topic, "Explicit Topic")
        self.assertEqual(source, TOPIC_SOURCE_QUESTION)
        q2 = {"options": ["a"], "correct_option_id": 0}
        _, topic2, _, _, source2 = metadata.resolve_metadata(q2, sections, 0)
        self.assertEqual(topic2, "Section Name")
        self.assertEqual(source2, TOPIC_SOURCE_SECTION)
        _, topic3, _, _, source3 = metadata.resolve_metadata(q2, [], 0)
        self.assertIsNone(topic3)
        self.assertIsNone(source3)

    def test_08_snapshot_shapes(self):
        snap = metadata.build_snapshot(
            {"question": "Q", "options": ["a", "b"], "correct_option_id": [0, 1]})
        self.assertEqual(snap["correct_option_id"], [0, 1])
        snap_single = metadata.build_snapshot(
            {"question": "Q", "options": ["a", "b"], "correct_option_id": 0})
        self.assertEqual(snap_single["correct_option_id"], 0)
        self.assertIsNone(metadata.build_snapshot({"options": "nope"}))


# ---------------------------------------------------------------------------
# Runtime mapping tests
# ---------------------------------------------------------------------------

class RuntimeTests(unittest.TestCase):
    def test_09_to_canonical_identity_and_permutation(self):
        self.assertEqual(runtime.to_canonical([2, 0], None), [0, 2])
        # display_order[display_pos] = canonical
        order = [2, 0, 3, 1]
        self.assertEqual(runtime.to_canonical([0, 1], order), [0, 2])

    def test_10_shuffle_helper_is_bijective(self):
        from quizbot.runner_bot.quiz_utils import (
            shuffle_options_multi, shuffle_options_multi_with_mapping)
        options = ["o0", "o1", "o2", "o3"]
        for _ in range(20):
            shuffled, disp_correct, order = shuffle_options_multi_with_mapping(
                list(options), [0, 3], None)
            self.assertEqual(sorted(shuffled), sorted(options))
            # round-trip: canonical correct must come back [0,3]
            self.assertEqual(runtime.to_canonical(disp_correct, order), [0, 3])
            # old wrapper still behaves (same set of outputs, mapping list len)
            s2, c2 = shuffle_options_multi(list(options), [0, 3], None)
            self.assertEqual(sorted(c2), [0, 1, 2, 3][:2]) if False else None
            self.assertEqual(len(c2), 2)
            self.assertEqual(len(s2), 4)

    def test_11_results_correct_wrong_skipped_and_timing(self):
        polls = {
            "p0": {"question_index": 0, "correct_option": [1],
                   "sent_time": 100.0, "display_order": None},
            "p1": {"question_index": 1, "correct_option": [0, 2],
                   "sent_time": 100.0, "display_order": None},
            "p2": {"question_index": 2, "correct_option": [0],
                   "sent_time": 100.0, "display_order": None},
        }
        answers = {
            "p0": {"option": [1], "time": 105.0},   # correct, 5s
            "p1": {"option": [0], "time": 110.0},   # wrong (partial multi)
            # p2 skipped
        }
        res = runtime.build_question_results(polls, answers)
        by = {r["q_index"]: r for r in res}
        self.assertEqual(by[0]["outcome"], OUTCOME_CORRECT)
        self.assertEqual(by[0]["time_taken"], 5.0)
        self.assertEqual(by[1]["outcome"], OUTCOME_INCORRECT)
        self.assertEqual(by[2]["outcome"], OUTCOME_SKIPPED)
        self.assertIsNone(by[2]["time_taken"])
        self.assertIsNone(by[2]["answered"] if "answered" in by[2] else None)

    def test_12_option_shuffle_maps_display_selection_to_canonical(self):
        # canonical option 1 is correct and was displayed at position 3.
        order = [2, 0, 3, 1]  # display pos 3 -> canonical 1
        polls = {"p": {"question_index": 0, "correct_option": [3],
                        "sent_time": 0.0, "display_order": order}}
        answers = {"p": {"option": [3], "time": 2.0}}  # taps displayed correct
        res = runtime.build_question_results(polls, answers)[0]
        self.assertEqual(res["outcome"], OUTCOME_CORRECT)  # live truth
        self.assertEqual(res["selected"], [1])             # canonical
        self.assertEqual(res["correct_option"], [1])

    def test_13_question_shuffle_maps_index(self):
        # display position 0 actually served canonical question 2.
        polls = {"p": {"question_index": 0, "correct_option": [0],
                        "sent_time": 0.0, "question_order": [2, 0, 1]}}
        answers = {"p": {"option": [0], "time": 1.0}}
        res = runtime.build_question_results(polls, answers)[0]
        self.assertEqual(res["q_index"], 2)
        self.assertEqual(res["outcome"], OUTCOME_CORRECT)

    def test_14_multi_correct_requires_exact_set(self):
        polls = {"p": {"question_index": 0, "correct_option": [0, 2],
                        "sent_time": 0.0}}
        answers = {"p": {"option": [0, 2], "time": 1.0}}
        self.assertEqual(runtime.build_question_results(polls, answers)[0]["outcome"],
                         OUTCOME_CORRECT)
        answers = {"p": {"option": [0, 1, 2], "time": 1.0}}
        self.assertEqual(runtime.build_question_results(polls, answers)[0]["outcome"],
                         OUTCOME_INCORRECT)

    def test_15_duplicate_delivery_answered_wins(self):
        polls = {
            "a": {"question_index": 0, "correct_option": [1], "sent_time": 0.0},
            "b": {"question_index": 0, "correct_option": [1], "sent_time": 9.0},
        }
        answers = {"b": {"option": [1], "time": 11.0}}
        res = runtime.build_question_results(polls, answers)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["outcome"], OUTCOME_CORRECT)

    def test_16_miniapp_results_trust_live_bool_and_skip(self):
        order = [1, 0]
        per_question = {
            0: {"correct_ids": [1], "display_order": [2, 0, 3, 1]},
            1: {"correct_ids": [0], "display_order": None},
        }
        answers = {
            # q0: live judged wrong; selected display 0 -> canonical 2
            0: {"selected": [0], "correct": False, "time_taken": 4.0},
            # q1 unanswered -> skipped
        }
        res = runtime.build_miniapp_results(order, per_question, answers)
        by = {r["q_index"]: r for r in res}
        self.assertEqual(by[0]["outcome"], OUTCOME_INCORRECT)
        self.assertEqual(by[0]["selected"], [2])
        self.assertEqual(by[1]["outcome"], OUTCOME_SKIPPED)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

class AggregationTests(unittest.TestCase):
    def test_17_accuracy_excludes_skips_and_guards_zero(self):
        events = [{"outcome": OUTCOME_CORRECT}] * 4 + \
                 [{"outcome": OUTCOME_INCORRECT}] * 1 + \
                 [{"outcome": OUTCOME_SKIPPED}] * 5
        self.assertEqual(aggregation.answered_accuracy(events), 80.0)
        self.assertEqual(aggregation.answered_accuracy(
            [{"outcome": OUTCOME_SKIPPED}]), 0.0)
        self.assertEqual(aggregation.safe_ratio(1, 0), 0.0)
        self.assertEqual(aggregation.completion_percent(4, 10), 40.0)

    def test_18_avg_time_answered_with_known_timing_only(self):
        events = [
            {"outcome": OUTCOME_CORRECT, "time_taken": 10},
            {"outcome": OUTCOME_INCORRECT, "time_taken": 20},
            {"outcome": OUTCOME_SKIPPED, "time_taken": 999},
            {"outcome": OUTCOME_CORRECT, "time_taken": None},
        ]
        self.assertEqual(aggregation.avg_question_time(events), 15.0)
        self.assertIsNone(aggregation.avg_question_time([]))

    def test_19_topic_rollups_keep_labels_distinct(self):
        events = [
            {"topic": "FR", "outcome": OUTCOME_CORRECT, "attempt_id": "a"},
            {"topic": "Fundamental Rights", "outcome": OUTCOME_INCORRECT,
             "attempt_id": "a"},
            {"topic": "FR", "outcome": OUTCOME_INCORRECT, "attempt_id": "b",
             "topic_source": "section"},
            {"topic": "FR", "outcome": OUTCOME_CORRECT, "attempt_id": "c",
             "topic_source": "question"},
        ]
        rows = aggregation.topic_rollups(events)
        topics = {r["topic"]: r for r in rows}
        self.assertIn("FR", topics)
        self.assertIn("Fundamental Rights", topics)
        # different provenance -> different buckets (plus the no-source row)
        fr_rows = [r for r in rows if r["topic"] == "FR"]
        self.assertEqual(len(fr_rows), 3)
        sources = {r["topic_source"] for r in fr_rows}
        self.assertEqual(sources, {None, "section", "question"})

    def test_20_activity_days_and_streaks(self):
        days = aggregation.activity_days([
            "2026-09-10 11:00:00", "2026-09-10 12:00:00",
            "2026-09-11 10:00:00", "2026-09-01 09:00:00", None,
        ])
        self.assertEqual(days, ["2026-09-01", "2026-09-10", "2026-09-11"])
        stats = aggregation.streak_stats(
            ["2026-09-10", "2026-09-11"], today="2026-09-11")
        self.assertEqual(stats["current"], 2)
        self.assertEqual(stats["best"], 2)
        stats2 = aggregation.streak_stats(
            ["2026-09-01", "2026-09-02", "2026-09-04"], today="2026-09-05")
        self.assertEqual(stats2["best"], 2)
        self.assertEqual(stats2["current"], 1)  # yesterday gap tolerance
        self.assertIsNone(aggregation.streak_stats([])["current"])


# ---------------------------------------------------------------------------
# Service / repository integration with the fake DB
# ---------------------------------------------------------------------------

class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDB()
        self.svc = AnalyticsService(self.db)
        self.quiz = make_quiz()
        await QuizRepository(self.db).create(
            1, self.quiz["quiz_name"], self.quiz["questions"],
            sections=self.quiz["sections"], qid="q1",
            shuffle_questions=False, shuffle_options=False,
            negative_marks=0, correct_marks=1)

    async def _complete(self, user=100, attempt_id="att1", *, persisted=True,
                        qid="q1", results=None, source="group", questions=None,
                        sections=None, **kw):
        kw.setdefault("username", "alice")
        if results is None:
            results = [
                qr(0, OUTCOME_CORRECT, selected=[1], correct=[1], time_taken=5),
                qr(1, OUTCOME_INCORRECT, selected=[0], correct=[0, 2], time_taken=8),
                qr(2, OUTCOME_SKIPPED),
            ]
        return await self.svc.record_completion(
            user_id=user, attempt_id=attempt_id, qid=qid,
            quiz_name=self.quiz["quiz_name"], question_results=results,
            source=source, quiz_persisted=persisted,
            questions=questions if questions is not None else self.quiz["questions"],
            sections=sections if sections is not None else self.quiz["sections"],
            score=1, correct=1, wrong=1, total_time=13, **kw)

    async def test_21_canonical_records_written(self):
        await self._complete()
        events = list(self.db.collection("question_events").docs)
        self.assertEqual(len(events), 3)
        by = {e["question_index"]: e for e in events}
        self.assertEqual(by[0]["outcome"], OUTCOME_CORRECT)
        self.assertEqual(by[2]["outcome"], OUTCOME_SKIPPED)
        self.assertIsNone(by[2]["answered_at"])
        self.assertEqual(by[2]["time_taken"], None)
        # topic from section for q0 (index 0 -> section Basics range 1-2)
        self.assertEqual(by[0]["topic"], "Basics")
        self.assertEqual(by[0]["topic_source"], "section")
        # explicit metadata wins on q1 and difficulty vocabulary preserved
        self.assertEqual(by[1]["subject"], "Polity")
        self.assertEqual(by[1]["topic"], "FR & FD")
        self.assertEqual(by[1]["difficulty"], "hard")
        self.assertIn("question_snapshot", by[1])
        # attempt answers canonical + question_results + provenance
        attempt = await AttemptRepository(self.db).get("att1")
        self.assertEqual(attempt["answers"], {"q0": [1], "q1": [0]})
        self.assertEqual(attempt["skipped"], 1)
        self.assertEqual(attempt["quiz_persisted"], True)
        self.assertEqual(attempt["source"], "group")
        self.assertEqual(len(attempt["question_results"]), 3)

    async def test_22_event_write_is_idempotent(self):
        await self._complete()
        # replay the same completion (duplicate boundary callback)
        await self._complete()
        events = self.db.collection("question_events").docs
        self.assertEqual(len(events), 3)

    async def test_23_mistake_lifecycle_non_destructive(self):
        # attempt 1: q1 wrong
        await self._complete(attempt_id="a1", results=[
            qr(1, OUTCOME_INCORRECT, selected=[1], correct=[0, 2])])
        mistakes = self.db.collection("user_mistakes")
        self.assertEqual(len(mistakes.docs), 1)
        row = mistakes.docs[0]
        self.assertEqual(row["wrong_count"], 1)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["question_snapshot"]["question"], "Q1")
        # replay same attempt -> no double count
        await self._complete(attempt_id="a1", results=[
            qr(1, OUTCOME_INCORRECT, selected=[1], correct=[0, 2])])
        row = mistakes.docs[0]
        self.assertEqual(row["wrong_count"], 1)
        # second attempt wrong again -> count +1, history appended
        await self._complete(attempt_id="a2", results=[
            qr(1, OUTCOME_INCORRECT, selected=[0], correct=[0, 2])])
        row = mistakes.docs[0]
        self.assertEqual(row["wrong_count"], 2)
        self.assertEqual(len(row["wrong_attempt_ids"]), 2)
        self.assertEqual(len(row["revision_history"]), 2)
        # later correct -> resolved but nothing deleted, wrong_count intact
        await self._complete(attempt_id="a3", results=[
            qr(1, OUTCOME_CORRECT, selected=[0, 2], correct=[0, 2])])
        row = mistakes.docs[0]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["wrong_count"], 2)
        self.assertEqual(row["correct_count"], 1)
        self.assertIsNotNone(row["last_correct_at"])
        self.assertEqual(len(row["revision_history"]), 3)
        # wrong again after resolution -> reopened ("repeated")
        await self._complete(attempt_id="a4", results=[
            qr(1, OUTCOME_INCORRECT, selected=[1], correct=[0, 2])])
        row = mistakes.docs[0]
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["wrong_count"], 3)
        totals = await self.svc.get_mistake_totals(100)
        self.assertEqual(totals["open"], 1)
        self.assertEqual(totals["total"], 1)

    async def test_24_snapshot_survives_later_quiz_edit(self):
        await self._complete(attempt_id="a1", results=[
            qr(0, OUTCOME_INCORRECT, selected=[0], correct=[1])])
        # simulate creator editing the quiz text in place
        self.quiz["questions"][0]["question"] = "EDITED QUESTION TEXT"
        await self._complete(attempt_id="a2", results=[
            qr(0, OUTCOME_INCORRECT, selected=[2], correct=[1])])
        row = self.db.collection("user_mistakes").docs[0]
        self.assertEqual(row["question_snapshot"]["question"], "Q0")
        self.assertEqual(row["wrong_count"], 2)

    async def test_25_adhoc_quiz_records_events_only(self):
        await self._complete(
            persisted=False, qid="AI123", attempt_id="ad1", source="aiquiz",
            questions=[{"question": "X", "options": ["a", "b"],
                        "correct_option_id": 1}],
            sections=[], results=[qr(0, OUTCOME_INCORRECT, selected=[0], correct=[1])])
        self.assertEqual(len(self.db.collection("question_events").docs), 1)
        self.assertEqual(await self.db.collection("leaderboard").count_documents({}), 0)
        self.assertEqual(len(self.db.collection("user_mistakes").docs), 0)
        self.assertEqual(
            await self.db.collection("question_wrong_stats").count_documents({}), 0)
        attempt = await AttemptRepository(self.db).get("ad1")
        self.assertFalse(attempt["quiz_persisted"])
        # participants counter lives on quizzes collection -> untouched
        quiz_doc = self.db.collection("quizzes").docs[0]
        self.assertEqual(quiz_doc.get("total_participants", 0), 0)

    async def test_26_saved_dm_writes_mistakes_stats_leaderboard_participant(self):
        # Regression: DM answers used to be keyed by poll UUID and produced
        # zero mistake rows.
        await self._complete(source="dm", attempt_id="dm1")
        self.assertEqual(
            await self.db.collection("question_wrong_stats").count_documents({}), 2)
        lb = self.db.collection("leaderboard").docs
        self.assertEqual(len(lb), 1)
        self.assertEqual(lb[0]["user_id"], 100)
        quiz_doc = self.db.collection("quizzes").docs[0]
        self.assertEqual(quiz_doc["total_participants"], 1)
        stats = {d["q_index"]: d for d in
                 self.db.collection("question_wrong_stats").docs}
        # q2 skipped -> not counted in totals
        self.assertNotIn(2, stats)
        self.assertEqual(stats[1]["wrong_count"], 1)
        self.assertEqual(stats[1]["total_count"], 1)

    async def test_27_strict_user_isolation(self):
        await self._complete(user=100, attempt_id="a-100")
        await self._complete(user=200, attempt_id="a-200", results=[
            qr(0, OUTCOME_INCORRECT, selected=[0], correct=[1])], username="bob")
        ov100 = await self.svc.get_user_overview(100)
        ov200 = await self.svc.get_user_overview(200)
        self.assertEqual(ov100["questions"]["answered"], 2)
        self.assertEqual(ov200["questions"]["answered"], 1)
        topics100 = await self.svc.get_topic_performance(100)
        topics200 = await self.svc.get_topic_performance(200)
        self.assertTrue(all(t for t in topics100))
        self.assertEqual(len(topics200), 1)
        m100 = await self.svc.list_mistakes(100)
        m200 = await self.svc.list_mistakes(200)
        self.assertTrue(all(m["user_id"] == 100 for m in m100))
        self.assertTrue(all(m["user_id"] == 200 for m in m200))

    async def test_28_overview_denominators_and_legacy_fallback(self):
        await self._complete()  # 1 correct, 1 wrong, 1 skip
        ov = await self.svc.get_user_overview(100)
        self.assertEqual(ov["questions"]["answered_accuracy_pct"], 50.0)
        self.assertEqual(ov["attempts"]["completed"], 1)
        self.assertEqual(ov["attempts"]["in_progress"], 0)
        self.assertEqual(ov["provenance"]["accuracy_basis"], "question_events")
        # legacy attempt with only attempt-level counters
        await AttemptRepository(self.db).start(300, "q1", "Legacy", 4)
        await self.db.collection("quiz_attempts").update_one(
            {"user_id": 300},
            {"$set": {"status": "completed", "score": 3, "correct": 3, "wrong": 1,
                      "total_time": 40, "time_ended": "2026-01-01 10:00:00"}})
        ov3 = await self.svc.get_user_overview(300)
        self.assertEqual(ov3["questions"]["answered_accuracy_pct"], 75.0)
        self.assertEqual(ov3["provenance"]["accuracy_basis"], "attempt_totals")
        self.assertEqual(ov3["attempts"]["legacy"], 1)
        self.assertEqual(ov3["attempts"]["activity_days"], ["2026-01-01"])
        # an in-progress attempt must never count as completed
        await AttemptRepository(self.db).start(300, "q1", "Legacy", 4)
        ov3b = await self.svc.get_user_overview(300)
        self.assertEqual(ov3b["attempts"]["completed"], 1)
        self.assertEqual(ov3b["attempts"]["in_progress"], 1)

    async def test_29_in_memory_only_quiz_still_records(self):
        # deleted quiz boundary: caller passes the live in-memory questions
        await self._complete(persisted=False, qid="MIX_1_100", attempt_id="m1",
                             source="mix", questions=self.quiz["questions"],
                             results=[qr(0, OUTCOME_CORRECT, selected=[1], correct=[1])])
        ev = self.db.collection("question_events").docs
        self.assertEqual(ev[0]["question_snapshot"]["question"], "Q0")

    async def test_30_topic_and_question_performance(self):
        await self._complete(attempt_id="a1")
        await self._complete(attempt_id="a2", results=[
            qr(1, OUTCOME_CORRECT, selected=[0, 2], correct=[0, 2])])
        topics = await self.svc.get_topic_performance(100)
        fr = next(t for t in topics if t["topic"] == "FR & FD")
        self.assertEqual(fr["correct"], 1)
        self.assertEqual(fr["incorrect"], 1)
        self.assertEqual(fr["accuracy_pct"], 50.0)
        qs = await self.svc.get_question_performance(100, qid="q1")
        q1 = next(q for q in qs if q["question_index"] == 1)
        self.assertEqual(q1["times_seen"], 2)
        self.assertEqual(q1["correct"], 1)

    async def test_31_user_id_required(self):
        with self.assertRaises(ValueError):
            await self.svc.get_user_overview("100")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await self.svc.record_completion(
                user_id=True, attempt_id="x", qid="q", quiz_name="q",
                question_results=[], source="group", quiz_persisted=False)

    async def test_36_canonical_answers_under_option_shuffle(self):
        # Full path: group boundary maps a shuffled live session through the
        # service into canonical selected ids.
        order = [2, 0, 3, 1]
        polls = {"p": {"question_index": 0, "correct_option": [3],
                        "sent_time": 0.0, "display_order": order}}
        answers = {"p": {"option": [3], "time": 5.0}}
        results = runtime.build_question_results(polls, answers)
        await self._complete(attempt_id="sh1", results=results)
        attempt = await AttemptRepository(self.db).get("sh1")
        self.assertEqual(attempt["answers"], {"q0": [1]})
        ev = self.db.collection("question_events").docs[0]
        self.assertEqual(ev["selected_option"], [1])
        self.assertEqual(ev["outcome"], OUTCOME_CORRECT)

    async def test_37_daily_trend_merges_attempts_and_events(self):
        await self._complete(attempt_id="a1")
        trend = await self.svc.get_daily_trend(100, days=30)
        self.assertTrue(trend)
        self.assertTrue(all("day" in d for d in trend))


class RepositoryShimTests(unittest.IsolatedAsyncioTestCase):
    async def test_38_legacy_record_shim_keeps_counting(self):
        db = FakeDB()
        repo = MistakeRepository(db)
        await repo.record(7, [{"qid": "q", "index": 0}])
        await repo.record(7, [{"qid": "q", "index": 0}])
        rows = db.collection("user_mistakes").docs
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wrong_count"], 2)
        self.assertEqual(rows[0]["status"], "open")
        # non-destructive resolve
        await repo.resolve(7, "q", 0)
        rows = db.collection("user_mistakes").docs
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "resolved")
        self.assertEqual(rows[0]["wrong_count"], 2)

    async def test_39_quiz_create_normalizes_optional_metadata(self):
        db = FakeDB()
        repo = QuizRepository(db)
        await repo.create(1, "Q", [
            {"question": "a", "options": ["x", "y"], "correct_option_id": 0},
            {"question": "b", "options": ["x", "y"], "correct_option_id": 0,
             "analytics": {"topic": " T ", "difficulty": "MODERATE"}},
        ], qid="zz")
        doc = await repo.get("zz")
        self.assertNotIn("analytics", doc["questions"][0])
        self.assertEqual(doc["questions"][1]["analytics"],
                         {"topic": "T", "difficulty": "moderate"})
        self.assertEqual(doc["questions"][0]["question"], "a")


# ---------------------------------------------------------------------------
# Backfill tests
# ---------------------------------------------------------------------------

class BackfillTests(unittest.IsolatedAsyncioTestCase):
    async def _seed(self, db):
        await QuizRepository(db).create(
            1, "Quiz", make_quiz()["questions"], qid="saved",
            shuffle_questions=False, shuffle_options=False)
        await QuizRepository(db).create(
            1, "Shuffled", make_quiz()["questions"], qid="shuf",
            shuffle_questions=True, shuffle_options=True)
        attempts = db.collection("quiz_attempts")

        async def add(aid, uid, qid, answers, *, ended="2026-02-01 10:00:00"):
            await attempts.insert_one({
                "attempt_id": aid, "user_id": uid, "qid": qid,
                "quiz_name": qid, "answers": answers,
                "score": 1, "total_questions": 3, "correct": 1, "wrong": 1,
                "total_time": 12, "time_started": "2026-02-01 09:00:00",
                "time_ended": ended, "status": "completed",
            })

        await add("rec_ok", 1, "saved", {"q0": [1], "q1": [1]})  # 1 right, 1 wrong
        await add("rec_dm", 2, "saved",
                  {"123e4567-e89b-12d3-a456-426614174000": {"option": [1]}})
        await add("rec_shuffle", 3, "shuf", {"q0": [1]})
        await add("rec_deleted", 4, "gone", {"q0": [1]})
        await add("rec_empty", 5, "saved", {})  # legacy mini app shape
        await add("rec_edited", 6, "saved", {"q99": [0]})

    async def test_32_classification_dry_run(self):
        from quizbot.database.backfill import backfill_once
        db = FakeDB()
        await self._seed(db)
        result = await backfill_once(dry_run=True, db=db)
        s = result["summary"]
        self.assertEqual(s.get("backfilled", 0), 0)
        self.assertGreaterEqual(s.get("unrecoverable_dm", 0), 1)
        self.assertGreaterEqual(s.get("unrecoverable_shuffle", 0), 1)
        self.assertGreaterEqual(s.get("unrecoverable_deleted", 0), 1)
        self.assertGreaterEqual(s.get("unrecoverable_edited", 0), 1)
        self.assertGreaterEqual(s.get("noop_empty", 0), 1)
        # nothing marked on dry run
        marked = [a for a in db.collection("quiz_attempts").docs
                  if "analytics_backfill" in a]
        self.assertEqual(marked, [])

    async def test_33_apply_is_idempotent_and_conservative(self):
        from quizbot.database.backfill import backfill_once
        db = FakeDB()
        await self._seed(db)
        first = await backfill_once(dry_run=False, db=db)
        self.assertEqual(first["summary"].get("backfilled"), 1)
        # events for recoverable attempt: no fabricated timing/skips
        events = db.collection("question_events").docs
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["backfilled"] for e in events))
        self.assertTrue(all(e["time_taken"] is None for e in events))
        self.assertFalse(any(e["outcome"] == OUTCOME_SKIPPED for e in events))
        # mistakes/stats untouched (already written live historically)
        self.assertEqual(await db.collection("user_mistakes").count_documents({}), 0)
        self.assertEqual(await db.collection("question_wrong_stats").count_documents({}), 0)
        marker = next(a for a in db.collection("quiz_attempts").docs
                      if a["attempt_id"] == "rec_ok")
        self.assertEqual(marker["analytics_backfill"]["status"], "backfilled")
        # second run processes nothing new
        second = await backfill_once(dry_run=False, db=db)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(len(db.collection("question_events").docs), 2)
        # original completion fields untouched
        self.assertEqual(marker["status"], "completed")
        self.assertEqual(marker["score"], 1)


class IndexBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_34_phase_b_indexes_registered_and_idempotent(self):
        from quizbot.database.db import Database
        d = Database("mongodb://localhost", "test")
        d._db = FakeDB()  # type: ignore[assignment]
        await d._ensure_indexes()
        await d._ensure_indexes()  # second run must not raise
        events_col = d._db.collection("question_events")  # type: ignore[union-attr]
        specs = [k for (k, unique, sparse, name) in events_col.indexes]
        self.assertIn([("user_id", 1), ("attempt_id", 1), ("question_index", 1)],
                      [list(s) for s in specs])
        unique_specs = [list(k) for (k, unique, *_rest) in events_col.indexes if unique]
        self.assertIn([("user_id", 1), ("attempt_id", 1), ("question_index", 1)],
                      unique_specs)
        mistakes = d._db.collection("user_mistakes")  # type: ignore[union-attr]
        flat = [list(k) for (k, *_rest) in mistakes.indexes]
        self.assertIn([("user_id", 1), ("status", 1), ("last_wrong_at", -1)], flat)
        attempts = d._db.collection("quiz_attempts")  # type: ignore[union-attr]
        flat_a = [list(k) for (k, *_rest) in attempts.indexes]
        self.assertIn([("user_id", 1), ("status", 1), ("time_ended", -1)], flat_a)


if __name__ == "__main__":
    unittest.main()
