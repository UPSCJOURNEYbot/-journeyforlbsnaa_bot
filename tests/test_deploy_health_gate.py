"""
Tests for deploy_health_gate.sh — the systemd stability gate that must never
print DEPLOY OK for a crash-looping service.

The old deploy check did one `systemctl is-active` probe ~6s after restart.
A Type=simple unit with Restart=always is "active" the instant a fresh
attempt starts, even if the process crashes seconds later (the production
incident: startup TypeError, exit 1, then crash-loop — yet DEPLOY OK).

These tests run the REAL gate script against fake systemctl/pgrep/journalctl
on PATH with an instant fake clock, covering: healthy, crash during the
window, never-active, duplicate processes, and crash only after the window.
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "deploy_health_gate.sh"
APP_DIR = "/opt/quizbot"

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
state_dir = pathlib.Path(os.environ["FAKE_STATE_DIR"])
clock = int((state_dir / "clock").read_text().strip())
scenario = (state_dir / "scenario").read_text().strip()
args = sys.argv[1:]

if args[:1] == ["show"]:
    prop = None
    if "-p" in args:
        prop = args[args.index("-p") + 1]
    base_pid = int((state_dir / "pid").read_text().strip())
    # default: healthy
    state, sub, pid, restarts = "active", "running", base_pid, 0
    if scenario == "never":
        state, sub, pid, restarts = "activating", "auto-restart", 0, 0
    elif scenario == "crashloop" and clock >= 2:
        state, sub, pid, restarts = "activating", "auto-restart", 0, 1
    elif scenario == "latecrash" and clock >= 4:
        state, sub, pid, restarts = "failed", "failed", 0, 1
    values = {"ActiveState": state, "SubState": sub,
              "ExecMainPID": pid, "NRestarts": restarts}
    print(values[prop])
    sys.exit(0)
if args[:1] == ["status"]:
    print("● quizbot.service - fake status for scenario", scenario)
    sys.exit(0)
sys.exit(0)
'''

FAKE_PGREP = r'''#!/usr/bin/env python3
import os, pathlib, re, sys
state_dir = pathlib.Path(os.environ["FAKE_STATE_DIR"])
pattern = sys.argv[-1] if len(sys.argv) > 1 else ".*"
app_dir = os.environ["FAKE_APP_DIR"]
# procs file lines: "<pid>|<cmdline>"
for line in (state_dir / "procs").read_text().splitlines():
    if not line.strip():
        continue
    pid, _, cmdline = line.partition("|")
    # Honour the pgrep -f regex (bracket forms like [r]un.py are literal once
    # the char class matches its single character) and the APP_DIR filter the
    # gate applies afterwards.
    if re.search(pattern, cmdline) and app_dir in cmdline:
        print(f"{pid} {cmdline}")
'''

FAKE_JOURNALCTL = r'''#!/usr/bin/env python3
print("-- fake journal: startup TypeError reproduction --")
print("TypeError: MotorCollection object is not callable")
print("Main process exited, code=exited, status=1/FAILURE")
'''

FAKE_DATE = r'''#!/usr/bin/env python3
import os, pathlib, sys
state_dir = pathlib.Path(os.environ["FAKE_STATE_DIR"])
clock_file = state_dir / "clock"
if sys.argv[1:] == ["+%s"]:
    value = int(clock_file.read_text().strip())
    clock_file.write_text(str(value + 1))
    print(value)
else:
    import subprocess
    sys.exit(subprocess.call(["/bin/date"] + sys.argv[1:]))
'''

FAKE_SLEEP = r'''#!/usr/bin/env sh
exit 0
'''


class HealthGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bindir = pathlib.Path(self.tmp.name) / "bin"
        self.state_dir = pathlib.Path(self.tmp.name) / "state"
        self.bindir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("pgrep", FAKE_PGREP),
                           ("journalctl", FAKE_JOURNALCTL), ("date", FAKE_DATE),
                           ("sleep", FAKE_SLEEP)):
            p = self.bindir / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        # Real long-lived decoy processes so the shell builtin `kill -0`
        # succeeds for the "running" PID (the gate checks liveness itself).
        self.decoys = []
        (self.state_dir / "clock").write_text("0")

    def tearDown(self):
        for proc in self.decoys:
            proc.kill()
        self.tmp.cleanup()

    def _spawn_decoy(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.decoys.append(proc)
        return proc.pid

    def _run_gate(self, scenario, n_procs=1, stable=2, interval=1, grace=5,
                  process_match=None, cmdline=None):
        pid = self._spawn_decoy()
        if cmdline is None:
            cmdline = f"python {APP_DIR}/run.py"
        proc_lines = [f"{pid}|{cmdline}"]
        if n_procs > 1:
            extra = self._spawn_decoy()
            proc_lines.append(f"{extra}|{cmdline}")
        (self.state_dir / "pid").write_text(str(pid))
        (self.state_dir / "procs").write_text("\n".join(proc_lines) + "\n")
        (self.state_dir / "scenario").write_text(scenario)
        env = os.environ.copy()
        env.update({
            "FAKE_STATE_DIR": str(self.state_dir),
            "FAKE_APP_DIR": APP_DIR,
            "PATH": f"{self.bindir}:{env['PATH']}",
            "HEALTH_START_GRACE": str(grace),
            "HEALTH_STABLE_SECS": str(stable),
            "HEALTH_INTERVAL": str(interval),
            "SUDO": "",
        })
        argv = ["bash", str(GATE), "quizbot", APP_DIR]
        if process_match:
            argv.append(process_match)
        return subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=60)

    def test_healthy_service_passes(self):
        r = self._run_gate("healthy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HEALTH GATE PASSED", r.stdout)
        self.assertNotIn("DEPLOY FAILED", r.stdout + r.stderr)

    def test_crash_loop_during_window_is_failure(self):
        r = self._run_gate("crashloop")
        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertIn("DEPLOY FAILED", combined)
        # Journal must be surfaced for the operator.
        self.assertIn("status=1/FAILURE", combined)

    def test_never_active_within_grace_is_failure(self):
        r = self._run_gate("never")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DEPLOY FAILED", r.stdout + r.stderr)

    def test_duplicate_processes_is_failure(self):
        r = self._run_gate("healthy", n_procs=2)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DEPLOY FAILED", r.stdout + r.stderr)
        self.assertIn("processes matching", r.stdout + r.stderr)
        self.assertIn("duplicate instances", r.stdout + r.stderr)

    def test_crash_after_window_caught_by_final_recheck(self):
        r = self._run_gate("latecrash")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DEPLOY FAILED", r.stdout + r.stderr)

    def test_custom_process_pattern_for_pdf_service(self):
        cmdline = f"/opt/quizbot/.venv/bin/python -m pdf_service.server"
        r = self._run_gate("healthy",
                           process_match="pdf_service[.]server",
                           cmdline=cmdline)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pdf_service[.]server", r.stdout)

    def test_wrong_process_pattern_counts_zero(self):
        # Gate looking for the PDF service must not be satisfied by a bot
        # run.py process (and vice versa).
        r = self._run_gate("healthy",
                           process_match="pdf_service[.]server",
                           cmdline=f"python {APP_DIR}/run.py")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DEPLOY FAILED", r.stdout + r.stderr)


class DeployScriptWiringTests(unittest.TestCase):
    """deploy_vps.sh may only claim DEPLOY OK after the gate exits 0."""

    def setUp(self):
        self.deploy = (REPO_ROOT / "deploy_vps.sh").read_text()
        self.gate = GATE.read_text()

    def test_gate_script_exists_and_is_executable(self):
        self.assertTrue(GATE.is_file())
        self.assertTrue(GATE.stat().st_mode & stat.S_IXUSR)

    def test_deploy_invokes_gate_before_success(self):
        # Compare executable statements, not comment prose mentioning them.
        invoke = '"$SCRIPT_DIR/deploy_health_gate.sh" "$SERVICE_NAME" "$APP_DIR"'
        self.assertIn(invoke, self.deploy)
        success_lines = [i for i, ln in enumerate(self.deploy.splitlines())
                         if "DEPLOY OK" in ln and ln.lstrip().startswith("log ")]
        self.assertEqual(len(success_lines), 1,
                         "exactly one executable DEPLOY OK success line")
        invoke_pos = self.deploy.index(invoke)
        success_pos = self.deploy.index(
            next(ln for ln in self.deploy.splitlines() if "DEPLOY OK" in ln
                 and ln.lstrip().startswith("log ")))
        self.assertLess(invoke_pos, success_pos)
        # And the gate runs INSIDE a failing branch that calls fail().
        guard_block = self.deploy[invoke_pos:success_pos]
        self.assertIn("then", guard_block)
        self.assertIn("DEPLOY FAILED", guard_block)

    def test_gate_enforces_all_required_invariants(self):
        for required in ("ActiveState", "SubState", "ExecMainPID", "NRestarts",
                         "HEALTH_STABLE_SECS", "final post-window re-check",
                         "kill -0", "[r]un.py"):
            self.assertIn(required, self.gate, required)

    def test_old_oneshot_6s_check_removed(self):
        # The false-positive pattern (single sleep 6 then is-active) is gone.
        self.assertNotIn("sleep 6\nif ! $SUDO systemctl is-active", self.deploy)

    def test_restart_policy_untouched(self):
        # Hardening must not disable production's restart-on-crash.
        self.assertIn("Restart=always", self.deploy)


class PdfServiceDeployWiringTests(unittest.TestCase):
    """The PDF microservice deploy must use the same stability gate."""

    def setUp(self):
        self.pdf_deploy = (REPO_ROOT / "deploy_pdf_service.sh").read_text()

    def test_pdf_deploy_invokes_gate_with_pdf_pattern(self):
        self.assertIn('deploy_health_gate.sh" "$SERVICE_NAME" "$APP_DIR" '
                      '"pdf_service[.]server"', self.pdf_deploy)

    def test_pdf_deploy_no_oneshot_sleep5_active_check(self):
        self.assertNotIn('sleep 5\n$SUDO systemctl is-active', self.pdf_deploy)

    def test_pdf_success_only_after_gate_and_functional_verify(self):
        gate_pos = self.pdf_deploy.index("pdf_service[.]server")
        verify_pos = self.pdf_deploy.index("verify_service\n", gate_pos)
        ok_pos = self.pdf_deploy.index("PDF DEPLOY OK")
        self.assertLess(gate_pos, verify_pos)
        self.assertLess(verify_pos, ok_pos)

    def test_pdf_service_keeps_restart_policy(self):
        self.assertIn("Restart=always", self.pdf_deploy)


if __name__ == "__main__":
    unittest.main()
