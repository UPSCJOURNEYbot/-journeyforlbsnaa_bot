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
            for mod in [m for m in list(sys.modules) if m.startswith("weasyprint")]:
                sys.modules.pop(m, None)
            self.assertFalse(self._render())

    def test_missing_package_returns_false(self):
        import builtins
        real_import = builtins.__import__

        def missing(name, *a, **k):
            if name == "weasyprint" or name.startswith("weasyprint."):
                raise ImportError("No module named 'weasyprint'")
            return real_import(name, *a, **k)

        with mock.patch.object(builtins, "__import__", side_effect=missing):
            for mod in [m for m in list(sys.modules) if m.startswith("weasyprint")]:
                sys.modules.pop(m, None)
            self.assertFalse(self._render())


if __name__ == "__main__":
    unittest.main()
