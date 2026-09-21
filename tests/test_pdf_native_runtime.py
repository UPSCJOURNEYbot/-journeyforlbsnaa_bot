"""
Native WeasyPrint/Pango runtime fix — focused tests.

Production failure: the pip side of WeasyPrint was healthy, but a missing
native stack (``OSError: cannot load library 'pango-1.0-0'``) made every
quiz report degrade to the generic Telegram message "the rendering library
is unavailable" while the operator log only carried a vague hint. The fix:

* ``tools/pdf_native_runtime.sh`` — single source of truth for the native
  dependency set (idempotent apt install + ctypes verification + a real
  tiny Devanagari render);
* ``quizbot/runner_bot/pdf_reports.py`` — in-process backend health
  (``native_library_report`` / ``pdf_backend_health`` /
  ``format_pdf_backend_error``) and exact-exception capture
  (``last_pdf_backend_error``) instead of a swallowed generic failure;
* ``run.py`` startup preflight — fail fast with the precise cause;
* deploy scripts / Dockerfile — delegate provisioning to the tool (no more
  hardcoded package lists, legacy names like libgdk-pixbuf2.0-0 handled in
  one place).

The PR #25/#27 Indic text-layer fixes are deliberately untouched; their
suites (test_weasyprint_runtime_compat.py, test_pdf_harfbuzz_tounicode.py,
test_phase1_result_pdf.py, test_runner_pdf_report.py) must keep passing —
plus the source-level guard in IndicFixRegressionGuardTests below.
"""

from __future__ import annotations

import ctypes
import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "pdf_native_runtime.sh"

from quizbot.runner_bot import pdf_reports  # noqa: E402


def _native_stack_present() -> bool:
    """True when every soname WeasyPrint dlopens loads via ctypes here."""
    for soname, _pkg in pdf_reports.REQUIRED_NATIVE_LIBRARIES:
        try:
            ctypes.CDLL(soname)
        except OSError:
            return False
    return True


NATIVE_OK = _native_stack_present()


class _FakeLib:
    def __init__(self, name: str) -> None:
        self._name = name


def _all_libs_load(name: str, *args, **kwargs):
    return _FakeLib(name)


def _no_libs_load(name: str, *args, **kwargs):
    raise OSError(5, "cannot open shared object file", name)


# ---------------------------------------------------------------------------
# 1. Native library detection
# ---------------------------------------------------------------------------

class NativeLibraryDetectionTests(unittest.TestCase):
    """ctypes probe of the exact sonames WeasyPrint 62.3 dlopens."""

    def test_reports_all_present_when_ctypes_can_load_every_library(self):
        with mock.patch.object(ctypes, "CDLL", side_effect=_all_libs_load):
            report = pdf_reports.native_library_report()
        self.assertEqual(report["missing"], [])
        self.assertEqual(
            set(report["present"]),
            {soname for soname, _pkg in pdf_reports.REQUIRED_NATIVE_LIBRARIES})

    def test_detects_missing_library_and_names_its_apt_package(self):
        def flaky(name, *a, **k):
            if name == "libpango-1.0.so.0":
                raise OSError(5, "cannot open shared object file", name)
            return _FakeLib(name)

        with mock.patch.object(ctypes, "CDLL", side_effect=flaky):
            report = pdf_reports.native_library_report()
        self.assertIn(("libpango-1.0.so.0", "libpango-1.0-0"), report["missing"])
        self.assertEqual(
            len(report["present"]), len(pdf_reports.REQUIRED_NATIVE_LIBRARIES) - 1)

    def test_required_set_matches_weasyprint_ffi_dlopens(self):
        # The five libraries weasyprint/text/ffi.py loads on Linux.
        sonames = {s for s, _p in pdf_reports.REQUIRED_NATIVE_LIBRARIES}
        self.assertEqual(sonames, {
            "libgobject-2.0.so.0", "libpango-1.0.so.0",
            "libpangoft2-1.0.so.0", "libharfbuzz.so.0", "libfontconfig.so.1",
        })

    def test_real_ctypes_report_is_consistent_with_this_environment(self):
        report = pdf_reports.native_library_report()
        self.assertEqual(
            len(report["present"]) + len(report["missing"]),
            len(pdf_reports.REQUIRED_NATIVE_LIBRARIES))
        if NATIVE_OK:
            self.assertEqual(report["missing"], [])
        else:
            self.assertTrue(
                any(s == "libpango-1.0.so.0" for s, _p in report["missing"]),
                "a bare host must at minimum report the missing pango library")


# ---------------------------------------------------------------------------
# 2. Missing-library detection (health layer)
# ---------------------------------------------------------------------------

class MissingLibraryHealthTests(unittest.TestCase):

    def test_health_unavailable_when_libraries_missing(self):
        with mock.patch.object(ctypes, "CDLL", side_effect=_no_libs_load):
            health = pdf_reports.pdf_backend_health()
        self.assertFalse(health["available"])
        self.assertEqual(
            len(health["missing_libraries"]),
            len(pdf_reports.REQUIRED_NATIVE_LIBRARIES))
        # The operator-visible summary must name the apt package, not just
        # the opaque soname.
        self.assertIn("libpango-1.0-0", health["detail"])
        self.assertIsNone(health["weasyprint"])

    @unittest.skipIf(NATIVE_OK, "native stack present; failure path not exercisable here")
    def test_health_reports_missing_libraries_in_this_bare_environment(self):
        health = pdf_reports.pdf_backend_health()
        self.assertFalse(health["available"])
        self.assertTrue(health["missing_libraries"])
        self.assertIn("libpango-1.0", " ".join(s for s, _p in health["missing_libraries"]))


# ---------------------------------------------------------------------------
# 3. WeasyPrint import failure
# ---------------------------------------------------------------------------

class WeasyPrintImportFailureTests(unittest.TestCase):

    def test_import_error_is_captured_in_health(self):
        def boom(module_name, *a, **k):
            raise ImportError("No module named 'weasyprint'")

        with mock.patch.object(ctypes, "CDLL", side_effect=_all_libs_load), \
             mock.patch.object(importlib, "import_module", side_effect=boom):
            health = pdf_reports.pdf_backend_health()
        self.assertFalse(health["available"])
        self.assertEqual(health["missing_libraries"], [])
        self.assertIn("No module named 'weasyprint'", health["import_error"])

    def test_failed_import_purges_partial_weasyprint_modules(self):
        # WeasyPrint dlopens pango late in __init__; a failed import leaves
        # half-initialised modules. A later health check must not trust them.
        partial = types.ModuleType("weasyprint")
        ffi_partial = types.ModuleType("weasyprint.text.ffi")
        partial.text = ffi_partial

        def boom(module_name, *a, **k):
            raise OSError("cannot load library 'pango-1.0-0'")

        with mock.patch.dict(sys.modules, {"weasyprint": partial,
                                           "weasyprint.text.ffi": ffi_partial}):
            with mock.patch.object(ctypes, "CDLL", side_effect=_all_libs_load), \
                 mock.patch.object(importlib, "import_module", side_effect=boom):
                health = pdf_reports.pdf_backend_health()
        self.assertFalse(health["available"])
        self.assertIn("cannot load library 'pango-1.0-0'", health["import_error"])
        self.assertNotIn("weasyprint", sys.modules)
        self.assertNotIn("weasyprint.text.ffi", sys.modules)


# ---------------------------------------------------------------------------
# 4. Successful tiny PDF render (needs the real native stack)
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    NATIVE_OK, "WeasyPrint native libs (pango/harfbuzz) absent in this "
    "environment; tools/pdf_native_runtime.sh verify enforces this on the VPS")
class TinyRenderProbeTests(unittest.TestCase):

    def test_probe_renders_a_real_pdf(self):
        health = pdf_reports.pdf_backend_health(probe=True)
        self.assertTrue(health["available"], health["detail"])
        self.assertIsNotNone(health["weasyprint"])
        self.assertIsNone(health["probe_error"])
        self.assertIsNone(health["import_error"])


# ---------------------------------------------------------------------------
# 5. Quiz PDF backend health + 6. accurate operator error reporting
# ---------------------------------------------------------------------------

class QuizPdfBackendHealthTests(unittest.TestCase):
    """A render-time backend failure degrades to False — and now also
    records the EXACT underlying exception for the operator journal."""

    @staticmethod
    def _broken_weasy(error: BaseException):
        class _Doc:
            def __init__(self, *, string=None, base_url=None):
                self.html = string

            def write_pdf(self, path):
                raise error

        mod = types.ModuleType("weasyprint")
        mod.__version__ = "62.3"
        mod.HTML = _Doc
        return mod

    def test_render_failure_records_exact_error(self):
        pdf_reports._clear_backend_error()
        broken = self._broken_weasy(
            AttributeError("'super' object has no attribute 'transform'"))
        with mock.patch.dict(sys.modules, {"weasyprint": broken}):
            ok = pdf_reports.render_quiz_pdf(
                "Q", "C", [], {}, {}, 0.25, 1.0, "/tmp/never_written_native_a.pdf")
        self.assertFalse(ok)
        err = pdf_reports.last_pdf_backend_error()
        self.assertIsNotNone(err)
        self.assertIn("'super' object has no attribute 'transform'", err)
        self.assertIn("[pdf-render]", err)
        pdf_reports._clear_backend_error()

    def test_render_failure_logs_remediation_in_operator_log(self):
        pdf_reports._clear_backend_error()
        broken = self._broken_weasy(RuntimeError("simulated native render crash"))
        with mock.patch.dict(sys.modules, {"weasyprint": broken}):
            with self.assertLogs("quizbot.runner_bot.pdf_reports", level="ERROR") as cap:
                ok = pdf_reports.render_quiz_pdf(
                    "Q", "C", [], {}, {}, 0.25, 1.0, "/tmp/never_written_native_b.pdf")
        self.assertFalse(ok)
        msgs = "\n".join(r.getMessage() for r in cap.records)
        self.assertIn("simulated native render crash", msgs)
        self.assertIn("Remediation", msgs)
        self.assertIn("tools/pdf_native_runtime.sh", msgs)
        pdf_reports._clear_backend_error()

    @unittest.skipUnless(NATIVE_OK, "native stack absent in this environment")
    def test_successful_render_clears_stale_error(self):
        pdf_reports._record_backend_error("test", RuntimeError("stale failure"))
        self.assertIsNotNone(pdf_reports.last_pdf_backend_error())
        questions = [{"question": "भारत की राजधानी?", "options": ["a", "b", "c", "d"],
                      "correct_option_id": 0, "explanation": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            out = f"{tmp}/native_ok.pdf"
            ok = pdf_reports.render_quiz_pdf(
                "Native OK", "Chat", questions, [], {}, 0.25, 1.0, out,
                shuffle_options=False, style="classic")
        self.assertTrue(ok)
        self.assertIsNone(pdf_reports.last_pdf_backend_error())
        self.assertTrue(pathlib.Path(out).read_bytes()[:5] == b"%PDF-")


class OperatorErrorReportingTests(unittest.TestCase):

    def test_format_contains_exact_cause_and_remediation(self):
        health = {
            "available": False,
            "weasyprint": "62.3",
            "missing_libraries": [("libpango-1.0.so.0", "libpango-1.0-0")],
            "import_error": "OSError: cannot load library 'pango-1.0-0'",
            "probe_error": None,
            "detail": "missing native libraries: libpango-1.0.so.0 (libpango-1.0-0)",
        }
        pdf_reports._record_backend_error(
            "weasyprint-import", OSError("cannot load library 'pango-1.0-0'"))
        try:
            text = pdf_reports.format_pdf_backend_error(health)
            # Exact cause (both the structured diagnosis and the raw
            # exception text) plus the exact remediation command.
            self.assertIn("cannot load library 'pango-1.0-0'", text)
            self.assertIn("libpango-1.0.so.0", text)
            self.assertIn("libpango-1.0-0", text)
            self.assertIn("tools/pdf_native_runtime.sh", text)
        finally:
            pdf_reports._clear_backend_error()

    def test_user_facing_message_stays_generic_and_private(self):
        # The Telegram notice stays friendly; internals (sonames, apt
        # packages, library names) must never leak into the chat message.
        src = (REPO_ROOT / "quizbot" / "runner_bot" / "handlers"
               / "quiz_play.py").read_text(encoding="utf-8")
        block = src[src.index("async def _send_pdf_report"):]
        block = block[:block.index("async def result_command")]
        self.assertIn("the rendering library is unavailable", block)
        self.assertNotIn("pango", block)
        self.assertNotIn("apt-get", block)
        self.assertNotIn("libharfbuzz", block)


# ---------------------------------------------------------------------------
# 7. Deployment / native package script behavior
# ---------------------------------------------------------------------------

def _run_tool(*args: str, env=None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(TOOL), *args], capture_output=True, text=True,
        env=env, timeout=timeout,
    )


def _run_sourced_function(
        function: str, *args: str, env=None,
        timeout: int = 180) -> subprocess.CompletedProcess:
    """Source the tool, then invoke one shell function in that same shell."""
    return subprocess.run(
        [
            "bash", "-c", 'source "$1"; shift; "$@"',
            "pdf-native-runtime-test", str(TOOL), function, *args,
        ],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


class NativeRuntimeScriptTests(unittest.TestCase):

    def test_script_exists_and_syntax_is_clean(self):
        self.assertTrue(TOOL.is_file())
        r = subprocess.run(["bash", "-n", str(TOOL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_print_packages_lists_the_required_native_set(self):
        r = _run_tool("print-packages")
        self.assertEqual(r.returncode, 0, r.stderr)
        pkgs = r.stdout.split()
        for required in ("libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz0b",
                         "fonts-noto-core", "fonts-deva", "fonts-noto-color-emoji"):
            self.assertIn(required, pkgs)

    def test_legacy_gdk_pixbuf_name_is_optional_not_core(self):
        # The old name must not sit in the core install list (it does not
        # exist on newer Debian/Ubuntu releases); the tool tries the new
        # name first, then the legacy one, best-effort.
        core = _run_tool("print-packages").stdout.split()
        optional = _run_tool("print-optional-packages").stdout.split()
        self.assertNotIn("libgdk-pixbuf2.0-0", core)
        self.assertIn("libgdk-pixbuf-4.0-0", optional)
        self.assertIn("libgdk-pixbuf2.0-0", optional)

    def test_t64_package_satisfies_canonical_package(self):
        # Noble installs libglib2.0-0t64 when apt is asked for the canonical
        # libglib2.0-0 name. pkg_installed must accept that concrete dpkg
        # package while the canonical apt package list stays unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_dpkg = fake_bin / "dpkg"
            fake_dpkg.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "sys.exit(0 if sys.argv[1:] == "
                "['-s', 'libglib2.0-0t64'] else 1)\n")
            fake_dpkg.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            r = _run_sourced_function(
                "pkg_installed", "libglib2.0-0", env=env)

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        core = _run_tool("print-packages").stdout.split()
        self.assertIn("libglib2.0-0", core)
        self.assertNotIn("libglib2.0-0t64", core)

    def test_genuinely_missing_package_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_dpkg = fake_bin / "dpkg"
            fake_dpkg.write_text("#!/bin/sh\nexit 1\n")
            fake_dpkg.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            r = _run_sourced_function(
                "pkg_installed", "libglib2.0-0", env=env)

        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_t64_alias_cannot_satisfy_unrelated_package(self):
        # An installed glib t64 package must not satisfy pango (or any other
        # canonical package); aliases are exact and package-specific.
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_dpkg = fake_bin / "dpkg"
            fake_dpkg.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "sys.exit(0 if sys.argv[1:] == "
                "['-s', 'libglib2.0-0t64'] else 1)\n")
            fake_dpkg.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            r = _run_sourced_function(
                "pkg_installed", "libpango-1.0-0", env=env)

        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skipIf(
        NATIVE_OK, "native stack present in this environment; the failure "
        "path is only exercisable on a bare host (e.g. CI)")
    def test_verify_fails_clearly_when_stack_absent(self):
        r = _run_tool("verify")
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertIn("VERIFY FAILED", out)
        # The operator must be told the exact apt package, not just the soname.
        self.assertIn("libpango-1.0-0", out)
        # ...and the exact remediation command.
        self.assertIn("tools/pdf_native_runtime.sh", out)

    @unittest.skipUnless(NATIVE_OK, "native stack absent in this environment")
    def test_verify_passes_when_stack_present(self):
        r = _run_tool("verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verification passed", r.stdout)

    def test_install_is_idempotent_noop_when_packages_present(self):
        # Fake a dpkg that reports every package installed: install must
        # skip apt entirely and (with --no-verify) exit 0.
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_dpkg = fake_bin / "dpkg"
            fake_dpkg.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "sys.exit(0 if '-s' in sys.argv else 1)\n")
            fake_dpkg.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            r = _run_tool("install", "--no-verify", env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already installed", r.stdout)
        self.assertNotIn("apt-get update", r.stdout)

    def test_full_installed_noble_t64_scenario_is_idempotent_noop(self):
        # Every package is installed, but Noble exposes glib only under its
        # concrete t64 dpkg name. Direct script execution must still skip apt.
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_dpkg = fake_bin / "dpkg"
            fake_dpkg.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "pkg = sys.argv[2] if sys.argv[1:2] == ['-s'] "
                "and len(sys.argv) > 2 else ''\n"
                "sys.exit(1 if pkg == 'libglib2.0-0' else 0)\n")
            fake_dpkg.chmod(0o755)
            fake_apt = fake_bin / "apt-get"
            fake_apt.write_text(
                "#!/bin/sh\necho 'apt-get unexpectedly called' >&2\nexit 97\n")
            fake_apt.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            r = _run_tool("install", "--no-verify", env=env, timeout=120)

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already installed", r.stdout)
        self.assertNotIn("unexpectedly called", r.stdout + r.stderr)

    def test_deploy_scripts_delegate_to_the_tool(self):
        for name in ("deploy_vps.sh", "install_and_run_final.sh",
                     "replace_single_bot.sh", "Dockerfile"):
            src = (REPO_ROOT / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertIn(
                    "pdf_native_runtime.sh", src,
                    f"{name} must delegate native PDF provisioning to "
                    "tools/pdf_native_runtime.sh (single source of truth)")

    def test_legacy_package_name_not_hardcoded_in_provisioning(self):
        # The legacy name may be MENTIONED in comments (documenting the
        # rename), but it must not appear in an actual `apt-get install`
        # provisioning line — the tool owns that decision.
        for name in ("deploy_vps.sh", "Dockerfile"):
            src = (REPO_ROOT / name).read_text(encoding="utf-8")
            provisioning = "\n".join(
                ln for ln in src.splitlines()
                if "apt-get install" in ln or "apt install" in ln)
            with self.subTest(script=name):
                self.assertNotIn(
                    "libgdk-pixbuf2.0-0", provisioning,
                    f"{name} must not hardcode the legacy package name")

    def test_run_py_startup_preflight_wired(self):
        src = (REPO_ROOT / "run.py").read_text(encoding="utf-8")
        self.assertIn("_pdf_runtime_preflight", src)
        # Enforced only when the runner bot (the quiz-PDF source) starts;
        # --only miniapp must not be blocked by a host without the stack.
        self.assertIn("only in (None, \"runner\", \"creator\")", src)
        self.assertIn("probe=True", src)


# ---------------------------------------------------------------------------
# 8. No regression to the existing PDF/Indic fixes (PR #25/#27)
# ---------------------------------------------------------------------------

class IndicFixRegressionGuardTests(unittest.TestCase):

    def test_render_still_applies_indic_actualtext_patch(self):
        src = (REPO_ROOT / "quizbot" / "runner_bot" / "pdf_reports.py").read_text(
            encoding="utf-8")
        body = src[src.index("def render_quiz_pdf"):]
        self.assertIn(
            "wp_indic_compat.apply_weasyprint_indic_actualtext_fix()", body,
            "render_quiz_pdf must keep applying the PR #25/#27 Indic "
            "text-layer fix on every render")
        self.assertIn("from quizbot.runner_bot import wp_indic_compat", body)

    def test_wp_indic_compat_module_imports_safely(self):
        # Importing the module (and re-applying) must never raise, even
        # without the native stack — the patch is fail-soft by design.
        from quizbot.runner_bot import wp_indic_compat
        self.assertTrue(
            callable(wp_indic_compat.apply_weasyprint_indic_actualtext_fix))
        self.assertIsInstance(wp_indic_compat.apply_weasyprint_indic_actualtext_fix(), bool)


if __name__ == "__main__":
    unittest.main()
