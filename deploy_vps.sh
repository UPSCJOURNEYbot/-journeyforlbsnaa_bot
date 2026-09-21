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
#   4. Installs OS packages (ffmpeg, tesseract) + the native WeasyPrint/
#      Pango PDF runtime (single source of truth: tools/pdf_native_runtime.sh)
#      + Python venv + requirements.txt — the same dependencies the repo
#      already declares.
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

# Path + content hash of THIS script as it was when the shell started it.
# The fast-forward pull below can replace the script on disk, but a running
# bash keeps reading the bytes it started with, so post-pull steps (pip
# gates, health check, DEPLOY OK string) can silently execute from the OLD
# version. If the pull changed us, we re-exec the new file once.
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
    # Run the rest of THIS deploy from the just-pulled script content.
    if [ "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")" = "$SELF_PATH" ]; then
      self_reexec_if_updated "$@"
    else
      # Invoked from a copy outside the checkout (e.g. /root) — always
      # switch to the freshly pulled canonical script inside APP_DIR.
      [ -n "${DEPLOY_SELF_REEXECED:-}" ] || {
        log "Switching to the freshly pulled canonical script in $APP_DIR ..."
        DEPLOY_SELF_REEXECED=1 exec bash "$APP_DIR/deploy_vps.sh" "$@"; }
    fi
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
if grep -Eq '^[[:space:]]*PDF_API_BASE=[[:space:]]*(off|none|disabled|false|0)[[:space:]]*$' "$APP_DIR/.env"; then
  log ".env note: PDF_API_BASE explicitly disabled — /testseries will reply that PDF generation is not configured."
else
  log ".env note: PDF generation targets the local microservice at 127.0.0.1:8090 (blank defaults there)."
  log "           Run ./deploy_pdf_service.sh on THIS host to install quizbot-pdf.service, otherwise /testseries reports the PDF service unreachable."
fi
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
  # NOTE: the WeasyPrint native library/font set (libpango*, libharfbuzz0b,
  # fonts-noto-*) is NOT listed here — it is provisioned in step 3a by
  # tools/pdf_native_runtime.sh, the single source of truth for that set
  # (it also handles newer-release package renames such as
  # libgdk-pixbuf2.0-0 -> libgdk-pixbuf-4.0-0 and verifies the result).
  log "Installing OS packages (ffmpeg, tesseract) ..."
  $SUDO apt-get update
  $SUDO apt-get install -y --no-install-recommends \
    git python3 python3-venv python3-pip \
    ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
    libffi-dev shared-mime-info
else
  log "Skipping apt-get (flag or non-Debian system). Ensuring ffmpeg/python exist ..."
  command -v python3 >/dev/null 2>&1 || fail "python3 not found."
  command -v ffmpeg  >/dev/null 2>&1 || fail "ffmpeg not found (required for /podcast MP3 creation)."
fi

# ---------------------------------------------------------------------------
# 2b. Swap on small VMs (target box is 2 GB/1 vCPU). WeasyPrint report renders
#     and ffmpeg /podcast encoding can spike RSS; without swap the OOM killer
#     can terminate the bot (systemd restarts it, but an in-flight job is
#     lost). Idempotent and NON-fatal: skip if swap already exists, if not
#     root, on non-Debian hosts, or when SKIP_SWAP=1.
# ---------------------------------------------------------------------------
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
if [ "${SKIP_SWAP:-0}" -eq 0 ] && [ -z "$(swapon --show 2>/dev/null)" ] && [ ! -f /swapfile ]; then
  if [ "$(id -u)" -eq 0 ] || command -v sudo >/dev/null 2>&1; then
    log "No swap detected and this is a small VPS -- creating a ${SWAP_SIZE_MB}MB /swapfile (non-fatal) ..."
    if $SUDO dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_SIZE_MB" status=none 2>/dev/null \
       && $SUDO chmod 600 /swapfile \
       && $SUDO mkswap /swapfile >/dev/null 2>&1 \
       && $SUDO swapon /swapfile >/dev/null 2>&1; then
      if ! grep -q '^/swapfile ' /etc/fstab 2>/dev/null; then
        echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null 2>&1 || true
      fi
      $SUDO sysctl -q vm.swappiness=10 2>/dev/null || true
      log "Swap enabled (${SWAP_SIZE_MB}MB)."
    else
      $SUDO rm -f /swapfile 2>/dev/null || true
      log "Swap setup failed/unsupported here (continuing; the service auto-restarts if the OOM killer ever fires)."
    fi
  fi
else
  log "Swap already configured (or SKIP_SWAP=1) -- leaving as-is."
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
# requirements.txt now pins WeasyPrint 62.3's ENTIRE transitive PDF stack
# (pydyf/tinycss2/cssselect2/pyphen/fonttools/cffi/html5lib). Re-running this
# command also DOWNGRADEs any previously-drifted versions (e.g. pydyf 0.12.x,
# whose removed Stream.transform() broke every report PDF in production with
# "AttributeError: 'super' object has no attribute 'transform'").
"$VENV_PY" -m pip install -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 3a. Native PDF runtime (single source of truth: tools/pdf_native_runtime.sh).
#     WeasyPrint's pip side was just installed above; its NATIVE side (the
#     shared libraries pango/harfbuzz/glib/fontconfig + Hindi/emoji fonts)
#     is provisioned and PROVEN here — exact sonames loaded via ctypes,
#     `import weasyprint`, and a tiny real Devanagari render. This runs
#     BEFORE the service restarts so the deploy aborts loudly instead of
#     landing a bot that starts fine but fails every result PDF with the
#     generic "rendering library is unavailable" message. With --no-apt the
#     host is pre-provisioned: the same verification must still PASS.
# ---------------------------------------------------------------------------
[ -f "$APP_DIR/tools/pdf_native_runtime.sh" ] || fail \
  "tools/pdf_native_runtime.sh missing (checkout incomplete?). Re-run the pull step."
if [ "$SKIP_APT" -eq 0 ]; then
  log "Provisioning + verifying the native WeasyPrint/Pango PDF runtime (tools/pdf_native_runtime.sh) ..."
  bash "$APP_DIR/tools/pdf_native_runtime.sh" install
else
  log "--no-apt: verifying the pre-provisioned native WeasyPrint/Pango PDF runtime ..."
  bash "$APP_DIR/tools/pdf_native_runtime.sh" verify
fi

# ---------------------------------------------------------------------------
# 3b. PDF stack self-check (runs BEFORE the service is restarted).
#     WeasyPrint's runtime dependencies are partly system libraries (pango,
#     installed in step 2) and partly pip packages. A version drift in either
#     layer used to ship as a bot that starts fine but fails every result PDF.
#     Prove the EXACT declared dependency set actually renders a real page
#     (English + Devanagari + table + a coordinate transform + a MathML tag)
#     here, at deploy time, and abort loudly rather than restart into a
#     broken PDF stack. The bot itself keeps its in-process fail-soft path;
#     this gate exists so an operator never silently lands on it.
# ---------------------------------------------------------------------------
log "Verifying the WeasyPrint result-PDF stack (dependency pins + real render) ..."
"$VENV_PY" - <<'PY'
import sys

errors = []

# (a) Declared pydyf API that weasyprint/pdf/stream.py requires.
import pydyf
try:
    parts = tuple(int(x) for x in pydyf.__version__.split(".")[:2])
except Exception:
    parts = (99, 99)
if not hasattr(pydyf.Stream, "transform") or parts >= (0, 12):
    errors.append(
        f"pydyf {pydyf.__version__} is incompatible with WeasyPrint 62.3 "
        "(Stream.transform removed in pydyf 0.12). requirements.txt pins "
        "pydyf==0.10.0; re-run the pip install step.")
else:
    s = pydyf.Stream()
    s.transform(1, 0, 0, 1, 0, 0)  # raises the production error on 0.12.x

# (b) Dependency-consistency check (a conflicting pin breaks the render stack
#     silently later). Only PDF-stack problems block the deploy; unrelated
#     warnings are reported but non-fatal.
import subprocess
check = subprocess.run([sys.executable, "-m", "pip", "check"],
                       capture_output=True, text=True)
if check.returncode != 0:
    keys = ("weasyprint", "pydyf", "tinycss2", "cssselect2", "pyphen",
            "fonttools", "cffi", "html5lib", "pillow",
            "pymupdf", "latex2mathml")
    lines = [ln for ln in (check.stdout + check.stderr).splitlines()
             if any(k in ln.lower() for k in keys)]
    if lines:
        errors.append("pip dependency problems: " + " | ".join(lines))
    else:
        print("[deploy] note: 'pip check' reports unrelated warnings "
              "(non-PDF packages); not blocking deployment.")

# (c) REAL end-to-end WeasyPrint render through pango + the pydyf writer.
try:
    from weasyprint import HTML
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "@page { size: A4; margin: 2cm; }"
        "body { font-family: sans-serif; }"
        ".rot { transform: rotate(3deg); width: 60mm; }"
        "table { border-collapse: collapse; } td { border: 1px solid black; padding: 4px; }"
        "</style></head><body>"
        "<h1>PDF smoke test</h1>"
        "<p>English and हिन्दी text: भारत की राजधानी नई दिल्ली है।</p>"
        "<div class='rot'>rotated block (exercises stream.transform)</div>"
        "<table><tr><th>Q</th><th>A</th></tr>"
        "<tr><td>भारत?</td><td>दिल्ली</td></tr></table>"
        "<p>MathML tag: <math display='inline'><mfrac><mi>x</mi><mn>2</mn></mfrac></math></p>"
        "</body></html>")
    pdf = HTML(string=html).write_pdf()
    assert pdf[:5] == b"%PDF-" and len(pdf) > 2000, "not a PDF"
    import fitz  # PyMuPDF, already a project dependency
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert doc.page_count >= 1
    text = "".join(p.get_text() for p in doc)
    assert "PDF smoke test" in text
    print(f"[deploy] WeasyPrint render OK: {len(pdf)} bytes, {doc.page_count} page(s), Devanagari shaped.")
except Exception as exc:  # noqa: BLE001
    errors.append(f"live WeasyPrint render failed: {type(exc).__name__}: {exc}. "
                  "Ensure OS packages from step 2 (libpango/libpangocairo/libcairo/"
                  "libgdk-pixbuf2.0, fonts-noto-core/fonts-deva) are installed.")

# (d) REAL Quiz Result PDF end-to-end: exercises the production template
#     (bundled Hind @font-face, leaderboard table geometry, content-aware
#     page breaks) and proves Hindi survives both visually and in the
#     ToUnicode text layer; also that Q&A begins on page 2, not page 1.
try:
    import tempfile
    from quizbot.runner_bot import pdf_reports
    from quizbot.runner_bot import wp_indic_tounicode as wpi
    questions = [
        {"question": "भारत की राजधानी क्या है?",
         "options": ["मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई"],
         "correct_option_id": 1,
         "explanation": "नई दिल्ली भारत की राजधानी है।"},
        {"question": "Q1. Stored badge must not duplicate on paper?",
         "options": ["a", "b", "c", "d"], "correct_option_id": 0,
         "explanation": "The renderer strips the stored badge."},
    ]
    leaderboard = [
        {"name": "राहुल", "correct": 1, "wrong": 0, "score": 1.0,
         "total_time": 9},
        {"name": "Amit", "correct": 0, "wrong": 1, "score": -0.25,
         "total_time": 21},
    ]
    polls = {f"p{i}": {"question_index": i, "correct_option": [
        questions[i]["correct_option_id"]], "sent_time": i}
        for i in range(len(questions))}
    with tempfile.TemporaryDirectory() as tmp:
        out = f"{tmp}/deploy_gate_report.pdf"
        ok = pdf_reports.render_quiz_pdf(
            "Deploy Gate क्विज़", "Gate चैट", questions, leaderboard, polls,
            0.25, 1.0, out, shuffle_options=False, style="classic")
        assert ok, "render_quiz_pdf returned False"
        pdf = open(out, "rb").read()
    audit = wpi.audit_report_pdf(
        pdf,
        expected_terms=["भारत की राजधानी", "नई दिल्ली",
                        "नई दिल्ली भारत की राजधानी", "Deploy Gate"],
        must_contain=["Answer:", "Explanation:", "Questions",
                      "Leaderboard", "2 Questions"])
    assert audit["ok"], f"result PDF audit failed: {audit}"
    pages = wpi.page_texts(pdf)
    assert len(pages) >= 2, "Q&A must start on page 2"
    assert "Answer:" not in pages[0], "answer leaked onto page 1"
    assert "Answer:" in "".join(pages[1:])
    print("[deploy] Quiz Result PDF render OK: Hindi roundtrip verified in "
          "the text layer, Q&A starts on page 2.")
except Exception as exc:  # noqa: BLE001
    errors.append(
        f"live Quiz Result PDF render failed: {type(exc).__name__}: {exc}.")

if errors:
    for e in errors:
        print(f"[deploy] PDF STACK CHECK FAILED: {e}")
    sys.exit(1)
print("[deploy] PDF stack self-check passed (WeasyPrint 62.3 + pinned pydyf, real render verified).")
PY

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
# 6. Verify the service is ACTIVE AND STABLE (no false "DEPLOY OK").
#    A single is-active probe a few seconds after restart is unsafe for a
#    Type=simple unit with Restart=always: a process that crashes AFTER the
#    probe (or is mid crash-loop between attempts) still reports active.
#    deploy_health_gate.sh requires active(running) continuously across a
#    stability window, with an unchanged live main PID, no NRestarts growth,
#    and exactly one matching run.py process — re-checked one final time
#    after the window. It dumps status+journal and exits non-zero otherwise.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/deploy_health_gate.sh" ] || fail "Missing $SCRIPT_DIR/deploy_health_gate.sh (checkout incomplete?)."
if ! SUDO="$SUDO" HEALTH_START_GRACE="${HEALTH_START_GRACE:-60}" \
     HEALTH_STABLE_SECS="${HEALTH_STABLE_SECS:-30}" HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}" \
     "$SCRIPT_DIR/deploy_health_gate.sh" "$SERVICE_NAME" "$APP_DIR"; then
  fail "Service did not pass the stability health gate — DEPLOY FAILED. $SERVICE_NAME was not proven healthy; inspect the journal output above, fix the startup error, and re-run this script (it is idempotent and did not change .env or data)."
fi

log "Recent logs:"
$SUDO journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true

log "DEPLOY OK — $SERVICE_NAME passed the stability gate (single process, active through the health window), restarts on crash/reboot."
log "Useful commands:"
log "  sudo systemctl status $SERVICE_NAME --no-pager"
log "  sudo journalctl -u $SERVICE_NAME -f"
log "  sudo systemctl restart $SERVICE_NAME"
