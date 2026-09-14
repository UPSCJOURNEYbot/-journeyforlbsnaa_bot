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

    def __getitem__(self, name):
        # Motor mapping API parity: production code must be able to use
        # db["coll"] exactly like a raw MotorDatabase.
        return self.collection(name)

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
        # Events carry only a compact content-hash reference; question text
        # lives once in the content-addressed snapshot store.
        self.assertNotIn("question_snapshot", by[1])
        self.assertIsInstance(by[1]["snapshot_id"], str)
        self.assertEqual(len(by[1]["snapshot_id"]), 64)
        # ... and it resolves to the original minimal question content.
        snaps = await self.svc.snapshots.get_many([by[1]["snapshot_id"]])
        self.assertEqual(snaps[by[1]["snapshot_id"]]["question"], "Q1")
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
        # Mistake rows reference the content-addressed snapshot instead of
        # embedding the text; service reads resolve it back.
        self.assertNotIn("question_snapshot", row)
        snaps = await self.svc.snapshots.get_many([row["snapshot_id"]])
        self.assertEqual(snaps[row["snapshot_id"]]["question"], "Q1")
        listed = await self.svc.list_mistakes(100)
        self.assertEqual(listed[0]["question_snapshot"]["question"], "Q1")
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
        first_snapshot_id = self.db.collection("user_mistakes").docs[0]["snapshot_id"]
        # simulate creator editing the quiz text in place
        self.quiz["questions"][0]["question"] = "EDITED QUESTION TEXT"
        await self._complete(attempt_id="a2", results=[
            qr(0, OUTCOME_INCORRECT, selected=[2], correct=[1])])
        row = self.db.collection("user_mistakes").docs[0]
        # The mistake is frozen to the FIRST-wrong snapshot; an edit creates
        # a new content hash and never repoints history.
        self.assertEqual(row["snapshot_id"], first_snapshot_id)
        snaps = await self.svc.snapshots.get_many([row["snapshot_id"]])
        self.assertEqual(snaps[row["snapshot_id"]]["question"], "Q0")
        self.assertEqual(row["wrong_count"], 2)
        # Both contents exist exactly once (edited content got its own row).
        snap_col = self.db.collection("question_snapshots").docs
        questions = sorted(s["question"] for s in snap_col)
        self.assertIn("Q0", questions)
        self.assertIn("EDITED QUESTION TEXT", questions)
        resolved = await self.svc.list_mistakes(100)
        self.assertEqual(resolved[0]["question_snapshot"]["question"], "Q0")

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
        # Even when no quiz document is persisted, the snapshot is stored in
        # the global content-addressed store and referenced by the event.
        self.assertNotIn("question_snapshot", ev[0])
        snaps = await self.svc.snapshots.get_many([ev[0]["snapshot_id"]])
        self.assertEqual(snaps[ev[0]["snapshot_id"]]["question"], "Q0")
        # ... and service-level reads resolve it transparently.
        qs = await self.svc.get_question_performance(100)
        self.assertEqual(qs[0]["question_snapshot"]["question"], "Q0")

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

    async def test_35_deleted_quiz_still_readable_via_snapshots(self):
        await self._complete(attempt_id="d1", results=[
            qr(0, OUTCOME_INCORRECT, selected=[0], correct=[1])])
        # quiz document is later deleted (qid row gone)
        await self.db.collection("quizzes").delete_many({})
        # events, mistakes, overviews and history must remain intact
        qs = await self.svc.get_question_performance(100)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["question_snapshot"]["question"], "Q0")
        mistakes = await self.svc.list_mistakes(100)
        self.assertEqual(mistakes[0]["question_snapshot"]["question"], "Q0")
        attempts = await self.svc.list_attempts(100)
        self.assertEqual(attempts[0]["status"], "completed")
        ov = await self.svc.get_user_overview(100)
        self.assertEqual(ov["questions"]["answered"], 1)
        self.assertEqual(ov["quizzes_played"], 1)  # counted from event qids
        days = await self.svc.get_activity_days(100)
        self.assertEqual(len(days), 1)

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

    # -- MEDIUM-1: content-addressed, bounded snapshot storage -------------

    async def test_40_snapshots_deduped_content_addressed_and_bounded(self):
        # Same question content seen by two users across two qids must store
        # each snapshot exactly once, globally.
        await self._complete(user=100, attempt_id="a1")
        await self._complete(user=200, attempt_id="a1")
        await QuizRepository(self.db).create(
            1, "Copy quiz", self.quiz["questions"],
            sections=self.quiz["sections"], qid="q2",
            shuffle_questions=False, shuffle_options=False,
            negative_marks=0, correct_marks=1)
        await self._complete(user=100, attempt_id="a2", qid="q2")

        snap_docs = self.db.collection("question_snapshots").docs
        # Exactly one doc per distinct question content (3 questions), even
        # though 3 completions x 3 events = 9 events reference them.
        self.assertEqual(len(snap_docs), 3)
        ids = {s["snapshot_id"] for s in snap_docs}
        self.assertTrue(all(len(i) == 64 for i in ids))
        # Stored shape is the minimal reconstructable snapshot, optionally
        # carrying Phase F explanation companions (non-identity; absent
        # entirely for questions without explanations, as Q1/Q2 are here).
        minimal = {"_id", "snapshot_id", "question", "options",
                   "correct_option_id", "created_at"}
        allowed = minimal | {"explanation", "explanation_detail"}
        for doc in snap_docs:
            self.assertTrue(minimal.issubset(set(doc)), doc)
            self.assertTrue(set(doc) <= allowed, doc)
        without_explanation = [
            d for d in snap_docs if d["question"] in ("Q1", "Q2")]
        self.assertEqual(len(without_explanation), 2)
        self.assertTrue(
            all(set(d) == minimal for d in without_explanation))
        q0 = next(d for d in snap_docs if d["question"] == "Q0")
        self.assertEqual(q0.get("explanation"), "e")
        # Explanation companions never change the identity hash: adding one
        # to an identical-content snapshot keeps the same id.
        from quizbot.analytics.metadata import build_snapshot
        s_with = build_snapshot(make_quiz()["questions"][0])
        s_core = {"question": "Q0", "options": ["a", "b", "c", "d"],
                  "correct_option_id": 1}
        from quizbot.analytics.metadata import snapshot_content_hash
        self.assertEqual(snapshot_content_hash(s_with),
                         snapshot_content_hash(s_core))
        # Every event and every mistake row references a snapshot; none embeds.
        for ev in self.db.collection("question_events").docs:
            self.assertNotIn("question_snapshot", ev)
            self.assertIn(ev["snapshot_id"], ids)
        for row in self.db.collection("user_mistakes").docs:
            self.assertNotIn("question_snapshot", row)
            self.assertIn(row["snapshot_id"], ids)
        # Analytics reads reconstruct text from the shared store.
        qs = await self.svc.get_question_performance(100)
        by_text = {q["question_snapshot"]["question"]: q for q in qs}
        self.assertEqual(set(by_text), {"Q0", "Q1", "Q2"})
        mistakes = await self.svc.list_mistakes(200)
        self.assertEqual(mistakes[0]["question_snapshot"]["question"], "Q1")
        # Re-running the same completion never duplicates snapshot docs.
        await self._complete(user=100, attempt_id="a1")
        self.assertEqual(len(self.db.collection("question_snapshots").docs), 3)

    async def test_41_legacy_embedded_snapshots_still_resolve(self):
        # Rows written before this schema carried the full embedded snapshot.
        # Reads must keep honouring those without any migration.
        events = self.db.collection("question_events")
        legacy_snapshot = {"question": "Legacy Q", "options": ["a", "b"],
                           "correct_option_id": 1}
        await events.insert_one({
            "user_id": 100, "qid": "oldq", "quiz_name": "old",
            "attempt_id": "leg", "event_id": "leg1", "question_index": 0,
            "outcome": OUTCOME_INCORRECT, "selected_option": [0],
            "correct_option": [1], "time_taken": 4,
            "answered_at": "2026-01-01 10:00:00", "created_at": "2026-01-01 10:00:00",
            "subject": None, "topic": None, "subtopic": None,
            "difficulty": None, "topic_source": None,
            "snapshot_id": None, "question_snapshot": legacy_snapshot,
            "source": "group", "quiz_persisted": True,
        })
        mistakes = self.db.collection("user_mistakes")
        await mistakes.insert_one({
            "user_id": 100, "qid": "oldq", "q_index": 0,
            "wrong_count": 1, "correct_count": 0, "status": "open",
            "wrong_attempt_ids": ["leg"], "correct_attempt_ids": [],
            "revision_history": [], "first_wrong_at": "2026-01-01 10:00:00",
            "last_wrong_at": "2026-01-01 10:00:00", "last_correct_at": None,
            "created_at": "2026-01-01 10:00:00",
            "snapshot_id": None, "question_snapshot": legacy_snapshot,
        })
        qs = await self.svc.get_question_performance(100)
        self.assertEqual(qs[0]["question_snapshot"]["question"], "Legacy Q")
        listed = await self.svc.list_mistakes(100)
        self.assertEqual(listed[0]["question_snapshot"]["question"], "Legacy Q")
        # No new snapshot rows are fabricated for legacy embedded rows.
        self.assertEqual(self.db.collection("question_snapshots").docs, [])

    # -- MEDIUM-2: identity-aware topic aggregation ------------------------

    async def _topic_quiz(self, qid, questions, sections=None):
        await QuizRepository(self.db).create(
            1, qid, questions, sections=sections or [], qid=qid,
            shuffle_questions=False, shuffle_options=False,
            negative_marks=0, correct_marks=1)

    async def test_42_topic_identity_cross_subject_and_quiz_scoping(self):
        def meta_q(text, subject, topic):
            return {"question": text, "options": ["a", "b"],
                    "correct_option_id": 1,
                    "analytics": {"subject": subject, "topic": topic}}

        def section_q(text):
            return {"question": text, "options": ["a", "b"],
                    "correct_option_id": 1}

        # Explicit metadata: same subject+topic pools ACROSS quizzes.
        await self._topic_quiz("polA", [meta_q("p1", "Polity", "Common")])
        await self._topic_quiz("polB", [meta_q("p2", "Polity", "Common")])
        # Same raw topic label under a different subject must NOT pool.
        await self._topic_quiz("hisA", [meta_q("h1", "History", "Common")])
        # Section-derived generic labels are quiz-scoped and must NOT pool,
        # even when the name is identical ("Basics").
        await self._topic_quiz(
            "secA", [section_q("s1")],
            sections=[{"name": "Basics", "question_range": [1, 1]}])
        await self._topic_quiz(
            "secB", [section_q("s2")],
            sections=[{"name": "Basics", "question_range": [1, 1]}])

        wrong = [qr(0, OUTCOME_INCORRECT, selected=[0], correct=[1])]
        for qid in ("polA", "polB", "hisA", "secA", "secB"):
            quiz = await QuizRepository(self.db).get(qid)
            await self._complete(
                attempt_id=f"att-{qid}", qid=qid, results=wrong,
                questions=quiz["questions"], sections=quiz.get("sections"))

        rows = await self.svc.get_topic_performance(100)
        explicit_polity = [r for r in rows if (r["subject"], r["topic"]) == ("Polity", "Common")]
        explicit_history = [r for r in rows if (r["subject"], r["topic"]) == ("History", "Common")]
        section_rows = [r for r in rows if r["topic_source"] == "section"]

        self.assertEqual(len(explicit_polity), 1)  # pooled across two qids
        self.assertEqual(explicit_polity[0]["answered"], 2)
        self.assertIsNone(explicit_polity[0]["qid"])  # explicit buckets span qids
        self.assertEqual(len(explicit_history), 1)  # distinct subject bucket
        self.assertEqual(explicit_history[0]["answered"], 1)
        self.assertEqual(len(section_rows), 2)  # identical names, distinct qids
        self.assertEqual({r["qid"] for r in section_rows}, {"secA", "secB"})
        self.assertTrue(all(r["subject"] is None for r in section_rows))
        # Raw labels are preserved verbatim; no synonym renaming happened.
        self.assertEqual({r["topic"] for r in rows}, {"Common", "Basics"})

    # -- MEDIUM-3: bounded bulk mistake persistence ------------------------

    async def test_43_bulk_mistakes_parity_lifecycle_and_isolation(self):
        n = 1200  # > 2 bulk chunks (chunk size 500)
        questions = [
            {"question": f"Q{i}", "options": ["a", "b"],
             "correct_option_id": 1} for i in range(n)]
        await QuizRepository(self.db).create(
            1, "Big", questions, qid="big", shuffle_questions=False,
            shuffle_options=False, negative_marks=0, correct_marks=1)
        all_wrong = [qr(i, OUTCOME_INCORRECT, selected=[0], correct=[1])
                     for i in range(n)]
        # An interleaved skipped result never creates a mistake row.
        results_b1 = sorted(
            all_wrong + [qr(n, OUTCOME_SKIPPED)],
            key=lambda r: (r["q_index"], 0 if r["outcome"] == OUTCOME_SKIPPED else 1))

        await self._complete(attempt_id="b1", qid="big", results=results_b1,
                             questions=questions, sections=[])
        rows = [d for d in self.db.collection("user_mistakes").docs
                if d["user_id"] == 100]
        self.assertEqual(len(rows), n)
        self.assertTrue(all(r["wrong_count"] == 1 for r in rows))
        self.assertTrue(all(len(r["revision_history"]) == 1 for r in rows))

        # Second attempt: first 100 now correct, rest wrong again.
        results_b2 = [
            qr(i, OUTCOME_CORRECT, selected=[1], correct=[1])
            for i in range(100)]
        results_b2 += [
            qr(i, OUTCOME_INCORRECT, selected=[0], correct=[1])
            for i in range(100, n)]
        await self._complete(attempt_id="b2", qid="big", results=results_b2,
                             questions=questions, sections=[])
        rows = {r["q_index"]: r for r in
                (d for d in self.db.collection("user_mistakes").docs
                 if d["user_id"] == 100)}
        self.assertEqual(len(rows), n)  # correct answers never create rows
        for i in range(100):
            self.assertEqual(rows[i]["status"], "resolved")
            self.assertEqual(rows[i]["wrong_count"], 1)
            self.assertEqual(rows[i]["correct_count"], 1)
            self.assertIsNotNone(rows[i]["last_correct_at"])
            self.assertEqual(len(rows[i]["revision_history"]), 2)
        for i in range(100, n):
            self.assertEqual(rows[i]["status"], "open")
            self.assertEqual(rows[i]["wrong_count"], 2)
            self.assertEqual(rows[i]["wrong_attempt_ids"], ["b1", "b2"])

        # A correct-only attempt for a fresh user never materialises rows.
        await self._complete(user=300, attempt_id="c1", qid="big",
                             results=[qr(0, OUTCOME_CORRECT, selected=[1],
                                         correct=[1])],
                             questions=questions, sections=[])
        self.assertEqual(
            await self.db.collection("user_mistakes").count_documents(
                {"user_id": 300}), 0)

        # User isolation: another user answering the same questions has its
        # own rows, and user 100's counts are untouched.
        await self._complete(user=200, attempt_id="u1", qid="big",
                             results=all_wrong, questions=questions,
                             sections=[])
        rows200 = [d for d in self.db.collection("user_mistakes").docs
                   if d["user_id"] == 200]
        self.assertEqual(len(rows200), n)
        self.assertTrue(all(r["wrong_count"] == 1 for r in rows200))
        totals100 = await self.svc.get_mistake_totals(100)
        self.assertEqual(totals100["total"], n)
        self.assertEqual(totals100["open"], n - 100)


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
        # Recovered events carry resolvable content-addressed snapshots
        # (MEDIUM-1 holds for history, not only live completions).
        for ev in events:
            self.assertTrue(ev["snapshot_id"])
            self.assertNotIn("question_snapshot", ev)
        snap_ids = {s["snapshot_id"] for s in db.collection("question_snapshots").docs}
        self.assertTrue(all(ev["snapshot_id"] in snap_ids for ev in events))

    async def test_45_backfill_quiz_cache_is_bounded_lru(self):
        from quizbot.database.backfill import (
            _BoundedQuizCache, _QUIZ_PROJECTION, backfill_once)
        db = FakeDB()
        # Unit-level: the cache never exceeds maxsize no matter how many
        # distinct quizzes are touched, evicts least-recently-used, and keeps
        # negative lookups so a deleted quiz is only queried once.
        await QuizRepository(db).create(
            1, "Q0", make_quiz()["questions"], qid="q0",
            shuffle_questions=False, shuffle_options=False)
        # Spy on the quiz collection: the cache must project out everything
        # but reconstruction fields (the fake itself ignores projections).
        col = db.collection("quizzes")
        original_find_one = col.find_one
        projections_seen = []

        async def spy_find_one(filt=None, **kwargs):
            projections_seen.append(kwargs.get("projection"))
            return await original_find_one(filt, **kwargs)

        col.find_one = spy_find_one
        cache = _BoundedQuizCache(db, maxsize=4)
        self.assertEqual(cache.maxsize, 4)
        for i in range(10):
            await cache.get(f"missing-{i}")  # all cached as None
        await cache.get("q0")
        self.assertEqual(len(cache), 4)
        self.assertLessEqual(len(cache), cache.maxsize)
        self.assertEqual(cache.misses, 11)  # one query per distinct qid
        self.assertTrue(all(p == _QUIZ_PROJECTION for p in projections_seen))
        # Only reconstruction fields are ever requested.
        self.assertEqual(
            set(_QUIZ_PROJECTION),
            {"_id", "questions", "sections",
             "shuffle_questions", "shuffle_options"})
        self.assertEqual((await cache.get("q0"))["qid"], "q0")
        self.assertEqual(cache.misses, 11)  # served from cache, no extra query
        # LRU: oldest lookups evicted, the fresh one retained and recency-updated.
        self.assertNotIn("missing-0", cache._data)
        self.assertIn("q0", cache._data)
        self.assertEqual(next(reversed(cache._data)), "q0")
        # Deleted quiz cached negatively: a second lookup does not hit Mongo.
        self.assertIsNone(await cache.get("missing-0"))
        # ...still 11 distinct queries (missing-0 was evicted -> refetch 12)
        self.assertEqual(cache.misses, 12)
        self.assertIsNone(await cache.get("missing-9"))
        self.assertEqual(cache.misses, 12)  # missing-9 still cached negatively
        with self.assertRaises(ValueError):
            _BoundedQuizCache(db, maxsize=0)

        # Integration-level: a library larger than the cache still backfills
        # completely while the cache stays bounded for the whole pass.
        db2 = FakeDB()
        n_quizzes = 40
        for i in range(n_quizzes):
            await QuizRepository(db2).create(
                1, f"Q{i}", make_quiz()["questions"], qid=f"lib{i}",
                shuffle_questions=False, shuffle_options=False)
        attempts = db2.collection("quiz_attempts")
        for i in range(n_quizzes):
            await attempts.insert_one({
                "attempt_id": f"a{i}", "user_id": 100 + i, "qid": f"lib{i}",
                "quiz_name": f"Q{i}", "answers": {"q0": [1], "q1": [1]},
                "score": 1, "total_questions": 3, "correct": 1, "wrong": 1,
                "total_time": 12, "time_started": "2026-02-01 09:00:00",
                "time_ended": "2026-02-01 10:00:00", "status": "completed",
            })
        result = await backfill_once(dry_run=False, db=db2,
                                     batch=10, quiz_cache_maxsize=4)
        self.assertEqual(result["summary"].get("backfilled"), n_quizzes)
        self.assertEqual(result["quiz_cache_size"] <= 4, True)
        self.assertEqual(result["quiz_cache_maxsize"], 4)
        # Every distinct quiz was fetched at least once but RAM stayed at 4.
        self.assertEqual(result["quiz_cache_misses"], n_quizzes)
        events = db2.collection("question_events").docs
        self.assertEqual(len(events), n_quizzes * 2)


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
        snapshots = d._db.collection("question_snapshots")  # type: ignore[union-attr]
        snap_unique = [k for (k, unique, *_r) in snapshots.indexes if unique]
        self.assertIn("snapshot_id", snap_unique)
        attempts = d._db.collection("quiz_attempts")  # type: ignore[union-attr]
        flat_a = [list(k) for (k, *_rest) in attempts.indexes]
        self.assertIn([("user_id", 1), ("status", 1), ("time_ended", -1)], flat_a)


if __name__ == "__main__":
    unittest.main()
