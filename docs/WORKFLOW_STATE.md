# WORKFLOW_STATE — Permanent Session-Continuity Ledger

> **This file is the single source of truth for every future Arena coding session on this repository.**
> Repo: `UPSCJOURNEYbot/-journeyforlbsnaa_bot` · Branch of record: `main`
> Last updated: 2026-09-12 (UTC) by the session that created this ledger.
> **Never** store secrets here or in any Git-tracked file (`BOT_TOKEN`, `MONGODB_URI`, `PODCAST_KEY_SECRET`, Gemini/OpenRouter/Razorpay keys, `API_HASH`, passwords). Only variable *names* are allowed.

---

## 1. Current Phase

**Phase 6 — Gemini Podcast system: COMPLETE and merged to `main`.**
Project is currently in **stabilisation / documentation / recovery-hardening**. No new feature phase is in progress.

## 2. Completed Phases / Milestones (verified in Git history)

| Milestone | Commit (main) | State |
|---|---|---|
| v5 single-bot Free-edition production build recovered from FINAL-v5 artifact (PR #1) | `f621008` | ✅ merged |
| docker-compose fixed for single-bot (no double polling on shared token) (PR #2) | `fe081e8` | ✅ merged |
| Robust idempotent `deploy_vps.sh` (systemd 24×7, single bot preserved) | `bd7e518` | ✅ merged |
| Phase 1–2: Test Series self-hosted PDF microservice + `/testseries` end-to-end | `f8b03af` | ✅ merged |
| Direct MCQ file → Test Series (bare `/testseries` upload) | `287e282` | ✅ merged |
| Test-series file flow: explicit validation counts + MD/statements tests | `bc7dcd7` | ✅ merged |
| Phase 3 m1–m3: visual engine + wiring into PDF solutions + large/real-world PDF validation | `38cd8f1` → `59fd01d` | ✅ merged |
| Phase 4 m1–m2: `/newseries` wizard + wizard config applied to PDF | `ccaec43`, `98f6f08` | ✅ merged |
| Phase 6: per-user encrypted Gemini keys + upgraded podcast system (PR #3) | `c78660a` / merge `a8e9782` | ✅ merged, 399/399 |
| Phase 6 fix: Gemini key compatibility — legacy plaintext + AIKey/env fallback (PR #4) | `f07ea46` / merge `85c66fb` | ✅ merged, 412/412 |
| Phase 6 fix: Gemini runtime compat — lazy SDK types, 429→quota, bounded call timeouts (PR #5) | `8f48444` / merge `763920b` | ✅ merged, 412/412 |
| **This session:** session-continuity ledger created (`docs/WORKFLOW_STATE.md`) | see §5 | ✅ done (docs only) |

PR history (all MERGED into `main`, none open): **#1, #2, #3, #4, #5**. No tags exist in this repository.

## 3. Active Task

**None.** This ledger exists so no future session ever has to re-derive project state. Next session starts at §11 ("Exact Next Action").

## 4. Latest Verified Checkpoints

| Item | Value |
|---|---|
| Latest merged `main` commit (as of this file) | `763920bd4df45e37fe4422e398827671512c8f44` — "Merge pull request #5 from arena/01a09352…" |
| Latest code-bearing commit on `main` | `8f48444` — Phase 6 Gemini runtime compat |
| Latest *verified* commit at time of writing | `763920b` (tip of `origin/main`; clean tree; 412 tests accounted for) |
| Session branch | `arena/01a09368-journeyforlbsnaa-bot` (branched from `763920b`) |
| Sandbox clone caveat | Arena sandboxes clone **grafted/shallow** (`git log` may show exactly 1 commit). That is a clone artifact, **not** lost history. |
| Remote branches still on GitHub (recovery sources) | `arena/01a08e86…` → `5a95381`, `arena/01a090a3…` → `bc7dcd7`, `arena/01a09260…` → `c78660a`, `arena/01a0930e…` → `f07ea46`, `arena/01a09352…` → `8f48444` |

## 5. Latest Test Count / Status

- **412 tests, 0 failures** — the number recorded in PR #4 and PR #5 (`PYTHONPATH=. python -m unittest discover -s tests`).
- Independently re-verified **statically** in the session that wrote this file: `grep -c "def test_" tests/*.py` sums to **412** (matches the executed count exactly).
  Breakdown: `test_gemini_compat` 13 · `test_podcast_phase6` 81 · `test_testseries_bot` 19 · `test_testseries_create` 58 · `test_testseries_file` 66 · `test_testseries_m2` 51 · `test_pdf_service` 16 · `test_viz_engine` 81 · `test_viz_integration` 10 · `test_viz_m3` 17.
- All tests are **offline/hermetic** (no network, no MongoDB, no Telegram). The `viz`/`pdf` suites are slow (≈39–59 s each); run targeted files while iterating and the full suite only before merge.
- A fresh Arena sandbox has **no Python deps installed** (`pyrogram`, `telegram`, `motor` missing) → `pip install -r requirements.txt` first if you need to execute the suite.
- This ledger-creation session ran **docs-only** changes: the full suite was intentionally not re-executed (no functional code touched).

## 6. Important Architecture Decisions (do not regress these)

1. **SINGLE-BOT MODE (hard rule).** One Telegram bot token, exactly **one** python-telegram-bot polling client; Creator behaviour is bridged into the Runner app (`quizbot/runner_bot/creator_bridge.py`). Never run `quizbot/creator_bot/bot.py` separately, never add a second service to `docker-compose.yml`/systemd that polls. `CREATOR_BOT_TOKEN`/`RUNNER_BOT_TOKEN` are backward-compatible aliases of `BOT_TOKEN`.
2. **Creator wizard callbacks** (`cws_*`) run in priority group `-2` — required for the single-client build; do not change.
3. **Async MongoDB only**, via Motor (`quizbot/database/db.py`); every access goes through repository classes in `quizbot/database/repositories.py`. Indexes are created automatically on connect; schema changes must be **additive** (e.g. `podcast_keys` collection + unique index added with no data migration). Never drop/reset/migrate data destructively.
4. **Config is env-driven** (`quizbot/shared/config.py`, `.env.example` template). `.env` is Git-ignored and must never be rewritten, printed, or committed. Secrets live only in the environment of the host.
5. **Phase 6 podcast key model:** users bring their own Gemini key via `/podcast`, Fernet-encrypted at rest using `PODCAST_KEY_SECRET`. Resolution order in `_load_user_key`: (a) per-user encrypted key, (b) generic `AIKeyRepository` gemini key (`/setkey`), (c) legacy `GEMINI_API_KEY`/`GOOGLE_API_KEY`/`GEMINI_API_KEYS` env fallback. The env fallback exists purely so older VPS installs are not stranded; **no new code should introduce or advertise a shared global key**. `decrypt_api_key` also accepts legacy plaintext `AIza…` values; invalid blobs must still raise. Key material is never logged, never echoed to Telegram, never committed.
6. **Gemini call resilience:** `google.genai.types` imported lazily via `_genai_types()`; `429`/`RESOURCE_EXHAUSTED` classified as quota (never blindly retried) and distinct from invalid/revoked keys; every Gemini call wrapped in `asyncio.wait_for(..., 180)`.
7. **PDF generation is a self-hosted microservice** (`pdf_service/`, FastAPI + fpdf2/HarfBuzz) reached over `PDF_API_BASE=http://127.0.0.1:8090` on the same VPS; the bot degrades gracefully when it is down. Limits, job-id allow-listing and auto-expiry are documented in `PDF_SERVICE.md`. Branding is always **Journey for लबासना**.
8. **Mini App** (FastAPI + Telegram WebApp) is optional and disabled when `MINI_APP_DOMAIN` is blank; `initData` verified with HMAC-SHA256; quiz payloads AES-256-GCM per session.
9. **Deployment is script-driven and idempotent** (`deploy_vps.sh`, `deploy_pdf_service.sh`): preserves `.env` and `data/`, verifies a single polling client, restart-safe. GitHub Actions workflow `.github/workflows/bot.yaml` is a legacy "run run.py" job — it is **not** the deployment mechanism and is expected to fail without secrets.

## 7. Known Issues / Watch Items

- **Shallow (grafted) Arena clones** make history look lost → always `gh api …/commits?sha=main` / `git ls-remote` before assuming anything is missing. Two Phase 6 commits (`d192dc4`, `8e09617`) were genuinely lost this way earlier and were re-created, not recovered.
- **Unpushed session branches are lost** when the sandbox is recycled — hence §10 rules; always `git push` before ending a session.
- `.env.example` documents `PODCAST_KEY_SECRET` but **not** the legacy `GEMINI_API_KEY` fallback (intentional; it exists only in code). Don't "fix" this by advertising a shared key.
- `BOTFATHER_COMMANDS.txt` still describes the **legacy two-bot** command lists; the runtime is single-bot. Docs drift, not a functional bug.
- `requirements.txt` deps are **not installed** in a fresh sandbox; test runs fail at import until `pip install -r requirements.txt`.
- GitHub repo has **no tags** and no release discipline yet; the four `Quizbot-*.zip` archives at the repo root are historical delivery artifacts (v5 = latest) and must not be deleted or regenerated casually.
- `.github/workflows/bot.yaml` runs `python run.py` on every push and daily cron — noisy/failing by design; no CI test job exists.

## 8. Pending Work (candidates, not commitments)

- Optional: real CI job that runs `python -m unittest discover -s tests` instead of `python run.py`.
- Optional: tag releases (`v5-single-bot`, `phase6-podcast`) so checkpoints are named, not just hashed.
- Optional: README/BOTFATHER alignment with single-bot reality.
- Operator-side (manual, on the VPS — **never from a coding session**): set `PODCAST_KEY_SECRET` in `/opt/quizbot/.env`, set `PDF_API_BASE`, run `./deploy_vps.sh` / `./deploy_pdf_service.sh`, then smoke-test `/podcast` and `/testseries`.
- No open bug is currently recorded against `main`.

## 9. Deployment Status

- **No deployment is performed by, or visible to, these coding sessions.** Nothing is deployed from the Arena sandbox; code ships only when a human runs `./deploy_vps.sh` **on the VPS**.
- Expected production shape (unchanged by recent work): `/opt/quizbot` + `/opt/quizbot/.venv`, systemd unit `quizbot.service` running `<APP_DIR>/.venv/bin/python run.py`, `Restart=always`; optional `quizbot-pdf.service` on `127.0.0.1:8090`; Mini App only when `MINI_APP_DOMAIN` is set.
- Post-merge action for the operator: pull `main`, `./deploy_vps.sh --check-only`, then `./deploy_vps.sh`, then confirm exactly one bot process.
- Live bot: `@advance_quiz_bot` (see README).

## 10. Mandatory Rules For Every Future Coding Session

1. **Read this file first**, before touching code, and treat it as the authoritative project state.
2. **Inspect Git history, branches, tags and PRs before assuming previous work is lost**: `git log --oneline --all`, `git fetch --unshallow || git fetch --depth=200`, `git ls-remote --heads origin`, `gh pr list --state all --repo UPSCJOURNEYbot/-journeyforlbsnaa_bot`, `gh api "repos/UPSCJOURNEYbot/-journeyforlbsnaa_bot/commits?sha=main&per_page=50"`. A grafted clone with 1 commit is normal here and means nothing about the real history.
3. **Continue from the latest verified checkpoint** in §4/§5 — branch from the current `origin/main` tip, never from a stale assumption, and keep changes minimal and reviewable.
4. **Never recreate existing work without first checking recoverability.** Verify locally *and* remotely (`git show <sha>`, `gh api …/commits/<sha>`, open/merged PR files) before rewriting anything; if a prior session's commit truly is unreachable, recreate the minimum safe delta and say so in the PR body.
5. **Update this file after every meaningful milestone** (phase completed, fix merged, tests run, deployment change, new known issue/pending item) — same PR as the work when possible, and always **push the session branch before the session ends**.

## 11. Exact Next Action

Docs-only change: this file was created on `arena/01a09368-journeyforlbsnaa-bot`, committed as a checkpoint, and pushed to that branch (no PR, no merge, no app-code change).

**Next session, do:**
1. `git fetch origin main && git log --oneline -5 origin/main` — if `origin/main` has moved past the §4 hash, re-read those commits (`gh pr list --state merged --limit 5`) and refresh §2/§4/§5 of this file first.
2. Otherwise, confirm the repo is at the Phase 6 checkpoint (412/412), then pick the first item of §8, or take the next instruction from the owner.
3. Never re-run the whole test suite for a docs-only change; run targeted suites while iterating and `PYTHONPATH=. python -m unittest discover -s tests` once, immediately before merge.
4. Finish by updating this file (Current Phase, Latest Verified Commit, Test Status, Pending Work, Next Action) and pushing the branch.

## 12. Fresh-Session Recovery Procedure (copy-paste)

```bash
cd /home/user/-journeyforlbsnaa_bot
cat docs/WORKFLOW_STATE.md                                   # 1. state of record
git status --porcelain -sb && git log --oneline -5           # 2. where am I (grafted = normal)
git fetch --depth=200 origin main 2>/dev/null || git fetch --unshallow   # 3. restore real history
git ls-remote --heads origin                                 # 4. recoverable session branches
gh pr list --repo UPSCJOURNEYbot/-journeyforlbsnaa_bot --state all --limit 20
git log --oneline origin/main -10                            # 5. latest merged checkpoint
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt   # only if tests must run
PYTHONPATH=. python -m unittest discover -s tests            # baseline: 412 tests, 0 failures
grep -nE "BOT_TOKEN|MONGODB_URI|PODCAST_KEY_SECRET|API_HASH|AIza" $(git diff --name-only) || echo "no secret strings in diff"
```

Then: continue from §4, obey §6 (single bot, additive DB, no secret exposure), obey §10, and rewrite §1–§5, §7–§11 when the milestone lands.
