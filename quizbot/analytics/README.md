# Phase B — Analytics data foundation

This package is the canonical, user-scoped analytics layer. It **attaches** to
the existing quiz lifecycle; it never re-implements quiz execution, scoring or
reporting. No premium command or UI is registered by this package — those are
later phases.

## Modules

| Module | Role |
| --- | --- |
| `metadata.py` | Optional question metadata (`analytics.subject/topic/subtopic/difficulty`), section→topic fallback, difficulty vocabulary normalisation, minimal question snapshot. Pure, no imports of DB/Telegram. |
| `runtime.py` | Converts the **live** scorer's poll answers into canonical, shuffle-safe `question_results` at the result boundary (group/DM polls + Mini App). Never re-scores stored answers. |
| `aggregation.py` | Pure stats with explicit denominators (`answered_accuracy = correct / (correct + incorrect)`, skips excluded), activity-day and streak helpers. |
| `repository.py` | `QuestionEventRepository` — idempotent upserts and user-scoped MongoDB aggregations over `question_events`. |
| `service.py` | `AnalyticsService` — the single write/read entry point shared by the runner bot and Mini App. |

The manual, opt-in history backfill lives at `quizbot/database/backfill.py`
and is **never** run at startup.

## Data model

### `question_events` (new collection)

One document per **(user, attempt, question)** — the canonical per-question
performance record:

- key: `user_id`, `attempt_id`, `question_index` (unique index = idempotency
  key); deterministic natural `event_id` (uuid5 of the triple);
- outcome: `correct` / `incorrect` / `skipped` (skipped is never wrong);
- `selected_option` / `correct_option` in **canonical** option indices;
- `time_taken` only where genuinely measured live (`null` for history/skips);
- `answered_at` (null for skipped), `created_at`;
- `subject`, `topic`, `subtopic`, `difficulty`, `topic_source`
  (`question` = explicit metadata, `section` = quiz section fallback, so free
  text section names are never mistaken for a formal taxonomy);
- `question_snapshot` = `{question, options, correct_option_id}` — minimal
  identity snapshot so later quiz edits cannot repoint history;
- provenance: `source` (`group`/`dm`/`miniapp`/`scheduled`/`aiquiz`/`pdfquiz`/
  `mix`/`unknown`), `quiz_persisted`, `qid`, `quiz_name`, `backfilled`.

### `quiz_attempts` (additive fields only)

`question_results` (canonical per-question list), `skipped`, `source`,
`quiz_persisted`. Legacy documents stay valid; `in_progress` is never counted
as completed. Ad-hoc AI/mix/PDF attempts are now recorded with
`quiz_persisted: false` and never touch leaderboard/question stats/participant
counters.

### `user_mistakes` (non-destructive)

Rows are never deleted. Status `open`/`resolved` (a wrong answer after
resolution re-opens the row), `wrong_count`, `correct_count`,
`first_wrong_at`/`last_wrong_at`/`last_correct_at`, bounded
`revision_history`, idempotency arrays `wrong_attempt_ids`/
`correct_attempt_ids`, and the first-wrong `question_snapshot`. The legacy
`record()` API still works (non-destructive); the old destructive
`resolve()` now marks `resolved` instead of deleting.

### Indexes (all idempotent, startup-safe)

- `question_events`: unique `(user_id, attempt_id, question_index)`,
  `(user_id, qid, question_index)`, `(user_id, created_at)`,
  `(user_id, topic, created_at)`;
- `quiz_attempts`: `(user_id, status, time_ended)`;
- `user_mistakes`: `(user_id, status, last_wrong_at)` (existing unique key
  kept).

## Boundary wiring

- **Group & DM saved/scheduled/ad-hoc quizzes** — quiz play now stores
  `display_order` (option permutation) and, for flat shuffled group quizzes,
  `question_order` in each poll record. `_record_attempt_and_report()` builds
  canonical question results from the live poll/answer maps and calls
  `AnalyticsService.record_completion()`. This also fixes the DM gap where
  answers were keyed by Telegram poll UUID and never became mistakes.
- **Mini App** — `complete_session()` persists the per-question answer map
  (previously dropped) through the same service call.

## What is deliberately not done in Phase B

- No `/coach /weakquiz /mistakes /dashboard /report /xp /userstats` or
  `/adminstats` commands/UI; no XP rules (activity = completed attempt is
  only *defined* and exposed as day lists);
- no hardcoded UPSC taxonomy and no synonym merging;
- no fabricated historical topic/difficulty/answer/timing;
- no second process, no background workers, no Python-side large aggregation;
- `/pollquiz` remains self-scored only; ad-hoc quizzes keep synthetic qids.
