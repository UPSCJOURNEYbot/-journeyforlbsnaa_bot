"""Phase H: the bounded question bank behind ``/pyq`` and ``/buildtest``.

Both commands answer the same question -- *"give me N questions matching a
filter"* -- so they share ONE bounded, access-scoped bank reader instead of
two ad-hoc scanners:

* **access-scoped.** The bank only ever contains quizzes the caller may play:
  quizzes they created, quizzes they have already played (a completed attempt
  is proof of access), and public/free quizzes. A paid quiz the user never
  played is never read, so a DM practice session can never launder paid
  content.
* **bounded.** At most :data:`BANK_QUIZ_CAP` quiz documents are fetched and at
  most :data:`BANK_QUESTION_CAP` questions are considered, on the 1 CPU /
  2 GB VPS budget (same discipline as Phase D/E).
* **content-addressed.** Questions are de-duplicated by their Phase B
  content hash (``snapshot_content_hash``), so the same question appearing in
  five test-series imports is ONE practice item.
* **nothing fabricated.** Every returned question is an existing stored
  question with its own options/answer; a filter that matches nothing returns
  an empty list plus an explicit reason for the caller to show.

PYQ tagging
-----------
``/pyq`` needs a *year* per question. Two honest sources exist, in order:

1. explicit metadata -- ``question["analytics"]["year"]`` (or
   ``pyq_year`` / a question-level ``year``);
2. an unambiguous year marker inside the question text -- ``(2023)``,
   ``UPSC 2021``, ``Prelims 2019``, ``PYQ 2018``.

A year is never inferred from a bare number: ``2023`` alone could be part of a
date in the stem. Untagged questions stay untagged and are reported as such.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any, Optional

from quizbot.analytics import mistake_revision as mr
from quizbot.analytics.gamification import local_day_key
from quizbot.analytics.metadata import (
    build_snapshot,
    extract_question_analytics,
    normalize_difficulty,
    normalize_label,
    snapshot_content_hash,
)
from quizbot.database.repositories import (
    AttemptRepository,
    MistakeRepository,
    QuizRepository,
)
from quizbot.analytics.repository import QuestionEventRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds (never unbounded work per command)
# ---------------------------------------------------------------------------

BANK_QUIZ_CAP = 40          # quiz documents fetched per command
BANK_QUESTION_CAP = 400     # stored questions considered per command
OWNED_QID_CAP = 25
PLAYED_QID_CAP = 25
PUBLIC_QID_CAP = 25
SEEN_EVENT_CAP = 300        # per-question history rows read for "seen" ids

MIN_YEAR = 1979             # UPSC CSE (GS) papers start here
MAX_YEAR_LOOKAHEAD = 0      # a "previous year" can never be in the future

SESSION_SIZES = (10, 20, 30, 50)
DEFAULT_SIZE = 20
MAX_SIZE = 50

#: Filter values the UI offers; anything else is rejected, not guessed.
DIFFICULTIES = ("easy", "moderate", "hard", "extreme")

SCOPE_BANK = "bank"
SCOPE_UNSEEN = "unseen"
SCOPE_MISTAKES = "mistakes"
SCOPE_DUE = "due"
SCOPE_WEAK = "weak"
SCOPES = (SCOPE_BANK, SCOPE_UNSEEN, SCOPE_MISTAKES, SCOPE_DUE, SCOPE_WEAK)

#: A question text marker is only trusted with an explicit exam/PYQ context.
_YEAR = r"(19\d{2}|20\d{2})"
_TEXT_YEAR_PATTERNS = (
    re.compile(rf"\b(?:pyq|upsc|cse|prelims|mains|paper|ias)\b[^0-9]{{0,24}}\b{_YEAR}\b",
               re.IGNORECASE),
    re.compile(rf"\b{_YEAR}\b[^0-9]{{0,16}}\b(?:pyq|upsc|cse|prelims|mains|paper|ias)\b",
               re.IGNORECASE),
    re.compile(rf"[\(\[]\s*{_YEAR}\s*[\)\]]"),
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _as_year(value: Any, max_year: int) -> Optional[int]:
    """Coerce a metadata value to a plausible PYQ year (or None)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        year = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    if MIN_YEAR <= year <= max_year:
        return year
    return None


def extract_pyq_year(
    question: Optional[dict], *, max_year: Optional[int] = None,
) -> tuple[Optional[int], Optional[str]]:
    """``(year, source)`` for one stored question; ``(None, None)`` when the
    question carries no trustworthy year."""
    if max_year is None:
        max_year = int(local_day_key()[:4]) + MAX_YEAR_LOOKAHEAD
    if not isinstance(question, dict):
        return None, None

    # Explicit metadata wins: the nested `analytics` block first (its year
    # keys are NOT part of the whitelisted Phase B label set, hence read
    # directly), then tolerated flat question-level keys.
    analytics = question.get("analytics")
    for container in (analytics if isinstance(analytics, dict) else None, question):
        if not isinstance(container, dict):
            continue
        for key in ("year", "pyq_year", "exam_year", "asked_year"):
            year = _as_year(container.get(key), max_year)
            if year is not None:
                return year, "metadata"

    text = question.get("question")
    if isinstance(text, str) and text:
        for pattern in _TEXT_YEAR_PATTERNS:
            match = pattern.search(text)
            if match:
                year = _as_year(match.group(1), max_year)
                if year is not None:
                    return year, "text"
    return None, None


def question_identity(
    qid: Optional[str], q_index: Any, question: dict,
) -> str:
    """Stable content identity: the Phase B content hash when the question is
    well-formed, else ``qid#index`` (never a fabricated hash)."""
    snapshot = build_snapshot(question)
    digest = snapshot_content_hash(snapshot) if snapshot else None
    if digest:
        return f"snap:{digest}"
    return f"q:{qid}#{int(q_index) if isinstance(q_index, int) else 0}"


def _topic_meta(question: dict) -> dict:
    meta = extract_question_analytics(question)
    out: dict[str, Any] = {}
    for key in ("subject", "topic", "subtopic"):
        label = normalize_label(meta.get(key))
        if label:
            out[key] = label
    difficulty = normalize_difficulty(meta.get("difficulty"))
    if difficulty:
        out["difficulty"] = difficulty
    return out


def bank_row(qid: str, quiz_name: Optional[str], q_index: int,
             question: Any, *, max_year: Optional[int] = None) -> Optional[dict]:
    """Normalise one stored question into a bank row, or None when it cannot
    be played (no options / no answer) -- unusable questions are skipped, never
    silently repaired."""
    if not mr.MistakeRevisionService._usable(question):
        return None
    options = [str(o) for o in question.get("options") or []]
    row = {
        "qid": qid,
        "quiz_name": quiz_name,
        "q_index": int(q_index),
        "question": question.get("question") or "",
        "options": options,
        "correct_option_id": question.get("correct_option_id"),
        "identity": question_identity(qid, q_index, question),
        "meta": _topic_meta(question),
    }
    year, source = extract_pyq_year(question, max_year=max_year)
    if year is not None:
        row["year"] = year
        row["year_source"] = source
    for optional in ("explanation", "explanation_detail", "file_id", "reply_text"):
        if question.get(optional):
            row[optional] = question[optional]
    return row


def dedupe_by_content(rows: list[dict]) -> list[dict]:
    """First occurrence wins, order preserved (deterministic)."""
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        identity = row.get("identity")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        out.append(row)
    return out


def filter_pool(
    pool: list[dict], *,
    years: Optional[list[int]] = None,
    topics: Optional[list[str]] = None,
    subject: Optional[str] = None,
    difficulty: Optional[str] = None,
    unseen_only: bool = False,
    seen: Optional[set[str]] = None,
) -> list[dict]:
    """Apply the caller's filters. Unknown filter values simply match nothing
    (the caller reports an honest empty state)."""
    seen = seen or set()
    wanted_years = {int(y) for y in (years or [])}
    wanted_topics = {normalize_label(t) for t in (topics or []) if normalize_label(t)}
    wanted_subject = normalize_label(subject)
    wanted_difficulty = normalize_difficulty(difficulty)

    out: list[dict] = []
    for row in pool:
        meta = row.get("meta") or {}
        if wanted_years and int(row.get("year") or 0) not in wanted_years:
            continue
        if wanted_topics and normalize_label(meta.get("topic")) not in wanted_topics:
            continue
        if wanted_subject and normalize_label(meta.get("subject")) != wanted_subject:
            continue
        if wanted_difficulty and normalize_difficulty(meta.get("difficulty")) != wanted_difficulty:
            continue
        if unseen_only and row.get("identity") in seen:
            continue
        out.append(row)
    return out


def select_questions(
    pool: list[dict], size: int, *, seed: Any = 0,
    prefer_unseen: bool = True, seen: Optional[set[str]] = None,
) -> list[dict]:
    """Deterministic, bounded selection.

    Unseen questions are preferred when available; if the pool is smaller than
    ``size`` the caller gets everything it has (never padded, never repeated).
    """
    size = max(1, min(MAX_SIZE, int(size or DEFAULT_SIZE)))
    seen = seen or set()
    ordered = sorted(pool, key=lambda r: (str(r.get("identity", "")),))
    rng = random.Random(str(seed))
    if prefer_unseen:
        fresh = [r for r in ordered if r.get("identity") not in seen]
        stale = [r for r in ordered if r.get("identity") in seen]
        rng.shuffle(fresh)
        rng.shuffle(stale)
        return (fresh + stale)[:size]
    shuffled = list(ordered)
    rng.shuffle(shuffled)
    return shuffled[:size]


def available_years(pool: list[dict]) -> list[int]:
    """Descending distinct years present in the pool (newest first)."""
    return sorted({int(r["year"]) for r in pool if r.get("year")}, reverse=True)


def topic_index(pool: list[dict], limit: int = 12) -> list[dict]:
    """``[{subject, topic, count}]`` ordered by count then label (bounded)."""
    counts: dict[tuple, int] = {}
    for row in pool:
        meta = row.get("meta") or {}
        topic = meta.get("topic")
        if not topic:
            continue
        key = (meta.get("subject"), topic)
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0][0] or ""), kv[0][1]))
    return [
        {"subject": subject, "topic": topic, "count": count}
        for (subject, topic), count in ordered[:limit]
    ]


def difficulty_index(pool: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in pool:
        difficulty = normalize_difficulty((row.get("meta") or {}).get("difficulty"))
        if difficulty:
            counts[difficulty] = counts.get(difficulty, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class QuestionBankService:
    """Bounded, access-scoped reader over the stored question bank."""

    def __init__(self, db: Any = None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.quizzes = QuizRepository(db)
        self.attempts = AttemptRepository(db)
        self.events = QuestionEventRepository(db)
        self.revision = mr.MistakeRevisionService(db)

    @staticmethod
    def _require_user(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("question bank requires a stable integer user_id")
        return user_id

    # -- discovery ---------------------------------------------------------

    async def candidate_qids(self, user_id: int) -> dict:
        """Bounded, de-duplicated qid sets the user may practise from."""
        user_id = self._require_user(user_id)
        owned = await self.quizzes.list_qids_by_creator(user_id, limit=OWNED_QID_CAP)

        played: list[str] = []
        try:
            rows = await self.attempts.list_completed_for_user(
                user_id, limit=PLAYED_QID_CAP, offset=0)
            played = [r.get("qid") for r in rows or [] if r.get("qid")]
        except Exception:
            logger.exception("bank: played-quiz lookup failed for user=%s", user_id)

        public: list[str] = []
        try:
            light = await self.quizzes.list_all(limit=PUBLIC_QID_CAP)
            public = [
                r.get("qid") for r in light
                if r.get("qid") and r.get("quiz_type") != "paid"
            ]
        except Exception:
            logger.exception("bank: public listing failed")

        ordered: list[str] = []
        for qid in [*owned, *played, *public]:
            if qid and qid not in ordered:
                ordered.append(qid)
        return {
            "owned": len(owned), "played": len(played), "public": len(public),
            "qids": ordered[:BANK_QUIZ_CAP],
        }

    async def load_pool(self, user_id: int) -> dict:
        """Bounded question pool for this user (no N+1: one fetch per quiz)."""
        user_id = self._require_user(user_id)
        discovery = await self.candidate_qids(user_id)
        pool: list[dict] = []
        quizzes_scanned = 0
        truncated = False
        for qid in discovery["qids"]:
            try:
                quiz = await self.quizzes.get(qid)
            except Exception:
                continue
            if not quiz or not isinstance(quiz.get("questions"), list):
                continue
            quizzes_scanned += 1
            for index, question in enumerate(quiz["questions"]):
                if len(pool) >= BANK_QUESTION_CAP:
                    truncated = True
                    break
                row = bank_row(qid, quiz.get("quiz_name"), index, question)
                if row is not None:
                    pool.append(row)
            if truncated:
                break
        pool = dedupe_by_content(pool)
        return {
            "user_id": user_id,
            "pool": pool,
            "quizzes_scanned": quizzes_scanned,
            "truncated": truncated,
        }

    async def seen_identities(self, user_id: int, pool: list[dict],
                              limit: int = SEEN_EVENT_CAP) -> set[str]:
        """Content identities this user has already answered (bounded).

        Snapshot ids from canonical question events AND Phase D mistake rows
        are accepted, plus the ``qid#index`` fallback identity, so "unseen"
        stays truthful on legacy data.
        """
        user_id = self._require_user(user_id)
        seen: set[str] = set()
        try:
            rows = await self.events.get_question_performance(user_id, limit=limit)
        except Exception:
            logger.exception("bank: question history lookup failed for user=%s", user_id)
            rows = []
        for row in rows or []:
            if row.get("snapshot_id"):
                seen.add(f"snap:{row['snapshot_id']}")
            if row.get("qid") is not None and row.get("question_index") is not None:
                seen.add(f"q:{row['qid']}#{int(row['question_index'])}")
        # Mistakes always count as seen, even if their event rows are gone.
        try:
            mistake_rows = await self.revision.mistakes.query_revision_rows(
                user_id, limit=MistakeRepository.REVISION_CANDIDATE_CAP)
        except Exception:
            mistake_rows = []
        for row in mistake_rows or []:
            if row.get("snapshot_id"):
                seen.add(f"snap:{row['snapshot_id']}")
            if row.get("qid") is not None and row.get("q_index") is not None:
                seen.add(f"q:{row['qid']}#{int(row['q_index'])}")
        # A stored question may carry the content-hash identity while history
        # only knows it as (qid, index); bridge the two so "unseen" stays true.
        for row in pool or []:
            if f"q:{row.get('qid')}#{row.get('q_index')}" in seen:
                seen.add(row.get("identity"))
        return seen

    async def index(self, user_id: int, now: Any = None) -> dict:
        """Bank summary used by both commands' cards."""
        data = await self.load_pool(user_id)
        pool = data["pool"]
        seen = await self.seen_identities(user_id, pool)
        unseen = [r for r in pool if r.get("identity") not in seen]
        return {
            "user_id": user_id,
            "today": local_day_key(now),
            "questions": len(pool),
            "quizzes_scanned": data["quizzes_scanned"],
            "truncated": data["truncated"],
            "unseen": len(unseen),
            "years": available_years(pool),
            "year_counts": {
                str(y): sum(1 for r in pool if r.get("year") == y)
                for y in available_years(pool)
            },
            "untagged": sum(1 for r in pool if not r.get("year")),
            "topics": topic_index(pool),
            "difficulties": difficulty_index(pool),
            "pool": pool,
            "seen": seen,
        }

    # -- builders ----------------------------------------------------------

    async def build_pyq(
        self, user_id: int, year: Optional[int] = None, *,
        size: int = DEFAULT_SIZE, topics: Optional[list[str]] = None,
        unseen_only: bool = False, difficulty: Optional[str] = None,
        seed: Any = None, now: Any = None,
    ) -> dict:
        """Build a PYQ practice set (a year is optional: all tagged years)."""
        data = await self.index(user_id, now)
        pool = data["pool"]
        # A PYQ set is made of TAGGED questions only: with no explicit year we
        # use every year the bank actually carries (never the untagged rest).
        if year is None and not data["years"]:
            return {
                "user_id": user_id, "year": None, "questions": [], "rows": [],
                "size": 0, "pool_size": len(pool), "available": 0,
                "reason": "no_pyq", "available_years": [],
                "tagged": 0, "index": data,
            }
        years = [int(year)] if year else list(data["years"])
        if year is not None and int(year) not in data["years"]:
            return {
                "user_id": user_id, "year": year, "questions": [],
                "rows": [], "size": 0, "pool_size": len(pool), "available": 0,
                "reason": "no_year", "available_years": data["years"],
                "index": data,
            }
        filtered = filter_pool(
            pool, years=years, topics=topics, difficulty=difficulty,
            unseen_only=unseen_only, seen=data["seen"],
        )
        chosen = select_questions(
            filtered, size, seed=seed if seed is not None else (year or "all"),
            prefer_unseen=True, seen=data["seen"],
        )
        return {
            "user_id": user_id,
            "year": year,
            "questions": [self._playable(row) for row in chosen],
            "rows": chosen,
            "size": len(chosen),
            "available": len(filtered),
            "pool_size": len(pool),
            "tagged": sum(1 for r in pool if r.get("year")),
            "available_years": data["years"],
            "reason": None if chosen else (
                "no_pyq" if not data["years"] else "no_match"
            ),
            "index": data,
        }

    async def build_test(self, user_id: int, spec: dict, now: Any = None) -> dict:
        """Assemble a custom test from the chosen source.

        ``scope``:
          * ``bank``     -- filtered bank questions (the default);
          * ``unseen``   -- bank questions this user has never answered;
          * ``mistakes`` -- Phase D Smart revision selection (fold-back safe);
          * ``due``      -- Phase H SRS due queue;
          * ``weak``     -- bank questions inside this user's weak buckets.
        """
        user_id = self._require_user(user_id)
        scope = str(spec.get("scope") or SCOPE_BANK).lower()
        if scope not in SCOPES:
            raise ValueError(f"unknown test scope: {scope!r}")
        size = max(1, min(MAX_SIZE, int(spec.get("size") or DEFAULT_SIZE)))
        topics = [str(t) for t in (spec.get("topics") or []) if str(t).strip()]
        difficulty = spec.get("difficulty")
        year = spec.get("year")
        seed = spec.get("seed")
        if seed is None:
            seed = f"{user_id}:{scope}:{size}:{','.join(sorted(topics))}:{difficulty}:{year}"

        base = {
            "user_id": user_id, "scope": scope, "size": 0,
            "questions": [], "rows": [], "origins_by_index": [],
            "available": 0, "pool_size": 0, "reason": None,
            "topics": topics, "difficulty": difficulty, "year": year,
        }

        # Sources that already own a resolution path reuse it verbatim, so the
        # fold-back provenance (mistakes) and due-ness (SRS) are preserved.
        if scope == SCOPE_MISTAKES:
            built = await self.revision.build_revision(
                user_id, mr.MODE_SMART, size=size, now=now)
            base.update({
                "questions": built["questions"],
                "origins_by_index": built["origins_by_index"],
                "size": built["size"], "available": built["selected"],
                "reason": None if built["size"] else "no_mistakes",
            })
            return base

        if scope == SCOPE_DUE:
            from quizbot.analytics.srs import SrsService
            built = await SrsService(self.db).build_revision(
                user_id, now, size=size)
            base.update({
                "questions": built["questions"],
                "origins_by_index": built["origins_by_index"],
                "size": built["size"], "available": built["due_count"],
                "reason": None if built["size"] else "nothing_due",
            })
            return base

        data = await self.index(user_id, now)
        pool = data["pool"]
        base["pool_size"] = len(pool)
        filter_topics = list(topics)
        if scope == SCOPE_WEAK and not filter_topics:
            filter_topics = await self._weak_topics(user_id)
        filtered = filter_pool(
            pool,
            years=[int(year)] if year else [],
            topics=filter_topics or None,
            difficulty=difficulty,
            unseen_only=(scope == SCOPE_UNSEEN
                         or (scope == SCOPE_BANK and bool(spec.get("unseen_only")))),
            seen=data["seen"],
        )
        chosen = select_questions(
            filtered, size, seed=seed, prefer_unseen=True, seen=data["seen"])
        base.update({
            "questions": [self._playable(row) for row in chosen],
            "rows": chosen,
            "size": len(chosen),
            "available": len(filtered),
            "reason": None if chosen else (
                "no_bank" if not pool else "no_match"
            ),
        })
        return base

    async def _weak_topics(self, user_id: int) -> list[str]:
        """This user's eligible weak topics (Phase E model; no new authority)."""
        try:
            from quizbot.analytics.weak_practice import WeakPracticeService
            buckets = await WeakPracticeService(self.db).weak_buckets(user_id)
        except Exception:
            logger.exception("bank: weak-bucket lookup failed for user=%s", user_id)
            return []
        return [b["topic"] for b in buckets.get("eligible", []) if b.get("topic")]

    @staticmethod
    def _playable(row: dict) -> dict:
        """Bank row -> the canonical stored-question shape the DM engine plays."""
        question = {
            "question": row.get("question", ""),
            "options": list(row.get("options") or []),
            "correct_option_id": row.get("correct_option_id"),
        }
        for optional in ("explanation", "explanation_detail", "file_id", "reply_text"):
            if row.get(optional):
                question[optional] = row[optional]
        meta = dict(row.get("meta") or {})
        if row.get("year"):
            meta.setdefault("year", int(row["year"]))
        meta = {k: v for k, v in meta.items() if v}
        if meta:
            question["analytics"] = meta
        return question
