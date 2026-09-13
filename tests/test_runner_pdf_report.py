"""
Runner result-PDF content safety / math rendering.

The runner result PDF is produced by WeasyPrint, which executes NO JavaScript
-- so the client-side KaTeX used in the interactive HTML report is unavailable
there. Math therefore has to be converted server-side to MathML via
``latex2mathml`` (a pure-Python, pinned dependency). These tests pin:

* LaTeX -> MathML conversion for inline/block math, with a graceful raw-text
  fallback when the optional package is missing (no crash);
* HTML escaping of non-math text (creator/user content cannot inject markup);
* the small GitHub-flavoured-markdown converter;
* the WeasyPrint availability guard returning False (rather than raising) when
  the package OR its native libraries cannot be loaded.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from quizbot.runner_bot import pdf_reports


class MathRenderTests(unittest.TestCase):
    def test_inline_and_block_math_become_mathml(self):
        out = pdf_reports._safe_html("Energy $E=mc^2$ then $$x=1$$ done.")
        self.assertIn("<math", out)
        self.assertEqual(out.count("<math"), 2)
        # surrounding prose survives
        self.assertIn("Energy", out)
        self.assertIn("done.", out)

    def test_mathml_tags_are_not_escaped_but_prose_is(self):
        out = pdf_reports._safe_html("a < b while $x>0$")
        self.assertIn("&lt;", out)          # the prose inequality is escaped
        self.assertNotIn("<script>", out)

    def test_non_math_text_is_escaped(self):
        out = pdf_reports._safe_html("<b>bold</b> & stuff", render_math=False)
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("&amp;", out)

    def test_missing_latex2mathml_falls_back_to_raw_text(self):
        # Simulate the optional package being unavailable.
        real = sys.modules.get("latex2mathml")
        fake = types.ModuleType("latex2mathml")
        # no .converter attribute -> the in-function ImportError path runs
        sys.modules["latex2mathml"] = fake
        sys.modules.pop("latex2mathml.converter", None)
        try:
            out = pdf_reports._render_math_in_html("$x^2$")
            self.assertEqual(out, "$x^2$")  # raw fallback, never raises
        finally:
            if real is not None:
                sys.modules["latex2mathml"] = real
            else:
                sys.modules.pop("latex2mathml", None)


class MarkdownConverterTests(unittest.TestCase):
    def test_inline_and_list(self):
        html = pdf_reports.render_markdown_to_html("**b** and *i*\n- a\n- b")
        self.assertIn("<strong>b</strong>", html)
        self.assertIn("<em>i</em>", html)
        self.assertIn("<li>a</li>", html)

    def test_raw_html_is_escaped_not_trusted(self):
        # Converter output must not let author HTML through verbatim.
        html = pdf_reports.render_markdown_to_html("<img src=x onerror=y>")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_dangerous_url_schemes_denied_safe_kept(self):
        # file:// / javascript: / protocol-relative must not become hrefs/src
        # (WeasyPrint would auto-load image resources).
        for bad in ("[x](file:///etc/passwd)",
                    "[x](javascript:alert(1))",
                    "![x](//169.254.169.254/latest/meta-data/)",
                    "![x](http://127.0.0.1:8090/healthz)"):
            html = pdf_reports.render_markdown_to_html(bad)
            self.assertNotIn('href="file:', html, bad)
            self.assertNotIn("javascript:", html, bad)
            self.assertNotIn('src="//', html, bad)
            self.assertNotIn("169.254.169.254", html, bad)
            self.assertNotIn("127.0.0.1", html, bad)
        good = pdf_reports.render_markdown_to_html(
            "[ok](https://example.com/a) ![pic](https://img.example.com/x.png)")
        self.assertIn('href="https://example.com/a"', good)
        self.assertIn('<img src="https://img.example.com/x.png"', good)

    def test_blockquote_marker_survives_escaping(self):
        html = pdf_reports.render_markdown_to_html("> quoted text")
        self.assertIn("<blockquote>", html)
        self.assertIn("quoted text", html)

    def test_attribute_breakout_neutralized(self):
        import re
        html = pdf_reports.render_markdown_to_html(
            '[x](https://a" onload="alert(1))')
        # Raw double quotes that would close the href attribute are escaped,
        # so the anchor keeps exactly href + target and no new attribute/tag.
        self.assertNotIn("<script", html)
        anchors = re.findall(r"<a [^>]*>", html)
        self.assertTrue(anchors)
        for tag in anchors:
            # No raw quote survives to break out of the href attribute, and
            # the tag is exactly href + target (the word "onload" may only
            # appear inertly inside the escaped attribute value).
            self.assertTrue(re.fullmatch(
                r'<a href="[^"]*" target="_blank">', tag), tag)

    def test_inline_code_escaped(self):
        html = pdf_reports.render_markdown_to_html("use `<b>` here")
        self.assertNotIn("<b>", html.replace("<strong>", "").replace("</strong>", ""))
        self.assertIn("<code>", html)


class QuestionAssemblyTests(unittest.TestCase):
    """The real production path that feeds WeasyPrint (no rendering needed):
    _build_questions_html covers Q->options->answer->explanation, the shuffle
    remap, math and escaping."""

    def _polls(self, n):
        return {f"p{i}": {"question_index": i, "correct_option": [],
                          "sent_time": 0} for i in range(n)}

    def test_math_hindi_and_long_text(self):
        questions = [{
            "question": "ऊर्जा का सूत्र? Energy $E=mc^2$ with a very long " +
                        "stem " * 40,
            "options": ["$a^2$", "दूसरा", "तीसरा", "चौथा"],
            "correct_option_id": 0,
            "explanation": "Because $$E=mc^2$$ — समानता।",
        }]
        html = pdf_reports._build_questions_html(
            questions, self._polls(1), None, False, "classic")
        self.assertIn("<math", html)           # inline + block math -> MathML
        self.assertIn("ऊर्जा", html)           # Devanagari preserved
        self.assertIn("समानता", html)
        self.assertIn("opt-correct", html)

    def test_everything_is_escaped(self):
        xss = "<img src=x onerror=alert(1)>"
        questions = [{
            "question": xss,
            "options": [xss, "b", "c", "d"],
            "correct_option_id": 1,
            "explanation": xss,
            "reply_text": xss,
        }]
        html = pdf_reports._build_questions_html(
            questions, self._polls(1), None, False, "classic")
        # No live tag/attribute: brackets are entity-escaped everywhere.
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_shuffle_keeps_exactly_one_correct_mapping(self):
        import random
        questions = [{
            "question": "q", "options": ["alpha", "beta", "gamma", "delta"],
            "correct_option_id": 2, "explanation": "gamma is right",
        }]
        orig_shuffle = random.shuffle
        try:
            for _ in range(20):
                html = pdf_reports._build_questions_html(
                    questions, self._polls(1), None, True, "classic")
                self.assertEqual(html.count("opt-correct"), 1)
        finally:
            random.shuffle = orig_shuffle

    def test_forced_shuffle_moves_correct_option(self):
        import random
        questions = [{
            "question": "q", "options": ["alpha", "beta", "gamma", "delta"],
            "correct_option_id": 0,
            "explanation_why": "",
            "option_notes": {0: "note-on-alpha"},
            "explanation": "see note",
        }]
        orig = random.shuffle

        def reverse_order(x):
            x[:] = list(reversed(x))

        random.shuffle = reverse_order
        try:
            html = pdf_reports._build_questions_html(
                questions, self._polls(1), None, True, "classic")
        finally:
            random.shuffle = orig
        # With reversed order canonical index 0 ("alpha") is printed last (D);
        # the opt-correct class must be on that row, not the first.
        first_correct = html.index("opt-correct")
        first_normal = html.index("opt-normal")
        self.assertGreater(first_correct, first_normal)
        self.assertIn("alpha", html)

    def test_section_banner_escaped(self):
        questions = [{"question": "q", "options": ["a", "b"],
                      "correct_option_id": 0}]
        sections = [{"name": "<script>x</script>", "start": 0, "end": 0}]
        html = pdf_reports._build_questions_html(
            questions, self._polls(1), sections, False, "classic")
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


class FullDocumentAssemblyTests(unittest.TestCase):
    """Drive render_quiz_pdf with a stubbed WeasyPrint (pango is absent in CI)
    so the complete HTML document is validated end to end."""

    def _stub_weasy(self, captured):
        import sys
        import types

        class _Doc:
            def __init__(self, *, string=None, base_url=None):
                captured["html"] = string

            def write_pdf(self, path):
                # write a tiny valid PDF so the "success" path runs
                with open(path, "wb") as fh:
                    fh.write(b"%PDF-1.4\nstub\n")

        mod = types.ModuleType("weasyprint")
        mod.HTML = _Doc
        return mod

    def test_full_report_assembles_and_escapes(self):
        import os
        import sys
        import types
        from unittest import mock

        captured = {}
        fake = self._stub_weasy(captured)
        questions = [
            {"question": "Q1 संविधान?", "options": ["a", "b <x>", "c", "d"],
             "correct_option_id": 1, "explanation": "because $x=1$"},
        ]
        leaderboard = [
            {"name": "<b>evil</b>", "correct": 1, "wrong": 0, "score": 1.0,
             "total_time": 12},
        ]
        polls = {"p0": {"question_index": 0, "correct_option": [1],
                        "sent_time": 0}}
        out = "/tmp/_audit_full_report.pdf"
        with mock.patch.dict(sys.modules, {"weasyprint": fake}):
            ok = pdf_reports.render_quiz_pdf(
                "<b>Eval</b>", "<chat>", questions, leaderboard, polls,
                0.25, 1.0, out, shuffle_options=False, style="classic")
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(out))
        os.remove(out)
        doc = captured["html"]
        # titles/leaderboard names escaped, no live injected tag
        self.assertNotIn("<b>Eval</b>", doc)
        self.assertIn("&lt;b&gt;Eval&lt;/b&gt;", doc)
        self.assertNotIn("<b>evil</b>", doc)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", doc)
        # question + MathML + correct row present
        self.assertIn("संविधान", doc)
        self.assertIn("<math", doc)
        self.assertIn("opt-correct", doc)
        self.assertIn("Questions &amp; Answers", doc)


class WeasyPrintGuardTests(unittest.TestCase):
    def _render(self):
        return pdf_reports.render_quiz_pdf(
            "Quiz", "Chat", [], {}, {}, 0.25, 1.0, "/tmp/never_written.pdf",
        )

    def test_native_library_failure_returns_false_not_raises(self):
        # A host with weasyprint present but pango/cairo missing raises
        # OSError at import time; the guard must turn that into False.
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **k):
            if name == "weasyprint" or name.startswith("weasyprint."):
                raise OSError("cannot load library 'pango-1.0-0'")
            return real_import(name, *a, **k)

        with mock.patch.object(builtins, "__import__", side_effect=boom):
            for name in [n for n in list(sys.modules)
                         if n == "weasyprint" or n.startswith("weasyprint.")]:
                sys.modules.pop(name, None)
            self.assertFalse(self._render())

    def test_missing_package_returns_false(self):
        import builtins
        real_import = builtins.__import__

        def missing(name, *a, **k):
            if name == "weasyprint" or name.startswith("weasyprint."):
                raise ImportError("No module named 'weasyprint'")
            return real_import(name, *a, **k)

        with mock.patch.object(builtins, "__import__", side_effect=missing):
            for name in [n for n in list(sys.modules)
                         if n == "weasyprint" or n.startswith("weasyprint.")]:
                sys.modules.pop(name, None)
            self.assertFalse(self._render())


if __name__ == "__main__":
    unittest.main()
