#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Keep the user's existing .env untouched.
if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Your bot secrets are required."
  exit 1
fi

# Prefer a supported Python instead of Codespace's Python 3.14 when available.
PYBIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    case "$ver" in
      3.11|3.12|3.13) PYBIN="$candidate"; break;;
    esac
  fi
done
if [[ -z "$PYBIN" ]]; then
  echo "ERROR: Python 3.11–3.13 is required; no supported interpreter was found."
  exit 1
fi

VENV_PY=".venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
  VENV_VER="$($VENV_PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
else
  VENV_VER=""
fi
if [[ "$VENV_VER" != "3.11" && "$VENV_VER" != "3.12" && "$VENV_VER" != "3.13" ]]; then
  rm -rf .venv
  "$PYBIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y ffmpeg
  else
    echo "ERROR: ffmpeg is required for MP3 creation and sudo is unavailable."
    exit 1
  fi
fi

exec .venv/bin/python run.py
