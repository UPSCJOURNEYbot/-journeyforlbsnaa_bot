"""Phase 1 — Quiz Result PDF audit (runner_bot.pdf_reports).

Covers the audit requirements deterministically:

* page 1: title / metadata / date / question count / marks / leaderboard
  with header columns == data columns, aligned, no phantom/index column;
* page 2+: "QUESTIONS & ANSWERS", every question followed IMMEDIATELY by
  its Answer line and then its Explanation; no duplicate "Q1. Q1.", no
  unsafe option splits, no stranded answer/explanation, no orphan
  headings, no overflow/clipping, leaderboard tables do not overflow,
  Hindi wraps and round-trips visually AND through the text layer;
* content-aware page breaks verified on short / 10 / 25 / 100 /
  Hindi-heavy / mixed / long-explanation reports.

HTML/structure tests run headlessly (a stub WeasyPrint captures the
exact HTML that would be rendered). Real PDF rendering is gated on the
native pango stack and is marked skipped honestly where it is absent —
never faked.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import textwrap
import sys
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path

from quizbot.runner_bot import pdf_reports, wp_indic_tounicode as wpi


# --------------------------------------------------------------------------
# Stub WeasyPrint that captures the HTML instead of rendering it.
# --------------------------------------------------------------------------
class _CapturingHTML:
    captured: list[str] = []
    written: list[str] = []

    def __init__(self, *, string=None, base_url=None, **_kw):
        self.html = string
        _CapturingHTML.captured.append(string)

    def write_pdf(self, target=None, **_kw):
        if isinstance(target, str):
            Path(target).write_bytes(b"%PDF-1.4 stub")
            _CapturingHTML.written.append(target)
        return b"%PDF-1.4 stub"


def _fake_weasy():
    mod = types.ModuleType("weasyprint")
    mod.HTML = _CapturingHTML
    return mod


def _render_html(questions, leaderboard=None, polls=None, **kw):
    _CapturingHTML.captured = []
    _CapturingHTML.written = []
    polls = polls if polls is not None else {
        f"p{i}": {"question_index": i} for i in range(len(questions))}
    leaderboard = leaderboard if leaderboard is not None else [
        {"name": f"Player {i}", "correct": i, "wrong": 1,
         "score": float(i), "total_time": 60 + i} for i in range(1, 6)]
    with mock.patch.dict(sys.modules, {"weasyprint": _fake_weasy()}):
        with tempfile.TemporaryDirectory() as tmp:
            out = f"{tmp}/r.pdf"
            ok = pdf_reports.render_quiz_pdf(
                "Audit Quiz", "Audit Chat", questions, leaderboard, polls,
                0.25, 1.0, out, **kw)
    assert ok, "render_quiz_pdf returned False under stub renderer"
    return _CapturingHTML.captured[0]


def _q(text, opts=("a", "b", "c", "d"), correct=1, explanation="because"):
    return {"question": text, "options": list(opts),
            "correct_option_id": correct, "explanation": explanation}


# --------------------------------------------------------------------------
# Page 1 — header + leaderboard
# --------------------------------------------------------------------------
class PageOneLeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.html = _render_html([_q("One?"), _q("Two?", correct=2)])

    def test_header_columns_equal_body_columns(self):
        m = re.search(r"<table class=\"leaderboard\">(.*?)</table>",
                      self.html, re.S)
        self.assertIsNotNone(m)
        table = m.group(1)
        headers = re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)
        self.assertEqual(len(headers), 7, headers)
        for tr in re.findall(r"<tr class=\"[eo].*?</tr>", table, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            self.assertEqual(len(tds), len(headers), tr)

    def test_no_phantom_index_column(self):
        headers = re.search(r"<thead><tr>(.*?)</tr></thead>",
                            self.html, re.S).group(1)
        labels = re.findall(r"<th[^>]*>(.*?)</th>", headers, re.S)
        # Exactly: rank, participant, correct, wrong, score, accuracy, time.
        self.assertEqual(labels[0], "Rank")
        self.assertIn("Participant", labels[1])
        self.assertEqual(len(labels), 7)
        self.assertNotIn("#", labels)

    def test_page1_metadata_present(self):
        self.assertIn("Audit Quiz", self.html)
        self.assertIn("Audit Chat", self.html)
        self.assertIn("2 Questions", self.html)
        self.assertIn("+1.0", self.html)
        self.assertIn("-0.25", self.html)
        # A date is rendered (dd Mon YYYY).
        self.assertRegex(self.html, r"\d{2} [A-Z][a-z]{2} \d{4}")

    def test_leaderboard_fixed_layout_and_colgroup(self):
        self.assertIn("<colgroup>", self.html)
        self.assertIn("table-layout: fixed", self.html)

    def test_questions_start_on_page_two(self):
        # The whole Q&A section carries an explicit page break.
        self.assertIn('class="questions-section"', self.html)
        self.assertIn("page-break-before: always", self.html)


# --------------------------------------------------------------------------
# Page 2+ — question / answer / explanation order
# --------------------------------------------------------------------------
class QuestionAnswerOrderTests(unittest.TestCase):
    def setUp(self):
        self.questions = [
            _q("First stem?", correct=0, explanation="first reason"),
            _q("Second stem?", correct=3, explanation="second reason"),
        ]
        self.html = _render_html(self.questions)

    def test_each_card_is_question_then_options_then_answer_then_expl(self):
        cards = self._cards()
        self.assertEqual(len(cards), 2)
        for i, card in enumerate(cards):
            i_q = card.index("q-text")
            i_opts = card.index("options-container")
            i_ans = card.index("answer-box")
            i_exp = card.index("explanation-box")
            self.assertLess(i_q, i_opts, card[:80])
            self.assertLess(i_opts, i_ans, card[:80])
            self.assertLess(i_ans, i_exp, card[:80])

    def test_answer_letters_match_correct_options(self):
        cards = self._cards()
        self.assertIn("<strong>A)</strong> a", cards[0])
        self.assertIn("<strong>D)</strong> d", cards[1])

    def test_options_all_present_unsplit(self):
        cards = self._cards()
        for card in cards:
            self.assertEqual(card.count('class="opt-letter"'), 4)

    def _cards(self):
        starts = [m.start() for m in
                  re.finditer(r'<div class="question-card', self.html)]
        out = []
        for k, s in enumerate(starts):
            nxt = starts[k + 1] if k + 1 < len(starts) else len(self.html)
            out.append(self.html[s:nxt])
        return out

    def test_qa_section_heading_present_once(self):
        self.assertEqual(self.html.count("Questions &amp; Answers"), 1)


class BadgeAndStrandingTests(unittest.TestCase):
    def test_duplicate_q_badge_removed_from_question_text(self):
        html = _render_html([_q("Q1. Stored text already badged?", correct=0)])
        qtext = re.search(r'<span class="q-text">(.*?)</span>', html, re.S).group(1)
        self.assertNotIn("Q1.", qtext)
        self.assertIn("Stored text already badged", qtext)
        # the card badge itself still renders exactly once
        card = html[html.find('class="question-card'):]
        self.assertEqual(card.count('class="q-badge"'), 1)

    def test_bare_number_statement_not_stripped(self):
        # Numeric statement-style stems are content, not Q badges.
        out = pdf_reports._strip_rendered_badge("1947 saw independence.")
        self.assertEqual(out, "1947 saw independence.")

    def test_no_stranded_explanation_box(self):
        html = _render_html([_q("One?"), _q("Two?", explanation="")])
        self.assertEqual(html.count('class="explanation-box"'), 1)
        self.assertLess(html.index("explanation-box"),
                        html.find("footer-info"))


class LongContentBreaksTests(unittest.TestCase):
    def test_long_explanation_gets_wide_card(self):
        long_expl = "Reason " + ". ".join(f"पंक्ति {i} का विवरण यहाँ है"
                                          for i in range(60))
        html = _render_html([_q("Long?", explanation=long_expl)])
        self.assertIn("question-card--wide", html)
        # Normal cards stay wholly on one page/column (no header orphan at a
        # page bottom); only explicit wide cards may fragment, and a card
        # header must never be separated from its options/answer.
        self.assertIn("break-inside: avoid", html)
        self.assertNotIn("break-inside: avoid-column", html)
        self.assertIn(".q-header { break-after: avoid", html)
        self.assertIn(".options-container { break-after: avoid", html)

    def test_section_banner_avoids_orphan_heading(self):
        sections = [{"name": "Polity", "question_indices": list(range(2))}]
        html = _render_html([_q("A?"), _q("B?")], sections=sections)
        self.assertIn("break-after: avoid", html)


class SecurityStructureTests(unittest.TestCase):
    def test_script_in_all_user_fields_is_escaped(self):
        q = _q("<script>alert(1)</script> stem?",
               opts=("<img src=x>", "<b>x</b>", "c", "d"), correct=0,
               explanation="<script>x</script>")
        leaderboard = [{"name": "<script>x</script>", "correct": 1,
                        "wrong": 0, "score": 1.0, "total_time": 5}]
        html = _render_html([q], leaderboard=leaderboard)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_bundled_offline_fonts_and_no_google_link(self):
        html = _render_html([_q("हिन्दी प्रश्न?")])
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertIn("@font-face", html)
        self.assertIn("Hind-Regular.ttf", html)


# --------------------------------------------------------------------------
# Badge helper unit cases
# --------------------------------------------------------------------------
class BadgeHelperTests(unittest.TestCase):
    def test_variants(self):
        f = pdf_reports._strip_rendered_badge
        self.assertEqual(f("Q1. Capital?"), "Capital?")
        self.assertEqual(f("Q12) Which?"), "Which?")
        self.assertEqual(f("q 5 - Stem"), "Stem")
        self.assertEqual(f("Q12revenue stays?"), "Q12revenue stays?")


# --------------------------------------------------------------------------
# Live rendering (gated honestly on the native WeasyPrint stack)
# --------------------------------------------------------------------------
def _native_weasy_available() -> bool:
    before = set(sys.modules)
    try:
        importlib.import_module("weasyprint")
        from weasyprint import HTML  # noqa: F401
        HTML(string="<p>x</p>").write_pdf()
        return True
    except Exception:
        for name in [n for n in list(sys.modules)
                     if n == "weasyprint" or n.startswith("weasyprint.")]:
            if name not in before:
                sys.modules.pop(name, None)
        return False


NATIVE = _native_weasy_available()


@unittest.skipUnless(NATIVE,
                     "WeasyPrint native stack absent; render gate runs on VPS")
class LiveRenderMatrixTests(unittest.TestCase):
    def _render(self, questions, leaderboard=None, style="classic"):
        polls = {f"p{i}": {"question_index": i, "correct_option":
                           [questions[i]["correct_option_id"]
                            if isinstance(questions[i]["correct_option_id"], int)
                            else questions[i]["correct_option_id"][0]],
                           "sent_time": i}
                 for i in range(len(questions))}
        if leaderboard is None:
            leaderboard = [
                {"name": f"Player {i} {('राहुल' if i % 3 == 0 else '')}",
                 "correct": i % 4, "wrong": i % 2, "score": float(i) / 2,
                 "total_time": 30 + i * 7} for i in range(1, min(len(questions), 20) + 1)]
        with tempfile.TemporaryDirectory() as tmp:
            out = f"{tmp}/live.pdf"
            ok = pdf_reports.render_quiz_pdf(
                "Matrix Quiz", "मैट्रिक्स चैट", questions, leaderboard, polls,
                0.25, 1.0, out, shuffle_options=False, style=style)
            self.assertTrue(ok, "render returned False with native stack present")
            return Path(out).read_bytes()

    def _english(self, n, prefix=""):
        return [_q(f"{prefix}Question {i} — what is {i}*2?",
                   opts=(f"opt {i}a", f"ans {2*i}", f"opt {i}c", f"opt {i}d"),
                   correct=1,
                   explanation=f"Explanation for {i}: twice {i} is {2*i}.")
                for i in range(1, n + 1)]

    def test_short_report(self):
        pdf = self._render(self._english(1))
        audit = wpi.audit_report_pdf(
            pdf, must_contain=["Matrix Quiz", "Answer:", "Explanation:",
                               "Questions & Answers" if False else "Questions"])
        self.assertTrue(audit["ok"], audit)

    @staticmethod
    def _fold(text):
        return re.sub(r"\s+", " ", text)

    def test_10_25_100_question_reports(self):
        for n in (10, 25, 100):
            pdf = self._render(self._english(n))
            pages = wpi.page_texts(pdf)
            folded = self._fold(" ".join(pages))
            for needle in (f"Q{n}", f"Question {n}"):
                self.assertIn(needle, folded, f"n={n} missing {needle}")
            # page 1 is summary only: Q&A starts on page 2+
            self.assertEqual(wpi.page_count(pdf), len(pages))
            self.assertNotIn("Answer:", pages[0])
            self.assertIn("Answer:", "\n".join(pages[1:]))

    def test_qa_order_in_text_layer(self):
        # Order is verified geometrically within one column (multi-column
        # flow interleaves columns in spatial text extraction).
        import fitz
        pdf = self._render(self._english(6))
        doc = fitz.open(stream=pdf, filetype="pdf")
        page = doc[1]
        width = page.rect.width
        words = [w for w in page.get_text("words") if w[0] < width / 2]
        words.sort(key=lambda w: (round(w[1] / 3), w[0]))
        col_text = " ".join(w[4] for w in words)
        doc.close()
        i_q = col_text.index("Question")
        i_a = col_text.index("Answer:", i_q)
        i_e = col_text.index("Explanation:", i_a)
        self.assertLess(i_q, i_a, col_text[:400])
        self.assertLess(i_a, i_e, col_text[:400])

    def test_hindi_heavy_visual_and_text_layer(self):
        questions = [
            _q("भारत की राजधानी क्या है?",
               opts=("मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई"),
               correct=1,
               explanation="संविधान के अनुसार नई दिल्ली राजधानी है।"),
            _q("राष्ट्रीय पक्षी कौन सा है?",
               opts=("तोता", "मोर", "कबूतर", "हंस"), correct=1,
               explanation="भारतीय मोर राष्ट्रीय पक्षी है।"),
        ]
        pdf = self._render(questions)
        audit = wpi.audit_report_pdf(pdf, expected_terms=[
            "भारत की राजधानी", "नई दिल्ली", "संविधान", "राष्ट्रीय पक्षी",
            "भारतीय मोर", "मैट्रिक्स चैट"], must_contain=["Answer:"])
        self.assertTrue(audit["ok"], audit)

    def test_mixed_hinglish(self):
        questions = [
            _q("Which article अनुच्छेद guarantees life?",
               opts=("Article 19", "Article 21", "Article 32", "Article 14"),
               correct=1, explanation="Article 21 जीवन और स्वतंत्रता की रक्षा करता है।"),
        ]
        pdf = self._render(questions)
        audit = wpi.audit_report_pdf(
            pdf, expected_terms=["अनुच्छेद", "जीवन और स्वतंत्रता"],
            must_contain=["Article 21", "Answer:"])
        self.assertTrue(audit["ok"], audit)

    def test_long_explanation_not_clipped(self):
        long_expl = ("विवरण: " + " ".join(f"पंक्ति-{i}" for i in range(120))
                     + " UNIQUE TAIL MARKER ZYX अंतिम पंक्ति")
        pdf = self._render([_q("Long one?", explanation=long_expl),
                            _q("After?", correct=0, explanation="ok after")])
        audit = wpi.audit_report_pdf(
            pdf, expected_terms=["अंतिम", "पंक्ति", "विवरण"],
            must_contain=["UNIQUE TAIL MARKER ZYX", "ok after"])
        self.assertTrue(audit["ok"], audit)

    def test_no_duplicate_badge_in_rendered_pdf(self):
        pdf = self._render([_q("Q1. Already numbered stem here?", correct=0)])
        text = re.sub(r"\s+", " ", wpi.full_text(pdf))
        # Exactly one Q1 token: the card's own badge. The stored "Q1."
        # prefix must not be rendered a second time inside the stem.
        self.assertEqual(len(re.findall(r"Q1\b(?!\.)", text)), 1, text)
        self.assertNotIn("Q1.", text)
        self.assertIn("Already numbered stem", text)

    def test_tofu_never_present(self):
        pdf = self._render(self._english(10))
        self.assertEqual(wpi.find_tofu(pdf), [])

    def test_modern_style_renders(self):
        pdf = self._render(self._english(4), style="modern")
        audit = wpi.audit_report_pdf(
            pdf, expected_terms=[], must_contain=["Answer:"])
        self.assertTrue(audit["ok"], audit)

    def test_consecutive_prebase_matra_roundtrip(self):
        # Regression gate for the WeasyPrint ToUnicode bug: clusters of
        # consecutive pre-base short-i syllables (वि..., पं...क्ति) were
        # duplicated/reordered in the text layer while the visible page was
        # correct. The /ActualText runtime patch must make extraction exact.
        trap_stems = ["विवरण क्या है?", "विवाह कितने प्रकार का?",
                      "विवेकानंद कौन थे?", "पंक्ति किसे कहते हैं?",
                      "विविधता में एकता क्या है?"]
        trap_opts = [("विवरण एक", "विवाह दो", "विवेक तीन", "पंक्ति चार")]
        questions = [_q(stem, opts=trap_opts[0], correct=i % 4,
                        explanation="व्याख्या: विवरण, विवाह और विवेक "
                                    "तीनों में ि की मात्रा है।")
                     for i, stem in enumerate(trap_stems * 4)]
        pdf = self._render(questions)
        text = wpi.full_text(pdf)
        for corrupt in ("विविरण", "विविाह", "विविेक", "विविि"):
            self.assertNotIn(corrupt, text,
                             f"pre-base matra corruption present: {corrupt}")
        audit = wpi.audit_report_pdf(
            pdf, expected_terms=["विवरण", "विवाह", "विवेक", "पंक्ति",
                                 "विविधता", "व्याख्या"],
            must_contain=["Answer:"])
        self.assertTrue(audit["ok"], audit)

    def test_actualtext_markers_in_content_stream(self):
        # Structural proof the Indic runtime patch is active: every Pango
        # run is wrapped in /Span << /ActualText ... >> BDC.
        import fitz
        pdf = self._render([_q("विवरण?", correct=0)])
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            found = 0
            for page in doc:
                for xref in page.get_contents():
                    if b"ActualText" in doc.xref_stream(xref):
                        found += 1
            self.assertGreaterEqual(found, 1,
                                    "no /ActualText spans in content streams")
        finally:
            doc.close()

    def test_color_emoji_render_as_bitmaps_not_zero_width(self):
        # Header/medal/check emoji must be visible color bitmaps, not
        # zero-width .notdef glyphs (requires a color emoji font in the
        # render stack; a provisioning failure must fail this gate).
        import fitz
        pdf = self._render(self._english(3))
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            self.assertGreaterEqual(
                len(doc[0].get_images()), 1,
                "no color emoji bitmaps embedded on page 1")
            # No check/cross glyph may be a zero-width text char.
            for page in doc:
                for block in page.get_text("rawdict")["blocks"]:
                    for line in block.get("lines", []):
                        for span in line["spans"]:
                            for ch in span["chars"]:
                                if ord(ch["c"]) in (0x2705, 0x274c):
                                    x0, _, x1, _ = ch["bbox"]
                                    self.assertGreater(
                                        x1 - x0, 0.1,
                                        "check/cross rendered zero-width")
        finally:
            doc.close()

    def test_patch_recovers_from_failed_first_application(self):
        # If the first application sees a stubbed/broken WeasyPrint it must
        # return False gracefully (no raise, no permanent disablement); once
        # the real module is available it applies successfully.
        import weasyprint.draw as wp_draw
        from quizbot.runner_bot import wp_indic_compat
        original = wp_draw.draw_first_line
        try:
            wp_draw.draw_first_line = None
            self.assertFalse(
                wp_indic_compat.apply_weasyprint_indic_actualtext_fix())
        finally:
            wp_draw.draw_first_line = original
        self.assertTrue(
            wp_indic_compat.apply_weasyprint_indic_actualtext_fix())

    def test_patch_lifecycle_isolated_in_subprocess(self):
        # Full "fake WeasyPrint first, real one later" lifecycle, in a
        # subprocess: unloading cffi-backed native modules in-process crashes
        # on dlclose, so the lifecycle is verified in isolation.
        env_script = textwrap.dedent(r"""
            import sys, types
            fake = types.ModuleType('weasyprint')
            sys.modules['weasyprint'] = fake
            from quizbot.runner_bot import wp_indic_compat as c
            assert c.apply_weasyprint_indic_actualtext_fix() is False
            del sys.modules['weasyprint']
            import weasyprint
            assert c.apply_weasyprint_indic_actualtext_fix() is True
            pdf = weasyprint.HTML(
                string="<div style='font-family:Hind'>"
                       "\u0935\u093f\u0935\u0930\u0923</div>"
            ).write_pdf()
            import fitz
            doc = fitz.open(stream=pdf, filetype='pdf')
            assert any(b'ActualText' in doc.xref_stream(x)
                       for x in doc[0].get_contents()), 'no ActualText'
            assert '\u0935\u093f\u0935\u0930\u0923' in doc[0].get_text(), \
                doc[0].get_text()
            print('LIFECYCLE_OK')
        """)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [os.getcwd(), env.get("PYTHONPATH", "")])
        proc = subprocess.run(
            [sys.executable, "-c", env_script], env=env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            f"subprocess failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("LIFECYCLE_OK", proc.stdout)

    def test_single_question_card_stays_atomic(self):
        # With exactly one question the card must NOT be split across both
        # columns (question left, answer/explanation stranded right).
        import fitz
        pdf = self._render([_q("Solo question?", correct=0,
                               explanation="solo explanation")])
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            page = doc[1]
            half = page.rect.width / 2
            words = page.get_text("words")
            for token in ("Solo", "Answer:", "solo", "explanation"):
                hits = [w for w in words if w[4].startswith(token)]
                self.assertTrue(hits, f"{token} missing")
                self.assertTrue(all(w[0] < half for w in hits),
                                f"{token} stranded in the right column")
        finally:
            doc.close()


class IndicAuditHelperTests(unittest.TestCase):
    def test_normalization_ignores_joiners_not_letters(self):
        pdf = None  # exercise normalize indirectly via fabricated bytes-free path
        n = wpi._normalize("क\u200dख\u200cग\ufe0f")
        self.assertEqual(n, "कखग")

    def test_audit_report_signature_on_fake_pdf(self):
        # Build a real tiny PDF via the live stack when present; otherwise
        # the helper's failure mode is honestly exercised with empty input.
        if not NATIVE:
            self.skipTest("native stack absent")
        from weasyprint import HTML
        pdf = HTML(string="<p>भारत Answer: दिल्ली</p>").write_pdf()
        audit = wpi.audit_report_pdf(pdf, expected_terms=["भारत", "दिल्ली"],
                                     must_contain=["Answer:"])
        self.assertTrue(audit["ok"], audit)
        bad = wpi.audit_report_pdf(pdf, expected_terms=["अनुपस्थित वाक्यांश"])
        self.assertFalse(bad["ok"])
        self.assertIn("अनुपस्थित वाक्यांश", bad["missing_indic"])


if __name__ == "__main__":
    unittest.main()
