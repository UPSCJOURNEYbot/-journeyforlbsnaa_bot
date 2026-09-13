"""Canonical analytics service (Phase B).

One write path for every quiz completion boundary (group, DM, Mini App,
scheduled, ad-hoc) and the single read surface the future learner features
will build on. Rules enforced here:

* users are keyed by stable Telegram ``user_id`` only; EVERY read requires
  it and never crosses users;
* correctness is taken from the live scorer's already-resolved question
  results -- this service never re-scores stored answers;
* skipped is a distinct outcome, never "wrong";
* in-progress attempts never count as completed;
* saved vs ad-hoc quizzes are distinguished via ``quiz_persisted``; only
  saved quizzes touch leaderboard / question stats / participant counters /
  mistake rows tied to a stored qid;
* all writes are idempotent, so double callbacks and backfill re-runs cannot
  duplicate data;
* nothing about unknown history is invented.
"""

from __future__ import annotations

import logging
from typing import Optional

from quizbot.analytics import aggregation
from quizbot.analytics.gamification import GamificationService
from quizbot.analytics.metadata import (
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
    OUTCOME_SKIPPED,
    build_snapshot,
    resolve_metadata,
)
from quizbot.analytics.repository import (
    QuestionEventRepository,
    QuestionSnapshotRepository,
)
from quizbot.database.repositories import (
    AttemptRepository,
    MistakeRepository,
    QuestionStatsRepository,
    QuizRepository,
    _now_iso,
)

logger = logging.getLogger(__name__)

# Source vocabulary (provenance only -- never used for access control).
SOURCE_GROUP = "group"
SOURCE_DM = "dm"
SOURCE_MINIAPP = "miniapp"
SOURCE_SCHEDULED = "scheduled"
SOURCE_AI_QUIZ = "aiquiz"
SOURCE_PDF_QUIZ = "pdfquiz"
SOURCE_MIX = "mix"
SOURCE_BACKFILL = "backfill"


class AnalyticsService:
    def __init__(self, db=None) -> None:
        if db is None:
            from quizbot.database.db import get_db
            db = get_db()
        self.db = db
        self.events = QuestionEventRepository(db)
        self.snapshots = QuestionSnapshotRepository(db)
        self.attempts = AttemptRepository(db)
        self.mistakes = MistakeRepository(db)
        self.question_stats = QuestionStatsRepository(db)
        self.quizzes = QuizRepository(db)
        # Phase C: XP / levels / streaks. Hooks into this same canonical
        # completion path; it never opens a second analytics pipeline.
        self.gamification = GamificationService(db)

    # ------------------------------------------------------------------ writes

    def _require_user(self, user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("analytics methods require a stable integer user_id")
        return user_id

    @staticmethod
    def _normalize_results(question_results) -> list[dict]:
        """Fail-soft input hygiene at the canonical result boundary.

        A single malformed row (non-dict, missing/non-integral ``q_index``)
        must never abort the whole completion/result/XP pipeline. Such rows
        are dropped; everything else is copied with ``q_index`` coerced to a
        non-negative int. Unknown *outcomes* are preserved here and filtered
        by the downstream consumers (events/mistakes only accept answered
        outcomes), matching prior behaviour for valid payloads.
        """

        def _coerce_index(value):
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                s = value.strip()
                if s.lstrip("-").isdigit():
                    return int(s)
            return None

        clean: list[dict] = []
        for row in question_results or []:
            if not isinstance(row, dict):
                continue
            idx = _coerce_index(row.get("q_index"))
            if idx is None or idx < 0:
                continue
            nr = dict(row)
            nr["q_index"] = idx
            clean.append(nr)
        return clean

    def _prepare_snapshots(
        self, question_results: list[dict], questions: Optional[list[dict]]
    ) -> tuple[dict[int, str], list[dict]]:
        """Build + content-de-duplicate minimal snapshots for this
        completion. Question text lives once in ``question_snapshots`` keyed
        by content hash; events/mistakes only carry the small id, keeping
        per-event rows bounded even when question text is long. Returns
        ``(q_index -> snapshot_id, distinct snapshot contents)``.
        """
        from quizbot.analytics.metadata import snapshot_content_hash

        ids_by_index: dict[int, str] = {}
        by_hash: dict[str, dict] = {}
        for qr in question_results or []:
            try:
                q_index = int(qr["q_index"])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(questions, list) and 0 <= q_index < len(questions):
                snapshot = build_snapshot(questions[q_index])
                sid = snapshot_content_hash(snapshot)
                if sid:
                    ids_by_index[q_index] = sid
                    by_hash[sid] = snapshot
        return ids_by_index, list(by_hash.values())

    def _enrich(
        self,
        *,
        qid: str,
        quiz_name: str,
        source: str,
        quiz_persisted: bool,
        question_results: list[dict],
        questions: Optional[list[dict]],
        sections: Optional[list[dict]],
        at: str,
        snapshot_ids: Optional[dict[int, str]] = None,
    ) -> list[dict]:
        snapshot_ids = snapshot_ids or {}
        enriched: list[dict] = []
        for qr in question_results or []:
            q_index = int(qr["q_index"])
            outcome = qr.get("outcome")
            if outcome not in (OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED):
                continue
            question = None
            if isinstance(questions, list) and 0 <= q_index < len(questions):
                question = questions[q_index]
            subject, topic, subtopic, difficulty, topic_source = resolve_metadata(
                question, sections, q_index
            )
            enriched.append({
                "qid": qid,
                "quiz_name": quiz_name,
                "source": source,
                "quiz_persisted": bool(quiz_persisted),
                "question_index": q_index,
                "selected_option": list(qr.get("selected") or []),
                "correct_option": list(qr.get("correct_option") or []),
                "outcome": outcome,
                "time_taken": qr.get("time_taken"),
                # Timing is recorded ONLY where the live runtime genuinely
                # measured it; skipped questions have no answered_at.
                "answered_at": at if outcome != OUTCOME_SKIPPED else None,
                "created_at": at,
                "subject": subject,
                "topic": topic,
                "subtopic": subtopic,
                "difficulty": difficulty,
                "topic_source": topic_source,
                # Compact reference; content is deduped in question_snapshots.
                "snapshot_id": snapshot_ids.get(q_index),
            })
        return enriched

    async def record_completion(
        self,
        *,
        user_id: int,
        attempt_id: str,
        qid: str,
        quiz_name: str,
        question_results: list[dict],
        source: str,
        quiz_persisted: bool,
        questions: Optional[list[dict]] = None,
        sections: Optional[list[dict]] = None,
        score: float = 0,
        correct: Optional[int] = None,
        wrong: Optional[int] = None,
        total_time: float = 0,
        current_question: Optional[int] = None,
        username: Optional[str] = None,
        finalize: bool = True,
        apply_mistakes: bool = True,
        backfilled: bool = False,
        at: Optional[str] = None,
        revision_origins: Optional[list] = None,
    ) -> dict:
        """Record one user's finished quiz at the result boundary.

        ``finalize=False`` is used only by the backfill script for attempts
        that were already completed historically (it must not overwrite
        status/time or re-post leaderboard rows / participant counters).

        ``revision_origins`` (Phase D) is set for /mistakes revision sessions:
        a list aligned with canonical question index, each entry a list of
        origin ``{qid, q_index, snapshot_id}`` the revision question stands
        for. Results are additionally folded back into those origin mistake
        rows (idempotent under this ``attempt_id``); it is not used for normal
        quizzes.
        """
        user_id = self._require_user(user_id)
        at = at or _now_iso()

        # Fail-soft hygiene: one malformed result row must never abort the
        # completion / result / HTML / PDF / XP pipeline.
        question_results = self._normalize_results(question_results)

        counts = aggregation.outcome_counts(question_results)
        if correct is None:
            correct = counts[OUTCOME_CORRECT]
        if wrong is None:
            wrong = counts[OUTCOME_INCORRECT]
        skipped = counts[OUTCOME_SKIPPED]

        # Canonical answers map, stable regardless of option/question
        # shuffle. Answered questions only (skips are absent, as before).
        canonical_answers = {
            f"q{int(qr['q_index'])}": list(qr.get("selected") or [])
            for qr in question_results or []
            if qr.get("outcome") in (OUTCOME_CORRECT, OUTCOME_INCORRECT)
        }

        # Make the completion path self-contained: the live runner normally
        # creates this row earlier, but an idempotent ensure makes replays
        # and direct callers safe without duplicating rows.
        total_questions = (
            len(questions) if isinstance(questions, list) and questions
            else len(question_results or [])
        )
        await self.attempts.ensure_started(
            attempt_id, user_id, qid, quiz_name, total_questions,
            source=source, quiz_persisted=bool(quiz_persisted),
        )

        # Attempt document: keep the legacy fields, add canonical results.
        update_fields = dict(
            answers=canonical_answers,
            question_results=[
                {k: qr.get(k) for k in
                 ("q_index", "selected", "correct_option", "outcome", "time_taken")}
                for qr in (question_results or [])
            ],
            score=score,
            correct=correct,
            wrong=wrong,
            total_time=total_time,
            skipped=skipped,
            source=source,
            quiz_persisted=bool(quiz_persisted),
        )
        if current_question is not None:
            update_fields["current_question"] = current_question
        await self.attempts.update(attempt_id, **update_fields)
        if finalize:
            await self.attempts.complete(
                attempt_id, score, username or "",
                persist_leaderboard=bool(quiz_persisted),
            )

        # Content-addressed snapshots: one bounded bulk ensure per
        # completion, deduped by question content hash (one DB round trip).
        snapshot_ids, snapshot_contents = self._prepare_snapshots(
            question_results, questions
        )
        if snapshot_contents:
            await self.snapshots.ensure(snapshot_contents, at=at)

        enriched = self._enrich(
            qid=qid, quiz_name=quiz_name, source=source,
            quiz_persisted=quiz_persisted, question_results=question_results,
            questions=questions, sections=sections, at=at,
            snapshot_ids=snapshot_ids,
        )
        events_inserted = await self.events.record_many(
            user_id, attempt_id, enriched, backfilled=backfilled
        )

        mistake_ops = 0
        stats_items = 0
        participant_incremented = False
        # Mistakes / question-level stats only exist for STORED quizzes.
        if quiz_persisted and apply_mistakes:
            mistake_ops = await self.mistakes.apply_attempt(
                user_id, qid, attempt_id, enriched, at=at
            )
            stats = [
                {"index": ev["question_index"],
                 "wrong": 1 if ev["outcome"] == OUTCOME_INCORRECT else 0,
                 "total": 1}
                for ev in enriched
                if ev["outcome"] in (OUTCOME_CORRECT, OUTCOME_INCORRECT)
            ]
            if stats:
                await self.question_stats.bulk_update_wrong_stats(qid, stats)
                stats_items = len(stats)
            if finalize:
                await self.quizzes.increment_participants(qid)
                participant_incremented = True

        # Phase D: /mistakes revision sessions are ad-hoc DM quizzes
        # (quiz_persisted=False), so the stored-quiz mistake block above is
        # intentionally skipped. Fold revision answers back into the ORIGINAL
        # mistake rows instead. This is an additional, independently fail-soft
        # operation: it must never block completion, results/HTML/PDF, the
        # normal analytics writes above, or the Phase C XP/streak step below.
        revision_folded = 0
        if revision_origins is None and isinstance(questions, list):
            # Origins ride along hidden on each in-memory revision question
            # under a private key the play engine ignores; aligned to canonical
            # question index, so option/question shuffling cannot misroute a
            # fold. A normal quiz carries no such key -> stay None (no fold).
            extracted = [
                q.get("_revision_origins") if isinstance(q, dict) else None
                for q in questions
            ]
            if any(extracted):
                revision_origins = extracted
        if revision_origins:
            try:
                revision_folded = await self._fold_revision(
                    user_id, attempt_id, enriched, revision_origins, at
                )
            except Exception:
                logger.exception(
                    "revision fold failed (fail-soft) user=%s attempt=%s",
                    user_id, attempt_id,
                )

        # Phase C: XP / levels / daily IST streaks. This MUST be fail-soft: a
        # gamification failure can never block quiz completion, result/HTML/PDF
        # report generation, Mini App completion or scheduled completion.
        gamification_result = None
        try:
            gamification_result = await self.gamification.on_completion(
                user_id=user_id,
                attempt_id=attempt_id,
                source=source,
                question_results=question_results,
                questions=questions,
                sections=sections,
                finalize=finalize,
                backfilled=backfilled,
                at=at,
                enriched_events=enriched,
            )
        except Exception:
            logger.exception(
                "gamification failed (fail-soft) for user=%s attempt=%s",
                user_id, attempt_id,
            )

        return {
            "attempt_id": attempt_id,
            "events_total": len(enriched),
            "events_inserted": events_inserted,
            "mistake_ops": mistake_ops,
            "question_stats_items": stats_items,
            "participant_incremented": participant_incremented,
            "skipped": skipped,
            "revision_folded": revision_folded,
            "gamification": gamification_result,
        }

    async def _fold_revision(
        self, user_id: int, attempt_id: str, enriched: list[dict],
        origins_by_index: list, at: str,
    ) -> int:
        """Fold one revision session's canonical events back into the ORIGINAL
        mistake rows, grouping by origin qid (a revision question may stand for
        several historical rows). Idempotent under ``attempt_id`` (the
        existing per-attempt guard arrays in ``apply_attempt``), shuffle-safe
        (canonical indices), user-scoped and bounded by the revision size.
        """
        by_qid: dict[str, list[dict]] = {}
        for ev in enriched:
            q_index = int(ev["question_index"])
            origins = origins_by_index[q_index] if q_index < len(
                origins_by_index) else None
            if not origins:
                continue
            seen: set = set()
            for o in origins:
                if not isinstance(o, dict):
                    continue
                origin_qid = o.get("qid")
                origin_index = o.get("q_index")
                if not origin_qid or origin_index is None:
                    continue
                try:
                    origin_index = int(origin_index)
                except (TypeError, ValueError):
                    continue
                if (origin_qid, origin_index) in seen:
                    continue  # one fold per origin question within an attempt
                seen.add((origin_qid, origin_index))
                fold_ev = dict(ev)
                fold_ev["question_index"] = origin_index
                if o.get("snapshot_id"):
                    fold_ev["snapshot_id"] = o["snapshot_id"]
                by_qid.setdefault(origin_qid, []).append(fold_ev)
        touched = 0
        for origin_qid, evs in by_qid.items():
            try:
                touched += await self.mistakes.apply_attempt(
                    user_id, origin_qid, attempt_id, evs, at=at
                )
            except Exception:
                # One bad origin qid must not stop the remaining folds.
                logger.exception(
                    "revision fold failed for qid=%s user=%s", origin_qid, user_id
                )
        return touched

    # ------------------------------------------------------------------- reads

    async def _attach_snapshots(
        self, rows: list[dict], *, id_key: str = "snapshot_id",
        out_key: str = "question_snapshot",
    ) -> list[dict]:
        """Resolve compact snapshot references to their minimal content with
        a single bounded ``$in`` query (page-sized callers only)."""
        ids = [row.get(id_key) for row in rows if row.get(id_key)]
        snap_map = await self.snapshots.get_many(ids) if ids else {}
        for row in rows:
            if out_key in row:
                continue  # legacy embedded snapshot keeps precedence
            row[out_key] = snap_map.get(row.get(id_key))
        return rows

    async def get_user_overview(self, user_id: int) -> dict:
        """Combined canonical/legacy overview for one user.

        Accuracy denominators are explicit:
          answered_accuracy = correct / (correct + incorrect)  [skips excluded]
        Canonical question events are preferred; legacy completed attempts
        without them contribute via attempt counters with a provenance flag.
        """
        user_id = self._require_user(user_id)
        events_ov = await self.events.get_overview(user_id)
        attempt_ov = await self.attempts.user_completed_overview(user_id)
        in_progress = await self.attempts.count_in_progress(user_id)
        mistake_totals = await self.mistakes.totals(user_id)

        ev_correct = int(events_ov.get("correct", 0) or 0)
        ev_incorrect = int(events_ov.get("incorrect", 0) or 0)
        ev_skipped = int(events_ov.get("skipped", 0) or 0)
        ev_answered = ev_correct + ev_incorrect

        ev_accuracy = round(aggregation.safe_ratio(ev_correct, ev_answered) * 100, 2)
        legacy_answered = attempt_ov["correct_sum"] + attempt_ov["wrong_sum"]
        legacy_accuracy = round(
            aggregation.safe_ratio(attempt_ov["correct_sum"], legacy_answered) * 100, 2
        )
        accuracy_pct = ev_accuracy if ev_answered else legacy_accuracy
        accuracy_basis = "question_events" if ev_answered else (
            "attempt_totals" if legacy_answered else "none"
        )

        time_sum = attempt_ov.get("time_sum", 0) or 0
        timed_questions = int(events_ov.get("timed_questions", 0) or 0)
        time_total_questions = float(events_ov.get("time_total", 0.0) or 0.0)
        avg_question_time = (
            round(time_total_questions / timed_questions, 2)
            if timed_questions else None
        )

        return {
            "user_id": user_id,
            "attempts": {
                "completed": attempt_ov["attempts"],
                "saved": attempt_ov["attempts"] - attempt_ov["adhoc"],
                "ad_hoc": attempt_ov["adhoc"],
                "canonical": attempt_ov["canonical"],
                "legacy": attempt_ov["legacy"],
                "in_progress": in_progress,  # explicitly never counted as completed
                "total_time_seconds": round(float(time_sum), 2),
                "activity_days": attempt_ov["activity_days"],
                "first_at": attempt_ov["first_at"],
                "last_at": attempt_ov["last_at"],
            },
            "questions": {
                "events": int(events_ov.get("questions", 0) or 0),
                "answered": ev_answered,
                "correct": ev_correct,
                "incorrect": ev_incorrect,
                "skipped": ev_skipped,
                "answered_accuracy_pct": accuracy_pct,
                "avg_question_time_seconds": avg_question_time,
            },
            "quizzes_played": attempt_ov["quiz_count"] or events_ov.get("quiz_count", 0),
            "topics_seen": events_ov.get("topic_count", 0),
            "mistakes": mistake_totals,
            "provenance": {
                "accuracy_basis": accuracy_basis,
                "canonical_events": ev_answered > 0,
                "has_legacy_attempts": attempt_ov["legacy"] > 0,
            },
        }

    async def list_attempts(self, user_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
        user_id = self._require_user(user_id)
        return await self.attempts.list_completed_for_user(user_id, limit, offset)

    async def get_topic_performance(self, user_id: int) -> list[dict]:
        user_id = self._require_user(user_id)
        rows = await self.events.get_topic_performance(user_id)
        for row in rows:
            answered = row["correct"] + row["incorrect"]
            row["answered"] = answered
            row["accuracy_pct"] = round(
                aggregation.safe_ratio(row["correct"], answered) * 100, 2
            )
            row["avg_time_seconds"] = (
                round(row["time_total"] / row["timed_questions"], 2)
                if row["timed_questions"] else None
            )
        rows.sort(key=lambda r: (r["accuracy_pct"], -r["incorrect"]))
        return rows

    async def get_question_performance(
        self, user_id: int, qid: Optional[str] = None, limit: int = 200
    ) -> list[dict]:
        user_id = self._require_user(user_id)
        rows = await self.events.get_question_performance(user_id, qid=qid, limit=limit)
        for row in rows:
            answered = row["correct"] + row["incorrect"]
            row["answered"] = answered
            row["accuracy_pct"] = round(
                aggregation.safe_ratio(row["correct"], answered) * 100, 2
            )
        # One bounded $in resolution for the whole page.
        return await self._attach_snapshots(rows)

    async def list_mistakes(
        self, user_id: int, status: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        user_id = self._require_user(user_id)
        rows = await self.mistakes.list_for_user(user_id, limit=limit, status=status)
        return await self._attach_snapshots(rows)

    async def get_mistake_totals(self, user_id: int) -> dict:
        user_id = self._require_user(user_id)
        return await self.mistakes.totals(user_id)

    async def get_activity_days(self, user_id: int) -> list[str]:
        """Distinct UTC days with at least one COMPLETED attempt. This is the
        Phase C prep definition of meaningful activity; no XP exists yet."""
        user_id = self._require_user(user_id)
        overview = await self.attempts.user_completed_overview(user_id)
        return overview["activity_days"]

    async def get_daily_trend(self, user_id: int, days: int = 30) -> list[dict]:
        """Merge completed-attempt counts with canonical per-question daily
        counts. Days with legacy attempts only still appear."""
        user_id = self._require_user(user_id)
        attempt_daily = {r["day"]: r["attempts"]
                         for r in await self.attempts.daily_completed(user_id, days=0)}
        event_ov = await self.events.get_overview(user_id)
        event_daily = {d["day"]: d for d in event_ov.get("daily", [])}
        all_days = sorted(set(attempt_daily) | set(event_daily))[-days:]
        out = []
        for day in all_days:
            ev = event_daily.get(day, {})
            out.append({
                "day": day,
                "attempts": attempt_daily.get(day, 0),
                "answered": ev.get("answered", 0),
                "correct": ev.get("correct", 0),
                "accuracy_pct": round(aggregation.safe_ratio(
                    ev.get("correct", 0), ev.get("answered", 0)) * 100, 2)
                if ev.get("answered") else None,
            })
        return out
