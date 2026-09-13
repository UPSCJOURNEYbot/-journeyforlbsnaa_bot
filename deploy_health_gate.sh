#!/usr/bin/env bash
#
# deploy_health_gate.sh — prove a freshly restarted systemd service is ACTIVE
# and STABLE before a deploy is allowed to report success.
#
# Why this exists
# ---------------
# `systemctl restart` + a single `systemctl is-active` check a few seconds
# later is NOT enough for a Type=simple unit with Restart=always:
#   * systemd marks the unit active the moment the process starts, even if it
#     crashes a few seconds later (e.g. after a slow MongoDB Atlas handshake);
#   * Restart=always then crash-loops the process (running for a few seconds,
#     failing, waiting RestartSec, running again) — a one-shot check can land
#     in any "active" window and wrongly print DEPLOY OK.
#
# This gate therefore requires, continuously:
#   1. the unit reaches active(running) within a startup grace period;
#   2. it STAYS active(running) for a full stability window;
#   3. the main PID does not change and stays kill-able (no restart cycle);
#   4. systemd's NRestarts counter does not increase (no crash-loop);
#   5. exactly ONE matching app process is running (duplicate-polling guard);
#   6. all of the above STILL hold in a final re-check AFTER the window
#      (the process must remain alive after the health check, too).
#
# Any violation prints status + recent journal and exits 1 with an explicit
# "DEPLOY FAILED" message. The gate never touches the unit's Restart= policy:
# a genuinely healthy deployment keeps restart-on-crash/reboot behaviour.
#
# Usage:
#   deploy_health_gate.sh SERVICE_NAME APP_DIR
#
# Tunables (env overrides, mainly for fast automated tests):
#   HEALTH_START_GRACE  seconds to wait for active(running)      (default 60)
#   HEALTH_STABLE_SECS  continuous healthy seconds required      (default 30)
#   HEALTH_INTERVAL     seconds between probes                   (default 2)
#   SUDO                privilege prefix to use for systemctl     ("" if root)
#
set -euo pipefail

SERVICE_NAME="${1:?usage: deploy_health_gate.sh SERVICE_NAME APP_DIR}"
APP_DIR="${2:?usage: deploy_health_gate.sh SERVICE_NAME APP_DIR}"
START_GRACE="${HEALTH_START_GRACE:-60}"
STABLE_SECS="${HEALTH_STABLE_SECS:-30}"
INTERVAL="${HEALTH_INTERVAL:-2}"
SUDO="${SUDO:-}"

log()  { printf '%s\n' "[health-gate] $*"; }
fail() {
  printf '%s\n' "[health-gate] DEPLOY FAILED: $*" >&2
  $SUDO systemctl status "$SERVICE_NAME" --no-pager >&2 2>/dev/null || true
  $SUDO journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 2>/dev/null || true
  exit 1
}

prop() {
  # Single-value systemd property, e.g. prop ActiveState -> active
  $SUDO systemctl show -p "$1" --value "$SERVICE_NAME" 2>/dev/null | tr -d '[:space:]'
}

matching_process_count() {
  # Exactly one 'run.py' whose command line belongs to THIS app directory.
  local matches
  matches="$(pgrep -af "[r]un.py" 2>/dev/null | grep -F "$APP_DIR" || true)"
  if [ -z "$matches" ]; then printf '0\n'; else
    printf '%s\n' "$matches" | grep -c .
  fi
}

snapshot() {
  # Echo "state substate pid restarts proccount" for a single point in time.
  local state sub pid restarts count
  state="$(prop ActiveState)"
  sub="$(prop SubState)"
  pid="$(prop ExecMainPID)"
  restarts="$(prop NRestarts)"
  count="$(matching_process_count)"
  printf '%s %s %s %s %s\n' "$state" "$sub" "$pid" "$restarts" "$count"
}

# ---------------------------------------------------------------------------
# Phase 1: reach active(running) within the startup grace period.
# ---------------------------------------------------------------------------
grace_deadline=$(( $(date +%s) + START_GRACE ))
while :; do
  read -r state sub pid restarts count <<< "$(snapshot)"
  if [ "$state" = "active" ] && [ "$sub" = "running" ] && [ "${pid:-0}" != "0" ]; then
    log "Service reached active(running) (pid=$pid, NRestarts=$restarts)."
    break
  fi
  if [ "$(date +%s)" -ge "$grace_deadline" ]; then
    fail "$SERVICE_NAME did not become active(running) within ${START_GRACE}s (last: state=${state:-?} sub=${sub:-?} pid=${pid:-?} NRestarts=${restarts:-?} proc=${count:-?}). Startup error (check .env/MongoDB/Telegram/dependency compatibility above)."
  fi
  sleep "$INTERVAL"
done

base_pid="$pid"
base_restarts="$restarts"

check_stable() {
  # One probe; returns success only if EVERY invariant holds. On failure it
  # reports the concrete violation (used both during and after the window).
  local label="$1"
  read -r c_state c_sub c_pid c_restarts c_count <<< "$(snapshot)"
  if [ "$c_state" != "active" ] || [ "$c_sub" != "running" ]; then
    fail "$label: service not active(running) (state=$c_state sub=$c_sub). The process crashed at/after startup — restart-on-crash is masking the failure."
  fi
  if [ "$c_pid" != "$base_pid" ] || [ "${c_pid:-0}" = "0" ]; then
    fail "$label: main PID changed/disappeared (was $base_pid, now ${c_pid:-0}) — the service restarted (crash loop)."
  fi
  if ! kill -0 "$c_pid" 2>/dev/null; then
    fail "$label: main PID $c_pid is not alive despite active state."
  fi
  if [ "$c_restarts" != "$base_restarts" ]; then
    fail "$label: NRestarts increased ($base_restarts -> $c_restarts) — the service crash-looped during the health window."
  fi
  if [ "$c_count" != "1" ]; then
    if [ "$c_count" = "0" ]; then
      fail "$label: no run.py process found for $APP_DIR although systemd reports active (state=$c_state)."
    fi
    pgrep -af "[r]un.py" 2>/dev/null | grep -F "$APP_DIR" >&2 || true
    fail "$label: $c_count run.py processes match $APP_DIR — duplicate polling would cause Telegram getUpdates conflicts."
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Phase 2: remain healthy for the whole stability window.
# ---------------------------------------------------------------------------
stable_deadline=$(( $(date +%s) + STABLE_SECS ))
probe_no=0
while [ "$(date +%s)" -lt "$stable_deadline" ]; do
  probe_no=$((probe_no + 1))
  check_stable "stability probe #$probe_no"
  sleep "$INTERVAL"
done

# ---------------------------------------------------------------------------
# Phase 3: final re-check AFTER the window — the process must still be alive
# (guards against a process that dies immediately after a short check).
# ---------------------------------------------------------------------------
sleep "$INTERVAL"
check_stable "final post-window re-check"

log "Service ACTIVE and STABLE for ${STABLE_SECS}s (pid=$base_pid, NRestarts=$base_restarts, exactly one run.py process)."
log "HEALTH GATE PASSED — safe to report DEPLOY OK. Restart-on-crash/reboot policy is unchanged."
