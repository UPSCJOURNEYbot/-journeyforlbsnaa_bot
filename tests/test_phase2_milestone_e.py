"""Phase 2 milestone E: Rule-13 semantic differentiation.

The visual must follow the question's interrogative intent, not just
content keywords: the same explanation yields different visuals for
different asks (EN + HI). When the question's intent has no matching
candidate, the best structure visual is kept (no veto); generic
questions keep the static default; fallback types never boost.
Offline and deterministic throughout.
"""

from __future__ import annotations

import unittest

from pdf_service.viz import engine as viz

# One multi-evidence explanation: places + years + numbered steps.
GANGA = ("The Ganga rises at Gangotri in Uttarakhand. In 1857 pilgrims "
         "gathered. In 1919 a flood struck. In 1942 a bridge opened. "
         "The river then flows onward:\n"
         "1. It crosses the plains.\n"
         "2. It joins the Yamuna at Prayagraj.\n"
         "3. It reaches Patna.")

COMPARE = ("Compare solar with wind power.\n"
           "• Solar is daytime only.\n• Wind is variable.\n"
           "• Both are renewable.")

YEARS = "In 1857 revolt. In 1919 massacre. In 1942 movement."

HINDI_GANGA = ("गंगा गंगोत्री से निकलती है। 1857 में जमावड़ा हुआ। "
               "1919 में बाढ़ आई। 1942 में पुल बना।")

NESTED = ("• Part one of the provisions explained here.\n"
          "  • First detail of part one here.\n"
          "  • Second detail of part one here.\n"
          "• Part two of the provisions explained here.\n"
          "  • First detail of part two here.\n"
          "  • Second detail of part two here.\n"
          "• Part three of the provisions explained here.\n"
          "  • First detail of part three here.\n"
          "  • Second detail of part three here.")


def decide(question, explanation=""):
    return viz.safe_decide_visual(question, (), explanation)


class DifferentiationCases(unittest.TestCase):
    def test_same_explanation_four_questions(self):
        cases = {
            "Where does the Ganga rise?": "location_map",
            "Arrange the Ganga events in chronological order.": "timeline",
            "Explain the course of the Ganga in steps.": "flowchart",
            "Trace the route of the Ganga from Gangotri to Patna.":
                "spatial_chain",
        }
        for question, want in cases.items():
            with self.subTest(question=question):
                spec = decide(question, GANGA)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, want)

    def test_cities_ask_gives_regional_map(self):
        spec = decide(
            "Name the cities on the Ganga: Prayagraj, Patna, Varanasi.",
            GANGA)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")

    def test_hindi_same_explanation_two_questions(self):
        where = decide("गंगा कहाँ से निकलती है?", HINDI_GANGA)
        chrono = decide("गंगा की घटनाओं को क्रम में लगाइए।", HINDI_GANGA)
        self.assertIsNotNone(where)
        self.assertIsNotNone(chrono)
        self.assertEqual(where.visual_type, "location_map")
        self.assertEqual(chrono.visual_type, "timeline")

    def test_hindi_order_phrasing_is_chronology(self):
        intents = viz.classify_intent("घटनाओं को क्रम में लगाइए।")
        self.assertIn("chronology", intents)
        intents = viz.classify_intent("घटनाक्रम को सही क्रम में बताइए।")
        self.assertIn("chronology", intents)

    def test_cause_beats_timeline_on_causal_ask(self):
        spec = decide(
            "What are the causes and effects of the 1942 movement?",
            "Causes: unrest spread in 1939.\n"
            "In 1940 protests grew.\n"
            "In 1942 revolt broke out.\n"
            "Effects: reforms followed.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "cause_effect")

    def test_classification_beats_timeline_on_classify_ask(self):
        spec = decide(
            "What are the types of movements in 1857, 1919 and 1942?",
            "Types of movements: revolt, massacre, agitation. "
            "In 1857 revolt. In 1919 massacre. In 1942 movement.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")


class NoVetoCases(unittest.TestCase):
    def test_unmatched_ask_keeps_structure(self):
        # A location ask with no place evidence keeps the comparison
        # the explanation actually supports (no veto).
        spec = decide("Where are solar plants located?", COMPARE)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")

    def test_unmatched_ask_keeps_timeline(self):
        spec = decide("Where did the 1857 revolt start?", YEARS)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "timeline")

    def test_general_question_prefers_evidenced_structure(self):
        # Correction (question-first): a general ask with a single
        # river mention and no spatial requirement earns NO map; the
        # explanation's three dated events still earn a timeline
        # (the 3+ evidence bar is met).
        spec = decide(
            "Tell me about the Ganga.",
            "In 1857 pilgrims gathered. In 1919 a flood struck. "
            "In 1942 a bridge opened.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "timeline")

    def test_fallback_types_never_boost(self):
        # Nested + long: mind and infographic both eligible, features
        # ask, yet the explicit nesting still wins (fallbacks last).
        spec = decide("Revise the provisions of the Act.", NESTED)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "mind_map")


if __name__ == "__main__":
    unittest.main()
