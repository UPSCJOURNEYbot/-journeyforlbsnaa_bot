"""
Security regression tests for the downloadable HTML reports
(quizbot/shared/html/quiz_report.py).

Quiz questions, options, explanations and participant names are
creator-/user-controlled strings rendered into a single-file HTML document.
These tests pin the defence in depth:

* the per-fragment allowlist sanitiser (``sanitizeReportHtml``) is present
  in the quiz report, runs over marked.js output BEFORE trusted KaTeX HTML is
  substituted back, and strips ``on*`` handlers / ``javascript:`` URLs;
* untrusted text crosses into JS only via the JS-string escaper (so a literal
  ``</script>`` cannot break out of the <script> block);
* the analysis report escapes participant/quiz text server-side.

The runtime DOM behaviour (real jsdom + marked, vectors such as
``<img onerror>``, ``javascript:`` hrefs and SVG onload) was additionally
verified out-of-band; these tests keep the wiring from silently regressing.
"""

from __future__ import annotations

import asyncio
import re
import unittest

from quizbot.shared.html import quiz_report


def _render(quiz):
    return asyncio.run(quiz_report.render_quiz_html(quiz))[0].decode("utf-8")


class QuizReportSanitiserTests(unittest.TestCase):
    def setUp(self):
        self.html = _render({
            "quiz_name": "security probe",
            "questions": [{
                "question": "Q",
                "options": ["a", "b", "c", "d"],
                "correct_option_id": 1,
                "explanation": "e",
            }],
        })

    def test_sanitiser_is_defined(self):
        self.assertIn("function sanitizeReportHtml(html)", self.html)
        # allowlist present
        self.assertIn("REPORT_ALLOWED_TAGS", self.html)
        # event handlers stripped
        self.assertRegex(self.html, r"indexOf\('on'\)\s*===?\s*0")
        # dangerous URL schemes stripped from href/src
        self.assertRegex(self.html, r"javascript\|vbscript\|data")

    def test_sanitiser_runs_before_katex_substitution(self):
        # The sanitizer call must precede the MATH-placeholder restore so
        # trusted KaTeX markup is never parsed as author HTML.
        san = self.html.index("html=sanitizeReportHtml(html)")
        math = self.html.index("html=html.replace(/@@MATHPH")
        self.assertLess(san, math)

    def test_author_script_breakout_is_escaped_in_js(self):
        evil = "</script><script>alert(1)</script><img src=x onerror=alert(2)>"
        html = _render({
            "quiz_name": evil,
            "questions": [{
                "question": evil,
                "options": [evil, "b", "c", "d"],
                "correct_option_id": 0,
                "explanation": evil,
            }],
        })
        # The closing-tag breakout is neutralised: the embedded qd payload
        # contains NO raw angle brackets at all (escaped to \u003c/\u003e).
        payload = html.split("const qd=", 1)[1].split("\n", 1)[0]
        self.assertNotIn("<", payload)
        self.assertIn("\\u003c/script\\u003e", payload)
        # The <img onerror> text rides along only inside the quoted JS string
        # (inert there); the DOM sanitiser (verified separately against jsdom)
        # strips that handler before innerHTML attachment.

    def test_js_string_escaper_neutralises_script_breakout(self):
        for raw in ("</script>", "</ScRiPt ><script>", "<!--",
                    "</style><script>x</script>", "line1\nline2",
                    'he said "hi"', "back\\slash"):
            esc = quiz_report._je(raw)
            self.assertNotIn("</", esc, raw)
            self.assertNotIn("<!--", esc, raw)
            # double quotes / backslashes / newlines are quoted
            self.assertNotIn('\n', esc)


class AnalysisReportEscapingTests(unittest.TestCase):
    def test_participant_and_quiz_text_escaped(self):
        html = asyncio.run(quiz_report.render_analysis_html(
            {
                "quiz_name": "<script>x</script>",
                "qid": "q1",
                "questions": [{"question": "<img src=x onerror=y>"}],
            },
            [{
                "user_name": "<b onmouseover=alert(1)>name</b>",
                "score": 1,
                "total_questions": 1,
                "time_taken": 5,
            }],
        ))[0].decode("utf-8")
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)
        # the malicious name/attribute are escaped to inert text ...
        self.assertNotIn("<b onmouseover", html)
        self.assertIn("&lt;b onmouseover=alert(1)&gt;", html)

    def test_chart_payload_is_encoded_as_json_literal(self):
        html = asyncio.run(quiz_report.render_analysis_html(
            {"quiz_name": "n", "qid": "q", "questions": []},
            [{"user_name": "</script>", "score": 0,
              "total_questions": 0, "time_taken": 0}],
        ))[0].decode("utf-8")
        # The embedded DATA literal must contain NO raw angle bracket at all
        # (json.dumps + bracket unicode-escaping); visible values are unchanged.
        start = html.index("var DATA =")
        # the DATA literal is a single line; bound before the static app JS
        segment = html[start: html.index("\n", start)]
        self.assertNotIn("<", segment)
        self.assertIn("\\u003c/script\\u003e", segment)


if __name__ == "__main__":
    unittest.main()
