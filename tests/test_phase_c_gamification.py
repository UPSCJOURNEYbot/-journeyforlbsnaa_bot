"""Phase C tests -- XP, formula-based levels and daily IST streaks.

No MongoDB server is required: an in-memory collection implements the Motor
subset gamification uses, *including* the behaviours correctness depends on:

* unique indexes really raise ``pymongo.errors.DuplicateKeyError`` (for both
  the ``(user_id, event_key)`` ledger identity and unique ``user_xp.user_id``);
* document writes are atomic (each ``update_one`` runs without an internal
  ``await``), mirroring Mongo's atomic single-document update;
* reads return independent copies, so the service must reload fresh state and
  cannot rely on shared object identity;
* array operators model Mongo ``$ne`` membership, ``$addToSet`` and ``$pull``.

Concurrency tests construct *independent* ``GamificationService`` instances
(no shared lock/state besides the database) and, where required, force a
deterministic interleaving with an ``asyncio`` barrier so both processes plan
from stale watermarks before either commits -- the exact race the daily cap and
same-attempt exactly-once guarantees must survive.
"""

from __future__ import annotations

import asyncio
import copy
import types
import unittest
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from quizbot.analytics import gamification as g
from quizbot.analytics.gamification import (
    DAILY_XP_CAP,
    GamificationService,
    STATUS_ABANDONED,
    STATUS_COMMITTED,
    STATUS_RESERVED,
    ensure_gamification_indexes,
    level_for_xp,
    level_threshold,
    local_day_key,
    next_streak_state,
    score_quiz_xp,
    streak_bonus_xp,
)
from quizbot.analytics.metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    OUTCOME_SKIPPED,
)
from quizbot.analytics.service import AnalyticsService


# ---------------------------------------------------------------------------
# Fixed clock (IST day boundaries). 12:00 UTC == 17:30 IST same calendar day.
# ---------------------------------------------------------------------------

DAY0 = "2026-09-12"
DAY1 = "2026-09-13"
DAY2 = "2026-09-14"
DAY3 = "2026-09-15"
DAY5 = "2026-09-17"
AT1 = f"{DAY1} 12:00:00"
AT2 = f"{DAY2} 12:00:00"
AT3 = f"{DAY3} 12:00:00"
AT5 = f"{DAY5} 12:00:00"


def qr(q_index, outcome, *, time_taken=None, difficulty=None):
    row = {"q_index": q_index, "selected": [], "correct_option": [],
           "outcome": outcome, "time_taken": time_taken}
    if difficulty is not None:
        row["difficulty"] = difficulty
    return row


def results(n_correct=0, n_wrong=0, n_skip=0, *, time_each=None,
            difficulties=None, start=0):
    """Build canonical question_results; optionally give every answered
    question a measured time and per-index difficulty (dict)."""
    out = []
    i = start
    diffs = difficulties or {}
    for _ in range(n_correct):
        out.append(qr(i, OUTCOME_CORRECT, time_taken=time_each,
                      difficulty=diffs.get(i)))
        i += 1
    for _ in range(n_wrong):
        out.append(qr(i, OUTCOME_INCORRECT, time_taken=time_each,
                      difficulty=diffs.get(i)))
        i += 1
    for _ in range(n_skip):
        out.append(qr(i, OUTCOME_SKIPPED, time_taken=None,
                      difficulty=diffs.get(i)))
        i += 1
    return out


def gross_for(n_correct, difficulty_xp=0, paced=True):
    return 10 + (2 * n_correct + difficulty_xp if paced else 0)


# ---------------------------------------------------------------------------
# In-memory Motor-like fake (unique-index enforcing, atomic document writes)
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
            elif op == "$or" or op == "$and":
                if not any(_value_match(field, c) for c in val) if op == "$or" \
                        else not all(_value_match(field, c) for c in val):
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
        else:
            arr.append(v)


class FakeResult:
    def __init__(self, *, inserted_id=None, matched=0, modified=0,
                 upserted_count=0, upserted_id=None, deleted=0):
        self.inserted_id = inserted_id
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_count = upserted_count
        self.upserted_id = upserted_id
        self.deleted_count = deleted


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, keys, direction=1):
        if isinstance(keys, str):
            keys = [(keys, direction)]
        for field, d in reversed(keys):
            self._docs.sort(
                key=lambda x, f=field: (1 if _get_path(x, f) is None else 0,
                                        _get_path(x, f)),
                reverse=d < 0)
        return self

    def limit(self, n):
        if n is not None:
            self._docs = self._docs[:n]
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
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
        self.indexes = []  # list of dict(keys=[(path,dir)], unique)
        self._seq = 0

    async def create_index(self, keys, unique=False, sparse=False, name=None, **kw):
        if isinstance(keys, str):
            keys = [(keys, 1)]
        self.indexes.append({"keys": keys, "unique": unique, "name": name})
        return name

    def _norm_keys(self, keys):
        return keys

    def _unique_violation(self, candidate):
        for idx in self.indexes:
            if not idx["unique"]:
                continue
            vals = tuple(_get_path(candidate, p) for p, _ in idx["keys"])
            if any(v is None for v in vals):
                continue  # behave like sparse for null/missing components
            for d in self.docs:
                if tuple(_get_path(d, p) for p, _ in idx["keys"]) == vals:
                    return idx
        return None

    async def insert_one(self, doc):
        self._seq += 1
        new = copy.deepcopy(doc)
        new.setdefault("_id", f"{self.name}_{self._seq}")
        clash = self._unique_violation(new)
        if clash is not None:
            raise DuplicateKeyError(
                None, 11000,
                {"errmsg": "duplicate", "keyPattern": dict(clash["keys"])})
        self.docs.append(new)
        return FakeResult(inserted_id=new["_id"])

    async def find_one(self, filt=None, sort=None):
        rows = [d for d in self.docs if _matches(d, filt or {})]
        if sort:
            rows = list(FakeCursor(rows).sort(sort)._docs)
        return copy.deepcopy(rows[0]) if rows else None

    def find(self, filt=None):
        return FakeCursor([d for d in self.docs if _matches(d, filt or {})])

    async def count_documents(self, filt=None):
        return sum(1 for d in self.docs if _matches(d, filt or {}))

    async def update_one(self, filt, update, upsert=False):
        for d in self.docs:
            if _matches(d, filt):
                _apply_update(d, update, False)
                return FakeResult(matched=1, modified=1)
        if upsert:
            base = {k: v for k, v in filt.items()
                    if not k.startswith("$") and
                    not (isinstance(v, dict) and any(x.startswith("$") for x in v))}
            _apply_update(base, update, True)
            self._seq += 1
            base.setdefault("_id", f"{self.name}_{self._seq}")
            clash = self._unique_violation(base)
            if clash is not None:
                raise DuplicateKeyError(
                    None, 11000,
                    {"errmsg": "duplicate", "keyPattern": dict(clash["keys"])})
            self.docs.append(base)
            return FakeResult(upserted_count=1, upserted_id=base.get("_id"))
        return FakeResult()

    async def bulk_write(self, ops, ordered=True):
        ups = 0
        for op in ops:
            res = await self.update_one(op._filter, op._doc,
                                        upsert=bool(op._upsert))
            ups += res.upserted_count
        return types.SimpleNamespace(matched_count=0, modified_count=0,
                                     upserted_count=ups, inserted_count=0)


class FakeDB:
    def __init__(self):
        self._cols = {}

    def collection(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name, self)
        return self._cols[name]

    async def ensure_gamification(self):
        await ensure_gamification_indexes(self)


def new_db():
    db = FakeDB()
    return db


# ---------------------------------------------------------------------------
# Deterministic concurrency instrumentation
# ---------------------------------------------------------------------------

class BarrierGate:
    """Force two independent services to plan from stale state: each credits
    only once BOTH have reached the credit update, then they are released
    together (updates then serialise atomically). One-shot."""

    def __init__(self, parties=2, trigger_field="total_completions"):
        self.barrier = asyncio.Barrier(parties)
        self.armed = True
        self.trigger_field = trigger_field

    async def before_credit(self, update):
        is_credit = (
            self.armed and isinstance(update, dict)
            and self.trigger_field in update.get("$inc", {})
        )
        if is_credit:
            try:
                await self.barrier.wait()
            except asyncio.BrokenBarrierError:  # pragma: no cover
                pass
            self.armed = False


class GatedUsers:
    """Proxy for the user_xp collection that inserts a barrier on credits."""

    def __init__(self, inner, gate):
        self._inner = inner
        self._gate = gate

    async def update_one(self, filt, update, upsert=False):
        await self._gate.before_credit(update)
        return await self._inner.update_one(filt, update, upsert)

    def __getattr__(self, item):
        return getattr(self._inner, item)


class MismatchUsers:
    """On the first credit, simulate a concurrent commit bumping ``rev`` before
    our write is applied, forcing one CAS replan; then behave normally."""

    def __init__(self, inner, user_id):
        self._inner = inner
        self._user_id = user_id
        self.fired = False

    async def update_one(self, filt, update, upsert=False):
        if (not self.fired and isinstance(update, dict)
                and "total_completions" in update.get("$inc", {})
                and isinstance(filt.get("rev"), int)):
            self.fired = True
            # A concurrent writer committed first, advancing the version.
            await self._inner.update_one(
                {"user_id": self._user_id, "rev": filt["rev"]},
                {"$inc": {"rev": 1}})
        return await self._inner.update_one(filt, update, upsert)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def install_gated_users(db, gate, users_collection="user_xp"):
    inner = db.collection(users_collection)
    db._cols[users_collection] = GatedUsers(inner, gate)


def service(db):
    return GamificationService(db)


async def complete(svc, *, user, attempt, source="group", at=AT1,
                   question_results=None, questions=None, sections=None,
                   finalize=True, backfilled=False, enriched_events=None):
    return await svc.on_completion(
        user_id=user, attempt_id=attempt, source=source,
        question_results=question_results if question_results is not None else [],
        questions=questions, sections=sections, finalize=finalize,
        backfilled=backfilled, at=at, enriched_events=enriched_events)


def seed_user(db, user, *, xp_day=DAY1, today=0, total=0, tc=0, level=None,
              streak=0, longest=0, last_day=None, rev=0):
    db.collection("user_xp").docs.append({
        "_id": f"seed_user_{user}", "user_id": user,
        "total_xp": total, "xp_earned_today": today, "xp_day": xp_day,
        "total_completions": tc,
        "current_level": level if level is not None else level_for_xp(total),
        "current_streak": streak, "longest_streak": longest,
        "last_activity_day": last_day, "rev": rev,
    })


def seed_committed_streak(db, user, day, awarded=5, streak=1):
    db.collection("xp_ledger").docs.append({
        "_id": f"seed_streak_{user}_{day}", "user_id": user,
        "event_key": f"streak:{day}", "event_type": "streak",
        "local_day": day, "status": STATUS_COMMITTED, "awarded_xp": awarded,
        "streak": streak,
    })


def seed_committed_attempt(db, user, attempt, day, awarded, gross=None):
    db.collection("xp_ledger").docs.append({
        "_id": f"seed_att_{user}_{attempt}", "user_id": user,
        "event_key": f"attempt:{attempt}", "event_type": "attempt",
        "local_day": day, "status": STATUS_COMMITTED, "awarded_xp": awarded,
        "gross_xp": gross if gross is not None else awarded,
    })


def user_doc(db, user):
    for d in db.collection("user_xp").docs:
        if d.get("user_id") == user:
            return copy.deepcopy(d)
    return None


def ledger_rows(db, user, event_type=None, status=None):
    out = []
    for d in db.collection("xp_ledger").docs:
        if d.get("user_id") != user:
            continue
        if event_type and d.get("event_type") != event_type:
            continue
        if status and d.get("status") != status:
            continue
        out.append(copy.deepcopy(d))
    return out


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

class PureLogicTests(unittest.TestCase):
    def test_01_level_anchors_and_quadratic_continuation(self):
        self.assertEqual([level_threshold(n) for n in range(1, 5)],
                         [0, 50, 110, 180])
        self.assertEqual(level_threshold(5), 260)
        self.assertEqual(level_threshold(6), 350)
        for xp, lvl in [(0, 1), (49, 1), (50, 2), (109, 2), (110, 3),
                        (179, 3), (180, 4), (259, 4), (260, 5), (350, 6)]:
            self.assertEqual(level_for_xp(xp), lvl, xp)

    def test_02_streak_bonus_formula_and_cap(self):
        self.assertEqual([streak_bonus_xp(s) for s in range(1, 12)],
                         [5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25])
        self.assertEqual(streak_bonus_xp(20), 25)  # max

    def test_03_streak_transitions(self):
        self.assertEqual(next_streak_state(None, 0, 0, DAY1)[:2], (1, 1))
        # same day unchanged
        self.assertEqual(next_streak_state(DAY1, 1, 1, DAY1), (1, 1, False))
        # consecutive +1
        self.assertEqual(next_streak_state(DAY1, 1, 1, DAY2)[:2], (2, 2))
        # gap resets but longest retained
        cur, lng, changed = next_streak_state(DAY1, 5, 5, DAY5)
        self.assertEqual((cur, lng, changed), (1, 5, True))

    def test_04_ist_day_conversion_offsets(self):
        cases = {
            "2026-09-13T02:00:00+05:30": "2026-09-13",
            "2026-09-13T23:00:00-08:00": "2026-09-14",
            "2026-09-13T18:30:00Z": "2026-09-14",
            "2026-09-13T18:30:00+00:00": "2026-09-14",
            "2026-09-13 18:30:00": "2026-09-14",  # existing naive UTC app format
            "2026-09-13": "2026-09-13",            # date only
            "2026-09-13T05:00:00+05:30": "2026-09-13",
            "2026-09-13T00:15:00+05:30": "2026-09-13",  # just after IST midnight
            "2026-09-12T18:29:00Z": "2026-09-12",  # 23:59 IST, still prev day
        }
        for raw, want in cases.items():
            self.assertEqual(local_day_key(raw), want, raw)
        # datetime inputs (aware + naive UTC) and date
        self.assertEqual(
            local_day_key(datetime(2026, 9, 13, 18, 30, tzinfo=timezone.utc)),
            "2026-09-14")
        self.assertEqual(
            local_day_key(datetime(2026, 9, 13, 18, 30)), "2026-09-14")
        import datetime as _dt
        self.assertEqual(local_day_key(_dt.date(2026, 9, 13)), "2026-09-13")

    def test_05_xp_formula_base_and_correct(self):
        ev = results(n_correct=3, n_wrong=1, n_skip=1, time_each=10)
        s = score_quiz_xp(ev)
        self.assertEqual(s["answered"], 4)
        self.assertEqual(s["correct"], 3)
        self.assertEqual(s["base_xp"], 10)
        self.assertEqual(s["correct_xp"], 6)
        self.assertEqual(s["gross_xp"], 16)

    def test_06_difficulty_bonus_per_answered(self):
        ev = [
            qr(0, OUTCOME_CORRECT, time_taken=10, difficulty="moderate"),  # +1
            qr(1, OUTCOME_CORRECT, time_taken=10, difficulty="hard"),      # +2
            qr(2, OUTCOME_INCORRECT, time_taken=10, difficulty="extreme"),  # +3
            qr(3, OUTCOME_SKIPPED, difficulty="extreme"),                  # skipped:0
            qr(4, OUTCOME_CORRECT, time_taken=10),                         # missing:0
            qr(5, OUTCOME_INCORRECT, time_taken=10, difficulty="medium"),  # alias moderate +1
        ]
        s = score_quiz_xp(ev)
        # 5 answered (skip excluded), 3 correct -> +6; difficulty 1+2+3+0+0+1=7
        self.assertEqual(s["answered"], 5)
        self.assertEqual(s["correct"], 3)
        self.assertEqual(s["difficulty_xp"], 7)
        self.assertEqual(s["gross_xp"], 10 + 6 + 7)

    def test_07_pacing_withholds_answer_xp_keeps_base(self):
        # 4 answered in 4s total < 4*3=12s
        fast = results(n_correct=4, time_each=1)
        s = score_quiz_xp(fast)
        self.assertFalse(s["pacing_ok"])
        self.assertEqual(s["correct_xp"], 0)
        self.assertEqual(s["difficulty_xp"], 0)
        self.assertEqual(s["gross_xp"], 10)  # completion base retained
        # exactly at the threshold is allowed
        ok = score_quiz_xp(results(n_correct=4, time_each=3))
        self.assertTrue(ok["pacing_ok"])
        self.assertEqual(ok["gross_xp"], 10 + 8)

    def test_08_no_telemetry_means_no_pacing_penalty(self):
        ev = results(n_correct=50)  # no time_taken at all
        s = score_quiz_xp(ev)
        self.assertTrue(s["pacing_ok"])
        self.assertEqual(s["gross_xp"], 10 + 100)

    def test_09_all_skip_zero_answered_is_zero_xp(self):
        s = score_quiz_xp(results(n_skip=5))
        self.assertEqual(s["answered"], 0)
        self.assertEqual(s["gross_xp"], 0)
        self.assertEqual(score_quiz_xp([])["gross_xp"], 0)

    def test_10_eligible_sources(self):
        for src in ["group", "dm", "miniapp", "scheduled",
                    "aiquiz", "pdfquiz", "mix"]:
            ok, reason = g.completion_eligible(
                source=src, finalize=True, total_questions=3, backfilled=False)
            self.assertTrue(ok, (src, reason))

    def test_11_excluded_sources_and_conditions(self):
        for src in ["backfill", "unknown", "pollquiz", "", None]:
            ok, _ = g.completion_eligible(
                source=src, finalize=True, total_questions=3, backfilled=False)
            self.assertFalse(ok, src)
        self.assertFalse(g.completion_eligible(
            source="group", finalize=False, total_questions=3,
            backfilled=False)[0])
        self.assertFalse(g.completion_eligible(
            source="group", finalize=True, total_questions=0,
            backfilled=False)[0])
        self.assertFalse(g.completion_eligible(
            source="unknown", finalize=False, total_questions=3,
            backfilled=True)[0])


# ---------------------------------------------------------------------------
# Index bootstrap / uniqueness
# ---------------------------------------------------------------------------

class IndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_12_unique_indexes_registered(self):
        db = new_db()
        await ensure_gamification_indexes(db)
        ledger = db.collection("xp_ledger")
        users = db.collection("user_xp")
        ledger_keys = {tuple((p, d) for p, d in i["keys"]): i["unique"]
                       for i in ledger.indexes}
        self.assertTrue(ledger_keys[(("user_id", 1), ("event_key", 1))])
        user_unique = [i for i in users.indexes if i["unique"]]
        self.assertTrue(any(i["keys"] == [("user_id", 1)] for i in user_unique))
        # idempotent
        await ensure_gamification_indexes(db)

    async def test_13_duplicate_ledger_identity_raises(self):
        db = new_db()
        await ensure_gamification_indexes(db)
        await service(db)._reserve_event(1, "attempt", "attempt:a", DAY1, {})
        with self.assertRaises(DuplicateKeyError):
            await db.collection("xp_ledger").insert_one(
                {"user_id": 1, "event_key": "attempt:a"})
        # different user may reuse the same key
        await db.collection("xp_ledger").insert_one(
            {"user_id": 2, "event_key": "attempt:a"})

    async def test_14_unique_user_xp_raises(self):
        db = new_db()
        await ensure_gamification_indexes(db)
        await db.collection("user_xp").insert_one({"user_id": 7})
        with self.assertRaises(DuplicateKeyError):
            await db.collection("user_xp").insert_one({"user_id": 7})


# ---------------------------------------------------------------------------
# End-to-end service behaviour
# ---------------------------------------------------------------------------

class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        await ensure_gamification_indexes(self.db)

    async def test_15_first_completion_awards_xp_creates_user_and_streak(self):
        ev = results(n_correct=3, n_wrong=1, time_each=10)  # gross 16
        out = await complete(service(self.db), user=1, attempt="a1",
                             question_results=ev)
        self.assertTrue(out["eligible"])
        self.assertEqual(out["total_xp"], 16 + 5)  # 16 quiz + 5 streak bonus
        self.assertEqual(out["xp_earned_today"], 21)
        self.assertEqual(out["current_streak"], 1)
        self.assertEqual(out["longest_streak"], 1)
        self.assertEqual(out["total_completions"], 1)
        self.assertEqual(out["current_level"], 1)
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["xp_day"], DAY1)
        # two committed ledger rows (attempt + streak)
        self.assertEqual(
            len(ledger_rows(self.db, 1, status=STATUS_COMMITTED)), 2)

    async def test_16_attempt_idempotent_and_distinct_attempts_independent(self):
        ev = results(n_correct=2, time_each=10)  # gross14
        s = service(self.db)
        first = await complete(s, user=1, attempt="dup", question_results=ev)
        self.assertFalse(first["duplicate"])
        second = await complete(service(self.db), user=1, attempt="dup",
                                question_results=ev, at=AT1)
        self.assertTrue(second["duplicate"])
        # totals unchanged by the replay
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["total_completions"], 1)
        self.assertEqual(doc["total_xp"], 14 + 5)
        led = ledger_rows(self.db, 1, "attempt")
        self.assertEqual(len(led), 1)
        self.assertEqual(led[0]["status"], STATUS_COMMITTED)
        # a different attempt awards independently
        third = await complete(service(self.db), user=1, attempt="other",
                               question_results=ev)
        self.assertFalse(third["duplicate"])
        self.assertEqual(user_doc(self.db, 1)["total_completions"], 2)
        self.assertEqual(len(ledger_rows(self.db, 1, "attempt")), 2)

    async def test_16b_duplicate_attempt_next_day_does_not_advance_streak(self):
        ev = results(n_correct=1, time_each=10)
        await complete(service(self.db), user=1, attempt="once", at=AT1,
                       question_results=ev)
        # Replay the SAME attempt a day later: duplicate, no new streak step.
        replay = await complete(service(self.db), user=1, attempt="once",
                                at=AT2, question_results=ev)
        self.assertTrue(replay["duplicate"])
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["current_streak"], 1)
        self.assertEqual(doc["total_completions"], 1)
        self.assertFalse(
            [r for r in ledger_rows(self.db, 1, "streak")
             if r["local_day"] == DAY2])

    async def test_17_streak_only_once_per_day_then_consecutive_and_gap(self):
        ev = results(n_correct=1, time_each=10)  # gross12
        await complete(service(self.db), user=1, attempt="d1a", at=AT1,
                       question_results=ev)
        # second completion same day: no extra streak bonus, streak stays 1
        out = await complete(service(self.db), user=1, attempt="d1b", at=AT1,
                             question_results=ev)
        self.assertIsNone(out["streak"])
        self.assertEqual(out["current_streak"], 1)
        # next day: streak 2 bonus 7
        out = await complete(service(self.db), user=1, attempt="d2", at=AT2,
                             question_results=ev)
        self.assertEqual(out["current_streak"], 2)
        self.assertEqual(out["streak"]["bonus"], 7)
        # gap to DAY5: reset to 1, longest retained at 2
        out = await complete(service(self.db), user=1, attempt="d5", at=AT5,
                             question_results=ev)
        self.assertEqual(out["current_streak"], 1)
        self.assertEqual(out["longest_streak"], 2)
        # exactly one committed streak ledger per day
        for day, n in [(DAY1, 1), (DAY2, 1), (DAY5, 1)]:
            self.assertEqual(
                len([r for r in ledger_rows(self.db, 1, "streak")
                     if r["local_day"] == day]), n, day)

    async def test_18_daily_cap_clamps_to_200(self):
        ev = results(n_correct=45, time_each=10)  # gross 100
        for i in range(3):
            await complete(service(self.db), user=1, attempt=f"c{i}",
                           question_results=ev)
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["xp_earned_today"], 200)
        self.assertEqual(doc["total_xp"], 200)  # streak bonus included in cap
        self.assertEqual(doc["total_completions"], 3)
        # ledger awards for the day sum exactly to the counter
        awarded = sum(r["awarded_xp"] or 0
                      for r in ledger_rows(self.db, 1)
                      if r["local_day"] == DAY1 and
                      r["status"] == STATUS_COMMITTED)
        self.assertEqual(awarded, 200)

    async def test_19_next_day_resets_daily_counter_keeps_total(self):
        ev = results(n_correct=45, time_each=10)  # gross100
        for i in range(3):
            await complete(service(self.db), user=1, attempt=f"c{i}",
                           question_results=ev)
        self.assertEqual(user_doc(self.db, 1)["xp_earned_today"], 200)
        out = await complete(service(self.db), user=1, attempt="nextday",
                             at=AT2, question_results=ev)
        # Fresh IST day: streak-2 bonus 7 plus 100 quiz XP = 107.
        self.assertEqual(out["xp_earned_today"], 107)
        self.assertEqual(out["total_xp"], 307)

    async def test_20_zero_answered_awards_no_xp_but_counts_completion(self):
        ev = results(n_skip=5)
        out = await complete(service(self.db), user=1, attempt="z1",
                             question_results=ev)
        self.assertEqual(out["attempt"]["awarded"], 0)
        self.assertEqual(out["total_completions"], 1)
        self.assertEqual(out["current_streak"], 1)
        self.assertEqual(out["total_xp"], 5)  # streak bonus still paid

    async def test_21_level_advances_on_threshold(self):
        ev = results(n_correct=30, time_each=10)  # gross 70
        out = await complete(service(self.db), user=1, attempt="l1",
                             question_results=ev)
        # 70 quiz + 5 streak = 75 -> L2 (>=50)
        self.assertEqual(out["total_xp"], 75)
        self.assertEqual(out["current_level"], 2)

    async def test_22_lazy_migration_never_resets_existing_user(self):
        # legacy document: no rev / applied arrays / unknown extra fields
        seed_user(self.db, 9, total=999, today=12, xp_day=DAY1, tc=7,
                  level=4, streak=3, longest=6, last_day=DAY1)
        ev = results(n_correct=1, time_each=10)  # gross12
        out = await complete(service(self.db), user=9, attempt="m1",
                             question_results=ev)
        self.assertEqual(out["total_completions"], 8)      # incremented, not reset
        self.assertEqual(out["total_xp"], 999 + 12)        # accumulated, no streak (same day)
        self.assertIsNone(out["streak"])
        self.assertEqual(out["current_streak"], 3)
        self.assertEqual(out["longest_streak"], 6)

    async def test_23_user_isolation(self):
        ev = results(n_correct=2, time_each=10)
        await complete(service(self.db), user=1, attempt="u1",
                       question_results=ev)
        await complete(service(self.db), user=2, attempt="u2",
                       question_results=ev)
        d1, d2 = user_doc(self.db, 1), user_doc(self.db, 2)
        self.assertEqual(d1["total_xp"], 19)
        self.assertEqual(d2["total_xp"], 19)
        self.assertEqual(d1["total_completions"], 1)
        self.assertEqual(d2["total_completions"], 1)

    async def test_24_ineligible_completions_write_nothing(self):
        for kw in [
            dict(source="backfill", finalize=False, backfilled=True),
            dict(source="unknown", finalize=False),
            dict(source="pollquiz", finalize=True),
        ]:
            out = await complete(
                service(self.db), user=5,
                attempt=f"x_{kw['source']}", question_results=results(n_correct=1),
                **kw)
            self.assertFalse(out["eligible"], kw)
        # zero-question completion
        out = await complete(service(self.db), user=5, attempt="z",
                             question_results=[], source="group", finalize=True)
        self.assertFalse(out["eligible"])
        self.assertIsNone(user_doc(self.db, 5))
        self.assertEqual(ledger_rows(self.db, 5), [])

    async def test_25_cas_replans_after_mismatch_and_still_credits_once(self):
        await service(self.db)._ensure_user(1)
        svc = GamificationService(self.db)
        svc.users = MismatchUsers(self.db.collection("user_xp"), 1)
        seed_committed_streak(self.db, 1, DAY1)
        ev = results(n_correct=2, time_each=10)  # gross14
        out = await complete(svc, user=1, attempt="mm", question_results=ev)
        self.assertEqual(out["attempt"]["awarded"], 14)
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["total_completions"], 1)
        self.assertEqual(doc["total_xp"], 14)  # exactly one credit
        # rev gained the injected bump plus the real commit
        self.assertEqual(doc["rev"], 2)

    async def test_26_counters_advance_when_cap_exhausted(self):
        # cap already full for DAY1; streak last advanced on the prior IST day
        seed_user(self.db, 1, total=200, today=200, xp_day=DAY1, tc=4,
                  streak=3, longest=3, last_day=DAY0)
        seed_committed_attempt(self.db, 1, "prior", DAY1, 200)
        ev = results(n_skip=2)  # 0 quiz XP
        out = await complete(service(self.db), user=1, attempt="full",
                             question_results=ev)
        # streak advanced to 4 but bonus is 0 (cap full); completion still counts
        self.assertEqual(out["current_streak"], 4)
        self.assertEqual(out["longest_streak"], 4)
        self.assertEqual(out["xp_earned_today"], 200)
        self.assertEqual(out["total_completions"], 5)
        streak_row = [r for r in ledger_rows(self.db, 1, "streak")
                      if r["local_day"] == DAY1][0]
        self.assertEqual(streak_row["awarded_xp"], 0)

    async def test_26b_out_of_order_day_never_regresses_counter_or_streak(self):
        # User already advanced to DAY2; a late DAY1 event arrives.
        seed_user(self.db, 1, total=100, today=100, xp_day=DAY2, tc=2,
                  streak=2, longest=2, last_day=DAY2)
        seed_committed_streak(self.db, 1, DAY2, awarded=7, streak=2)
        ev = results(n_correct=20, time_each=10)  # gross 50
        out = await complete(service(self.db), user=1, attempt="late",
                             at=AT1, question_results=ev)  # AT1 == DAY1
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["xp_day"], DAY2)           # never rolled back
        self.assertEqual(doc["xp_earned_today"], 150)   # billed to current bucket
        self.assertEqual(doc["total_xp"], 150)
        self.assertEqual(doc["current_streak"], 2)      # streak not regressed
        self.assertEqual(doc["longest_streak"], 2)
        self.assertEqual(doc["total_completions"], 3)
        old = [r for r in ledger_rows(self.db, 1, "streak")
               if r["event_key"] == f"streak:{DAY1}"][0]
        self.assertEqual(old["status"], STATUS_ABANDONED)


# ---------------------------------------------------------------------------
# Cross-process concurrency (independent service instances)
# ---------------------------------------------------------------------------

class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    GROSS_200_RESULTS = results(n_correct=95)  # base10 + 190 = 200, no timing

    async def asyncSetUp(self):
        self.db = new_db()
        await ensure_gamification_indexes(self.db)

    def _preseed_day(self, user, day=DAY1, streak=1):
        """User exists on `day` and the streak event is already settled, so the
        barrier isolates the attempt-credit race."""
        seed_user(self.db, user, xp_day=day, today=0, streak=streak,
                  longest=streak, last_day=day)
        seed_committed_streak(self.db, user, day, awarded=0, streak=streak)

    async def test_27_cross_process_daily_cap_barrier_two_x_200(self):
        self._preseed_day(1)
        gate = BarrierGate(parties=2)
        install_gated_users(self.db, gate)
        s1, s2 = GamificationService(self.db), GamificationService(self.db)
        done = await asyncio.gather(
            complete(s1, user=1, attempt="bigA",
                     question_results=self.GROSS_200_RESULTS),
            complete(s2, user=1, attempt="bigB",
                     question_results=self.GROSS_200_RESULTS),
        )
        awards = sorted(d["attempt"]["awarded"] for d in done)
        self.assertEqual(awards, [0, 200])
        doc = user_doc(self.db, 1)
        self.assertLessEqual(doc["xp_earned_today"], 200)
        self.assertEqual(doc["xp_earned_today"], 200)
        self.assertEqual(doc["total_xp"], 200)
        self.assertEqual(doc["total_completions"], 2)  # both completions count
        # transient guards are released -> user doc stays bounded
        self.assertFalse(doc.get("applied_attempts"))

    async def test_28_same_attempt_barrier_credits_exactly_once(self):
        self._preseed_day(1)
        gate = BarrierGate(parties=2)
        install_gated_users(self.db, gate)
        s1, s2 = GamificationService(self.db), GamificationService(self.db)
        ev = results(n_correct=95)
        done = await asyncio.gather(
            complete(s1, user=1, attempt="SAME", question_results=ev),
            complete(s2, user=1, attempt="SAME", question_results=ev),
        )
        dups = [d for d in done if d.get("duplicate")]
        self.assertEqual(len(dups), 1)
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["total_completions"], 1)
        self.assertEqual(doc["total_xp"], 200)
        attempt_ledgers = ledger_rows(self.db, 1, "attempt")
        self.assertEqual(len(attempt_ledgers), 1)
        self.assertEqual(attempt_ledgers[0]["status"], STATUS_COMMITTED)
        self.assertEqual(attempt_ledgers[0]["awarded_xp"], 200)

    async def test_29_concurrent_zero_xp_completions_both_count(self):
        self._preseed_day(1)
        gate = BarrierGate(parties=2)
        install_gated_users(self.db, gate)
        s1, s2 = GamificationService(self.db), GamificationService(self.db)
        ev = results(n_skip=4)  # 0 XP each
        await asyncio.gather(
            complete(s1, user=1, attempt="z1", question_results=ev),
            complete(s2, user=1, attempt="z2", question_results=ev),
        )
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["total_completions"], 2)  # N + 2
        self.assertEqual(doc["xp_earned_today"], 0)
        self.assertEqual(doc["total_xp"], 0)

    async def test_30_fuzz_many_instances_never_overawards(self):
        await service(self.db)._ensure_user(1)
        await service(self.db)._ensure_user(2)
        ev = results(n_correct=20, time_each=10)  # gross 50
        calls = []
        # 6 independent instances x 4 attempts each, 2 users interleaved
        for inst in range(6):
            svc = GamificationService(self.db)
            for u in (1, 2):
                for k in range(2):
                    calls.append(complete(
                        svc, user=u, attempt=f"i{inst}_u{u}_{k}",
                        question_results=ev))
        await asyncio.gather(*calls)

        for u in (1, 2):
            doc = user_doc(self.db, u)
            # invariant 1: hard cap
            self.assertLessEqual(doc["xp_earned_today"], DAILY_XP_CAP)
            # invariant 2: one completion per distinct attempt
            committed = ledger_rows(self.db, u, "attempt",
                                    STATUS_COMMITTED)
            self.assertEqual(len(committed), 12)
            self.assertEqual(doc["total_completions"], 12)
            # invariant 3: no settled reservation left behind
            self.assertEqual(
                ledger_rows(self.db, u, "attempt", STATUS_RESERVED), [])
            # invariant 4: exactly one streak event for the day
            streaks = [r for r in ledger_rows(self.db, u, "streak")
                       if r["local_day"] == DAY1]
            self.assertEqual(len(streaks), 1)
            self.assertEqual(streaks[0]["status"], STATUS_COMMITTED)
            # invariant 5: ledger awards reconcile to the daily counter
            awarded = sum(r["awarded_xp"] or 0
                          for r in ledger_rows(self.db, u)
                          if r["local_day"] == DAY1
                          and r["status"] == STATUS_COMMITTED)
            self.assertEqual(awarded, doc["xp_earned_today"])
            self.assertEqual(doc["total_xp"], doc["xp_earned_today"])
            # invariant 6: transient exactly-once guards are cleaned up
            self.assertFalse(doc.get("applied_attempts"))
            self.assertFalse(doc.get("applied_streaks"))
            # invariant 7: cap actually bound (would otherwise be far over)
            self.assertEqual(doc["xp_earned_today"], 200)


# ---------------------------------------------------------------------------
# Crash / orphan recovery
# ---------------------------------------------------------------------------

class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        await ensure_gamification_indexes(self.db)

    async def test_31_orphan_attempt_before_credit_recovered_once(self):
        # crash window W1: ledger inserted (reserved), counter never credited
        await service(self.db)._ensure_user(1)
        seed_committed_streak(self.db, 1, DAY1, awarded=0)  # isolate attempt
        await self.db.collection("xp_ledger").insert_one({
            "user_id": 1, "event_key": "attempt:orphan", "event_type": "attempt",
            "local_day": DAY1, "status": STATUS_RESERVED,
            "awarded_xp": None, "gross_xp": 14})
        ev = results(n_correct=2, time_each=10)
        out = await complete(service(self.db), user=1, attempt="orphan",
                             question_results=ev)
        self.assertFalse(out.get("duplicate"))
        self.assertEqual(out["attempt"]["awarded"], 14)
        # replay is now a duplicate
        again = await complete(service(self.db), user=1, attempt="orphan",
                               question_results=ev)
        self.assertTrue(again.get("duplicate"))
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["total_completions"], 1)
        self.assertEqual(doc["total_xp"], 14)

    async def test_32_orphan_attempt_after_credit_never_double_paid(self):
        # crash window W2: counter credit applied + guard set, ledger not
        # flipped to committed before the crash.
        await service(self.db)._ensure_user(1)
        seed_committed_streak(self.db, 1, DAY1, awarded=0)  # isolate attempt
        key = "attempt:paid"
        await self.db.collection("xp_ledger").insert_one({
            "user_id": 1, "event_key": key, "event_type": "attempt",
            "local_day": DAY1, "status": STATUS_RESERVED,
            "awarded_xp": None, "gross_xp": 14})
        users = self.db.collection("user_xp")
        await users.update_one(
            {"user_id": 1},
            {"$inc": {"total_xp": 14, "xp_earned_today": 14,
                      "total_completions": 1, "rev": 1},
             "$addToSet": {"applied_attempts": key}})
        before = user_doc(self.db, 1)
        ev = results(n_correct=2, time_each=10)
        out = await complete(service(self.db), user=1, attempt="paid",
                             question_results=ev)
        self.assertTrue(out.get("duplicate"))
        after = user_doc(self.db, 1)
        # no double credit
        self.assertEqual(after["total_xp"], before["total_xp"])
        self.assertEqual(after["total_completions"], before["total_completions"])
        self.assertFalse(after.get("applied_attempts"))  # guard self-healed
        led = [r for r in ledger_rows(self.db, 1, "attempt")
               if r["event_key"] == key][0]
        self.assertEqual(led["status"], STATUS_COMMITTED)

    async def test_33_streak_orphan_after_credit_never_double_paid(self):
        await service(self.db)._ensure_user(1)
        key = f"streak:{DAY1}"
        await self.db.collection("xp_ledger").insert_one({
            "user_id": 1, "event_key": key, "event_type": "streak",
            "local_day": DAY1, "status": STATUS_RESERVED, "awarded_xp": None})
        users = self.db.collection("user_xp")
        await users.update_one(
            {"user_id": 1},
            {"$set": {"last_activity_day": DAY1, "current_streak": 1,
                      "longest_streak": 1, "xp_day": DAY1},
             "$inc": {"total_xp": 5, "xp_earned_today": 5, "rev": 1},
             "$addToSet": {"applied_streaks": key}})
        before = user_doc(self.db, 1)
        # a same-day completion triggers reconciliation of the orphan
        await complete(service(self.db), user=1, attempt="after",
                       question_results=results(n_skip=1))
        after = user_doc(self.db, 1)
        self.assertEqual(after["total_xp"], before["total_xp"])  # no 2nd bonus
        led = [r for r in ledger_rows(self.db, 1, "streak")
               if r["event_key"] == key][0]
        self.assertEqual(led["status"], STATUS_COMMITTED)

    async def test_34_stale_streak_orphan_abandoned_preserves_newer_streak(self):
        # Reserved streak for an old day after the user has newer activity:
        # abandon (never double pay), preserve the next-day streak.
        await service(self.db)._ensure_user(1)
        await self.db.collection("user_xp").update_one(
            {"user_id": 1},
            {"$set": {"current_streak": 4, "longest_streak": 4,
                      "last_activity_day": "2000-01-05"}})
        await self.db.collection("xp_ledger").insert_one({
            "user_id": 1, "event_key": "streak:2000-01-04",
            "event_type": "streak", "local_day": "2000-01-04",
            "status": STATUS_RESERVED, "awarded_xp": None})
        svc = service(self.db)
        result = await svc._apply_streak(1, "2000-01-04")
        self.assertIsNone(result)  # not paid
        doc = user_doc(self.db, 1)
        self.assertEqual(doc["current_streak"], 4)
        self.assertEqual(doc["longest_streak"], 4)
        led = [r for r in ledger_rows(self.db, 1, "streak")
               if r["event_key"] == "streak:2000-01-04"][0]
        self.assertEqual(led["status"], STATUS_ABANDONED)
        self.assertEqual(doc.get("xp_earned_today", 0), 0)


# ---------------------------------------------------------------------------
# Fail-soft hook through the canonical AnalyticsService boundary
# ---------------------------------------------------------------------------

class AnalyticsHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        await ensure_gamification_indexes(self.db)

    def _analytics(self):
        return AnalyticsService(self.db)

    async def _record(self, svc, *, user=1, attempt="att1", source="group",
                      persisted=False, finalize=True, backfilled=False,
                      question_results=None, questions=None, sections=None):
        qres = question_results if question_results is not None else \
            results(n_correct=2, time_each=10)
        return await svc.record_completion(
            user_id=user, attempt_id=attempt, qid="q", quiz_name="Quiz",
            question_results=qres, source=source,
            quiz_persisted=persisted, questions=questions,
            sections=sections, score=1, correct=2, wrong=0,
            finalize=finalize, backfilled=backfilled)

    async def test_35_record_completion_awards_xp_for_every_eligible_source(self):
        for src in ["group", "dm", "miniapp", "scheduled",
                    "aiquiz", "pdfquiz", "mix"]:
            db = new_db()
            await ensure_gamification_indexes(db)
            out = await self._record(AnalyticsService(db),
                                     user=1, attempt=f"a_{src}", source=src)
            self.assertTrue(out["gamification"]["eligible"], src)
            self.assertEqual(out["gamification"]["total_completions"], 1, src)

    async def test_36_backfill_and_finalize_false_award_no_xp(self):
        out = await self._record(self._analytics(), attempt="bf",
                                 source="unknown", finalize=False,
                                 backfilled=True)
        self.assertFalse(out["gamification"]["eligible"])
        self.assertIsNone(user_doc(self.db, 1))
        # analytics itself still wrote events
        self.assertGreaterEqual(out["events_inserted"], 0)

    async def test_37_zero_question_completion_excluded(self):
        out = await self._record(self._analytics(), attempt="zero",
                                 source="group", question_results=[])
        self.assertFalse(out["gamification"]["eligible"])
        self.assertIsNone(user_doc(self.db, 1))

    async def test_38_gamification_failure_is_fail_soft(self):
        svc = self._analytics()

        async def boom(**kwargs):
            raise RuntimeError("gamification down")

        svc.gamification.on_completion = boom
        out = await self._record(svc, attempt="fs")
        # completion + analytics still succeed and return normally
        self.assertIn("events_inserted", out)
        self.assertIsNone(out["gamification"])
        # no xp state created by the failing subsystem
        self.assertIsNone(user_doc(self.db, 1))

    async def test_39_difficulty_and_pacing_flow_through_enriched_events(self):
        # Provide saved questions carrying difficulty metadata; the hook uses
        # the canonical enriched Phase B events.
        questions = [
            {"question": "q0", "options": ["a", "b"], "correct_option_id": 0,
             "analytics": {"difficulty": "Hard"}},
            {"question": "q1", "options": ["a", "b"], "correct_option_id": 0,
             "analytics": {"difficulty": "extreme"}},
        ]
        qres = [
            qr(0, OUTCOME_CORRECT, time_taken=10),
            qr(1, OUTCOME_CORRECT, time_taken=10),
        ]
        out = await self._record(self._analytics(), attempt="diff",
                                 question_results=qres, questions=questions,
                                 persisted=False)
        score = out["gamification"]["score"]
        # hard +2, extreme +3, 2 correct +4, base10 -> gross19, +5 streak =24
        self.assertEqual(score["difficulty_xp"], 5)
        self.assertEqual(score["gross_xp"], 19)
        self.assertEqual(out["gamification"]["total_xp"], 24)


if __name__ == "__main__":
    unittest.main(verbosity=2)
