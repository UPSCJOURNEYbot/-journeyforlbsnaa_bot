# Self-Hosted Test Series PDF Microservice

Production-quality PDF backend for `/testseries`, running on the **same VPS**
as the Telegram bot. No external API, no per-PDF cost, no data leaving the VPS.

```
/testseries ──▶ quizbot.service (Telegram bot, existing)
                     │  PDF_API_BASE=http://127.0.0.1:8090
                     ▼
              quizbot-pdf.service (this service: FastAPI + fpdf2/HarfBuzz)
                     │  writes data/pdf_jobs/<job>.pdf (temporary, auto-cleaned)
                     ▼
              PDF bytes ──▶ back to the bot ──▶ Telegram document
```

## API contract (unchanged — the bot side already speaks it)

Base URL = value of `PDF_API_BASE` (trailing slash tolerated).

| Method & path | Purpose |
|---|---|
| `POST /api/generate` | Body: `{questions_json, institute_name, tagline, exam_title, solution_display, quiz_names, async}`. Returns `200 {"job_id", "status":"queued", "progress_url", "download_url"}` (both URLs are root-relative paths the bot prefixes with `PDF_API_BASE`) |
| `GET /api/progress/<job>` | `{"status": queued\|processing\|done\|error, "progress": 0–100, "error"?}` |
| `GET /api/download/<job>` | `200 application/pdf` once done; `409` while rendering; `404` for unknown/expired jobs |
| `GET /healthz` | `{"status":"ok"}` for monitoring/systemd checks |

- `solution_display`: `"inline"` (answer + explanation after each question) or
  `"end"` (`keyonly`: questions, then Answer Key + Detailed Solutions).
- `correct_option_id` may be an int or a list (multi-correct renders as "A, C").
- Visible branding is always **Journey for लबासना** (Hindi brand name); the
  payload's `institute_name` is accepted but does not override the brand.
- Optional `series_setup` object (sent only by `/newseries`): applies the
  wizard configuration to the PDF — test identification grid (subject,
  paper, test/booklet numbers, test code, duration, totals, marks),
  candidate-detail boxes, cover institute name + logo, per-page watermark
  (`none` | `text` | `image` | `both`; images as base64, job-scoped, never
  public), answer-key / detailed-solutions toggles, and visual-aids mode
  (`auto` | `yes` | `no`). Unknown or mistyped values fall back to the
  historical rendering; the field is omitted entirely by `/testseries`.

## Limits & safety

- Max 2000 questions / 2–10 options per question / 20 MB request body (headroom for base64 logo/watermark images).
- At most 8 concurrent active jobs (`429` beyond that — the bot shows a clean
  "retry shortly" error); 2 render workers.
- Job IDs are allow-listed (`[A-Za-z0-9_-]{8,64}`) and resolved inside
  `data/pdf_jobs/` only — no path traversal, no arbitrary paths.
- Finished jobs + PDFs auto-expire after 1 hour; stuck jobs are marked failed
  after 30 min; a 5-minute sweeper also removes orphan PDFs.
- Failures return short safe messages; tracebacks stay in the journal only.

## VPS deployment

Prerequisite: `./deploy_vps.sh` has been run (provides `/opt/quizbot/.venv`,
`.env`, and the bot service).

```bash
cd /opt/quizbot
./deploy_pdf_service.sh --check-only   # verify-only (after first install)
./deploy_pdf_service.sh                # install/enable/verify (idempotent)
```

The script ends with an **end-to-end smoke test** (English + Hindi questions →
poll → download → `%PDF` validation). Then:

```bash
nano /opt/quizbot/.env                 # set PDF_API_BASE=http://127.0.0.1:8090
sudo systemctl restart quizbot         # bot reads config at startup
```

Exact production format (same machine, localhost — no public domain needed):

```
PDF_API_BASE=http://127.0.0.1:8090
```

Optional overrides (all have safe defaults; see `.env.example`):

```
PDF_SERVICE_HOST=127.0.0.1
PDF_SERVICE_PORT=8090
```

Useful commands:

```bash
sudo systemctl status quizbot-pdf --no-pager
sudo journalctl -u quizbot-pdf -f
curl -s http://127.0.0.1:8090/healthz
```

## Failure behavior (bot never dies because of the PDF service)

| Situation | Bot behavior |
|---|---|
| Service down / unreachable | `/testseries` replies "PDF generation failed: …" (clean message); `/start`, quizzes, everything else unaffected |
| Job lost (service restarted mid-render) | `/testseries` replies "PDF job lost … Please retry" immediately (no 3-minute hang) |
| Render error (bad data) | Progress reports `error`; user gets "PDF generation failed: <short reason>" |
| `PDF_API_BASE` empty | `/testseries` replies "not configured … use `/whtml`" (pre-existing behavior) |

## Files

- `pdf_service/app.py` — FastAPI app + thread-safe job manager + cleanup
- `pdf_service/render.py` — fpdf2/HarfBuzz A4 renderer + branding + sanitiser
- `pdf_service/server.py` — uvicorn entrypoint (`python -m pdf_service.server`)
- `pdf_service/fonts/` — vendored OFL Hind Regular/Bold (Latin + Devanagari)
- `tests/test_pdf_service.py` — 10/55/100/200/300-question API + content tests
- `tests/test_testseries_bot.py` — bot-side parsing/payload/wiring/architecture tests
