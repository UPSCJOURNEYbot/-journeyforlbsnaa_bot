#!/usr/bin/env bash
#
# tools/pdf_native_runtime.sh — SINGLE SOURCE OF TRUTH for the native
# (non-pip) runtime dependencies of the WeasyPrint quiz-PDF backend.
#
# WHY THIS FILE EXISTS
#   Quiz result PDFs are rendered in-process by WeasyPrint 62.3. Its pip side
#   is pinned in requirements.txt (and render-gated in deploy_vps.sh §3b),
#   but its runtime side is native shared libraries (Pango/HarfBuzz/glib/
#   fontconfig) and fonts that only the OS package manager can provide.
#   When that native stack is absent, WeasyPrint raises
#       OSError: cannot load library 'pango-1.0-0'
#   and every quiz report degrades to the generic Telegram message
#   "the rendering library is unavailable" — a hidden runtime failure that
#   only becomes visible at the end of a real quiz.
#   This script provisions AND proves the native stack in one place, so
#   deploy_vps.sh, the Dockerfile, install_and_run_final.sh,
#   replace_single_bot.sh and the run.py startup preflight all agree on the
#   exact same dependency set. Do NOT hardcode this package list anywhere
#   else.
#
# GUARANTEES
#   `install`  — idempotent apt provisioning of the CORE set (skips apt
#                entirely when every package is already installed) plus a
#                best-effort OPTIONAL set, then verifies (unless
#                --no-verify is given, e.g. Docker layers where the Python
#                stack is installed later).
#   `verify`   — direct runtime proof, no root/apt needed:
#                 a. ctypes loads the exact sonames that
#                    weasyprint/text/ffi.py dlopens (WeasyPrint 62.3);
#                 b. `import weasyprint` succeeds in the project interpreter
#                    (.venv, or PDF_RT_PYTHON, or python3);
#                 c. a tiny HTML page (English + Devanagari) renders to a
#                    real PDF through the full layout pipeline.
#                Exits non-zero with the precise cause when any step fails.
#   `print-packages` / `print-optional-packages` — machine-readable lists
#                for CI / image builds.
#
# USAGE (run on the target host; sudo is used internally only when needed):
#   bash tools/pdf_native_runtime.sh install
#   bash tools/pdf_native_runtime.sh install --no-verify
#   bash tools/pdf_native_runtime.sh verify
#   bash tools/pdf_native_runtime.sh print-packages
#
# EXIT CODES: 0 = healthy (or provisioned + verified), 1 = failed.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log()  { printf '[pdf-rt] %s\n' "$*"; }
fail() { printf '[pdf-rt] ERROR: %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
fi

# ---------------------------------------------------------------------------
# Canonical package set (the single source of truth).
#
# CORE = every shared library WeasyPrint 62.3 dlopens at import time (see
#        REQUIRED_NATIVE_LIBRARIES in quizbot/runner_bot/pdf_reports.py —
#        keep the two lists in sync) plus the fonts the result PDF needs on
#        a minimal host:
#          libglib2.0-0      -> libgobject-2.0.so.0   (pango's runtime base)
#          libpango-1.0-0    -> libpango-1.0.so.0     (text layout)
#          libpangoft2-1.0-0 -> libpangoft2-1.0.so.0  (font loading)
#          libharfbuzz0b     -> libharfbuzz.so.0      (shaping; required for
#                                                      correct Indic output)
#          libfontconfig1    -> libfontconfig.so.1    (font discovery)
#        libpangocairo-1.0-0 / libcairo2 are NOT dlopened by WeasyPrint 62
#        but have always been provisioned alongside pango in this project's
#        deploy stacks; they stay in CORE for parity (harmless, small).
#        fonts-noto-core / fonts-deva / fonts-noto-color-emoji: system-level
#        Hindi + emoji coverage (the report additionally bundles a Hind
#        @font-face subset, so rendering degrades gracefully if a font
#        package is ever dropped).
#
# OPTIONAL = best-effort only. The legacy name libgdk-pixbuf2.0-0 does not
#        exist on newer Debian/Ubuntu releases (the stack was split and
#        renamed to libgdk-pixbuf-4.0-0), so trying it unconditionally broke
#        provisioning on new images. Neither candidate is required by the
#        quiz PDF path — a missing optional package only warns.
# ---------------------------------------------------------------------------
CORE_PACKAGES=(
  libglib2.0-0
  libpango-1.0-0
  libpangoft2-1.0-0
  libpangocairo-1.0-0
  libcairo2
  libharfbuzz0b
  libfontconfig1
  fonts-noto-core
  fonts-deva
  fonts-noto-color-emoji
  fonts-liberation
)
OPTIONAL_PACKAGES=(
  libgdk-pixbuf-4.0-0
  libgdk-pixbuf2.0-0
)

pick_python() {
  # Project venv first (that is where requirements.txt puts WeasyPrint),
  # then an explicit override, then the system interpreter.
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    printf '%s' "$REPO_ROOT/.venv/bin/python"; return
  fi
  if [ -n "${PDF_RT_PYTHON:-}" ] && command -v "$PDF_RT_PYTHON" >/dev/null 2>&1; then
    printf '%s' "$PDF_RT_PYTHON"; return
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' python3; return
  fi
  fail "No Python interpreter found (create the project .venv or set PDF_RT_PYTHON)."
}

have_pkg_manager() {
  command -v apt-get >/dev/null 2>&1 && command -v dpkg >/dev/null 2>&1
}

pkg_installed() { dpkg -s "$1" >/dev/null 2>&1; }

apt_install_core() {
  have_pkg_manager || fail \
    "apt-get/dpkg not available on this host — cannot provision the native PDF stack. Install manually: ${CORE_PACKAGES[*]}"

  local missing=() p
  for p in "${CORE_PACKAGES[@]}"; do
    pkg_installed "$p" || missing+=("$p")
  done

  if [ "${#missing[@]}" -eq 0 ]; then
    log "Core native packages already installed (idempotent no-op — apt not run)."
  else
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
      fail "Not root and sudo is unavailable — cannot install: ${missing[*]}. Run as root, or: sudo apt-get install -y --no-install-recommends ${missing[*]}"
    fi
    log "Installing core native packages: ${missing[*]}"
    $SUDO apt-get update -y
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends "${missing[@]}"
    for p in "${missing[@]}"; do
      pkg_installed "$p" || fail "Package '$p' was not installed after apt-get (network? repo?) — re-run this script."
    done
  fi

  # Best-effort optional set (newer-release names first; legacy names may not
  # exist). Never fatal — the quiz PDF path does not require gdk-pixbuf.
  local o
  for o in "${OPTIONAL_PACKAGES[@]}"; do
    pkg_installed "$o" && continue
    [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ] && continue
    if DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends "$o" 2>/dev/null; then
      log "Optional package installed: $o"
    fi
  done
  for o in "${OPTIONAL_PACKAGES[@]}"; do
    if ! pkg_installed "$o"; then
      log "note: optional package '$o' unavailable on this release (renamed/split upstream?); not required for quiz PDFs."
    fi
  done
}

verify_runtime() {
  local py
  py="$(pick_python)"
  log "Verifying the native PDF runtime with interpreter: $py"
  "$py" - <<'PY'
import ctypes
import importlib
import sys

# Exact sonames weasyprint/text/ffi.py dlopens on Linux (WeasyPrint 62.3).
# Must stay in sync with REQUIRED_NATIVE_LIBRARIES in
# quizbot/runner_bot/pdf_reports.py.
SONAMES = (
    ("libgobject-2.0.so.0", "libglib2.0-0"),
    ("libpango-1.0.so.0", "libpango-1.0-0"),
    ("libpangoft2-1.0.so.0", "libpangoft2-1.0-0"),
    ("libharfbuzz.so.0", "libharfbuzz0b"),
    ("libfontconfig.so.1", "libfontconfig1"),
)

errors = []
for soname, pkg in SONAMES:
    try:
        ctypes.CDLL(soname)
        print(f"[pdf-rt] native lib OK: {soname}")
    except OSError as exc:
        errors.append(f"native library missing: {soname} (apt package: {pkg}): {exc}")
        print(f"[pdf-rt] native lib MISSING: {soname} (apt package: {pkg})")

if not errors:
    try:
        wp = importlib.import_module("weasyprint")
        print(f"[pdf-rt] WeasyPrint import OK: {getattr(wp, '__version__', 'unknown')}")
    except Exception as exc:  # ImportError (pip side) or OSError (native)
        errors.append(f"WeasyPrint import failed: {type(exc).__name__}: {exc}")
        print(f"[pdf-rt] WeasyPrint import FAILED: {type(exc).__name__}: {exc}")
else:
    print("[pdf-rt] WeasyPrint import skipped (native libraries missing).")

if not errors:
    try:
        from weasyprint import HTML
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "@page { size: A4; margin: 2cm; }"
            "body { font-family: sans-serif; }</style></head><body>"
            "<h1>PDF native runtime check</h1>"
            "<p>भारत की राजधानी नई दिल्ली है।</p>"
            "</body></html>"
        )
        pdf = HTML(string=html).write_pdf()
        if not (pdf[:5] == b"%PDF-" and len(pdf) > 1000):
            raise RuntimeError(f"render did not produce a PDF ({len(pdf)} bytes)")
        print(f"[pdf-rt] tiny PDF render OK: {len(pdf)} bytes, Devanagari page.")
    except Exception as exc:
        errors.append(f"tiny PDF render failed: {type(exc).__name__}: {exc}")
        print(f"[pdf-rt] tiny PDF render FAILED: {type(exc).__name__}: {exc}")

if errors:
    for e in errors:
        print(f"[pdf-rt] VERIFY FAILED: {e}")
    print(
        "[pdf-rt] Remediation: sudo bash tools/pdf_native_runtime.sh install  "
        "(core packages: " + " ".join(pkg for _s, pkg in SONAMES) + " "
        "fonts-noto-core fonts-deva fonts-noto-color-emoji fonts-liberation), "
        "then re-run: bash tools/pdf_native_runtime.sh verify"
    )
    sys.exit(1)

print("[pdf-rt] Native PDF runtime verification passed.")
PY

  # Soft font check: the report bundles a Hind @font-face subset, so missing
  # system Devanagari fonts degrade gracefully — warn, never fail.
  if command -v fc-list >/dev/null 2>&1; then
    if ! fc-list :lang=hi 2>/dev/null | grep -q .; then
      log "note: fontconfig sees no Devanagari font; the report falls back to the bundled Hind @font-face (offline-safe), but system Hindi fonts are recommended."
    fi
  fi
}

print_packages() { printf '%s\n' "${CORE_PACKAGES[@]}"; }
print_optional_packages() { printf '%s\n' "${OPTIONAL_PACKAGES[@]}"; }

usage() {
  cat <<'USAGE'
tools/pdf_native_runtime.sh — single source of truth for the native
WeasyPrint/Pango quiz-PDF runtime (install + verify).

Usage:
  bash tools/pdf_native_runtime.sh install              # provision + verify (default)
  bash tools/pdf_native_runtime.sh install --no-verify  # provision only (Docker layer)
  bash tools/pdf_native_runtime.sh verify               # prove the stack, no root needed
  bash tools/pdf_native_runtime.sh print-packages       # core apt set (one per line)
  bash tools/pdf_native_runtime.sh print-optional-packages

Environment:
  PDF_RT_PYTHON  interpreter for the WeasyPrint check (default: .venv, then python3)

Exit codes: 0 = healthy / provisioned+verified, 1 = failed.
USAGE
  exit 0
}

cmd="${1:-install}"
[ $# -gt 0 ] && shift
NO_VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --no-verify) NO_VERIFY=1 ;;
    -h|--help|help) usage ;;
    *) fail "Unknown argument: $arg (see --help)" ;;
  esac
done

case "$cmd" in
  install)
    apt_install_core
    if [ "$NO_VERIFY" -eq 0 ]; then
      verify_runtime
    else
      log "verification skipped (--no-verify) — the caller MUST verify separately before serving traffic."
    fi
    ;;
  verify)
    verify_runtime
    ;;
  print-packages)
    print_packages
    ;;
  print-optional-packages)
    print_optional_packages
    ;;
  *)
    usage
    ;;
esac
