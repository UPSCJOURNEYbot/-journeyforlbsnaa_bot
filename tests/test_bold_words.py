"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.

Tests for automatic important-word bold formatting + the new /start
"Journey for LBSNAA" welcome text.

The bolding is presentation-only: stripping the <b> tags and unescaping the
entities of any formatted output must yield *exactly* the original text, and
options/order/answer keys are never touched (the formatter is simply never
handed them).
"""

from __future__ import annotations

import html
import inspect
import re
import unittest

from quizbot.shared.bold_words import (
    MAX_BOLD_RATIO,
    MAX_BOLD_SPANS,
    MIN_TEXT_LEN,
    apply_bold_to_poll_fields,
    bold_important_words,
    format_bold_html,
)

_B_TAG_RE = re.compile(r"</?b>")
_SPAN_RE = re.compile(r"<b>(.*?)</b>", re.S)


def _plain(html_text: str) -> str:
    """Inverse of bold_important_words' escaping+tagging."""
    return html.unescape(_B_TAG_RE.sub("", html_text))


def _spans(html_text: str) -> list[str]:
    return _SPAN_RE.findall(html_text)


class BoldEnglishTests(unittest.TestCase):
    EN_Q = (
        "Which Article of the Constitution of India deals with the "
        "Fundamental Duties of citizens?"
    )

    def test_wording_preserved_exactly(self):
        out = bold_important_words(self.EN_Q)
        self.assertEqual(_plain(out), self.EN_Q)

    def test_bold_span_count_within_balance(self):
        out = bold_important_words(self.EN_Q)
        self.assertGreaterEqual(len(_spans(out)), 1)
        self.assertLessEqual(len(_spans(out)), MAX_BOLD_SPANS)

    def test_char_ratio_within_balance(self):
        out = bold_important_words(self.EN_Q)
        bolded = sum(len(s) for s in _spans(out))
        self.assertLessEqual(bolded, MAX_BOLD_RATIO * len(self.EN_Q))

    def test_genuinely_important_words_selected(self):
        out = bold_important_words(self.EN_Q)
        spans = " | ".join(_spans(out)).casefold()
        self.assertIn("constitution", spans)
        self.assertNotIn("which", spans)
        self.assertNotIn("with", spans)

    def test_output_is_valid_escaped_html(self):
        out = bold_important_words(self.EN_Q)
        # no raw ampersands or angle brackets outside our tags
        stripped = _B_TAG_RE.sub("", out)
        self.assertNotIn("<", stripped)
        self.assertGreaterEqual(out.count("<b>"), 1)
        self.assertEqual(out.count("<b>"), out.count("</b>"))


class BoldMultilingualTests(unittest.TestCase):
    def test_hindi_devanagari(self):
        q = "भारतीय संविधान के मौलिक कर्तव्यों का वर्णन संविधान के किस अनुच्छेद में किया गया है?"
        out = bold_important_words(q)
        self.assertEqual(_plain(out), q)
        self.assertGreaterEqual(len(_spans(out)), 1)
        self.assertLessEqual(len(_spans(out)), MAX_BOLD_SPANS)
        spans_cf = " | ".join(_spans(out)).casefold()
        self.assertTrue("संविधान" in spans_cf or "मौलिक" in spans_cf,
                        f"no Hindi keyword bolded: {spans_cf}")

    def test_hinglish_romanised(self):
        q = "Bharat ke Samvidhan ko pesh karne wala aayog kaun tha aur usne kya zimmedari li thi?"
        out = bold_important_words(q)
        self.assertEqual(_plain(out), q)
        self.assertGreaterEqual(len(_spans(out)), 1)
        spans_cf = " | ".join(_spans(out)).casefold()
        self.assertIn("samvidhan", spans_cf)

    def test_mixed_hinglish_english(self):
        q = "Fundamental Rights aur Directive Principles mein kya ant hai? Explain with reference to the Constitution."
        out = bold_important_words(q)
        self.assertEqual(_plain(out), q)
        self.assertGreaterEqual(len(_spans(out)), 2)


class BoldSafetyTests(unittest.TestCase):
    def test_html_chars_escaped(self):
        text = "R&D spending of India and the USA stayed below 2020 targets in year"
        out = bold_important_words(text)
        self.assertEqual(_plain(out), text)
        self.assertIn("&amp;", out)

    def test_angle_bracket_text_displayed_literally(self):
        text = "If the value of x is less than 5 then compare x < 10 in this case study"
        out = bold_important_words(text)
        self.assertEqual(_plain(out), text)
        self.assertIn("&lt;", out)

    def test_existing_markup_passthrough_untouched(self):
        text = "Question with <b>existing bold</b> and <i>markup</i> that must stay"
        self.assertEqual(format_bold_html(text), (text, False))
        self.assertEqual(bold_important_words(text), text)

    def test_existing_entity_passthrough_untouched(self):
        text = "Costs rose 5 &percnt; last year and the central bank intervened"
        self.assertEqual(format_bold_html(text), (text, False))

    def test_short_text_not_bolded_but_escaped(self):
        text = "Short & sweet"
        out = bold_important_words(text)
        self.assertNotIn("<b>", out)
        self.assertEqual(_plain(out), text)

    def test_empty_text(self):
        self.assertEqual(bold_important_words(""), "")
        self.assertEqual(format_bold_html(""), ("", False))

    def test_double_application_is_noop(self):
        once = bold_important_words(
            "The Preamble of the Indian Constitution describes the ideals of the nation state"
        )
        twice = bold_important_words(once)
        self.assertEqual(twice, once)

    def test_no_bold_spans_for_stopword_only_text(self):
        text = "What is this and that which was given below the statement of the"
        out = bold_important_words(text)
        self.assertNotIn("<b>", out)
        self.assertEqual(_plain(out), text)


class BoldBalanceTests(unittest.TestCase):
    def test_span_cap_on_keyword_rich_text(self):
        text = ("Parliament enacted the Constitutional amendment establishing "
                "the Panchayati municipality judiciary framework regarding "
                "federal principles and fundamental obligations of the "
                "satellite photosynthesis monsoon ecosystem")
        out = bold_important_words(text)
        self.assertEqual(_plain(out), text)
        self.assertLessEqual(len(_spans(out)), MAX_BOLD_SPANS)
        bolded = sum(len(s) for s in _spans(out))
        self.assertLessEqual(bolded, MAX_BOLD_RATIO * len(text))

    def test_ratio_cap_never_exceeded_greedily(self):
        text = "Constitutional parliament judiciary fundamental amendment obligations framework established principles"
        out = bold_important_words(text)
        bolded = sum(len(s) for s in _spans(out))
        self.assertLessEqual(bolded, MAX_BOLD_RATIO * len(text))

    def test_same_word_bolded_only_once(self):
        text = ("India signed the treaty; the treaty bound India and the "
                "treaty council together in India")
        out = bold_important_words(text)
        for span in _spans(out):
            self.assertEqual(out.count(f"<b>{span}</b>"), 1)


class PollFieldTests(unittest.TestCase):
    def test_plain_question_and_explanation_get_modes(self):
        q, e, extra = apply_bold_to_poll_fields(
            "Which Article of the Constitution of India covers Fundamental Duties?",
            "The Article enumerates eleven Fundamental Duties for citizens today",
        )
        self.assertEqual(extra.get("question_parse_mode"), "HTML")
        self.assertEqual(extra.get("explanation_parse_mode"), "HTML")
        self.assertIn("<b>", q)
        self.assertIn("<b>", e)
        self.assertEqual(_plain(q),
                         "Which Article of the Constitution of India covers Fundamental Duties?")

    def test_placeholder_question_skipped(self):
        q, e, extra = apply_bold_to_poll_fields(
            "[1/10] Choose the correct option",
            None,
            skip_question=True,
        )
        self.assertEqual(q, "[1/10] Choose the correct option")
        self.assertNotIn("question_parse_mode", extra)

    def test_markup_question_passthrough_no_mode(self):
        original = "Question with <b>authored markup</b> stays exactly as written"
        q, e, extra = apply_bold_to_poll_fields(original, None)
        self.assertEqual(q, original)
        self.assertNotIn("question_parse_mode", extra)

    def test_empty_explanation_untouched(self):
        q, e, extra = apply_bold_to_poll_fields(
            "Which Article of the Constitution of India covers Fundamental Duties?",
            None,
        )
        self.assertIsNone(e)
        self.assertEqual(set(extra), {"question_parse_mode"})

    def test_ptb_send_poll_supports_parse_modes(self):
        from telegram import Bot

        params = inspect.signature(Bot.send_poll).parameters
        self.assertIn("question_parse_mode", params)
        self.assertIn("explanation_parse_mode", params)


class WelcomeTextTests(unittest.TestCase):
    """Requirement 2: /start welcome replaced; buttons/menus unchanged."""

    @classmethod
    def setUpClass(cls):
        from quizbot.runner_bot.handlers import quiz_play

        cls.quiz_play = quiz_play
        cls.welcome = quiz_play._START_WELCOME_TEXT

    def test_contains_required_phrases(self):
        for phrase in (
            "🎯 Welcome to Journey for LBSNAA",
            "🇮🇳 Dreaming of the Civil Services?",
            "📚 Practice UPSC-focused MCQs",
            "🧠 Strengthen your Concepts &amp; Understanding",
            "📝 Attempt Test Series &amp; Practice Tests",
            "📊 Learn from Detailed Explanations",
            "🎙️ Turn your study material into Smart Learning",
            "⚡ Practice. Analyse. Improve. Repeat.",
            "Every question is an opportunity to learn.",
            "Every test takes you one step closer to your goal.",
            "🚀 Journey for LBSNAA",
            "Your preparation. Your journey. Your destination.",
            "👇 Choose an option below and begin your preparation.",
        ):
            self.assertIn(phrase, self.welcome)

    def test_ampersands_are_html_safe(self):
        self.assertIsNone(re.search(r"&(?!amp;|lt;|gt;|#)", self.welcome))

    def test_old_welcome_gone(self):
        self.assertNotIn("Advance Quiz Bot", self.welcome)
        self.assertNotIn("Welcome to <b>Advance Quiz Bot</b>",
                         inspect.getsource(self.quiz_play))

    def test_start_handler_uses_constant_without_new_buttons(self):
        src = inspect.getsource(self.quiz_play.start_quiz)
        self.assertIn("_START_WELCOME_TEXT", src)
        # no buttons/menus added to the bare-/start branch
        self.assertNotIn("InlineKeyboardButton", src.split("if not ctx.args:")[1].split("return")[0])


class WiringTests(unittest.TestCase):
    """Bold formatting is wired into the GROUP quiz senders only."""

    def test_group_sender_applies_bolding(self):
        from quizbot.runner_bot.handlers import quiz_play

        src = inspect.getsource(quiz_play._send_group_question)
        self.assertIn("apply_bold_to_poll_fields", src)
        self.assertIn("format_bold_html", src)

    def test_channel_pollquiz_sender_applies_bolding(self):
        from quizbot.runner_bot.handlers import poll_quiz

        src = inspect.getsource(poll_quiz._pollquiz_send_one)
        self.assertIn("apply_bold_to_poll_fields", src)

    def test_private_question_sender_left_alone(self):
        from quizbot.runner_bot.handlers import quiz_play

        src = inspect.getsource(quiz_play.send_private_question)
        self.assertNotIn("apply_bold_to_poll_fields", src)

    def test_explanation_message_applies_bolding(self):
        from quizbot.runner_bot.handlers import quiz_play

        src = inspect.getsource(quiz_play._send_explanation_after_poll)
        self.assertIn("format_bold_html", src)

    def test_gemini_prompt_untouched(self):
        """The AI question-generation prompts must not mention bolding."""
        import quizbot.runner_bot.ai_providers as providers

        for template in (providers.QUESTION_FORMAT,
                         providers.QUESTION_FORMAT_BILINGUAL):
            self.assertNotIn("bold", template.casefold())
            self.assertNotIn("<b>", template)


if __name__ == "__main__":
    unittest.main()
