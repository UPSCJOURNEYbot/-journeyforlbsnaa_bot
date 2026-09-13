"""Phase D mistake revision: selection, Smart ranking and revision-set build.

This module adds NO new collection, NO parallel quiz engine and NO second
analytics/XP path. It reads the Phase B non-destructive ``user_mistakes``
history plus the content-addressed ``question_snapshots``, groups a user's
mistakes by *question content*, ranks/deterministically selects a bounded
revision set, and turns that set into the ordinary question structure the
existing private (DM) quiz engine already plays. Completion then flows through
the single canonical ``AnalyticsService.record_completion()`` boundary (source
``dm``), so Phase C XP/streaks work once and naturally, and revision answers are
folded back into the original mistake rows.

Everything that can be pure (content identity, grouping, Smart scoring,
selection, pagination) lives at module level and is tested without a database;
:class:`MistakeRevisionService` only performs the bounded DB reads and the
live-vs-snapshot content resolution.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from quizbot.analytics.metadata import (
    build_snapshot,
    normalize_difficulty,
    snapshot_content_hash,
)
from quizbot.analytics.repository import QuestionSnapshotRepository
from quizbot.database.repositories import MistakeRepository, QuizRepository

logger = logging.getLogger(__name__)

# Bounded revision UX (1 CPU / 2 GB VPS).
REVISION_SIZE = 10          # questions per revision session (~8-10 target)
PAGE_SIZE = 10              # mistakes shown per "All mistakes" page
RECENCY_HORIZON_DAYS = 30   # recency term fully decays after this many days

# Smart ranking weights -- repeated wrong answers dominate on purpose so a
# single wrong answer can never look like a high-priority weakness.
W_REPEAT = 100     # per extra wrong occurrence beyond the first
W_RELAPSE = 60     # currently open again after having been correct
W_OPEN = 15        # currently unresolved
W_DIFFICULTY = {"moderate": 2, "hard": 4, "extreme": 6}
RECENT_DAYS = 3    # "Recently missed" reason window

MODE_SMART = "smart"
MODE_REPEATED = "repeated"
MODE_ALL = "all"
MODE_TOPIC = "topic"
MODES = frozenset({MODE_SMART, MODE_REPEATED, MODE_ALL, MODE_TOPIC})


# ---------------------------------------------------------------------------
# Time (tolerant; mistake rows use the app's naive-UTC "%Y-%m-%d %H:%M:%S")
# ---------------------------------------------------------------------------

def to_epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    for parser in (
        lambda: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
        lambda: datetime.strptime(s, "%Y-%m-%d"),
    ):
        try:
            dt = parser()
            break
        except ValueError:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _now_epoch(now: Any = None) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(now, (int, float)):
        return float(now)
    return to_epoch(now) or datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Content identity / grouping (pure)
# ---------------------------------------------------------------------------

def content_key(row: dict) -> tuple[str, str]:
    """Logical content identity for a mistake row.

    Prefer the content-addressed snapshot so the SAME question encountered in
    different quizzes groups together; legacy rows without a snapshot fall
    back to a stable ``qid + canonical index`` identity."""
    sid = row.get("snapshot_id")
    if sid:
        return ("snap", str(sid))
    return ("q", f'{row.get("qid")}#{int(row.get("q_index", 0))}')


def _is_open(row: dict) -> bool:
    # Legacy rows have no status and are open by definition.
    return row.get("status") != MistakeRepository.STATUS_RESOLVED


def _mode_label(values: list[Optional[str]]) -> Optional[str]:
    """Deterministic most-common non-empty label; ties resolved alphabetically."""
    present = sorted({v for v in values if v})
    if not present:
        return None
    counts = Counter(v for v in values if v)
    best = max(counts.values())
    return next(v for v in present if counts[v] == best)


def group_mistakes(rows: list[dict]) -> list[dict]:
    """Collapse raw mistake rows (one per user/qid/q_index) into content
    groups (one logical question, spanning every quiz the user saw it in)."""
    by_key: dict[tuple, dict] = {}
    ordered: list[tuple] = []
    for row in rows:
        key = content_key(row)
        group = by_key.get(key)
        if group is None:
            group = {
                "key": key,
                "snapshot_id": row.get("snapshot_id"),
                "origins": [],
                "wrong_total": 0,
                "wrong_max": 0,
                "correct_total": 0,
                "open": False,
                "relapse": False,
                "first_wrong_at": None,
                "last_wrong_at": None,
                "last_correct_at": None,
            }
            by_key[key] = group
            ordered.append(key)
        wrong = int(row.get("wrong_count") or 0)
        correct = int(row.get("correct_count") or 0)
        is_open = _is_open(row)
        group["origins"].append({
            "qid": row.get("qid"),
            "q_index": int(row.get("q_index", 0)),
            "snapshot_id": row.get("snapshot_id"),
        })
        group["wrong_total"] += wrong
        group["wrong_max"] = max(group["wrong_max"], wrong)
        group["correct_total"] += correct
        group["open"] = group["open"] or is_open
        # Re-opened after a prior correct == a relapse (correct_count is never
        # reset, so an open row with any correct count was previously fixed).
        if is_open and correct >= 1:
            group["relapse"] = True
        for field, kind in (
            ("first_wrong_at", "min"),
            ("last_wrong_at", "max"),
            ("last_correct_at", "max"),
        ):
            val = row.get(field)
            if val is None:
                continue
            cur = group[field]
            group[field] = val if cur is None else (
                val if (kind == "max") == (val > cur) else cur
            )
        subjects = [row.get("subject")]
        topics = [row.get("topic")]
        diffs = [normalize_difficulty(row.get("difficulty"))]
        group.setdefault("_subject", []).extend([s for s in subjects if s])
        group.setdefault("_topic", []).extend([t for t in topics if t])
        group.setdefault("_diff", []).extend([d for d in diffs if d])

    groups = []
    for key in ordered:
        g = by_key[key]
        g["subject"] = _mode_label(g.pop("_subject"))
        g["topic"] = _mode_label(g.pop("_topic"))
        g["difficulty"] = _mode_label(g.pop("_diff"))
        # De-duplicate origins defensively (same qid/q_index repeated in input).
        uniq, seen = [], set()
        for o in g["origins"]:
            ok = (o["qid"], o["q_index"])
            if ok not in seen:
                seen.add(ok)
                uniq.append(o)
        g["origins"] = uniq
        groups.append(g)
    return groups


# ---------------------------------------------------------------------------
# Smart ranking (pure, deterministic, explainable)
# ---------------------------------------------------------------------------

def _age_days(group: dict, now_epoch: float) -> Optional[int]:
    last = to_epoch(group.get("last_wrong_at"))
    if last is None:
        return None
    return max(0, int((now_epoch - last) // 86400))


def smart_score(group: dict, now_epoch: float) -> int:
    score = W_REPEAT * max(0, group["wrong_max"] - 1)
    if group["relapse"]:
        score += W_RELAPSE
    if group["open"]:
        score += W_OPEN
    score += W_DIFFICULTY.get(normalize_difficulty(group.get("difficulty")), 0)
    age = _age_days(group, now_epoch)
    if age is not None:
        score += max(0, RECENCY_HORIZON_DAYS - age)
    return score


def smart_reasons(group: dict, now_epoch: float) -> list[str]:
    reasons: list[str] = []
    if group["relapse"]:
        reasons.append("Relapsed after correction")
    if group["wrong_max"] >= 2:
        reasons.append(f"Repeated mistake ({group['wrong_max']}\u00d7 wrong)")
    if normalize_difficulty(group.get("difficulty")) in ("hard", "extreme"):
        reasons.append("Difficult question")
    age = _age_days(group, now_epoch)
    if age is not None and age <= RECENT_DAYS:
        reasons.append("Recently missed")
    if not reasons:
        reasons.append("Missed once")
    return reasons


def _rank_key(group: dict, now_epoch: float):
    # Higher score first; then most recently wrong; then a stable content key.
    last = to_epoch(group.get("last_wrong_at"))
    return (
        -smart_score(group, now_epoch),
        -(last if last is not None else -1.0),
        group["key"][0], group["key"][1],
    )


def rank_groups(groups: list[dict], now: Any = None) -> list[dict]:
    now_epoch = _now_epoch(now)
    return sorted(groups, key=lambda g: _rank_key(g, now_epoch))


def is_repeated(group: dict) -> bool:
    """A genuine repeated/relapsed mistake. A single wrong answer is never
    considered repeated."""
    return group["wrong_max"] >= 2 or group["relapse"]


def select_groups(
    groups: list[dict], mode: str, *, size: int = REVISION_SIZE,
    topic: Optional[str] = None, now: Any = None, offset: int = 0,
) -> list[dict]:
    """Deterministic bounded selection for a revision session.

    ``offset`` (a multiple of ``size``) lets All-mode practise a specific
    page rather than always the global top slice."""
    pool = list(groups)
    if topic is not None:
        pool = [g for g in pool if g.get("topic") == topic]
    offset = max(0, int(offset))
    start = offset * size
    if mode == MODE_SMART:
        pool = [g for g in pool if g["open"]]
        return rank_groups(pool, now)[start:start + size]
    if mode == MODE_REPEATED:
        pool = [g for g in pool if g["open"] and is_repeated(g)]
        return rank_groups(pool, now)[start:start + size]
    if mode == MODE_ALL:
        # A practice set drawn from the full history, current weaknesses first.
        return rank_groups(pool, now)[start:start + size]
    if mode == MODE_TOPIC:
        pool = [g for g in pool if g["open"]]
        return rank_groups(pool, now)[start:start + size]
    raise ValueError(f"unknown revision mode: {mode!r}")


def paginate(groups: list[dict], page: int, page_size: int = PAGE_SIZE) -> list[dict]:
    page = max(0, int(page))
    start = page * page_size
    return groups[start:start + page_size]


def page_count(n_items: int, page_size: int = PAGE_SIZE) -> int:
    if n_items <= 0:
        return 0
    return (n_items - 1) // page_size + 1


# ---------------------------------------------------------------------------
# Revision-set assembly (DB-backed, bounded, edit/delete safe)
# ---------------------------------------------------------------------------

class MistakeRevisionService:
    """Bounded reads + live/snapshot content resolution for /mistakes."""

    def __init__(self, db: Any = None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.mistakes = MistakeRepository(db)
        self.quizzes = QuizRepository(db)
        self.snapshots = QuestionSnapshotRepository(db)

    # -- reads -------------------------------------------------------------

    async def overview(self, user_id: int) -> dict:
        """Counts used to render the menu and decide which modes have data."""
        user_id = self._require_user(user_id)
        rows = await self.mistakes.query_revision_rows(
            user_id, limit=MistakeRepository.REVISION_CANDIDATE_CAP)
        groups = group_mistakes(rows)
        open_groups = [g for g in groups if g["open"]]
        repeated = [g for g in open_groups if is_repeated(g)]
        topics = await self.mistakes.list_mistake_topics(user_id)
        totals = await self.mistakes.totals(user_id)
        return {
            "user_id": user_id,
            "total_mistakes": totals["total"],
            "open_mistakes": totals["open"],
            "resolved_mistakes": totals["resolved"],
            "content_groups": len(groups),
            "open_groups": open_groups,
            "repeated_groups": repeated,
            "groups": groups,
            "topics": topics,
        }

    async def topics(self, user_id: int) -> list[dict]:
        return await self.mistakes.list_mistake_topics(user_id)

    async def browse_all(
        self, user_id: int, page: int = 0, now: Any = None,
    ) -> dict:
        """One bounded, deterministic page of content groups for All mode."""
        rows = await self.mistakes.query_revision_rows(
            user_id, limit=MistakeRepository.REVISION_CANDIDATE_CAP)
        groups = rank_groups(group_mistakes(rows), now)
        total = len(groups)
        page = max(0, int(page))
        items = paginate(groups, page)
        # Resolve snapshots only for the <=PAGE_SIZE items on this page.
        resolved = await self._resolve_summaries(items)
        return {
            "items": resolved, "page": page,
            "pages": page_count(total), "total": total,
        }

    async def build_revision(
        self, user_id: int, mode: str, *, topic: Optional[str] = None,
        size: int = REVISION_SIZE, now: Any = None, page: Optional[int] = None,
    ) -> dict:
        """Return a ready-to-play, bounded revision set plus the fold map.

        ``questions`` is the canonical-order list the DM engine plays;
        ``origins_by_index`` aligns 1:1 with canonical question index and is
        passed to record_completion so results fold back to origin rows even
        after option shuffling.
        """
        if mode not in MODES:
            raise ValueError(f"unknown revision mode: {mode!r}")
        user_id = self._require_user(user_id)
        rows = await self.mistakes.query_revision_rows(
            user_id, limit=MistakeRepository.REVISION_CANDIDATE_CAP)
        groups = group_mistakes(rows)
        selected = select_groups(
            groups, mode, size=size, topic=topic, now=now,
            offset=page or 0,
        )

        questions, origins_by_index, reasons, excluded = [], [], [], 0
        # Bounded live-quiz cache (at most `size` distinct stored quizzes).
        quiz_cache: dict[str, Optional[dict]] = {}

        # Pre-fetch every immutable snapshot the selected groups might need in
        # ONE bounded $in (no per-question N+1).
        snap_ids = {g["snapshot_id"] for g in selected if g.get("snapshot_id")}
        snap_map = await self.snapshots.get_many(snap_ids) if snap_ids else {}

        for g in selected:
            built = await self._build_question(g, quiz_cache, snap_map)
            if built is None:
                excluded += 1
                continue
            question, meta = built
            questions.append(question)
            origins_by_index.append([
                {"qid": o["qid"], "q_index": o["q_index"],
                 "snapshot_id": o.get("snapshot_id") or g.get("snapshot_id")}
                for o in g["origins"] if o.get("qid")
            ])
            reasons.append({
                "content_key": list(g["key"]),
                "reasons": smart_reasons(g, _now_epoch(now)),
                "topic": g.get("topic"),
                "difficulty": g.get("difficulty"),
            })

        return {
            "user_id": user_id, "mode": mode, "topic": topic, "page": page,
            "questions": questions, "origins_by_index": origins_by_index,
            "reasons": reasons, "excluded": excluded,
            "selected": len(selected), "size": len(questions),
        }

    # -- content resolution -------------------------------------------------

    @staticmethod
    def _require_user(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("revision requires a stable integer user_id")
        return user_id

    async def _get_quiz(self, qid: Optional[str], cache: dict) -> Optional[dict]:
        if not qid:
            return None
        if qid not in cache:
            try:
                cache[qid] = await self.quizzes.get(qid)
            except Exception:  # deleted/inaccessible quiz -> None
                cache[qid] = None
        return cache[qid]

    @staticmethod
    def _usable(question: Optional[dict]) -> bool:
        if not isinstance(question, dict):
            return False
        options = question.get("options")
        correct = question.get("correct_option_id")
        if not isinstance(options, list) or not options:
            return False
        if isinstance(correct, list):
            return bool(correct) and all(
                isinstance(c, int) and not isinstance(c, bool) for c in correct)
        return isinstance(correct, int) and not isinstance(correct, bool)

    def _metadata(self, group: dict) -> dict:
        meta = {}
        if group.get("subject"):
            meta["subject"] = group["subject"]
        if group.get("topic"):
            meta["topic"] = group["topic"]
        diff = normalize_difficulty(group.get("difficulty"))
        if diff:
            meta["difficulty"] = diff
        return meta

    async def _build_question(
        self, group: dict, quiz_cache: dict, snap_map: dict,
    ) -> Optional[tuple[dict, dict]]:
        """Prefer unchanged LIVE content (richer explanation/media); fall back
        to the immutable snapshot when the quiz was edited/deleted; exclude
        (never fabricate) when neither is usable."""
        target_sid = group.get("snapshot_id")
        # Origins are deterministic by qid/q_index, so resolution is stable.
        for origin in sorted(group["origins"], key=lambda o: (str(o["qid"]), o["q_index"])):
            quiz = await self._get_quiz(origin.get("qid"), quiz_cache)
            if not quiz:
                continue
            questions = quiz.get("questions")
            idx = origin["q_index"]
            if not isinstance(questions, list) or not (0 <= idx < len(questions)):
                continue
            live = questions[idx]
            if not self._usable(live):
                continue
            if target_sid:
                live_hash = snapshot_content_hash(build_snapshot(live))
                if live_hash != target_sid:
                    # Edited in place -> the live question is a DIFFERENT
                    # question; do not show it for this historical mistake.
                    continue
            question = self._from_live(live, group)
            if self._usable(question):
                return question, {"source": "live", "qid": origin.get("qid")}

        # Immutable snapshot fallback (survives edit AND deletion).
        if target_sid and target_sid in snap_map:
            snap = snap_map[target_sid]
            question = self._from_snapshot(snap, group)
            if self._usable(question):
                return question, {"source": "snapshot", "snapshot_id": target_sid}
        return None

    def _from_live(self, live: dict, group: dict) -> dict:
        q = {
            "question": live.get("question", ""),
            "options": list(live.get("options") or []),
            "correct_option_id": live.get("correct_option_id"),
        }
        # Rich fields only exist on the live (stored) question.
        for optional in ("explanation", "file_id", "reply_text"):
            if live.get(optional):
                q[optional] = live[optional]
        meta = self._metadata(group)
        live_analytics = live.get("analytics")
        if isinstance(live_analytics, dict) and live_analytics:
            # The stored question's own metadata is authoritative; fill only
            # blanks from the mistake history. Never overwrite/fabricate.
            for k in ("subject", "topic", "subtopic", "difficulty"):
                meta.setdefault(k, live_analytics.get(k))
        meta = {k: v for k, v in meta.items() if v}
        if meta:
            q["analytics"] = meta
        return q

    def _from_snapshot(self, snap: dict, group: dict) -> dict:
        q = {
            "question": snap.get("question", ""),
            "options": list(snap.get("options") or []),
            "correct_option_id": snap.get("correct_option_id"),
        }
        meta = self._metadata(group)
        if meta:
            q["analytics"] = meta
        return q

    async def _resolve_summaries(self, items: list[dict]) -> list[dict]:
        """Lightweight display rows for the All-mode page (text only, bounded
        snapshot $in, no media/explanation)."""
        snap_ids = {g["snapshot_id"] for g in items if g.get("snapshot_id")}
        snap_map = await self.snapshots.get_many(snap_ids) if snap_ids else {}
        out = []
        for g in items:
            text = None
            sid = g.get("snapshot_id")
            if sid and sid in snap_map:
                text = snap_map[sid].get("question")
            out.append({
                "content_key": list(g["key"]),
                "question": text, "topic": g.get("topic"),
                "subject": g.get("subject"),
                "difficulty": normalize_difficulty(g.get("difficulty")),
                "wrong": g["wrong_max"], "open": g["open"],
                "relapse": g["relapse"],
                "last_wrong_at": g.get("last_wrong_at"),
            })
        return out
