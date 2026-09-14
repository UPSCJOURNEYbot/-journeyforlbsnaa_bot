"""
Telegram MTProto crypto dependency compatibility (Python 3.12 deploy target).

TgCrypto 1.25 — an OPTIONAL C accelerator for kurigram/pyrogram — ships
manylinux wheels only for CPython 3.7..3.11. On the Ubuntu 24.04 / Python
3.12 VPS an unconditional requirement forces a source build that needs
gcc + python3.12-dev (absent from the minimal image) and fails the whole
``pip install -r requirements.txt``.

The application imports neither TgCrypto nor pyaes directly: kurigram's
``pyrogram/crypto/aes.py`` does ``try: import tgcrypto except ImportError:
import pyaes``. These tests pin:

* requirements.txt guards TgCrypto with a ``python_version < "3.12"`` marker
  and explicitly pins the pure-Python fallback ``pyaes``;
* the fallback really works (IGE round-trip) with TgCrypto made
  unimportable, so a 3.12 VPS install (no TgCrypto) is functional;
* production code never hard-imports TgCrypto.
"""

from __future__ import annotations

import builtins
import importlib
import os
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIREMENTS = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")


class RequirementsMarkerTests(unittest.TestCase):
    def _requirement_lines(self):
        return [
            ln.strip() for ln in REQUIREMENTS.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]

    def test_tgcrypto_guarded_for_python312(self):
        lines = [ln for ln in self._requirement_lines()
                 if ln.lower().startswith("tgcrypto")]
        self.assertEqual(len(lines), 1, lines)
        spec = lines[0]
        self.assertIn("python_version", spec)
        # Marker must EXCLUDE 3.12 (the VPS runtime) where no wheel exists.
        self.assertRegex(spec,
                         r"tgcrypto==1\.2\.5\s*;\s*python_version\s*<\s*['\"]3\.12['\"]")

    def test_pyaes_fallback_explicitly_pinned(self):
        lines = [ln for ln in self._requirement_lines()
                 if ln.lower().startswith("pyaes")]
        self.assertTrue(lines,
                        "pyaes must be explicitly pinned as the TgCrypto fallback")
        self.assertRegex(lines[0], r"^pyaes==\d")

    def test_tgcrypto_skipped_under_python_312_marker_logic(self):
        # Static evaluation of the declared marker at representative versions.
        line = next(ln for ln in self._requirement_lines()
                    if ln.lower().startswith("tgcrypto"))
        marker = line.split(";", 1)[1].strip()
        # packaging ships with the venv (pip dependency); use it to evaluate.
        try:
            from packaging.markers import Marker
        except ImportError:  # pragma: no cover
            self.skipTest("packaging unavailable")
        m = Marker(marker)
        self.assertTrue(m.evaluate({"python_version": "3.11"}))
        self.assertFalse(m.evaluate({"python_version": "3.12"}))
        self.assertFalse(m.evaluate({"python_version": "3.13"}))


class PyaesFallbackFunctionalTests(unittest.TestCase):
    """kurigram must encrypt/decrypt correctly when TgCrypto is absent."""

    def setUp(self):
        # Drop any cached pyrogram/tgcrypto modules so the import is replayed
        # in an environment where tgcrypto cannot be imported.
        self._real_import = builtins.__import__
        self._removed = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == "tgcrypto" or name.startswith(("pyrogram", "kurigram"))
        }

        def block(name, *args, **kwargs):
            if name == "tgcrypto":
                raise ImportError("No module named 'tgcrypto'")
            return self._real_import(name, *args, **kwargs)

        builtins.__import__ = block

    def tearDown(self):
        builtins.__import__ = self._real_import
        for name in list(sys.modules):
            if name == "tgcrypto" or name.startswith(("pyrogram", "kurigram")):
                sys.modules.pop(name, None)
        sys.modules.update(self._removed)

    def test_ige256_roundtrip_via_pyaes(self):
        aes = importlib.import_module("pyrogram.crypto.aes")
        self.assertFalse(hasattr(aes, "tgcrypto"),
                         "tgcrypto must not have been imported")
        for size in (16, 64, 256):  # MTProto blocks are multiples of 16
            data = os.urandom(size)
            key, iv = os.urandom(32), os.urandom(32)
            enc = aes.ige256_encrypt(data, key, iv)
            dec = aes.ige256_decrypt(enc, key, iv)
            self.assertEqual(dec, data)
            self.assertNotEqual(enc, data)

    def test_ctr256_roundtrip_via_pyaes(self):
        aes = importlib.import_module("pyrogram.crypto.aes")
        data = os.urandom(128)
        key = os.urandom(32)
        iv0 = bytearray(os.urandom(16))  # CTR counter is mutated as it runs
        enc = aes.ctr256_encrypt(data, key, bytearray(iv0), bytearray(1))
        dec = aes.ctr256_decrypt(enc, key, bytearray(iv0), bytearray(1))
        self.assertEqual(dec, data)


class NoDirectTgcryptoImportTests(unittest.TestCase):
    def test_production_code_never_hard_imports_tgcrypto(self):
        offenders = []
        for py in (REPO_ROOT / "quizbot").rglob("*.py"):
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "import tgcrypto" in code or "from tgcrypto" in code:
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{i}")
        self.assertEqual(offenders, [],
                         f"TgCrypto must stay optional (kurigram handles fallback): {offenders}")


if __name__ == "__main__":
    unittest.main()
