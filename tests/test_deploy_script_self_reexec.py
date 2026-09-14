"""
The deploy scripts fast-forward their own checkout (``git pull``) while
running. bash keeps reading the file bytes it started with, aap jo abhi
dekha: a checkout pulled to the new code (the bot reached "Runner Bot polling
started", i.e. the Motor fix was live) yet printed the OLD one-shot
``DEPLOY OK — quizbot is active, single process`` line without ever running
the 30 s stability gate or the new PDF self-check output.

Both deploy scripts therefore snapshot their own sha256 at startup and
``exec`` themselves once if the fast-forward changed them. These tests pin
that guard statically and prove the mechanism in a temp dir with a real
self-overwriting bash script.
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Mirror of the guard shipped in both deploy scripts (plain string, single
# braces — it is written verbatim into the temp bash script).
_GUARD_TEMPLATE = '''
SELF_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SELF_HASH_BEFORE="$(sha256sum "$SELF_PATH" | awk '{print $1}')"
log() { printf '%s\\n' "$*"; }
self_reexec_if_updated() {
  [ -z "${DEPLOY_SELF_REEXECED:-}" ] || return 0
  local after
  after="$(sha256sum "$SELF_PATH" | awk '{print $1}')"
  if [ -n "$SELF_HASH_BEFORE" ] && [ "$after" != "$SELF_HASH_BEFORE" ]; then
    log "REEXEC-NEW-VERSION"
    DEPLOY_SELF_REEXECED=1 exec bash "$SELF_PATH" "$@"
  fi
}
'''


def _script(tail: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
        + _GUARD_TEMPLATE
        + f"""
# Simulated `git pull`: on request, overwrite this running file with v2.
if [ -f "$DIR/update.flag" ]; then
  cp "$DIR/v2.sh" "$SELF_PATH"
  self_reexec_if_updated "$@"
fi
echo "{tail}:$*"
"""
    )


class SelfReexecMechanismTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        v1 = self.dir / "deploy.sh"
        v1.write_text(_script("V1-OLD-TAIL"))
        v2 = self.dir / "v2.sh"
        # v2 has the SAME guard and pull block but a NEW tail; when it runs,
        # copying itself over itself leaves the hash unchanged (no loop).
        v2.write_text(_script("V2-NEW-TAIL"))
        for p in (v1, v2):
            p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        self.tmp.cleanup()

    def test_self_update_runs_new_tail_not_old(self):
        (self.dir / "update.flag").touch()
        r = subprocess.run(["bash", str(self.dir / "deploy.sh"),
                            "--check-only", "--app-dir", "/tmp/x"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REEXEC-NEW-VERSION", r.stdout)
        self.assertIn("V2-NEW-TAIL:--check-only --app-dir /tmp/x", r.stdout,
                      "arguments must survive the re-exec")
        self.assertNotIn("V1-OLD-TAIL", r.stdout,
                         "the stale pre-pull tail must never execute")

    def test_no_update_runs_normally_without_reexec(self):
        r = subprocess.run(["bash", str(self.dir / "deploy.sh")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("V1-OLD-TAIL", r.stdout)
        self.assertNotIn("REEXEC-NEW-VERSION", r.stdout)

    def test_no_infinite_loop_when_marker_set(self):
        (self.dir / "update.flag").touch()
        env = dict(os.environ, DEPLOY_SELF_REEXECED="1")
        # Marker set on the ORIGINAL v1: guard must no-op even though the
        # simulated pull overwrites the file (one-shot loop breaker).
        r = subprocess.run(["bash", str(self.dir / "deploy.sh")],
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Key property: the guard is one-shot and never chains another exec.
        self.assertNotIn("REEXEC-NEW-VERSION", r.stdout)
        self.assertEqual(r.stdout.count("TAIL"), 1, r.stdout)


class DeployScriptGuardWiringTests(unittest.TestCase):
    def _read(self, name):
        return (REPO_ROOT / name).read_text()

    def test_both_scripts_snapshot_hash_at_startup(self):
        for name in ("deploy_vps.sh", "deploy_pdf_service.sh"):
            src = self._read(name)
            self.assertIn("SELF_HASH_BEFORE", src, name)
            self.assertIn("DEPLOY_SELF_REEXECED=1 exec bash", src, name)
            self.assertIn('self_reexec_if_updated "$@"', src, name)

    def test_reexec_runs_only_after_the_fast_forward_pull(self):
        # Markers that MUST appear later in each script than the re-exec call.
        later_by_script = {
            "deploy_vps.sh": ("pip install -r", "systemctl restart",
                              "deploy_health_gate.sh", 'log "DEPLOY OK'),
            "deploy_pdf_service.sh": ("pip install -r", "systemctl restart",
                                      "deploy_health_gate.sh", "PDF DEPLOY OK"),
        }
        def code_only(text: str) -> str:
            # Drop full-line comments (the script headers literally describe
            # steps like "systemctl restart" before they are executed).
            return "\n".join(
                ln for ln in text.splitlines()
                if not ln.lstrip().startswith("#"))

        for name, later_markers in later_by_script.items():
            src = code_only(self._read(name))
            pull_pos = src.index("pull --ff-only")
            call_pos = src.index('self_reexec_if_updated "$@"')
            self.assertLess(pull_pos, call_pos, name)
            for later_marker in later_markers:
                self.assertIn(later_marker, src,
                              f"{name}: missing {later_marker}")
                self.assertLess(call_pos, src.index(later_marker),
                                f"{name}: re-exec must precede {later_marker}")

    def test_new_success_strings_present_old_oneshot_removed(self):
        vps = self._read("deploy_vps.sh")
        self.assertIn("passed the stability gate", vps)
        self.assertNotIn(
            "DEPLOY OK — $SERVICE_NAME is active, single process, restarts", vps)
        pdf = self._read("deploy_pdf_service.sh")
        self.assertIn("stability gate and renders valid PDFs", pdf)

    def test_guard_is_bash_syntax_clean(self):
        for name in ("deploy_vps.sh", "deploy_pdf_service.sh"):
            r = subprocess.run(["bash", "-n", str(REPO_ROOT / name)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (name, r.stderr))


if __name__ == "__main__":
    unittest.main()
