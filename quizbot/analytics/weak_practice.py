"""Phase E weak-topic -> targeted practice quiz (``/weakquiz``).

Weakness is computed ONLY from the authenticated user's own
``question_events`` plus ``user_mistakes``; the global, all-user question
statistics collections are never consulted for an individual's weakness.
This module adds:

* no new collection, no new field, no new index, no second quiz engine and
  no second analytics/XP path;
* a deterministic, sample-size-aware weakness model (a Wilson score-interval
  lower bound on each topic's accuracy, so a 0/2 fluke cannot outrank a
  well-sampled weak topic) with explicit minimum-evidence gates;
* bounded candidate loading (recently-played + owned stored quizzes, one
  server-side bank aggregation, one per-question history read, one snapshot
  $in), Phase D content identity/dedup and live-vs-snapshot resolution, and
  a tiered, explainable selection of at most :data:`PRACTICE_SIZE`
  questions handed to the EXISTING private DM quiz engine.

Every practice answer flows back through the single canonical
``AnalyticsService.record_completion(source="dm")`` boundary: question_events,
topic performance and XP/streaks update naturally, and questions that stand
in for stored mistakes carry the same Phase D provenance key so answers fold
into the non-destructive mistake lifecycle.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from quizbot.analytics import mistake_revision as mr
from quizbot.analytics.metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    build_snapshot,
    normalize_difficulty,
    snapshot_content_hash,
)
from quizbot.analytics.repository import QuestionSnapshotRepository
from quizbot.database.repositories import MistakeRepository, QuizRepository

from .repository import QuestionEventRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounded, deterministic model constants (no UPSC taxonomy anywhere).
# ---------------------------------------------------------------------------

WILSON_Z = 1.96
MIN_ANSWERED = 5
MIN_INCORRECT = 2
WEAK_ACCURACY_CEIL = 70.0     # accuracy at or below this, with evidence, is weak
RECENT_DAYS = 30

PLAYED_QID_CAP = 50           # most recent stored quizzes the user has played
OWN_QID_CAP = 50             # quizzes created by the user
POOL_CAP = 60                # bounded $in list against the bank
BANK_CANDIDATE_CAP = 100     # matched embedded questions pulled server-side
PERF_CAP = 300               # per-question history rows over the pool
MISTAKE_CAP = MistakeRepository.REVISION_CANDIDATE_CAP

PRACTICE_SIZE = 10           # hard per-session cap (this is NOT a pool cap)
REPEATED_TIER_CAP = 4        # T1 contribution cap (no single-item domination)
MIN_MEANINGFUL = 3           # below this, honestly flag "limited practice"

TIER_REPEATED = 1
TIER_OPEN = 2
TIER_POOR_SEEN = 3
TIER_FRESH = 4
TIER_MASTERED = 5

_DIFF_RANK = {"extreme": 3, "hard": 2, "moderate": 1}


# ---------------------------------------------------------------------------
# Pure statistics / ranking
# ---------------------------------------------------------------------------

def wilson_lower(correct: int, answered: int, z: float = WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for the correct-answer
    proportion in [0, 1]. Lower == plausibly worse. With no evidence it
    returns 1.0 so an empty topic can never rank as weak."""
    n = int(answered or 0)
    if n <= 0:
        return 1.0
    c = int(correct or 0)
    phat = c / n
    z2 = z * z
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    return max(0.0, (center - margin) / (1 + z2 / n))


def bucket_key(subject: Optional[str], topic: Optional[str]) -> tuple:
    return (subject or None, topic or None)


def normalize_rollup(row: dict) -> dict:
    correct = int(row.get("correct", 0) or 0)
    incorrect = int(row.get("incorrect", 0) or 0)
    skipped = int(row.get("skipped", 0) or 0)
    answered = correct + incorrect
    accuracy = round(correct / answered * 100, 2) if answered else None
    subject = row.get("subject")
    topic = None if row.get("subject_only") else row.get("topic")
    return {
        "subject": subject,
        "topic": topic,
        "key": bucket_key(subject, topic),
        "subject_only": bool(row.get("subject_only")),
        "label": topic if topic else (f"{subject} \u00b7 untagged" if subject else "Untagged"),
        "correct": correct,
        "incorrect": incorrect,
        "skipped": skipped,
        "answered": answered,
        "attempt_count": int(row.get("attempt_count", 0) or 0),
        "recent_incorrect": int(row.get("recent_incorrect", 0) or 0),
        "last_answered_at": row.get("last_answered_at"),
        "accuracy_pct": accuracy,
        "wilson": wilson_lower(correct, answered),
        # mistake signals (filled by the caller from user_mistakes)
        "open_mistakes": 0,
        "repeated_mistakes": 0,
        "relapse_mistakes": 0,
    }


def is_eligible(bucket: dict) -> bool:
    """Weak only with real, minimum evidence. A single wrong answer can
    never qualify; a tiny bad sample is gated by the Wilson rank."""
    return (
        bucket["answered"] >= MIN_ANSWERED
        and bucket["incorrect"] >= MIN_INCORRECT
        and bucket["accuracy_pct"] is not None
        and bucket["accuracy_pct"] <= WEAK_ACCURACY_CEIL
    )


def bucket_reasons(bucket: dict) -> list[str]:
    """Deterministic, honest explanation lines; only values derived from
    the user's own records appear."""
    reasons: list[str] = []
    if bucket["answered"]:
        reasons.append(
            f"{bucket['accuracy_pct']:.0f}% over {bucket['answered']} answered"
        )
    if bucket["answered"] and bucket["answered"] < MIN_ANSWERED + 3:
        reasons.append(f"Limited sample ({bucket['answered']} answered)")
    if bucket["repeated_mistakes"]:
        n = bucket["repeated_mistakes"]
        reasons.append(
            f"{n} repeated mistake{'s' if n != 1 else ''}")
    elif bucket["open_mistakes"]:
        n = bucket["open_mistakes"]
        reasons.append(f"{n} open mistake{'s' if n != 1 else ''}")
    if bucket["relapse_mistakes"]:
        n = bucket["relapse_mistakes"]
        reasons.append(
            f"{n} missed again after correcting"
            if n == 1 else f"{n} relapsed after correction")
    if bucket["recent_incorrect"]:
        n = bucket["recent_incorrect"]
        reasons.append(
            f"{n} wrong in the last {RECENT_DAYS} days"
            if n != 1 else f"1 wrong in the last {RECENT_DAYS} days")
    return reasons or ["Missed questions"]


def _rank_key(bucket: dict) -> tuple:
    last = mr.to_epoch(bucket.get("last_answered_at")) or 0.0
    subject, topic = bucket["key"]
    return (
        bucket["wilson"],                       # worse accuracy evidence first
        -bucket["open_mistakes"],
        -bucket["repeated_mistakes"],
        -bucket["relapse_mistakes"],
        -bucket["recent_incorrect"],
        -bucket["answered"],                    # more evidence before less
        -last,                                  # most recently answered first
        subject or "",
        topic or "",
    )


def rank_buckets(buckets: list[dict]) -> list[dict]:
    return sorted(buckets, key=_rank_key)


# ---------------------------------------------------------------------------
# Mistake -> topic signals (Phase D content groups, explicit metadata only)
# ---------------------------------------------------------------------------

def mistake_topic_signals(rows: list[dict]) -> tuple[dict, dict]:
    """Aggregate Phase D content groups (mistakes) per explicit
    ``(subject, topic)`` bucket. Returns ``(signals_by_key,
    explicit_groups)`` where groups exclude section-only derived labels
    (section names are quiz-scoped free text, never global skills)."""
    groups = mr.group_mistakes(rows)
    sources_by_key: dict = {}
    for row in rows:
        sources_by_key.setdefault(mr.content_key(row), []).append(
            row.get("topic_source"))
    explicit_groups = []
    signals: dict = {}
    for g in groups:
        sources = sources_by_key.get(g["key"], [])
        # Keep the group when at least one origin carries explicit question
        # metadata (or legacy rows with no provenance at all).
        if sources and all(src == "section" for src in sources if src):
            continue
        explicit_groups.append(g)
        key = bucket_key(g.get("subject"), g.get("topic"))
        sig = signals.setdefault(key, {
            "open": 0, "repeated": 0, "relapse": 0})
        if g["open"]:
            sig["open"] += 1
            if mr.is_repeated(g):
                sig["repeated"] += 1
        if g.get("relapse"):
            sig["relapse"] += 1
    return signals, explicit_groups


# ---------------------------------------------------------------------------
# Candidate tiering / assembly (pure given resolved candidate dicts)
# ---------------------------------------------------------------------------

def _epoch(value: Any) -> float:
    return mr.to_epoch(value) or 0.0


def assign_tier(candidate: dict) -> int:
    g = candidate.get("mistake_group")
    if g is not None:
        if g["open"] and g["wrong_max"] >= 2:
            return TIER_REPEATED
        if g["open"]:
            return TIER_OPEN
        # Resolved: truly mastered (>=2 correct) questions only fill an
        # otherwise undersized session; a single correction stays in T3.
        return TIER_MASTERED if g.get("correct_total", 0) >= 2 else TIER_POOR_SEEN
    history = candidate.get("history")
    if history:
        answered = history.get("correct", 0) + history.get("incorrect", 0)
        acc = history.get("correct", 0) / answered if answered else 1.0
        if history.get("last_outcome") == OUTCOME_INCORRECT or acc < 0.5:
            return TIER_POOR_SEEN
    return TIER_FRESH


def _tier_sort_key(c: dict) -> tuple:
    g = c.get("mistake_group") or {}
    h = c.get("history") or {}
    diff = _DIFF_RANK.get(normalize_difficulty(
        c.get("difficulty") or g.get("difficulty")), 0)
    key = c["key"][0], str(c["key"][1])
    tier = c["tier"]
    if tier == TIER_REPEATED:
        return (-int(g.get("wrong_max", 0)),
                -(1 if g.get("relapse") else 0),
                -_epoch(g.get("last_wrong_at")), *key)
    if tier == TIER_OPEN:
        return (-_epoch(g.get("last_wrong_at")), -int(g.get("wrong_total", 0)), *key)
    if tier == TIER_POOR_SEEN:
        return (-int(h.get("incorrect", 0)), -_epoch(h.get("last_answered_at")), *key)
    if tier == TIER_MASTERED:
        # Oldest mastered first within the tail (most forgotten first).
        return (_epoch(g.get("last_correct_at")), *key)
    # TIER_FRESH: never-seen first, then least-recently-seen, then harder,
    # then stable content key.
    seen = 1 if h else 0
    return (seen, _epoch(h.get("last_answered_at")) if h else 0.0,
            -diff, *key)


def assemble_session(
    candidates: list[dict], bucket_order: list[tuple],
    size: int = PRACTICE_SIZE,
) -> list[dict]:
    """Deterministic, fairness-aware pick. Tiers are global (repeated/open
    mistakes lead), but within a tier topics take turns so one weak topic
    cannot consume the whole multi-topic session. T1 is globally capped."""
    by_bucket: dict = {k: [] for k in bucket_order}
    for c in candidates:
        by_bucket.setdefault(c["bucket"], []).append(c)
    for k in by_bucket:
        by_bucket[k].sort(key=_tier_sort_key)

    selected: list[dict] = []
    used_ids: set = set()

    def take_tier(tier: int) -> None:
        progress = True
        while progress and len(selected) < size:
            progress = False
            for bkey in bucket_order:
                if len(selected) >= size:
                    break
                # T1 dominates only up to the cap on the first pass; the
                # deferred T1 items get a final refill pass below.
                if tier == TIER_REPEATED and len([
                        c for c in selected if c["tier"] == TIER_REPEATED]
                ) >= REPEATED_TIER_CAP and not _t1_overflow_pass:
                    break
                for cand in by_bucket.get(bkey, []):
                    if cand["tier"] != tier or id(cand) in used_ids:
                        continue
                    used_ids.add(id(cand))
                    selected.append(cand)
                    progress = True
                    break

    # Open mistakes, poor-seen and fresh questions jump ahead of excess
    # repeated items, which still outrank merely-mastered fillers and take
    # any slots left before the mastered tail.
    _t1_overflow_pass = False
    take_tier(TIER_REPEATED)
    for tier in (TIER_OPEN, TIER_POOR_SEEN, TIER_FRESH):
        take_tier(tier)
    if len(selected) < size:
        _t1_overflow_pass = True
        take_tier(TIER_REPEATED)
    if len(selected) < size:
        _t1_overflow_pass = False
        take_tier(TIER_MASTERED)
    return selected[:size]


# ---------------------------------------------------------------------------
# DB-backed service
# ---------------------------------------------------------------------------

class WeakPracticeService:
    """Bounded reads over existing collections; resolves practice questions
    reusing Phase D live/snapshot logic; launches nothing itself."""

    def __init__(self, db: Any = None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.events = QuestionEventRepository(db)
        self.mistakes = MistakeRepository(db)
        self.quizzes = QuizRepository(db)
        self.snapshots = QuestionSnapshotRepository(db)
        # Reuse Phase D resolution/usability verbatim (no forked copy).
        self.revision = mr.MistakeRevisionService(db)

    @staticmethod
    def _require_user(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("weak practice requires a stable integer user_id")
        return user_id

    @staticmethod
    def _cutoff(now: Any) -> str:
        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(now, datetime):
            now_dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        else:
            now_dt = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
        return (now_dt.astimezone(timezone.utc) - timedelta(days=RECENT_DAYS)
                ).strftime("%Y-%m-%d %H:%M:%S")

    # -- weakness overview ------------------------------------------------

    async def weak_buckets(self, user_id: int, now: Any = None) -> dict:
        user_id = self._require_user(user_id)
        cutoff = self._cutoff(now)
        rollup = await self.events.get_practice_rollups(user_id, cutoff)
        mistake_rows = await self.mistakes.query_revision_rows(
            user_id, limit=MISTAKE_CAP)
        signals, groups = mistake_topic_signals(mistake_rows)

        buckets = [normalize_rollup(r)
                   for r in (rollup.get("topics", [])
                             + rollup.get("subject_only", []))]
        # A bucket can exist ONLY via mistakes when every question in it was
        # answered wrong (rollups are outcome-agnostic grouping, so in
        # practice it always exists; this keeps coverage airtight).
        known = {b["key"] for b in buckets}
        for key in signals:
            if key not in known:
                subject, topic = key
                base = {"subject": subject, "topic": topic,
                        "subject_only": topic is None,
                        "correct": 0, "incorrect": 0, "skipped": 0,
                        "attempt_count": 0, "recent_incorrect": 0,
                        "last_answered_at": None}
                buckets.append(normalize_rollup(base))
                known.add(key)
        for b in buckets:
            sig = signals.get(b["key"])
            if sig:
                b["open_mistakes"] = sig["open"]
                b["repeated_mistakes"] = sig["repeated"]
                b["relapse_mistakes"] = sig["relapse"]
            b["reasons"] = bucket_reasons(b)

        eligible = rank_buckets([b for b in buckets if is_eligible(b)])
        return {
            "user_id": user_id,
            "answered": int(rollup.get("answered", 0) or 0),
            "eligible": eligible,
            "buckets": buckets,
            "mistake_groups": groups,
        }

    async def overview(self, user_id: int, now: Any = None) -> dict:
        data = await self.weak_buckets(user_id, now)
        eligible = data["eligible"]
        if data["answered"] == 0:
            state = "no_history"
        elif eligible:
            state = "ready"
        elif any(b["answered"] >= MIN_ANSWERED for b in data["buckets"]):
            # At least one topic has a full evidence window but nothing
            # crosses the weakness threshold: the honest answer is "no weak
            # topics", not "I need more data".
            state = "no_weak"
        else:
            state = "insufficient"
        data["state"] = state
        return data

    # -- practice set build ----------------------------------------------

    async def build_practice(
        self, user_id: int, *, topic_index: Optional[int] = None,
        now: Any = None,
    ) -> dict:
        user_id = self._require_user(user_id)
        data = await self.weak_buckets(user_id, now)
        eligible = data["eligible"]
        if data["answered"] == 0:
            return {"state": "no_history", "questions": [], "size": 0}
        if not eligible:
            if any(b["answered"] >= MIN_ANSWERED for b in data["buckets"]):
                state = "no_weak"
            elif data["buckets"]:
                state = "insufficient"
            else:
                state = "no_history"
            return {"state": state, "answered": data["answered"],
                    "questions": [], "size": 0}

        if topic_index is None:
            wanted = eligible[:2]          # auto: best, +runner-up if needed
            manual = False
        else:
            if not (0 <= int(topic_index) < len(eligible)):
                return {"state": "stale_topic", "questions": [], "size": 0}
            wanted = [eligible[int(topic_index)]]
            manual = True
        bucket_order = [b["key"] for b in wanted]
        wanted_set = set(bucket_order)
        groups = [g for g in data["mistake_groups"]
                  if bucket_key(g.get("subject"), g.get("topic")) in wanted_set]

        # ---- bounded accessible pool (never a global bank scan) --------
        played = await self.events.distinct_played_qids(
            user_id, PLAYED_QID_CAP)
        owned = await self.quizzes.list_qids_by_creator(
            user_id, OWN_QID_CAP)
        pool: list[str] = []
        for qid in list(played) + list(owned):
            if qid and qid not in pool:
                pool.append(qid)
        pool = pool[:POOL_CAP]

        bank_rows = await self.quizzes.topic_candidate_questions(
            pool, bucket_order, limit=BANK_CANDIDATE_CAP)
        perf_rows = await self.events.get_question_performance(
            user_id, qids=pool, limit=PERF_CAP)
        perf_map = {(r["qid"], r["question_index"]): r for r in perf_rows}
        adhoc_rows = await self.events.get_seen_ad_hoc(
            user_id, bucket_order, self._cutoff(now),
            limit=BANK_CANDIDATE_CAP)

        snap_ids = ({g.get("snapshot_id") for g in groups if g.get("snapshot_id")}
                    | {r["snapshot_id"] for r in adhoc_rows if r.get("snapshot_id")})
        snap_map = await self.snapshots.get_many(snap_ids) if snap_ids else {}

        candidates: dict[Any, dict] = {}
        excluded = 0

        def _content_key_for(question: dict, qid: Optional[str],
                            idx: Optional[int], sid: Optional[str]):
            if sid:
                return ("snap", sid)
            snap = build_snapshot(question)
            hash_sid = snapshot_content_hash(snap)
            if hash_sid:
                return ("snap", hash_sid)
            return ("q", f"{qid}#{idx}")

        # 1) Live stored-bank questions first (richest: explanations/media).
        for row in bank_rows:
            q = row["question"]
            qid, idx = row["qid"], row["q_index"]
            if not self.revision._usable(q) or not str(q.get("question") or "").strip():
                excluded += 1
                continue
            analytics = q.get("analytics") if isinstance(q.get("analytics"), dict) else {}
            key = _content_key_for(q, qid, idx, None)
            ckey = bucket_key(analytics.get("subject"), analytics.get("topic"))
            if ckey not in wanted_set:
                ckey = bucket_key(analytics.get("subject"), None)
            if ckey not in wanted_set:
                continue
            # The question comes from a real STORED quiz in the user's pool,
            # so its origin is legitimate fold provenance: a wrong practice
            # answer opens the non-destructive mistake row for that origin
            # (idempotent under the practice attempt id); a correct answer on
            # a never-missed question is a no-op there, exactly as designed.
            origin_sid = key[1] if key[0] == "snap" else None
            candidates[key] = {
                "key": key, "question": q, "bucket": ckey,
                "origins": [{"qid": qid, "q_index": idx,
                             "snapshot_id": origin_sid}],
                "mistake_group": None,
                "history": perf_map.get((qid, idx)),
                "difficulty": analytics.get("difficulty"),
            }

        # 2) Mistake-origin content: merge into identical bank candidates
        #    (gaining provenance + tier signals), otherwise resolve through
        #    Phase D (live if unchanged, immutable snapshot if edited/deleted).
        quiz_cache: dict = {}
        for g in groups:
            sid = g.get("snapshot_id")
            match_key = ("snap", sid) if sid else None
            existing = candidates.get(match_key) if match_key else None
            if existing is None:
                for origin in g["origins"]:
                    probe = ("q", f"{origin.get('qid')}#{origin.get('q_index')}")
                    if probe in candidates:
                        existing = candidates[probe]
                        break
            if existing is not None:
                # Union: identical content may live in several stored
                # quizzes, all of which the practice answer applies to.
                merged = list(existing.get("origins") or [])
                for o in g["origins"]:
                    if not o.get("qid"):
                        continue
                    entry = {"qid": o["qid"], "q_index": o["q_index"],
                             "snapshot_id": o.get("snapshot_id") or sid}
                    if (entry["qid"], entry["q_index"]) not in [
                            (e["qid"], e["q_index"]) for e in merged]:
                        merged.append(entry)
                existing["origins"] = merged
                existing["mistake_group"] = g
                continue
            built = await self.revision._build_question(g, quiz_cache, snap_map)
            if built is None:
                excluded += 1
                continue
            question, _meta = built
            if not self.revision._usable(question) or not str(
                    question.get("question") or "").strip():
                excluded += 1
                continue
            key = ("snap", sid) if sid else (
                _content_key_for(question, None, None, None))
            candidates[key] = {
                "key": key, "question": question,
                "bucket": bucket_key(g.get("subject"), g.get("topic")),
                "origins": [
                    {"qid": o["qid"], "q_index": o["q_index"],
                     "snapshot_id": o.get("snapshot_id") or sid}
                    for o in g["origins"] if o.get("qid")],
                "mistake_group": g, "history": None,
                "difficulty": g.get("difficulty"),
            }

        # 3) Ad-hoc (AI/PDF/mix) seen questions recovered via snapshots.
        for row in adhoc_rows:
            sid = row.get("snapshot_id")
            snap = snap_map.get(sid)
            if not snap:
                excluded += 1
                continue
            analytics = {
                k: v for k, v in (
                    ("subject", row.get("subject")),
                    ("topic", row.get("topic")),
                    ("difficulty", normalize_difficulty(row.get("difficulty"))),
                ) if v
            }
            question = {
                "question": snap.get("question", ""),
                "options": list(snap.get("options") or []),
                "correct_option_id": snap.get("correct_option_id"),
                "analytics": analytics,
            }
            # Phase F: snapshot documents may carry explanation companions
            # (absent on legacy snapshots -> honest explanation-free item).
            snap_expl = snap.get("explanation")
            if isinstance(snap_expl, str) and snap_expl.strip():
                question["explanation"] = snap_expl
            snap_detail = snap.get("explanation_detail")
            if isinstance(snap_detail, dict) and snap_detail:
                question["explanation_detail"] = snap_detail
            if not self.revision._usable(question) or not str(
                    question["question"]).strip():
                excluded += 1
                continue
            key = ("snap", sid)
            ckey = bucket_key(row.get("subject"), row.get("topic"))
            if key in candidates:
                if not candidates[key]["history"]:
                    candidates[key]["history"] = row
                continue
            if ckey not in wanted_set:
                continue
            candidates[key] = {
                "key": key, "question": question, "bucket": ckey,
                "origins": [], "mistake_group": None, "history": row,
                "difficulty": row.get("difficulty"),
            }

        all_candidates = list(candidates.values())
        for c in all_candidates:
            c["tier"] = assign_tier(c)

        per_bucket_counts = {
            k: sum(1 for c in all_candidates if c["bucket"] == k)
            for k in bucket_order
        }

        # Auto mode: the runner-up topic only joins when the top topic
        # cannot fill a session on its own (never combine gratuitously).
        chosen_order = list(bucket_order)
        if not manual and len(wanted) > 1:
            if per_bucket_counts.get(bucket_order[0], 0) >= PRACTICE_SIZE:
                chosen_order = [bucket_order[0]]
                all_candidates = [c for c in all_candidates
                                  if c["bucket"] == bucket_order[0]]

        if not all_candidates:
            return {
                "state": "no_questions", "questions": [], "size": 0,
                "buckets": [self._bucket_public(wanted[0])],
                "excluded": excluded,
            }

        picked = assemble_session(all_candidates, chosen_order, PRACTICE_SIZE)

        questions: list[dict] = []
        for c in picked:
            q = c["question"]
            # Fold provenance only for stored-mistake origins; the key is
            # the same canonical-index-aligned, quiz_persisted=False-gated
            # mechanism Phase D introduced.
            if c["origins"]:
                q["_revision_origins"] = c["origins"]
            questions.append(q)

        return {
            "state": "ready",
            "questions": questions,
            "size": len(questions),
            "limited": len(questions) < MIN_MEANINGFUL,
            "buckets": [self._bucket_public(b)
                        for b in wanted if b["key"] in chosen_order],
            "candidate_counts": per_bucket_counts,
            "excluded": excluded,
        }

    @staticmethod
    def _bucket_public(bucket: dict) -> dict:
        return {
            "subject": bucket["subject"],
            "topic": bucket["topic"],
            "label": bucket["label"],
            "accuracy_pct": bucket["accuracy_pct"],
            "answered": bucket["answered"],
            "incorrect": bucket["incorrect"],
            "reasons": bucket.get("reasons", []),
        }
