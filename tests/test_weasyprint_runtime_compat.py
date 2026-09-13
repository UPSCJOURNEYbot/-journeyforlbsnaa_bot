"""
WeasyPrint result-PDF runtime-compatibility regressions.

Production incident (quizbot.service, Ubuntu 24.04 / Python 3.12.3):

    WeasyPrint PDF generation failed:
    AttributeError: 'super' object has no attribute 'transform'

WeasyPrint 62.3 (2024-06-21) declares ``pydyf>=0.10.0`` with NO upper bound.
pydyf 0.12.0 (2025-12-02) removed ``Stream.transform()``; WeasyPrint 62.3's
``weasyprint/pdf/stream.py`` calls ``super().transform(...)`` for every
coordinate transform, so any environment that resolved the transitive set
after that date crashed while serialising EVERY page. The root fix is exact
pinning of WeasyPrint's transitive stack in requirements.txt plus a real
render gate in deploy_vps.sh. These tests pin:

* the declared dependency versions;
* the pydyf API surface WeasyPrint 62.3 requires (no native libs needed, so
  the exact production AttributeError can be reproduced/guarded in CI);
* a real WeasyPrint render wherever pango/cairo ARE available (skipped in
  minimal CI containers, enforced on the VPS by deploy_vps.sh's gate);
* the Quiz Result PDF end-to-end path and its render-time fail-soft guard;
* the Test Series (fpdf2) PDF path, which renders headlessly;
* the PR #20 markdown/URL escaping protections staying intact.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import pathlib
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIREMENTS = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

# The exact transitive set WeasyPrint 62.3 shipped with (June 2024); every
# pin has cp312 manylinux / py3-none-any wheels.
REQUIRED_PINS = {
    "weasyprint": "62.3",
    "pydyf": "0.10.0",
    "tinycss2": "1.3.0",
    "cssselect2": "0.7.0",
    "pyphen": "0.15.0",
    "fonttools": "4.53.1",
    "cffi": "1.16.0",
    "html5lib": "1.1",
}


class DeclaredDependencyPinsTests(unittest.TestCase):
    """requirements.txt must lock WeasyPrint's whole transitive stack."""

    def test_transitive_stack_is_exactly_pinned(self):
        for package, version in REQUIRED_PINS.items():
            with self.subTest(package=package):
                # Plain 'name==version' (fonttools may carry the [woff] extra).
                pattern = rf"(?m)^{re.escape(package)}(?:\[[\w,]+\])?=={re.escape(version)}\s*$"
                self.assertRegex(
                    REQUIREMENTS, pattern,
                    f"{package}=={version} must be pinned in requirements.txt "
                    "(WeasyPrint 62.3 leaves these transitive deps unbounded)")

    def test_pydyf_is_not_resolvable_to_0_12(self):
        # Belt-and-braces: no loose pydyf SPEC LINE may coexist with the pin
        # (comments documenting the upstream lower bound are fine).
        spec_lines = [
            ln.strip() for ln in REQUIREMENTS.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
        pydyf_lines = [ln for ln in spec_lines if ln.lower().startswith("pydyf")]
        self.assertEqual(pydyf_lines, ["pydyf==0.10.0"])


class PydyfApiSurfaceTests(unittest.TestCase):
    """The precise class of the production crash, reproducible WITHOUT pango.

    WeasyPrint's PDF writer is pure Python; only text LAYOUT needs native
    pango. The transform failure happens at page serialisation, so we can
    execute that exact method surface here.
    """

    @staticmethod
    def _weasyprint_stream_source() -> pathlib.Path:
        # Locate weasyprint/pdf/stream.py without importing weasyprint
        # (importing the package triggers the native-library dlopen).
        try:
            dist = md.distribution("weasyprint")
            for installed_file in dist.files or ():
                if str(installed_file).endswith(os_sep_safe("pdf/stream.py")):
                    return installed_file.locate()
        except md.PackageNotFoundError:
            pass
        # Fallback: same site-packages directory that holds pydyf.
        import pydyf
        candidate = pathlib.Path(pydyf.__file__).parent.parent / \
            "weasyprint" / "pdf" / "stream.py"
        return candidate

    def test_stream_source_is_present(self):
        path = self._weasyprint_stream_source()
        self.assertTrue(path.is_file(), f"weasyprint/pdf/stream.py not found ({path})")

    def test_every_super_method_weasyprint_calls_exists_on_pydyf_stream(self):
        import pydyf
        source = self._weasyprint_stream_source().read_text(encoding="utf-8")
        methods = sorted(set(re.findall(r"super\(\)\.([a-zA-Z_]+)\(", source)))
        self.assertIn("transform", methods)  # the production method
        missing = [m for m in methods if not hasattr(pydyf.Stream, m)]
        self.assertEqual(missing, [],
                         f"pydyf {pydyf.__version__} lacks {missing}: this is the "
                         "'super object has no attribute' production failure")

    def test_transform_emits_cm_operator(self):
        import pydyf
        # The exact call WeasyPrint makes for every positioned/transformed box.
        stream = pydyf.Stream()
        stream.transform(1, 0, 0, 1, 72, 144)
        data = stream.stream
        raw = data.get_data() if hasattr(data, "get_data") else data
        if isinstance(raw, list):
            raw = b"".join(raw)
        self.assertIn(b"1 0 0 1 72 144 cm", raw)

    def test_installed_pydyf_is_compatible_with_weasyprint_62(self):
        import pydyf
        major, minor = (int(x) for x in pydyf.__version__.split(".")[:2])
        self.assertTrue(
            hasattr(pydyf.Stream, "transform") and (major, minor) < (0, 12),
            f"installed pydyf {pydyf.__version__} incompatible with WeasyPrint 62.3")


def os_sep_safe(rel: str) -> str:
    return rel  # PackagePath always uses '/' inside wheels/source installs


def _weasyprint_native_available() -> bool:
    before = set(sys.modules)
    try:
        importlib.import_module("weasyprint")
        return True
    except Exception:  # OSError/ImportError when pango/cairo are absent
        # A failed import can leave partially-loaded submodules (weasyprint
        # dlopens pango late in __init__); remove them so they don't leak into
        # other tests' sys.modules assumptions.
        for name in [n for n in list(sys.modules)
                     if n == "weasyprint" or n.startswith("weasyprint.")]:
            if name not in before:
                sys.modules.pop(name, None)
        return False


_SMOKE_HTML = """<!doctype html><html><head><meta charset='utf-8'>
<style>
@page { size: A4; margin: 2cm; }
body { font-family: sans-serif; }
.rot { transform: rotate(3deg); width: 60mm; border: 1px solid #888; }
table { border-collapse: collapse; } td, th { border: 1px solid black; padding: 4px; }
</style></head><body>
<h1>PDF-SMOKE-MARKER</h1>
<p>English and हिन्दी: भारत की राजधानी नई दिल्ली है।</p>
<div class='rot'>rotated block — exercises pydyf Stream.transform</div>
<table><tr><th>Q</th><th>A</th></tr><tr><td>भारत?</td><td>दिल्ली</td></tr></table>
<p>MathML: <math display='inline'><mfrac><mi>x</mi><mn>2</mn></mfrac></math></p>
</body></html>"""


@unittest.skipUnless(_weasyprint_native_available(),
                     "WeasyPrint native libs (pango/cairo/gdk-pixbuf) absent; "
                     "the deploy_vps.sh render gate enforces this on the VPS")
class LiveWeasyPrintRenderTests(unittest.TestCase):
    def test_minimal_real_pdf_renders(self):
        import fitz
        from weasyprint import HTML
        pdf = HTML(string=_SMOKE_HTML).write_pdf()
        self.assertTrue(pdf[:5] == b"%PDF-" and len(pdf) > 2000, pdf[:16])
        doc = fitz.open(stream=pdf, filetype="pdf")
        self.assertGreaterEqual(doc.page_count, 1)
        text = "".join(page.get_text() for page in doc)
        self.assertIn("PDF-SMOKE-MARKER", text)
        self.assertIn("भारत", text)  # Devanagari actually shaped, not tofu crash

    def test_runner_result_pdf_end_to_end(self):
        from quizbot.runner_bot import pdf_reports
        questions = [
            {"question": "भारत की राजधानी?", "options": ["मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई"],
             "correct_option_id": 1, "explanation": "राजधानी नई दिल्ली है, since $x=1$."},
        ]
        leaderboard = [
            {"name": "खिलाड़ी", "correct": 1, "wrong": 0, "score": 1.0, "total_time": 9},
        ]
        polls = {"p0": {"question_index": 0, "correct_option": [1], "sent_time": 0}}
        with tempfile.TemporaryDirectory() as tmp:
            out = f"{tmp}/report.pdf"
            ok = pdf_reports.render_quiz_pdf(
                "Smoke Quiz", "Smoke Chat", questions, leaderboard, polls,
                0.25, 1.0, out, shuffle_options=False, style="classic")
            self.assertTrue(ok, "render_quiz_pdf returned False with native stack present")
            pdf = pathlib.Path(out).read_bytes()
        self.assertTrue(pdf[:5] == b"%PDF-" and len(pdf) > 5000)
        import fitz
        doc = fitz.open(stream=pdf, filetype="pdf")
        self.assertGreaterEqual(doc.page_count, 1)
        self.assertIn("भारत", "".join(p.get_text() for p in doc))


class RunnerResultPdfFailSoftTests(unittest.TestCase):
    """A render-time backend crash degrades to False, never propagates."""

    @staticmethod
    def _broken_weasy(error: BaseException):
        class _Doc:
            def __init__(self, *, string=None, base_url=None):
                self.html = string

            def write_pdf(self, path):
                raise error

        mod = types.ModuleType("weasyprint")
        mod.HTML = _Doc
        return mod

    def test_production_transform_error_returns_false(self):
        from quizbot.runner_bot import pdf_reports
        broken = self._broken_weasy(
            AttributeError("'super' object has no attribute 'transform'"))
        with mock.patch.dict(sys.modules, {"weasyprint": broken}):
            ok = pdf_reports.render_quiz_pdf(
                "Q", "C", [], {}, {}, 0.25, 1.0, "/tmp/never_written_compat.pdf")
        self.assertFalse(ok)

    def test_any_render_exception_returns_false(self):
        from quizbot.runner_bot import pdf_reports
        broken = self._broken_weasy(RuntimeError("pydyf stream failure"))
        with mock.patch.dict(sys.modules, {"weasyprint": broken}):
            ok = pdf_reports.render_quiz_pdf(
                "Q", "C", [], {}, {}, 0.25, 1.0, "/tmp/never_written_compat2.pdf")
        self.assertFalse(ok)


class TestSeriesPdfHeadlessTests(unittest.TestCase):
    """The fpdf2 Test Series PDF needs no native libs and must render for real."""

    def test_testseries_pdf_renders_english_hindi(self):
        from pdf_service.render import render_testseries_pdf
        import fitz
        questions = [
            {"question": "भारत की राजधानी क्या है?",
             "options": ["मुंबई", "दिल्ली", "कोलकाता", "चेन्नई"],
             "correct_option_id": 1, "explanation": "नई दिल्ली।"},
            {"question": "2 + 2 = ?", "options": ["3", "4", "5", "6"],
             "correct_option_id": 1, "explanation": "Arithmetic."},
            {"question": "Select primes.", "options": ["2", "4", "5", "9"],
             "correct_option_id": [0, 2], "explanation": ""},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = f"{tmp}/ts.pdf"
            stats = render_testseries_pdf(
                questions, exam_title="Compat Smoke", tagline="Test Series",
                quiz_names=["SMOKE"], solution_display="end", output_path=out)
            pdf = pathlib.Path(out).read_bytes()
        self.assertEqual(pdf[:5], b"%PDF-")
        self.assertGreater(len(pdf), 5000)
        self.assertGreaterEqual(stats.get("pages", 0), 1)
        doc = fitz.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, stats["pages"])
        text = "".join(p.get_text() for p in doc)
        self.assertIn("भारत", text)


class ResultPdfSecurityRegressionTests(unittest.TestCase):
    """PR #20 escaping/SSRF guards on the runner PDF path stay effective."""

    def test_prose_script_tag_is_escaped(self):
        from quizbot.runner_bot import pdf_reports
        out = pdf_reports._safe_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_dangerous_markdown_urls_denied(self):
        from quizbot.runner_bot import pdf_reports
        self.assertIsNone(pdf_reports._safe_md_url("javascript:alert(1)"))
        self.assertIsNone(pdf_reports._safe_md_url("data:text/html,<x>"))
        self.assertIsNone(pdf_reports._safe_md_url("http://127.0.0.1:8090/x"))
        self.assertEqual(pdf_reports._safe_md_url("https://example.com/a.png"),
                         "https://example.com/a.png")


if __name__ == "__main__":
    unittest.main()
