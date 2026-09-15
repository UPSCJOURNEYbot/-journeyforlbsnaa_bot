"""Correction: question requirement first, never data/keyword first.

QUESTION REQUIREMENT > VISUAL CAPABILITY > AVAILABLE DATA. A place
mention, river name, or content word alone must never force a visual:
maps need a spatial requirement (location/route/relation ask), a
geo-framed set enumeration, or 3+ content places; structure visuals
need their documented content evidence. NO_VISUAL is first-class.

Each rationale case records the full chain: QUESTION -> WHY A VISUAL
IS (NOT) NEEDED -> WHY THIS TYPE -> SUPPORTING EVIDENCE -> WHAT IS
DISPLAYED. Offline and deterministic throughout.
"""

from __future__ import annotations

import unittest

from pdf_service.viz import engine as viz


def decide(question, explanation=""):
    return viz.safe_decide_visual(question, (), explanation)


class FalsePositiveCases(unittest.TestCase):
    """A keyword alone must not force a visual."""

    def test_which_statement_about_indus_no_map(self):
        # Factual recall about one river: no spatial requirement, so
        # no map -- and the sourced Indus geometry must NOT draw
        # merely because the river was mentioned.
        spec = decide(
            "Which of the following statements about the Indus river "
            "system is correct?",
            "The Indus rises in Tibet and flows through Ladakh into "
            "Pakistan.")
        self.assertIsNone(spec)

    def test_which_statement_about_mahanadi_no_map(self):
        spec = decide(
            "Which statement about the Mahanadi river is correct?",
            "The Mahanadi rises in Chhattisgarh and flows through "
            "Odisha.")
        self.assertIsNone(spec)

    def test_single_africa_mention_no_map(self):
        spec = decide("Tell me about Africa.",
                      "Africa is a large continent with many countries.")
        self.assertIsNone(spec)

    def test_general_ask_single_river_no_map(self):
        spec = decide("Tell me about the Indus river.",
                      "The Indus is one of the longest rivers in Asia.")
        self.assertIsNone(spec)

    def test_assertion_single_river_no_map(self):
        spec = decide(
            "Consider the following statements about the Mahanadi.",
            "Statement 1 is correct. Statement 2 is incorrect.")
        self.assertIsNone(spec)

    def test_two_incidental_mentions_no_map(self):
        spec = decide("Indus and Ganga are Himalayan rivers.",
                      "Both rise in the Himalayas and flow south.")
        self.assertIsNone(spec)

    def test_relation_needs_two_parties(self):
        # A relationship cannot be drawn from one party, and the
        # missing party must never be invented.
        spec = decide("Does Guwahati border the sea?",
                      "Guwahati is a landlocked city in Assam state.")
        self.assertIsNone(spec)

    def test_route_ask_without_evidence_no_map(self):
        # One endpoint, no sourced geometry, no ordered links: a route
        # ask with nothing drawable stays text.
        spec = decide("Trace the journey to Delhi.",
                      "The traveller finally reached Delhi.")
        self.assertIsNone(spec)

    def test_exam_marks_are_not_map_marks(self):
        spec = decide("How many marks is the Delhi question worth?",
                      "Delhi and Mumbai are discussed in class.")
        self.assertIsNone(spec)

    def test_non_geo_subject_blocks_incidental_places(self):
        spec = decide(
            "The President visited Delhi, Mumbai and Chennai.",
            "The tour covered three cities in a week.")
        self.assertIsNone(spec)

    def test_process_word_alone_no_diagram(self):
        spec = decide(
            "What does the author say?",
            "This process is long and complex with many aspects to "
            "consider carefully by all readers.")
        self.assertIsNone(spec)

    def test_mechanism_word_alone_no_diagram(self):
        spec = decide(
            "What is said about the machine?",
            "The mechanism is complicated and hard to understand "
            "without study and care here.")
        self.assertIsNone(spec)


class TruePositiveCases(unittest.TestCase):
    """A genuine spatial requirement still earns its visual."""

    def test_location_statement_about_place_maps(self):
        # Same "which statement" frame as the Indus false positive --
        # but the question IS about location, so the map is required.
        spec = decide(
            "Which statement about the location of Chilika is correct?",
            "Chilika lies on the Odisha coast.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "location_map")

    def test_route_ask_with_geometry_maps_single_place(self):
        # One named place, but sourced Ganga geometry answers the
        # route ask: the map draws the river, not just a dot.
        spec = decide("Trace the Ganga's route.",
                      "The Ganga flows from Haridwar to Patna.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "location_map")
        self.assertEqual([r["id"] for r in spec.payload["routes"]],
                         ["ganga"])

    def test_set_ask_backfills_named_members(self):
        # "List the tributaries" requires a river set; the question
        # names Ganga, the explanation names the rest. Displayed: the
        # named members plus their sourced river geometry.
        spec = decide("List the tributaries of the Ganga.",
                      "The Yamuna joins the Ganga at Prayagraj.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["ganga", "prayagraj", "yamuna"])
        self.assertEqual(
            sorted(r["id"] for r in spec.payload["routes"]),
            ["ganga", "yamuna"])

    def test_set_ask_never_adds_unasked_places(self):
        # The set is complete in the question: explanation mentions
        # (Delhi) must not pollute the displayed set.
        spec = decide("Name the lakes: Chilika, Sambhar, Wular.",
                      "Delhi hosted the lakes conference.")
        self.assertIsNotNone(spec)
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["chilika", "sambhar", "wular"])

    def test_relation_ask_shows_both_parties(self):
        spec = decide("Which city lies north of Delhi?",
                      "Jaipur lies north of Delhi on the map.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["delhi", "jaipur"])

    def test_relation_ask_without_which_word(self):
        spec = decide("Is Itanagar east of Guwahati?",
                      "Yes, Itanagar is east of Guwahati.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["guwahati", "itanagar"])

    def test_hindi_relation_ask_maps(self):
        spec = decide("गुवाहाटी के पूर्व में कौन-सा शहर है?",
                      "ईटानगर गुवाहाटी के पूर्व में है।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["guwahati", "itanagar"])

    def test_hindi_set_ask_maps(self):
        spec = decide("झीलों के नाम बताइए: चिल्का और सांभर।",
                      "चिल्का ओडिशा में है। सांभर राजस्थान में है।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["chilika", "sambhar"])

    def test_three_place_content_set_maps(self):
        # No spatial ask, but three distinct geo entities in
        # geo-framed content meet the same 3+ bar every structure
        # gate uses; sourced river geometry draws.
        spec = decide("Indus, Ganga and Brahmaputra are Himalayan "
                      "rivers.",
                      "All three rise in the Himalayas.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual(len(spec.payload["places"]), 3)
        self.assertEqual(
            sorted(r["id"] for r in spec.payload["routes"]),
            ["brahmaputra", "ganga", "indus"])


class RationaleChainCases(unittest.TestCase):
    """QUESTION -> NEED -> TYPE -> EVIDENCE -> DISPLAYED, asserted."""

    CHAINS = (
        {
            "question": ("Which of the following statements about the "
                         "Indus river system is correct?"),
            "explanation": "The Indus rises in Tibet.",
            "need": "none: factual recall, single mention, no spatial ask",
            "type": None,
        },
        {
            "question": "Where does the Mahanadi flow?",
            "explanation": "It flows east to the Bay of Bengal.",
            "need": "spatial: where-ask about a river",
            "type": "location_map",
            "evidence": ("places=1",),
            "routes": [],
            "note": "honest gap: no Mahanadi geometry is drawn",
        },
        {
            "question": "Trace the Ganga's route.",
            "explanation": "The Ganga flows from Haridwar to Patna.",
            "need": "spatial: route ask, sourced geometry answers",
            "type": "location_map",
            "evidence": ("intent=route", "places=1", "routes=1"),
            "routes": ["ganga"],
        },
        {
            "question": "List the tributaries of the Ganga.",
            "explanation": "The Yamuna joins the Ganga at Prayagraj.",
            "need": "spatial: set enumeration of rivers",
            "type": "regional_map",
            "evidence": ("places=3",),
            "places": ["ganga", "prayagraj", "yamuna"],
        },
        {
            "question": "गुवाहाटी के पूर्व में कौन-सा शहर है?",
            "explanation": "ईटानगर गुवाहाटी के पूर्व में है।",
            "need": "spatial: directional relation, both parties named",
            "type": "regional_map",
            "evidence": ("places=2",),
            "places": ["guwahati", "itanagar"],
        },
    )

    def test_chains(self):
        for chain in self.CHAINS:
            with self.subTest(question=chain["question"][:40]):
                spec = decide(chain["question"], chain["explanation"])
                if chain["type"] is None:
                    self.assertIsNone(spec, chain["need"])
                    continue
                self.assertIsNotNone(spec, chain["need"])
                self.assertEqual(spec.visual_type, chain["type"])
                for bit in chain.get("evidence", ()):
                    self.assertIn(bit, spec.evidence)
                if "places" in chain:
                    self.assertEqual(
                        [p["id"] for p in spec.payload["places"]],
                        chain["places"])
                if "routes" in chain:
                    self.assertEqual(
                        [r["id"] for r in spec.payload["routes"]],
                        chain["routes"])


class InterrogativeParityCases(unittest.TestCase):
    def test_bare_kaun_is_person_recall(self):
        self.assertIn("recall",
                      viz.classify_intent("चक्रवर्ती सम्राट कौन था?"))

    def test_hyphenated_kaun_sa_is_not_recall(self):
        # कौन-सा (which ...) asks about a thing, like EN "which city".
        self.assertNotIn(
            "recall",
            viz.classify_intent("गुवाहाटी के पूर्व में कौन-सा शहर है?"))

    def test_kisne_stays_recall(self):
        self.assertIn("recall",
                      viz.classify_intent("यह काम किसने किया था?"))

    def test_set_and_relation_detectors(self):
        self.assertTrue(viz._is_set_question("Name the lakes."))
        self.assertTrue(viz._is_set_question("List the tributaries."))
        self.assertTrue(
            viz._is_set_question("झीलों के नाम बताइए।"))
        self.assertFalse(viz._is_set_question("How many marks?"))
        self.assertTrue(
            viz._is_relation_question("Is Itanagar east of Guwahati?"))
        self.assertTrue(
            viz._is_relation_question("Which state borders Assam?"))
        self.assertTrue(
            viz._is_relation_question("गुवाहाटी के पूर्व में क्या है?"))
        self.assertFalse(
            viz._is_relation_question("Where is Chilika lake?"))


if __name__ == "__main__":
    unittest.main()
