#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
pkill -f '[r]un.py' 2>/dev/null || true
exec ./.venv/bin/python run.py
