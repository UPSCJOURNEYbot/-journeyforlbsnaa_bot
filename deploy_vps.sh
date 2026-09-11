#!/usr/bin/env bash
#
# deploy_vps.sh — Robust, idempotent production deployment for the EXISTING
# Journey-for-LBSNAA / Advance Quiz Bot application on a VPS (systemd).
#
# WHAT THIS SCRIPT DOES
#   1. Uses the existing checkout (or clones origin/main once) — never rewrites app code.
#   2. Fast-forwards to origin/main (never force-pushes, never discards local data).
#   3. Preserves .env as-is (NEVER overwrites it) and validates required keys
#      WITHOUT printing secret values.
#   4. Installs OS packages (ffmpeg, tesseract, weasyprint libs) + Python venv +
#      requirements.txt — the same dependencies the repo already declares.
#   5. Verifies the SINGLE-BOT build (exactly one python-telegram-bot polling
#      client) before starting anything.
#   6. Installs/enables a systemd unit that runs ONE process:
#        <APP_DIR>/.venv/bin/python run.py
#      with Restart=always so the bot survives crashes AND server reboots.
#   7. Verifies the service is active and that exactly ONE bot process runs.
#
# WHAT THIS SCRIPT NEVER DOES
#   - Never modifies bot features, handlers, commands, or quiz behavior.
#   - Never touches MongoDB data (no drops, no resets, no migrations).
#   - Never writes, prints, or hard-codes secrets (tokens stay in .env only).
#   - Never deletes data/ (local cache/tmp only; real data lives in MongoDB).
#   - Never adds /testseries (or anything) to Telegram's command menu.
#     /testseries stays exactly as coded: private-chat only, premium-gated,
#     ownership-checked, via the existing PDF microservice when configured.
#   - Never starts a second polling client (no separate creator/runner
#     processes; the Mini App reuses this SAME process when MINI_APP_DOMAIN
#     is set — no extra poller is created).
#
# IDEMPOTENCY: safe to re-run any number of times. Re-runs pull the latest
# main, refresh dependencies if needed, and restart the single service.
#
# USAGE (run ON the VPS, from the repo directory or anywhere):
#   ./deploy_vps.sh                          # deploy /opt/quizbot or current repo
#   ./deploy_vps.sh --app-dir /opt/quizbot   # explicit target directory
#   ./deploy_vps.sh --check-only             # validate only, change nothing
#   ./deploy_vps.sh --no-apt                 # skip apt-get (non-Debian / pre-provisioned)
#
# PREREQUISITES (on the VPS, set up once by the operator):
#   - A complete .env file in the app directory (copy .env.example -> .env and
#     fill in real values on the VPS itself; never commit .env to git).
#   - Network access to api.telegram.org and your MongoDB Atlas cluster.
#
set -euo pipefail

SERVICE_NAME="quizbot"
BRANCH="main"
REPO_URL="https://github.com/UPSCJOURNEYbot/-journeyforlbsnaa_bot.git"
APP_DIR=""
CHECK_ONLY=0
SKIP_APT=0

log()  { printf '%s\n' "[deploy] $*"; }
fail() { printf '%s\n' "[deploy] ERROR: $*" >&2; exit 1; }

usage() {
  sed -n '2,/^#$/p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir)   APP_DIR="${2:?--app-dir needs a path}"; shift 2 ;;
    --branch)    BRANCH="${2:?--branch needs a name}"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --no-apt)    SKIP_APT=1; shift ;;
    -h|--help)   usage ;;
    *) fail "Unknown argument: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------------------
# 0. Locate (or clone) the application directory. No app code is altered here.
# ---------------------------------------------------------------------------
if [ -z "$APP_DIR" ]; then
  if [ -f "./run.py" ] && [ -f "./.env.example" ]; then
    APP_DIR="$(pwd)"
  elif [ -f "/opt/quizbot/run.py" ]; then
    APP_DIR="/opt/quizbot"
  else
    APP_DIR="/opt/quizbot"
  fi
fi

if [ "$CHECK_ONLY" -eq 0 ]; then
  if [ ! -d "$APP_DIR" ]; then
    log "Cloning $REPO_URL (branch $BRANCH) into $APP_DIR ..."
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
      sudo mkdir -p "$APP_DIR" && sudo chown "$(whoami):$(id -gn)" "$APP_DIR"
    else
      mkdir -p "$APP_DIR"
    fi
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
  fi
  if [ ! -f "$APP_DIR/run.py" ]; then
    fail "$APP_DIR does not look like this project (run.py missing). Aborting."
  fi
  if [ -d "$APP_DIR/.git" ]; then
    log "Fast-forwarding $APP_DIR to origin/$BRANCH (never force, never discard data) ..."
    git -C "$APP_DIR" fetch origin
    git -C "$APP_DIR" checkout "$BRANCH" --quiet
    # --ff-only guarantees we never rewrite history or lose local commits.
    git -C "$APP_DIR" pull --ff-only origin "$BRANCH" || fail \
      "git pull --ff-only failed (local changes? run 'git -C $APP_DIR status' on the VPS and resolve, then re-run)."
  fi
else
  [ -f "$APP_DIR/run.py" ] || fail "$APP_DIR/run.py not found; cannot --check-only."
  log "--check-only: skipping clone/pull."
fi

cd "$APP_DIR"
log "App directory: $APP_DIR"

# ---------------------------------------------------------------------------
# 1. .env: must exist, is NEVER overwritten. Fail fast if required keys lack
#    values. Secret VALUES are never printed — only key names + OK/MISSING.
# ---------------------------------------------------------------------------
[ -f "$APP_DIR/.env" ] || fail \
  ".env not found in $APP_DIR. Create it on the VPS from .env.example with REAL values (never commit it). Refusing to invent credentials."

# Fast fail-before-apt check using shell parsing (thorough python check later).
env_has() {
  local key="$1" line val
  line="$(grep -E "^[[:space:]]*${key}=" "$APP_DIR/.env" | tail -n1 || true)"
  [ -n "$line" ] || return 1
  val="$(printf '%s' "$line" | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -n "$val" ]
}

missing=0
if env_has BOT_TOKEN || env_has CREATOR_BOT_TOKEN || env_has RUNNER_BOT_TOKEN; then
  log ".env check: bot token present (value hidden) — OK"
else
  log ".env check: BOT_TOKEN (or CREATOR_BOT_TOKEN/RUNNER_BOT_TOKEN) — MISSING"
  missing=1
fi
if env_has MONGODB_URI; then log ".env check: MONGODB_URI present (value hidden) — OK";
else log ".env check: MONGODB_URI — MISSING"; missing=1; fi
if env_has OWNER_ID; then log ".env check: OWNER_ID present (value hidden) — OK";
else log ".env check: OWNER_ID — MISSING"; missing=1; fi
# Feature-relevant but optional: warn only, never block the whole deployment.
env_has GEMINI_API_KEY || log ".env note: GEMINI_API_KEY empty — /podcast voice generation will report its own error until set."
env_has PDF_API_BASE   || log ".env note: PDF_API_BASE empty — /testseries will reply that PDF generation is not configured (existing behavior)."
[ -z "${MINI_APP_DOMAIN_FROM_ENV:-}" ] || true
if grep -Eq '^[[:space:]]*MINI_APP_DOMAIN=[[:space:]]*"?https?://[^[:space:]"]' "$APP_DIR/.env"; then
  log ".env note: MINI_APP_DOMAIN set — Mini App HTTP server will also start INSIDE the same single process (no extra poller)."
else
  log ".env note: MINI_APP_DOMAIN blank — Mini App disabled (existing behavior, no Play buttons)."
fi
[ "$missing" -eq 0 ] || fail \
  "Required .env keys are missing (see above). Fill them in $APP_DIR/.env on the VPS and re-run. Refusing to invent credentials."

if [ "$CHECK_ONLY" -eq 1 ]; then
  log "--check-only: .env validation passed. Stopping before any system changes."
  log "Next: run without --check-only ON the VPS to deploy."
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. OS packages (Debian/Ubuntu). Idempotent; skipped with --no-apt.
#    Same system libraries the Dockerfile already declares.
# ---------------------------------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "Need root (or sudo) for apt + systemd. Re-run as root or install sudo."
  SUDO="sudo"
fi

if [ "$SKIP_APT" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
  log "Installing OS packages (ffmpeg, tesseract, weasyprint libs) ..."
  $SUDO apt-get update
  $SUDO apt-get install -y --no-install-recommends \
    git python3 python3-venv python3-pip \
    ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libcairo2 \
    libffi-dev shared-mime-info fonts-liberation
else
  log "Skipping apt-get (flag or non-Debian system). Ensuring ffmpeg/python exist ..."
  command -v python3 >/dev/null 2>&1 || fail "python3 not found."
  command -v ffmpeg  >/dev/null 2>&1 || fail "ffmpeg not found (required for /podcast MP3 creation)."
fi

# ---------------------------------------------------------------------------
# 3. Python venv (3.11–3.13) + requirements.txt. Venv is rebuilt ONLY when its
#    interpreter is outside the supported range — otherwise reused as-is.
# ---------------------------------------------------------------------------
PYBIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    case "$ver" in 3.11|3.12|3.13) PYBIN="$candidate"; break;; esac
  fi
done
[ -n "$PYBIN" ] || fail "Python 3.11–3.13 is required; no supported interpreter found."

VENV_PY="$APP_DIR/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  VENV_VER="$("$VENV_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
else
  VENV_VER=""
fi
if [ "$VENV_VER" != "3.11" ] && [ "$VENV_VER" != "3.12" ] && [ "$VENV_VER" != "3.13" ]; then
  log "Creating venv with $PYBIN ..."
  rm -rf "$APP_DIR/.venv"
  "$PYBIN" -m venv "$APP_DIR/.venv"
else
  log "Reusing existing venv ($VENV_VER)."
fi

log "Installing requirements.txt (existing pinned dependencies, unchanged) ..."
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r "$APP_DIR/requirements.txt"

# Thorough .env validation with the project's own dotenv parsing (names only).
log "Validating .env values with project parser (values never printed) ..."
"$VENV_PY" - <<'PY'
import sys
from dotenv import dotenv_values
v = dotenv_values('.env')
def has(*keys):
    return any((v.get(k) or '').strip().strip('"').strip("'").strip() for k in keys)
problems = []
if not has('BOT_TOKEN', 'CREATOR_BOT_TOKEN', 'RUNNER_BOT_TOKEN'):
    problems.append('BOT_TOKEN (or CREATOR_BOT_TOKEN/RUNNER_BOT_TOKEN) is empty')
uri = (v.get('MONGODB_URI') or '').strip()
if not uri:
    problems.append('MONGODB_URI is empty')
elif not uri.startswith(('mongodb://', 'mongodb+srv://')):
    problems.append('MONGODB_URI does not look like a MongoDB connection string')
if not (v.get('OWNER_ID') or '').strip().isdigit():
    problems.append('OWNER_ID must be your numeric Telegram user ID')
if problems:
    for p in problems:
        print(f'[deploy] .env problem: {p}')
    sys.exit(1)
print('[deploy] .env values validated (values hidden) — OK')
PY

# ---------------------------------------------------------------------------
# 4. Guard: prove this is the SINGLE-BOT build (one polling client) BEFORE
#    starting anything. Prevents accidentally deploying a dual-poller layout.
# ---------------------------------------------------------------------------
log "Verifying SINGLE-BOT build (exactly one polling client) ..."
"$VENV_PY" - <<'PY'
from pathlib import Path
run_py = Path('run.py').read_text()
bot_py = Path('quizbot/runner_bot/bot.py').read_text()
assert 'ONE Telegram bot / ONE polling client' in run_py, 'run.py is not the single-bot launcher'
assert 'run_creator_bot' not in run_py, 'legacy separate Creator runner is wired into run.py'
assert 'register_creator_bridge(application)' in bot_py, 'Creator bridge not registered'
assert 'start_polling' in bot_py, 'poller missing from runner bot'
print('[deploy] single-bot build verified: one PTB polling client (Creator+Runner bridged)')
PY

# ---------------------------------------------------------------------------
# 5. systemd unit: ONE service, ONE process, restart always + on boot.
#    Mini App (if MINI_APP_DOMAIN is set) starts inside this SAME process via
#    run.py — no second unit, no second poller, ever.
# ---------------------------------------------------------------------------
SERVICE_USER="$(stat -c '%U' "$APP_DIR")"
mkdir -p "$APP_DIR/data"
if [ "$(id -u)" -eq 0 ] && [ "$SERVICE_USER" != "root" ]; then
  chown -R "$SERVICE_USER:$(id -gn "$SERVICE_USER")" "$APP_DIR/data" "$APP_DIR/.venv" 2>/dev/null || true
fi

UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
log "Installing systemd unit $UNIT_PATH (user=$SERVICE_USER) ..."
$SUDO tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=Journey for LBSNAA Quiz Bot (single polling process)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run.py
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

# ---------------------------------------------------------------------------
# 6. Verify: service active + EXACTLY ONE bot process (duplicate-polling guard)
#    + recent logs. Any failure here exits non-zero with guidance.
# ---------------------------------------------------------------------------
sleep 6
if ! $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
  $SUDO systemctl status "$SERVICE_NAME" --no-pager || true
  $SUDO journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
  fail "Service $SERVICE_NAME is not active. See logs above (often: wrong .env values, MongoDB/Telegram unreachable)."
fi
log "Service state: active."

# Duplicate-polling guard: exactly one 'run.py' for this app directory.
MATCHING_PROCS="$(pgrep -af "[r]un.py" | grep -F "$APP_DIR" || true)"
COUNT="$(printf '%s\n' "$MATCHING_PROCS" | grep -c . || true)"
if [ "$COUNT" -eq 1 ]; then
  log "Polling guard: exactly ONE bot process for $APP_DIR — OK"
elif [ "$COUNT" -eq 0 ]; then
  $SUDO journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
  fail "Polling guard: no bot process found for $APP_DIR although the service is active. See logs above."
else
  printf '%s\n' "$MATCHING_PROCS" >&2
  fail "Polling guard: $COUNT bot processes match $APP_DIR — duplicate polling would cause Telegram getUpdates conflicts. Stop the extra one(s) (e.g. an old 'python run.py' shell session, start_single_bot.sh, or a duplicate container: 'docker ps' / 'docker stop <name>'), then re-run this script."
fi

log "Recent logs:"
$SUDO journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true

log "DEPLOY OK — $SERVICE_NAME is active, single process, restarts on crash/reboot."
log "Useful commands:"
log "  sudo systemctl status $SERVICE_NAME --no-pager"
log "  sudo journalctl -u $SERVICE_NAME -f"
log "  sudo systemctl restart $SERVICE_NAME"
