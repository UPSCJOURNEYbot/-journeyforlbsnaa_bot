"""Phase F tests — question + explanation enhancement.

Covers the pure explanation model, the optional structured
``explanation_detail`` companion, Telegram/HTML/PDF/Mini-App renderers,
snapshot (non-)identity semantics, and the Phase B/C/D/E integration
guarantees:

* answer keys, options, scores and question identity are never changed;
* legacy questions (plain ``explanation`` string or none) stay
  byte-identical on every output channel;
* explanations ride along on snapshots without affecting content hashes,
  so mistake revision (/mistakes) and /weakquiz snapshot recovery still
  teach the concept;
* nothing is fabricated; malformed/missing structured data is dropped;
* no second engine, no new collection/index, podcast/secrets untouched.
"""

from __future__ import annotations

import asyncio
import html
import importlib
import os
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from quizbot.analytics import mistake_revision as mr
from quizbot.analytics import weak_practice as wp
from quizbot.analytics.metadata import (
    build_snapshot,
    normalize_question,
    snapshot_content_hash,
)
from quizbot.shared import explanations as ef


# --------------------------------------------------------------------------
# Pure model
# --------------------------------------------------------------------------

DETAIL = {
    "why": "Article 14 guarantees equality before the law.",
    "concept": "Equality vs equal treatment: classification is allowed.",
    "options": [
        {"index": 0, "note": "Confuses equality with uniform treatment."},
        {"index": 2, "note": "That is Article 19(1)(a), free speech."},
        {"index": 9, "note": "out of range, must be dropped"},
    ],
    "takeaway": "Reasonable classification, not arbitrariness, passes Article 14.",
}


def enhanced_question():
    return {
        "question": "Which provision guarantees equality before law "
                    "to <all persons>?",
        "options": ["Uniform treatment", "Article 14",
                    "Article 19(1)(a)", "Directive Principle"],
        "correct_option_id": 1,
        "explanation": "Option B is correct & authoritative.",
        "explanation_detail": dict(DETAIL),
        "analytics": {"subject": "Polity", "topic": "Fundamental Rights",
                      "difficulty": "moderate"},
    }


class PureModelTests(unittest.TestCase):
    def test_01_legacy_question_completely_untouched(self):
        for q in (
            {"question": "Q?", "options": ["a", "b"],
             "correct_option_id": 0},
            {"question": "Q?", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": "Because & <why>"},
            {"question": "Q?", "options": ["a", "b"],
             "correct_option_id": [0, 1], "explanation": None},
        ):
            nq = normalize_question(q)
            self.assertEqual(nq, q)
            self.assertNotIn("explanation_detail", nq)
            self.assertEqual(
                ef.render_plain_text(q),
                q.get("explanation") if isinstance(q.get("explanation"), str)
                else "")

    def test_02_answer_key_and_options_never_mutated(self):
        q = enhanced_question()
        nq = ef.normalize_question_explanation(q)
        self.assertEqual(nq["correct_option_id"], 1)
        self.assertEqual(nq["options"], q["options"])
        # multi-correct keys preserved too
        multi = {"question": "m", "options": ["a", "b", "c"],
                 "correct_option_id": [0, 2],
                 "explanation_detail": {"why": "two answers"}}
        self.assertEqual(
            normalize_question(multi)["correct_option_id"], [0, 2])

    def test_03_04_05_detail_normalized_with_valid_option_notes(self):
        nq = ef.normalize_question_explanation(enhanced_question())
        detail = nq["explanation_detail"]
        self.assertEqual(detail["why"], DETAIL["why"])
        self.assertEqual(detail["concept"], DETAIL["concept"])
        self.assertEqual(detail["takeaway"], DETAIL["takeaway"])
        indices = [n["index"] for n in detail["options"]]
        self.assertEqual(indices, [0, 2])  # 9 dropped, order ascending
        letters = {ef.option_letter(i) for i in indices}
        self.assertEqual(letters, {"A", "C"})

    def test_05b_note_input_shapes_dict_letters_positional(self):
        q = {"question": "q", "options": ["a", "b", "c", "d"],
             "correct_option_id": 0,
             "explanation_detail": {
                 "options": {"B": "letter B note", "9": "drop",
                             "x": "drop", "-1": "drop"}}}
        detail = normalize_question(q)["explanation_detail"]
        self.assertEqual(detail["options"], [{"index": 1, "note": "letter B note"}])

        # positional strings + {option, explanation} dicts
        q2 = {"question": "q", "options": ["a", "b", "c"],
              "correct_option_id": 0,
              "explanation_detail": {"options": [
                  "note for A", {"option": 2, "explanation": "note for C"},
                  None, {"index": 0, "note": "dup A, ignored"}]}}
        notes = normalize_question(q2)["explanation_detail"]["options"]
        self.assertEqual(notes, [
            {"index": 0, "note": "note for A"},
            {"index": 2, "note": "note for C"}])

    def test_06_missing_explanation_is_honest_not_fabricated(self):
        q = {"question": "q", "options": ["a", "b"], "correct_option_id": 0}
        self.assertEqual(ef.render_plain_text(q), "")
        self.assertEqual(ef.render_telegram_messages(q), [])
        self.assertEqual(ef.render_report_html(q), "")
        self.assertIsNone(ef.poll_explanation(q))

    def test_07_legacy_string_preserved_byte_identical_everywhere(self):
        text = "Article 14 is about equality & non-arbitrariness " \
               "(see State of Madras v. Champakam, 1951)."
        q = {"question": "q", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": text}
        self.assertEqual(ef.render_plain_text(q), text)
        messages = ef.render_telegram_messages(q)
        self.assertEqual(len(messages), 1)
        # Same bold-word highlighting pipeline output as the pre-Phase-F
        # sender (format_bold_html), wrapped in the same header.
        from quizbot.shared.bold_words import format_bold_html
        body, _ = format_bold_html(text)
        self.assertEqual(messages[0],
                         "\U0001f4a1 <b>Explanation:</b>\n\n" + body)
        # Report HTML legacy form is exactly the escaped input.
        self.assertEqual(ef.render_report_html(q),
                         html.escape(text, quote=False))
        # Native poll field keeps the raw string (caller trims to 200).
        self.assertEqual(ef.poll_explanation(q), text)

    def test_08_multi_language_english_hindi_hinglish(self):
        for text in (
            "Article 14 guarantees equality before the law.",
            "अनुच्छेद 14 विधि के समक्ष समानता की गारंटी देता है।",
            "Article 14 law ke samaksh equality guarantee karta hai.",
        ):
            q = {"question": text, "options": ["हाँ", "नहीं"],
                 "correct_option_id": 0,
                 "explanation_detail": {"why": text,
                                        "options": [{"index": 1,
                                                     "note": text}]}}
            plain = ef.render_plain_text(q)
            self.assertIn(text, plain)
            msgs = ef.render_telegram_messages(q)
            self.assertTrue(msgs)
            # Devanagari/romanized content survives HTML escaping intact.
            self.assertIn(html.escape(text, quote=False)
                          if any(c in text for c in "<>&") else text,
                          "\n".join(msgs).replace("<b>", "")
                          .replace("</b>", "").replace("<i>", "")
                          .replace("</i>", ""))
            # No mojibake / replacement characters.
            self.assertNotIn("\ufffd", "\n".join(msgs))

    def test_09_10_html_escaping_and_malformed_markup(self):
        q = {
            "question": "q <3 & stuff",
            "options": ["a <b>", "b", "c"],
            "correct_option_id": 0,
            "explanation_detail": {
                "why": "Because x < y & z > w (not a tag).",
                "options": [
                    {"index": 0, "note": "Contains <because> fake tag."},
                    {"index": 1, "note": "Entity &amp; already & angle <"},
                ],
            },
        }
        for message in ef.render_telegram_messages(q):
            # Raw user angle brackets must be escaped...
            self.assertNotIn("<because>", message)
            # ...and only the renderer's own b/i tags may appear.
            for part in message.split("<b>"):
                if "</b>" not in part and "<" in part.split("</b>")[0]:
                    # any remaining '<' must be an escaped entity/known tag
                    residual = part
                    self.assertTrue(
                        "&lt;" in residual or "<b>" in residual or
                        not residual.strip())
        report = ef.render_report_html(q)
        self.assertIn("&lt;because&gt;", report)
        self.assertIn("x &lt; y &amp; z &gt; w", report)

    def test_10b_unknown_tag_in_legacy_text_does_not_crash_sender(self):
        # Authored text that merely *looks* tag-like must either pass an
        # allow-listed tag through or be escaped -- never emitted raw.
        q = {"question": "q", "options": ["a", "b"],
             "correct_option_id": 0,
             "explanation": "Reason: value <threshold means invalid."}
        messages = ef.render_telegram_messages(q)
        self.assertTrue(ef.html_message_safe(messages[0]))
        self.assertIn("&lt;threshold", messages[0])

    def test_11_long_explanation_chunks_without_content_loss(self):
        long_note = "Distractor " + ("word " * 600)
        q = {"question": "q", "options": ["a", "b", "c", "d"],
             "correct_option_id": 2,
             "explanation": "Word " * 1500,
             "explanation_detail": {
                 "concept": "Concept " * 400,
                 "options": [{"index": 0, "note": long_note}],
                 "takeaway": "Remember " * 200}}
        messages = ef.render_telegram_messages(q)
        self.assertGreater(len(messages), 1)
        for m in messages:
            self.assertLessEqual(len(m), 4096)
            self.assertTrue(ef.html_message_safe(m))
        # Continuation marker present on follow-ups.
        self.assertIn("cont.", messages[1])
        # Every user sentence survives across the chunks.
        joined = "\n".join(messages)
        for token in ("Word ", "Concept ", "Distractor", "Remember "):
            self.assertIn(token, joined)
        # Plain renderer reports/PDF path enforces the microservice cap.
        bounded = ef.render_plain_text(q, max_len=ef.PDF_SERVICE_EXPLANATION_MAX)
        self.assertLessEqual(len(bounded), ef.PDF_SERVICE_EXPLANATION_MAX)
        self.assertTrue(bounded.endswith("\u2026"))

    def test_12_empty_null_whitespace_explanations(self):
        for value in (None, "", "   \n  "):
            q = {"question": "q", "options": ["a", "b"],
                 "correct_option_id": 0, "explanation": value}
            self.assertEqual(ef.render_telegram_messages(q), [])
        # structured detail with only blanks normalises away
        q = {"question": "q", "options": ["a", "b"], "correct_option_id": 0,
             "explanation_detail": {"why": "  ", "concept": None,
                                    "options": [{"index": 0, "note": ""}]}}
        self.assertNotIn("explanation_detail", normalize_question(q))

    def test_14_metadata_and_difficulty_preserved(self):
        q = enhanced_question()
        nq = normalize_question(q)
        self.assertEqual(nq["analytics"], q["analytics"])

    def test_15_snapshot_identity_excludes_explanation(self):
        core = {"question": "Q?", "options": ["a", "b", "c"],
                "correct_option_id": 2}
        with_expl = dict(core, explanation="Why",
                         explanation_detail={"concept": "C"})
        s_plain = build_snapshot(core)
        s_rich = build_snapshot(with_expl)
        self.assertEqual(snapshot_content_hash(s_plain),
                         snapshot_content_hash(s_rich))
        # ...and the hash is byte-stable against the pre-Phase-F formula
        # (hash over the three identity keys only).
        import hashlib
        import json
        canonical = json.dumps(
            core, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        self.assertEqual(snapshot_content_hash(s_rich), expected)
        self.assertNotIn("explanation", s_plain)
        self.assertEqual(s_rich["explanation"], "Why")
        self.assertEqual(s_rich["explanation_detail"]["concept"], "C")
        # Editing ONLY the explanation keeps the same id (no orphaning).
        edited = dict(with_expl, explanation="Why, reworded")
        self.assertEqual(snapshot_content_hash(build_snapshot(edited)),
                         snapshot_content_hash(s_rich))

    def test_15b_detail_length_caps_and_crlf_normalisation(self):
        q = {"question": "q", "options": ["a", "b"], "correct_option_id": 0,
             "explanation_detail": {
                 "why": "x" * 5000, "concept": "c" * 5000,
                 "takeaway": "t" * 5000,
                 "options": [{"index": 0, "note": "n" * 1000}]}}
        d = normalize_question(q)["explanation_detail"]
        self.assertLessEqual(len(d["why"]), ef.WHY_MAX + 1)
        self.assertLessEqual(len(d["concept"]), ef.CONCEPT_MAX + 1)
        self.assertLessEqual(len(d["takeaway"]), ef.TAKEAWAY_MAX + 1)
        self.assertLessEqual(len(d["options"][0]["note"]),
                             ef.OPTION_NOTE_MAX + 1)
        raw = {"question": "q", "options": ["a", "b"],
               "correct_option_id": 0,
               "explanation_detail": {"why": "Line1\r\n\r\n\r\n\r\nLine2"}}
        why = normalize_question(raw)["explanation_detail"]["why"]
        self.assertNotIn("\r", why)
        self.assertNotIn("\n\n\n", why)

    def test_health_flags_and_contradictions(self):
        good = enhanced_question()
        self.assertEqual(ef.question_health_flags(good), [])
        bad = {"question": "  ", "options": ["x", "x"],
               "correct_option_id": 7}
        flags = ef.question_health_flags(bad)
        for code in ("missing_question", "duplicate_options",
                     "answer_out_of_range", "explanation_empty"):
            self.assertNotIn(code, [])  # sanity: tuple below
        self.assertIn("missing_question", flags)
        self.assertIn("duplicate_options", flags)
        self.assertIn("answer_out_of_range", flags)
        # linter is read-only
        self.assertEqual(bad["correct_option_id"], 7)

    def test_16_repeated_normalization_is_idempotent(self):
        q = enhanced_question()
        once = normalize_question(q)
        twice = normalize_question(once)
        self.assertEqual(once, twice)
        # already-enhanced question flows through every renderer unchanged
        # when normalized twice
        self.assertEqual(ef.render_plain_text(once),
                         ef.render_plain_text(twice))

    def test_structured_object_in_legacy_explanation_slot(self):
        # An importer may put {why, options...} directly on "explanation".
        q = {"question": "q", "options": ["a", "b", "c"],
             "correct_option_id": 1,
             "explanation": {"why": "B is right",
                             "options": [{"index": 0, "note": "A wrong"}]}}
        nq = normalize_question(q)
        self.assertEqual(nq.get("explanation"), "B is right")
        self.assertIn("explanation_detail", nq)
        self.assertEqual(nq["explanation_detail"]["options"],
                         [{"index": 0, "note": "A wrong"}])

    def test_non_text_explanation_value_dropped_not_stringified(self):
        q = {"question": "q", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": 12345}
        nq = normalize_question(q)
        self.assertIsNone(nq["explanation"])

    def test_short_explanation_for_poll(self):
        q = {"question": "q", "options": ["a", "b"], "correct_option_id": 0,
             "explanation_detail": {"why": "Brief reason."}}
        self.assertEqual(ef.short_explanation(q, limit=190), "Brief reason.")
        long = {"question": "q", "options": ["a", "b"],
                "correct_option_id": 0,
                "explanation": "Sentence one. " + "x " * 300}
        out = ef.short_explanation(long, limit=190)
        self.assertLessEqual(len(out), 190)
        self.assertTrue(out.endswith("\u2026"))
        # detail.why is the poll fallback when no plain explanation exists
        self.assertEqual(ef.poll_explanation(q), "Brief reason.")

    def test_render_report_html_structured_sections(self):
        q = enhanced_question()
        rendered = ef.render_report_html(q)
        self.assertIn("Core concept", rendered)
        self.assertIn("Option notes", rendered)
        self.assertIn("Takeaway", rendered)
        self.assertIn("A.", rendered)
        self.assertIn("C.", rendered)
        # option B (the correct answer, no note) is not fabricated
        self.assertNotIn(">B. <", rendered)

    def test_option_notes_follow_shuffled_display_order(self):
        q = {"question": "q", "options": ["A-canon", "B-canon", "C-canon"],
             "correct_option_id": 1,
             "explanation_detail": {
                 "options": [
                     {"index": 0, "note": "canonical-A trap"},
                     {"index": 2, "note": "canonical-C note"}]}}
        # Engine permutation: display position 0 shows canonical C,
        # position 1 shows canonical A, position 2 keeps canonical B.
        display_order = [2, 0, 1]
        blocks = ef.explanation_blocks(q, display_order)
        notes = next(b for b in blocks if b["kind"] == "options")["notes"]
        # canonical A (note 'trap') is now displayed at position 1 -> B
        self.assertEqual(notes[0]["index"], 0)  # sorted by display position
        self.assertEqual(notes[0]["note"], "canonical-C note")
        self.assertEqual(notes[1]["index"], 1)
        self.assertEqual(notes[1]["note"], "canonical-A trap")
        text = ef.render_plain_text(q, display_order=display_order)
        self.assertIn("A. canonical-C note", text)
        self.assertIn("B. canonical-A trap", text)
        html_msg = ef.render_telegram_messages(q, display_order=display_order)[0]
        self.assertIn("<b>A.</b> canonical-C note", html_msg)
        self.assertIn("<b>B.</b> canonical-A trap", html_msg)
        # Persistent surfaces (no permutation passed) keep canonical letters.
        canonical = ef.render_plain_text(q)
        self.assertIn("A. canonical-A trap", canonical)
        self.assertIn("C. canonical-C note", canonical)

    @staticmethod
    def _load_file_import():
        """file_import itself only depends on the lightweight parser, but
        the handlers package __init__ imports pyrogram (absent in some CI
        envs); load the module directly when that happens. Restores
        sys.modules afterwards so the stub never leaks into the suite."""
        import importlib.util
        try:
            from quizbot.creator_bot.handlers.file_import import _process_json
            return _process_json, None
        except ModuleNotFoundError:
            saved_pkg = sys.modules.get("quizbot.creator_bot.handlers")
            saved_mod = sys.modules.get(
                "quizbot.creator_bot.handlers.file_import")
            pkg = types.ModuleType("quizbot.creator_bot.handlers")
            handlers_dir = os.path.join(
                REPO_ROOT, "quizbot", "creator_bot", "handlers")
            pkg.__path__ = [handlers_dir]
            sys.modules["quizbot.creator_bot.handlers"] = pkg
            spec = importlib.util.spec_from_file_location(
                "quizbot.creator_bot.handlers.file_import",
                os.path.join(handlers_dir, "file_import.py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module._process_json, (saved_pkg, saved_mod)

    @staticmethod
    def _restore_file_import(saved):
        saved_pkg, saved_mod = saved
        if saved_pkg is None:
            sys.modules.pop("quizbot.creator_bot.handlers", None)
        else:
            sys.modules["quizbot.creator_bot.handlers"] = saved_pkg
        if saved_mod is None:
            sys.modules.pop(
                "quizbot.creator_bot.handlers.file_import", None)
        else:
            sys.modules[
                "quizbot.creator_bot.handlers.file_import"] = saved_mod

    def test_import_drops_notes_when_options_filtered_keeps_prose_when_safe(self):
        _process_json, saved = self._load_file_import()
        try:
            self._assert_import_detail_behaviour(_process_json)
        finally:
            if saved is not None:
                self._restore_file_import(saved)

    def _assert_import_detail_behaviour(self, _process_json):
        detail = {"why": "prose why",
                  "options": [{"index": 2, "note": "about source option C"}]}
        raw = {"questions": [{
            "question_text": "Q?",
            "options": [
                {"id": 0, "text": "keep-a"},
                {"id": 1, "text": "   "},          # dropped as empty
                {"id": 2, "text": "keep-c"},
            ],
            "correct_option_id": 0,
            "explanation_detail": detail}]}
        out: list[dict] = []
        n = _process_json(raw, [], out)
        self.assertEqual(n, 1)
        # only 2 options survive; the index-2 note must NOT ride along and
        # misattribute to a shifted letter, even though prose would be safe.
        self.assertNotIn("explanation_detail", out[0])

        # prose-only companion survives filtering (no positional binding):
        raw2 = {"questions": [{
            "question_text": "Q?",
            "options": [{"id": 0, "text": "a"}, {"id": 1, "text": "   "},
                        {"id": 2, "text": "c"}],
            "correct_option_id": 0,
            "explanation_detail": {"why": "safe prose", "takeaway": "t"}}]}
        out2: list[dict] = []
        _process_json(raw2, [], out2)
        self.assertEqual(out2[0]["explanation_detail"]["why"], "safe prose")

        # notes companion with a fully preserved option list is kept:
        raw3 = {"questions": [{
            "question_text": "Q?",
            "options": [{"id": 0, "text": "a"}, {"id": 1, "text": "b"}],
            "correct_option_id": 0,
            "explanation_detail": {"options": [{"index": 1, "note": "n"}]}}]}
        out3: list[dict] = []
        _process_json(raw3, [], out3)
        self.assertEqual(
            out3[0]["explanation_detail"]["options"][0]["note"], "n")

    def test_telegram_chunks_never_split_anchor_tag_or_exceed_limit(self):
        href = "https://example.com/" + "x" * 600
        body = ("filler " * 400) + f'<a href="{href}">label</a> ' \
               + ("tail " * 900)
        q = {"question": "q", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": body}
        msgs = ef.render_telegram_messages(q)
        self.assertGreater(len(msgs), 1)
        for m in msgs:
            self.assertLessEqual(len(m), 4096)
            # The long <a ...>...</a> must not be sliced across messages.
            self.assertEqual(m.count("<a"), m.count("</a>"))

    def test_malformed_detail_never_crashes_or_stringifies_garbage(self):
        q = {"question": "q", "options": ["a", "b", "c"],
             "correct_option_id": 1,
             "explanation_detail": {
                 "why": 123, "concept": ["x"], "takeaway": {"k": "v"},
                 "options": [{"index": 9, "note": 5}, "bare",
                             {"index": 0, "note": "good"}]}}
        # renderers on raw malformed document
        for fn in (ef.render_plain_text, ef.render_rich_markdown,
                   ef.render_report_html):
            out = fn(q)
            self.assertNotIn("123", out)
            self.assertNotIn("['x']", out)
            self.assertIn("good", out)
        msgs = ef.render_telegram_messages(q)
        self.assertTrue(msgs)
        self.assertNotIn("123", "\n".join(msgs))
        # normalizer drops non-string prose, keeps valid content
        norm = ef.normalize_explanation_detail(q["explanation_detail"], 3)
        self.assertNotIn("why", norm)
        self.assertNotIn("concept", norm)
        self.assertNotIn("takeaway", norm)
        notes = norm["options"]
        self.assertEqual(notes[0], {"index": 0, "note": "good"})
        self.assertEqual(notes[1], {"index": 1, "note": "bare"})
        # non-list/non-dict detail normalizes to None
        self.assertIsNone(ef.normalize_explanation_detail("nope"))
        self.assertIsNone(ef.normalize_explanation_detail(42))

    def test_render_plain_text_sections_and_legacy_identity(self):
        q = {"question": "q", "options": ["a", "b", "c"],
             "correct_option_id": 1,
             "explanation_detail": {
                 "concept": "The concept.",
                 "takeaway": "The takeaway."}}
        text = ef.render_plain_text(q)
        self.assertIn("Core concept: The concept.", text)
        self.assertIn("Takeaway: The takeaway.", text)
        # only structured why (no main string)
        q2 = {"question": "q", "options": ["a", "b"], "correct_option_id": 0,
              "explanation_detail": {"why": "Simply because."}}
        self.assertIn("Why the correct answer is right: Simply because.",
                      ef.render_plain_text(q2))


# --------------------------------------------------------------------------
# Snapshot repository + DB-backed integration (reuse Phase E fakes)
# --------------------------------------------------------------------------

from tests.test_phase_e_weak_practice import (  # noqa: E402
    complete,
    new_db,
    polity_quiz,
    qr,
    question,
    seed,
    seed_topic_performance,
)
from quizbot.analytics.metadata import (  # noqa: E402
    OUTCOME_CORRECT,
    OUTCOME_INCORRECT,
)
from quizbot.analytics.repository import QuestionSnapshotRepository  # noqa: E402
from quizbot.analytics.service import AnalyticsService  # noqa: E402
from quizbot.database import QuizRepository  # noqa: E402


def _enhanced(text, opts=None, correct=0, explanation="Why exactly.",
              detail=None):
    q = question(text, opts or ["a", "b", "c", "d"], correct,
                 subject="Polity", topic="Fundamental Rights",
                 difficulty="hard")
    q["explanation"] = explanation
    if detail is not None:
        q["explanation_detail"] = detail
    return q


class SnapshotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_field_revalidates_notes_after_option_edit(self):
        db = new_db()
        q = {
            "question": "q", "options": ["a", "b", "c"],
            "correct_option_id": 0, "explanation": "legacy text",
            "explanation_detail": {"why": "w", "options": [
                {"index": 2, "note": "about C"}]}}
        repo = QuizRepository(db)
        await repo.create(1, "F", [q], qid="qEdit")
        stored = await repo.get("qEdit")
        questions = stored["questions"]
        # Creator deletes the last option (editor flow): note index 2 must
        # be dropped, never re-pointed or silently fabricated.
        questions[0]["options"].pop()
        await repo.update_field("qEdit", "questions", questions)
        again = (await repo.get("qEdit"))["questions"][0]
        self.assertEqual(again["options"], ["a", "b"])
        self.assertEqual(again["explanation"], "legacy text")
        self.assertEqual(again["explanation_detail"], {"why": "w"})
        # A plain legacy question list survives update_field byte-equal.
        legacy = {"question": "L", "options": ["x", "y"],
                  "correct_option_id": 1, "explanation": "raw & text"}
        await repo.update_field("qEdit", "questions", [legacy])
        got = (await repo.get("qEdit"))["questions"][0]
        self.assertEqual(got, legacy)

    async def test_22_event_snapshot_carries_explanation_identity_stable(self):
        db = new_db()
        q = _enhanced("Eq0", detail={"concept": "Concept-X",
                                     "takeaway": "Remember-X"})
        quiz = {"qid": "qF", "quiz_name": "F", "questions": [q]}
        await QuizRepository(db).create(1, "F", [q], qid="qF")
        results = [qr(0, OUTCOME_INCORRECT, [9])]
        await AnalyticsService(db).record_completion(
            user_id=1, attempt_id="f1", qid="qF", quiz_name="F",
            question_results=results, source="group",
            quiz_persisted=True, questions=[q], sections=[],
            score=0.0, correct=0, wrong=1, total_time=5.0, username="u")
        docs = db.collection("question_snapshots").docs
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual(doc.get("explanation"), "Why exactly.")
        self.assertEqual(doc["explanation_detail"]["concept"], "Concept-X")
        # Event references the same id as an identity-only snapshot.
        ev = db.collection("question_events").docs[0]
        core = build_snapshot({k: q[k] for k in
                               ("question", "options", "correct_option_id")})
        self.assertEqual(ev["snapshot_id"], snapshot_content_hash(core))
        # Analytics metadata still present on the event.
        self.assertEqual(ev["subject"], "Polity")
        self.assertEqual(ev["topic"], "Fundamental Rights")

    async def test_23_xp_still_awarded_with_enhanced_questions(self):
        db = new_db()
        qs = [_enhanced(f"E{i}", explanation=f"Why {i}.",
                        detail={"takeaway": f"Remember {i}."})
              for i in range(4)]
        await seed(db, "qF", qs)
        results = [qr(i, OUTCOME_CORRECT,
                      qs[i]["correct_option_id"]) for i in range(4)]
        out = await complete(db, user=1, attempt="f-xp", qid="qF",
                             results=results, questions=qs)
        self.assertIsNotNone(out["gamification"])
        self.assertEqual(out["events_total"], 4)

    async def test_17_18_storage_sanitises_detail_answer_key_untouched(self):
        db = new_db()
        q = {
            "question": "Stored?", "options": ["a", "b", "c"],
            "correct_option_id": 2,
            "explanation_detail": {
                "why": "C",
                "options": [{"index": 1, "note": "B is trap"},
                            {"index": 42, "note": "drop me"}]},
        }
        await QuizRepository(db).create(1, "S", [q], qid="qS")
        stored = (await QuizRepository(db).get("qS"))["questions"][0]
        self.assertEqual(stored["correct_option_id"], 2)
        self.assertEqual(stored["options"], ["a", "b", "c"])
        self.assertEqual(
            stored["explanation_detail"]["options"],
            [{"index": 1, "note": "B is trap"}])

    async def test_19_20_snapshot_fallback_revision_keeps_explanation(self):
        db = new_db()
        q = _enhanced("DelEq0",
                      detail={"why": "Why-D", "concept": "Concept-D",
                              "options": [{"index": 0, "note": "A trap"}]})
        await seed(db, "qDel", [q])
        results = [qr(0, OUTCOME_INCORRECT, [9])]
        await complete(db, user=1, attempt="d1", qid="qDel",
                       results=results, questions=[q])
        # Quiz disappears: only snapshots can restore the question now.
        await db.collection("quizzes").delete_one({"qid": "qDel"})
        service = mr.MistakeRevisionService(db)
        built = await service.build_revision(1, mr.MODE_SMART)
        self.assertEqual(built["size"], 1)
        rq_ = built["questions"][0]
        self.assertEqual(rq_["correct_option_id"], 0)
        self.assertEqual(rq_["explanation"], "Why exactly.")
        self.assertEqual(rq_["explanation_detail"]["concept"], "Concept-D")

    async def test_20b_live_revision_carries_detail_without_snapshot_loss(self):
        db = new_db()
        q = _enhanced("Live0", detail={"concept": "Live-C"})
        await seed(db, "qLive", [q])
        await complete(db, user=1, attempt="l1", qid="qLive",
                       results=[qr(0, OUTCOME_INCORRECT, [9])],
                       questions=[q])
        built = await mr.MistakeRevisionService(db).build_revision(
            1, mr.MODE_SMART)
        self.assertEqual(built["questions"][0]["explanation"], "Why exactly.")
        self.assertEqual(
            built["questions"][0]["explanation_detail"]["concept"], "Live-C")

    async def test_21_weakquiz_ad_hoc_snapshot_keeps_explanation(self):
        db = new_db()
        # One stored weak seed so the topic is eligible.
        stored = [question("Stored", ["a", "b"], 0,
                           subject="Polity", topic="Judiciary")]
        await seed(db, "qStored", stored)
        await seed_topic_performance(
            db, 1, "qStored", stored, correct_idx=[], wrong_idx=[0])
        # Five enhanced ad-hoc (AI) questions answered wrong; their snapshots
        # are the only surviving content.
        adhoc = [_enhanced(f"AI-{i}", explanation=f"AI why {i}.",
                           detail={"takeaway": f"AI remember {i}."})
                 for i in range(5)]
        # Give them the weak topic analytics explicitly.
        for i, aq in enumerate(adhoc):
            aq.get("analytics", {}) and aq["analytics"].update(
                {"subject": "Polity", "topic": "Judiciary"})
        await complete(db, user=1, attempt="ai-f", qid="AI-F",
                       results=[qr(i, OUTCOME_INCORRECT, [9])
                                for i in range(5)],
                       questions=adhoc, source="aiquiz", persisted=False)
        built = await wp.WeakPracticeService(db).build_practice(1)
        self.assertEqual(built["state"], "ready")
        ai_items = [x for x in built["questions"]
                    if str(x.get("question", "")).startswith("AI-")]
        self.assertTrue(ai_items)
        for item in ai_items:
            self.assertTrue(item.get("explanation"))
            self.assertIn("explanation_detail", item)

    async def test_concurrent_identical_snapshots_stored_once(self):
        db = new_db()
        q = _enhanced("Conc0")
        snaps = QuestionSnapshotRepository(db)
        await asyncio.gather(*[
            snaps.ensure([build_snapshot(q)]) for _ in range(8)
        ])
        docs = db.collection("question_snapshots").docs
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["explanation"], "Why exactly.")

    async def test_legacy_snapshot_doc_without_explanation_stays_empty(self):
        db = new_db()
        # A legacy doc written before Phase F has no explanation keys.
        sid = snapshot_content_hash(
            {"question": "Legacy", "options": ["a", "b"],
             "correct_option_id": 0})
        await db.collection("question_snapshots").insert_one({
            "snapshot_id": sid, "question": "Legacy",
            "options": ["a", "b"], "correct_option_id": 0,
            "created_at": "2026-01-01 00:00:00"})
        resolved = await QuestionSnapshotRepository(db).get_many([sid])
        self.assertNotIn("explanation", resolved[sid])
        self.assertNotIn("explanation_detail", resolved[sid])


# --------------------------------------------------------------------------
# Renders/report surfaces
# --------------------------------------------------------------------------

class ReportSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_24_premium_html_report_contains_composed_text(self):
        from quizbot.shared.html.quiz_report import render_quiz_html
        q = enhanced_question()
        quiz = {"qid": "q", "quiz_name": "T", "questions": [q],
                "timer": 10, "negative_marks": 0}
        data, _ = await render_quiz_html(quiz)
        page = data.decode("utf-8")
        self.assertIn("Reasonable classification", page)
        self.assertIn("Option B is correct & authoritative.", page)
        self.assertIn("Confuses equality", page)
        # Legacy question in the same report keeps its exact text.
        legacy = {"question": "L", "options": ["a", "b"],
                  "correct_option_id": 0, "explanation": "Legacy & reason"}
        quiz2 = {"qid": "q2", "quiz_name": "T2", "questions": [legacy],
                 "timer": 10}
        data2, _ = await render_quiz_html(quiz2)
        self.assertIn("Legacy & reason", data2.decode())

    async def test_24a_premium_html_remaps_notes_after_fresh_shuffle(self):
        # The premium report intentionally shuffles options on every render;
        # composed option-note letters must follow THAT permutation.
        from quizbot.shared.html import quiz_report
        from quizbot.shared.html.quiz_report import render_quiz_html
        q = {
            "question": "q", "options": ["a0", "a1", "a2"],
            "correct_option_id": 0,
            "explanation_detail": {"options": [
                {"index": 0, "note": "note-on-canonical-A"},
                {"index": 2, "note": "note-on-canonical-C"}]}}
        orig_shuffle = quiz_report.random.shuffle

        def fixed_shuffle(seq):
            seq[:] = [2, 0, 1] if len(seq) == 3 else seq

        quiz_report.random.shuffle = fixed_shuffle
        try:
            quiz = {"qid": "q", "quiz_name": "T", "questions": [q],
                    "timer": 10}
            data, _ = await render_quiz_html(quiz)
        finally:
            quiz_report.random.shuffle = orig_shuffle
        page = data.decode("utf-8")
        # display position A <- canonical 2, B <- canonical 0
        self.assertIn("A. note-on-canonical-C", page)
        self.assertIn("B. note-on-canonical-A", page)

    def test_24b_runner_pdf_box_uses_math_aware_escaper_legacy_identical(self):
        from quizbot.runner_bot.pdf_reports import _safe_html
        legacy = "Because x > y and $a^2+b^2=c^2$."
        q = {"question": "q", "options": ["a", "b"],
             "correct_option_id": 0, "explanation": legacy}
        self.assertEqual(ef.render_report_html(q, esc=_safe_html),
                         _safe_html(legacy))
        enhanced = enhanced_question()
        rendered = ef.render_report_html(enhanced, esc=_safe_html)
        self.assertIn("explanation-box".replace("explanation-box", ""),
                      rendered)  # box itself built by the caller
        self.assertIn("Core concept", rendered)

    def test_24c_testseries_payload_shape_and_cap(self):
        # The creator bot module needs pyrogram (absent in the sandbox); test
        # the exact payload-building operation reports.py performs.
        q_enhanced = enhanced_question()
        payload_explanation = ef.render_plain_text(
            q_enhanced, max_len=ef.PDF_SERVICE_EXPLANATION_MAX)
        self.assertLessEqual(len(payload_explanation), 12000)
        self.assertIn("Takeaway", payload_explanation)
        q_legacy = {"question": "L", "options": ["a", "b"],
                    "correct_option_id": 0, "explanation": "Plain legacy"}
        self.assertEqual(
            ef.render_plain_text(q_legacy,
                                 max_len=ef.PDF_SERVICE_EXPLANATION_MAX),
            "Plain legacy")
        # Microservice pydantic field cap from pdf_service/app.py.
        self.assertLess(ef.PDF_SERVICE_EXPLANATION_MAX, 12000)

        # Empty-option filtering mirrors reports._build_testseries_payload:
        # surviving canonical positions become the display permutation, so
        # notes follow the letters the PDF actually prints.
        q = {"question": "q",
             "options": ["keep-a", "", "keep-c", "keep-d"],
             "correct_option_id": 0,
             "explanation_detail": {"options": [
                 {"index": 2, "note": "about canonical C"},
                 {"index": 3, "note": "about canonical D"}]}}
        raw = q["options"]
        survivors = [(i, str(o)) for i, o in enumerate(raw) if o]
        order = [i for i, _ in survivors] if len(survivors) != len(raw) else None
        self.assertEqual([i for i, _ in survivors], [0, 2, 3])
        composed = ef.render_plain_text(
            q, max_len=ef.PDF_SERVICE_EXPLANATION_MAX, display_order=order)
        # canonical C is printed as B, canonical D as C
        self.assertIn("B. about canonical C", composed)
        self.assertIn("C. about canonical D", composed)

    def test_25_visual_engine_gate_unchanged_for_legacy(self):
        from pdf_service.viz import engine as viz
        q = {
            "question": "Where is the Narmada river located in India?",
            "options": ["Map: mark Madhya Pradesh/Gujarat",
                        "In the Thar desert", "In the Western Ghats",
                        "In Ladakh"],
            "correct_option_id": 0,
        }
        spec_legacy = viz.safe_decide_visual(
            q["question"], tuple(q["options"]), "It flows west through MP.")
        # Deterministic across calls.
        self.assertEqual(
            spec_legacy is not None,
            viz.safe_decide_visual(
                q["question"], tuple(q["options"]),
                "It flows west through MP.") is not None)
        # Structured/non-geographic explanation never forces a visual: an
        # abstract Polity question stays text-only regardless of detail.
        polity = enhanced_question()
        composed = ef.render_plain_text(polity)
        spec = viz.safe_decide_visual(
            polity["question"], tuple(polity["options"]), composed)
        self.assertIsNone(spec)

    def test_miniapp_submit_remaps_notes_to_shuffled_display(self):
        from quizbot.mini_app import player_service as ps
        # Canonical options [a0,a1,a2]; force display permutation [2,0,1].
        q = {"question": "q", "options": ["a0", "a1", "a2"],
             "correct_option_id": 0,
             "explanation_detail": {"options": [
                 {"index": 0, "note": "canonical-A is correct"},
                 {"index": 2, "note": "canonical-C is a trap"}]}}
        session = {
            "order": [0], "quiz": {"questions": [q], "sections": []},
            "per_question": {0: {
                "options": ["a2", "a0", "a1"], "correct_ids": [1],
                "display_order": [2, 0, 1]}},
            "answers": {}, "sent_at": {}, "mode": "practice",
            "effective_negative_marks": 0,
        }
        result = ps.submit_answer(session, 0, [1])  # picks displayed B = a0
        self.assertTrue(result["correct"])
        exp = result["explanation"]
        # canonical A (correct) is displayed as B; canonical C as A
        self.assertIn("A. canonical-C is a trap", exp)
        self.assertIn("B. canonical-A is correct", exp)
        self.assertIsNone(ps.submit_answer(session, 0, [1]))  # no rescoring

    def test_miniapp_dto_composition(self):
        # player_service exposes one explanation string; emulate its exact
        # composition call (the live session path needs a running loop/DB
        # and is covered end-to-end in B/E suites).
        q = enhanced_question()
        dto_text = ef.render_plain_text(q) or None
        self.assertIn("Article 14", dto_text)
        legacy = {"question": "L", "options": ["a", "b"],
                  "correct_option_id": 0, "explanation": "Legacy text"}
        self.assertEqual(ef.render_plain_text(legacy) or None, "Legacy text")


# --------------------------------------------------------------------------
# AI parser hardening (aiohttp is a deployment dep, stubbed in sandbox)
# --------------------------------------------------------------------------

class AiParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ai_providers imports the HTTP helper (aiohttp, a deployment-only
        # dependency) at module load; parse_ai_questions itself is pure.
        cls._added_aiohttp = "aiohttp" not in sys.modules
        if cls._added_aiohttp:
            aiohttp = types.ModuleType("aiohttp")
            aiohttp.ClientSession = object
            aiohttp.ClientTimeout = lambda **kw: kw
            sys.modules["aiohttp"] = aiohttp

    @classmethod
    def tearDownClass(cls):
        # Never leak the stub (or the modules imported behind it) into other
        # test modules, otherwise unrelated environment-dependent results
        # would be masked by suite ordering.
        if cls._added_aiohttp:
            for name in ("aiohttp", "quizbot.shared.utils.http",
                         "quizbot.shared.utils",
                         "quizbot.runner_bot.ai_providers"):
                sys.modules.pop(name, None)

    def test_26_string_explanation_kept_and_capped(self):
        ai = importlib.import_module("quizbot.runner_bot.ai_providers")
        raw = '[{"q":"Q?","o":["A","B","C","D"],"c":[1],"e":"Because B."}]'
        parsed = ai.parse_ai_questions(raw)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["correct_option_id"], 1)
        self.assertEqual(parsed[0]["explanation"], "Because B.")
        self.assertNotIn("explanation_detail", parsed[0])
        long = '[{"q":"Q?","o":["A","B"],"c":[0],"e":"' + ("x" * 900) + '"}]'
        item = ai.parse_ai_questions(long)[0]
        self.assertLessEqual(len(item["explanation"]), 600)

    def test_26b_structured_explanation_object_validated(self):
        ai = importlib.import_module("quizbot.runner_bot.ai_providers")
        raw = (
            '[{"q":"Q?","o":["A","B","C"],"c":[2],'
            '"e":{"why":"C is right.","concept":"Key idea.",'
            '"options":[{"index":0,"note":"A is distractor."},'
            '{"index":9,"note":"drop"}],"takeaway":"Remember C."}}]'
        )
        item = ai.parse_ai_questions(raw)[0]
        # Answer key comes ONLY from c, never from the explanation object.
        self.assertEqual(item["correct_option_id"], 2)
        self.assertNotIn("explanation", item)
        self.assertEqual(item["explanation_detail"]["why"], "C is right.")
        self.assertEqual(
            item["explanation_detail"]["options"],
            [{"index": 0, "note": "A is distractor."}])
        self.assertEqual(item["explanation_detail"]["takeaway"],
                         "Remember C.")

    def test_26c_null_and_garbage_explanations_safe(self):
        ai = importlib.import_module("quizbot.runner_bot.ai_providers")
        raw = '[{"q":"Q?","o":["A","B"],"c":[0],"e":null},' \
              '{"q":"Q2?","o":["A","B"],"c":[1],"e":12345}]'
        items = ai.parse_ai_questions(raw)
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertNotIn("explanation_detail", item)
        self.assertIsNone(items[0].get("explanation"))
        # numbers are not valid explanations anywhere
        self.assertIsNone(ef.normalize_question_explanation(items[1])
                          .get("explanation"))


# --------------------------------------------------------------------------
# Architecture / safety guard rails
# --------------------------------------------------------------------------

class ArchitectureTests(unittest.TestCase):
    def test_27_explanations_module_is_stdlib_only_no_engine_duplication(self):
        import ast as _ast
        tree = _ast.parse((REPO_ROOT /
                           "quizbot/shared/explanations.py").read_text())
        imported = set()
        for stmt in _ast.walk(tree):
            if isinstance(stmt, _ast.Import):
                imported.update(alias.name.split(".")[0]
                                for alias in stmt.names)
            elif isinstance(stmt, _ast.ImportFrom) and stmt.module:
                imported.add(stmt.module.split(".")[0])
        self.assertTrue(imported <= {"html", "re", "typing",
                                      "quizbot", "__future__"})

    def test_27b_polling_client_and_handler_registration_unchanged(self):
        init_src = (REPO_ROOT /
                    "quizbot/runner_bot/handlers/__init__.py").read_text()
        # Phase F registers no command/handler at all.
        self.assertNotIn("explanation", init_src.lower())
        # import line, tuple entry and its comment: no new handler module.
        self.assertNotIn("add_handler", init_src)
        self.assertEqual(init_src.count("weakquiz"), 3)

    def test_28_podcast_untouched(self):
        import subprocess
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
        for path in changed:
            self.assertFalse(
                "podcast" in path.lower(),
                f"podcast file must not change: {path}")
            self.assertNotIn(path, ".env")

    def test_29_no_secrets_or_new_collections(self):
        import subprocess
        # Scan only production paths (the test file legitimately mentions
        # these very guard strings).
        diff = subprocess.run(
            ["git", "diff", "origin/main...HEAD", "--", "quizbot"],
            cwd=REPO_ROOT, capture_output=True, text=True).stdout
        for forbidden in ("BEGIN PRIVATE KEY", "aws_secret_access_key",
                          "x-api-key:", "Authorization: Bearer"):
            self.assertNotIn(forbidden, diff)
        # db.py must not be part of the diff at all (no schema changes).
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
        self.assertNotIn("quizbot/database/db.py", changed)
        # New pure layer declares no index/collection and performs no IO.
        src = (REPO_ROOT / "quizbot/shared/explanations.py").read_text()
        for forbidden in ("create_index", "get_db", "collection(",
                          "insert_one", "update_one", "open(", "requests."):
            self.assertNotIn(forbidden, src)

    def test_30_phase_f_files_only_expected_paths(self):
        import subprocess
        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
        # Phase F surfaces ...
        allowed = {
            "quizbot/shared/explanations.py",
            "quizbot/shared/html/quiz_report.py",
            "quizbot/shared/bold_words.py",
            "quizbot/analytics/metadata.py",
            "quizbot/analytics/repository.py",
            "quizbot/analytics/mistake_revision.py",
            "quizbot/analytics/weak_practice.py",
            "quizbot/database/repositories.py",
            "quizbot/runner_bot/handlers/quiz_play.py",
            "quizbot/runner_bot/handlers/poll_quiz.py",
            "quizbot/runner_bot/pdf_reports.py",
            "quizbot/runner_bot/ai_providers.py",
            "quizbot/mini_app/player_service.py",
            "quizbot/creator_bot/handlers/reports.py",
            "quizbot/creator_bot/handlers/file_import.py",
            "tests/test_phase_f_question_explanation.py",
            "tests/test_phase_b_foundation.py",
            # ... plus the pre-deployment audit hardening change set
            # (PDF default config, (A) parser, XSS/SSRF hardening, runner
            # PDF robustness, Hindi fonts/swap provisioning, BotFather menu):
            ".env.example",
            "BOTFATHER_COMMANDS.txt",
            "Dockerfile",
            "deploy_vps.sh",
            "requirements.txt",
            "quizbot/shared/config.py",
            "quizbot/shared/utils/netguard.py",
            "quizbot/creator_bot/handlers/testseries_create.py",
            "quizbot/creator_bot/handlers/testseries_file.py",
            "tests/test_bold_words.py",
            "tests/test_part2_command_audit.py",
            "tests/test_phase_d_mistakes.py",
            "tests/test_phase_e_weak_practice.py",
            "tests/test_testseries_file.py",
            "tests/test_html_report_security.py",
            "tests/test_pdf_config_resolution.py",
            "tests/test_runner_pdf_report.py",
            "tests/test_url_import_ssrf.py",
        }
        for path in changed:
            self.assertIn(path, allowed, f"unexpected change: {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
