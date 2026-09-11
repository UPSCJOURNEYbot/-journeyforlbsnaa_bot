#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "[1/4] Stopping only this bot..."
pkill -f '[.]venv/bin/python run.py' 2>/dev/null || true
pkill -f '[p]ython run.py' 2>/dev/null || true
sleep 1

echo "[2/4] Verifying SINGLE-BOT build..."
./.venv/bin/python - <<'PY'
from pathlib import Path
p=Path('run.py').read_text()
assert 'ONE Telegram bot / ONE polling client' in p, 'This ZIP is not the Single-Bot build.'
assert 'run_creator_bot' not in p, 'Legacy separate Creator runner is still wired into run.py.'
assert 'register_creator_bridge(application)' in Path('quizbot/runner_bot/bot.py').read_text(), 'Creator bridge not registered.'
print('OK: one Telegram polling client')
PY

echo "[3/4] Checking environment..."
if [ ! -f .env ]; then echo 'ERROR: .env missing; restore your existing .env first.'; exit 1; fi
./.venv/bin/python - <<'PY'
from pathlib import Path
from dotenv import dotenv_values
v=dotenv_values('.env')
a=(v.get('CREATOR_BOT_TOKEN') or '').strip()
b=(v.get('RUNNER_BOT_TOKEN') or '').strip()
c=(v.get('BOT_TOKEN') or '').strip()
t=a or b or c
if not t: raise SystemExit('ERROR: no bot token found in .env')
print('OK: bot token present (value hidden)')
PY

echo "[4/4] Installing requirements and starting..."
./.venv/bin/pip install -q -r requirements.txt
exec ./.venv/bin/python run.py
