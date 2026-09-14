#!/usr/bin/env bash
#
# deploy_pdf_service.sh — Idempotent production deployment for the SELF-HOSTED
# Test Series PDF microservice (quizbot-pdf.service) on the same VPS as the bot.
#
# WHAT THIS SCRIPT DOES
#   1. Uses the existing checkout (fast-forward only, never force).
#   2. Ensures the existing .venv has the PDF dependencies (fpdf2/uharfbuzz
#      from requirements.txt -- pip-only, no system libraries needed).
#   3. Installs/enables quizbot-pdf.service: ONE uvicorn process on
#      127.0.0.1:8090 (host/port overridable via PDF_SERVICE_HOST/PORT in .env),
#      Restart=always so it survives crashes AND server reboots.
#   4. Verifies: /healthz responds AND an end-to-end 3-question PDF (English +
#      Hindi) generates, polls to done, and downloads as a valid %PDF file.
#
# WHAT THIS SCRIPT NEVER DOES
#   - Never touches the Telegram bot service (quizbot.service) or its polling.
#   - Never modifies .env (it only READS host/port for its own checks).
#   - Never touches MongoDB data.
#   - Never prints or hard-codes secrets.
#
# PREREQUISITE: run ./deploy_vps.sh first (it creates .venv + .env + bot service).
#
# AFTER THIS SCRIPT: set  PDF_API_BASE=http://127.0.0.1:8090  in /opt/quizbot/.env
# (edit on the VPS) and restart the bot once:  sudo systemctl restart quizbot
# (the bot reads its configuration at startup).
#
# USAGE (run ON the VPS):
#   ./deploy_pdf_service.sh                # install/enable/verify
#   ./deploy_pdf_service.sh --check-only   # verify only, change nothing
#
set -euo pipefail

SERVICE_NAME="quizbot-pdf"
BRANCH="main"
APP_DIR=""
CHECK_ONLY=0

log()  { printf '%s\n' "[pdf-deploy] $*"; }
fail() { printf '%s\n' "[pdf-deploy] ERROR: $*" >&2; exit 1; }

# Path + content hash of THIS script as the shell started it; the ff pull can
# replace it on disk while bash keeps executing old bytes. Re-exec the new
# version once after such a self-update (same guard as deploy_vps.sh).
SELF_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
SELF_HASH_BEFORE="$(sha256sum "$SELF_PATH" 2>/dev/null | awk '{print $1}')"

self_reexec_if_updated() {
  [ -z "${DEPLOY_SELF_REEXECED:-}" ] || return 0
  local after
  after="$(sha256sum "$SELF_PATH" 2>/dev/null | awk '{print $1}')"
  if [ -n "$SELF_HASH_BEFORE" ] && [ "$after" != "$SELF_HASH_BEFORE" ]; then
    log "This deploy script was updated by the fast-forward pull — re-executing the NEW version before continuing ..."
    DEPLOY_SELF_REEXECED=1 exec bash "$SELF_PATH" "$@"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir)   APP_DIR="${2:?--app-dir needs a path}"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    -h|--help)   sed -n '2,/^#$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "Unknown argument: $1 (see --help)" ;;
  esac
done

if [ -z "$APP_DIR" ]; then
  if [ -f "./run.py" ] && [ -d "./pdf_service" ]; then
    APP_DIR="$(pwd)"
  elif [ -f "/opt/quizbot/run.py" ]; then
    APP_DIR="/opt/quizbot"
  else
    APP_DIR="/opt/quizbot"
  fi
fi
[ -f "$APP_DIR/run.py" ] || fail "$APP_DIR/run.py not found. Run ./deploy_vps.sh first."
[ -d "$APP_DIR/pdf_service" ] || fail "$APP_DIR/pdf_service missing. Update the checkout first."
cd "$APP_DIR"
log "App directory: $APP_DIR"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "Need root (or sudo) for systemd."
  SUDO="sudo"
fi

# Host/port the service will bind (same defaults as pdf_service/server.py).
pdf_host() { grep -E "^[[:space:]]*PDF_SERVICE_HOST=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d "\"' " | grep -E ".+" || echo "127.0.0.1"; }
pdf_port() { grep -E "^[[:space:]]*PDF_SERVICE_PORT=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d "\"' " | grep -E "^[0-9]+$" || echo "8090"; }

verify_service() {
  local host port base
  host="$(pdf_host)"; port="$(pdf_port)"; base="http://${host}:${port}"
  log "Verifying $base ..."
  "$APP_DIR/.venv/bin/python" - "$base" <<'PY'
import json, sys, time, urllib.request
base = sys.argv[1]
def get(path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return r.status, r.read()
def post(path, obj):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode())
s, body = get("/healthz")
assert s == 200 and json.loads(body.decode()).get("status") == "ok", body
print("[pdf-deploy] /healthz OK")
payload = {
    "questions_json": [
        {"question": "भारत की राजधानी क्या है?", "options": ["मुंबई", "दिल्ली", "कोलकाता", "चेन्नई"],
         "correct_option_id": 1, "explanation": "नई दिल्ली।"},
        {"question": "2 + 2 = ?", "options": ["3", "4", "5", "6"],
         "correct_option_id": 1, "explanation": "Basic math."},
        {"question": "Select primes.", "options": ["2", "4", "5", "9"],
         "correct_option_id": [0, 2], "explanation": ""},
    ],
    "institute_name": "Quiz Creator", "tagline": "Test Series",
    "exam_title": "Deploy Smoke Test", "solution_display": "end",
    "quiz_names": ["SMOKE"], "async": True,
}
s, job = post("/api/generate", payload)
assert s == 200 and job.get("progress_url") and job.get("download_url"), job
print(f"[pdf-deploy] job queued: {job['job_id']}")
deadline = time.time() + 120
while time.time() < deadline:
    s, body = get(job["progress_url"])
    assert s == 200, (s, body)
    st = json.loads(body.decode())
    if st.get("status") == "done":
        break
    if st.get("status") == "error":
        raise SystemExit(f"smoke job failed: {st.get('error')}")
    time.sleep(1.5)
else:
    raise SystemExit("smoke job timed out")
with urllib.request.urlopen(base + job["download_url"], timeout=30) as r:
    pdf = r.read()
assert pdf[:4] == b"%PDF" and len(pdf) > 5000, f"bad pdf: {len(pdf)} bytes"

# Content + searchable-text-layer checks. A PDF that only *looks* right is
# not enough: fpdf2 2.8.8 used to omit ToUnicode for the extra glyphs of a
# split Devanagari cluster (pre-base i-matra: दि/कि/स्थि), so copy/search
# extracted raw subset codes inside words ("दिGल्ली"). Verify real words
# round-trip verbatim with PyMuPDF (a project dependency).
import fitz
doc = fitz.open(stream=pdf, filetype="pdf")
assert doc.page_count >= 1
full = "\n".join(p.get_text() for p in doc)
for must in ("Deploy Smoke Test", "दिल्ली", "नई दिल्ली", "2 + 2",
             "Select primes", "Answer Key"):
    assert must in full, f"smoke PDF missing/damaged text: {must!r} -> {full[:400]!r}"
controls = [c for c in full if ord(c) < 0x20 and c not in "\n\r\t"]
assert not controls, f"raw-CID/control chars in PDF text layer: {controls!r}"
assert "(cid:" not in full and "\ufffd" not in full, "unmapped glyphs in text layer"
doc.close()
print(f"[pdf-deploy] end-to-end smoke OK ({len(pdf)} bytes, valid %PDF, Hindi text layer intact)")
PY
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  log "--check-only: verifying without changes."
  $SUDO systemctl is-active --quiet "$SERVICE_NAME" \
    && log "service state: active." \
    || fail "service $SERVICE_NAME is not active."
  verify_service
  log "CHECK-ONLY OK."
  exit 0
fi

# --- code freshness (fast-forward only; deploy_vps.sh owns the full flow) ---
if [ -d "$APP_DIR/.git" ]; then
  log "Fast-forwarding to origin/$BRANCH ..."
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" checkout "$BRANCH" --quiet
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH" || fail \
    "git pull --ff-only failed. Resolve locally, then re-run."
  self_reexec_if_updated "$@"
fi

# --- dependencies (same venv as the bot; pip-only) ---
VENV_PY="$APP_DIR/.venv/bin/python"
[ -x "$VENV_PY" ] || fail "No venv at $APP_DIR/.venv. Run ./deploy_vps.sh first."
log "Ensuring PDF dependencies (fpdf2/uharfbuzz via requirements.txt) ..."
"$VENV_PY" -m pip install -r "$APP_DIR/requirements.txt"
for f in Hind-Regular.ttf Hind-Bold.ttf; do
  [ -f "$APP_DIR/pdf_service/fonts/$f" ] || fail "Missing bundled font pdf_service/fonts/$f."
done
log "PDF dependencies + bundled fonts OK."

# --- systemd unit (dedicated service; the bot service is untouched) ---
SERVICE_USER="$(stat -c '%U' "$APP_DIR")"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
log "Installing systemd unit $UNIT_PATH (user=$SERVICE_USER) ..."
$SUDO tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Journey Test Series PDF microservice (localhost only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m pdf_service.server
Restart=always
RestartSec=10
EnvironmentFile=$APP_DIR/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"
log "Service enabled (starts on boot) and restarted."
# Same stability gate as the bot deploy: a one-shot is-active probe cannot
# distinguish a healthy service from a Restart=always crash loop. The gate
# requires active(running) to hold across a stability window with one stable
# PID and exactly one pdf_service process; the functional end-to-end render
# check below then proves the service actually serves PDFs.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/deploy_health_gate.sh" ] || fail "Missing $SCRIPT_DIR/deploy_health_gate.sh (checkout incomplete?)."
if ! SUDO="$SUDO" HEALTH_START_GRACE="${HEALTH_START_GRACE:-60}" \
     HEALTH_STABLE_SECS="${HEALTH_STABLE_SECS:-30}" HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}" \
     "$SCRIPT_DIR/deploy_health_gate.sh" "$SERVICE_NAME" "$APP_DIR" "pdf_service[.]server"; then
  fail "PDF service did not pass the stability health gate — DEPLOY FAILED. Inspect the journal above, fix the startup error, and re-run (idempotent; .env and data untouched)."
fi
verify_service

log "PDF DEPLOY OK — $SERVICE_NAME passed the stability gate and renders valid PDFs end to end."
log "NEXT: set PDF_API_BASE=http://127.0.0.1:$(pdf_port) in $APP_DIR/.env,"
log "THEN restart the bot once so it picks up the new value:"
log "  sudo systemctl restart quizbot"
log "Logs: sudo journalctl -u $SERVICE_NAME -f"
