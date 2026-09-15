"""Master-directive verification: question-driven, generalizable visuals.

Pins the verification findings: no word-alone trigger (§11), Hindi
interrogative parity (§15: किसके बीच/किस दिशा में/किस क्रम में/कैसे/
क्यों), causal-ask understanding, subject neutrality (§16:
polity/history/economy/science factuals stay text-only), and
stateless operation with no question-count limit (§17). Offline and
deterministic throughout.
"""

from __future__ import annotations

import unittest

from pdf_service.viz import engine as viz


def decide(question, explanation=""):
    return viz.safe_decide_visual(question, (), explanation)


class WordAloneCases(unittest.TestCase):
    """Content words without evidence never force a visual (§11)."""

    NONES = (
        ("Compare the two systems briefly.",
         "Both systems are old and complex with many interacting "
         "parts inside them."),
        ("What causes rust in iron?",
         "Rust is a common problem that affects iron structures "
         "over many years."),
        ("What happened in 1942?",
         "Many important things happened in that year across the "
         "country."),
        ("How are rocks classified?",
         "Rocks are classified by experts into several broad groups "
         "today."),
        ("What are the features?",
         "There are many features worth noting in this particular "
         "case here."),
        ("How to apply?",
         "1. Fill the form completely.\n2. Submit it at the counter."),
        ("Give a timeline.",
         "Things happened over time in a particular sequence of "
         "events."),
        ("Revise.", "• One point here.\n• Two points here."),
        ("Revise.",
         "• One.\n• Two.\n• Three.\n• Four.\n• Five.\n• Six."),
    )

    def test_words_without_evidence_stay_text(self):
        for question, expl in self.NONES:
            with self.subTest(question=question[:30]):
                self.assertIsNone(decide(question, expl))

    def test_three_bullets_is_explicit_list_evidence(self):
        # Three bullets meet the universal 3+ bar: the explanation's
        # own list is formatted (no question requirement is
        # contradicted; cf. assertion+years -> timeline).
        spec = decide("Revise.",
                      "• One point here.\n• Two points here.\n"
                      "• Three points here.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")
        self.assertEqual(len(spec.payload["satellites"]), 3)


class HindiInterrogativeCases(unittest.TestCase):
    def test_ke_beech_difference_is_comparison(self):
        spec = decide("गंगा और यमुना के बीच क्या अंतर है?",
                      "• गंगा लंबी है।\n• यमुना सहायक है।\n"
                      "• दोनों पवित्र हैं।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")

    def test_kis_disha_mein_is_relation(self):
        spec = decide("पटना किस दिशा में है?",
                      "पटना दिल्ली के पूर्व में स्थित है।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["patna", "delhi"])

    def test_kis_kram_mein_is_chronology(self):
        self.assertIn(
            "chronology",
            viz.classify_intent("घटनाएँ किस क्रम में हुईं?"))
        spec = decide("घटनाएँ किस क्रम में हुईं?",
                      "1857 में विद्रोह। 1919 में हत्याकांड हुआ। "
                      "1942 में आंदोलन हुआ।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "timeline")
        self.assertIn("intent=chronology", spec.evidence)

    def test_kaise_steps_read_flowchart(self):
        spec = decide(
            "वर्षा कैसे होती है?",
            "1. वाष्पीकरण से जल ऊपर उठता है।\n"
            "2. संघनन से बादल बनते हैं।\n"
            "3. वर्षण से वर्षा होती है।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "flowchart")

    def test_kyon_is_causal(self):
        self.assertIn("causal", viz.classify_intent("बाढ़ क्यों आती है?"))
        spec = decide(
            "बाढ़ क्यों आती है?",
            "कारण: भारी वर्षा होती है।\nप्रभाव: फसल नष्ट होती है।\n"
            "प्रभाव: घर डूबते हैं।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "cause_effect")
        self.assertIn("intent=causal", spec.evidence)


class CausalAskCases(unittest.TestCase):
    def test_causal_intents(self):
        for question in ("Why did the floods happen?",
                         "What are the causes of soil erosion?",
                         "What are the reasons for inflation?",
                         "What factors affect climate?",
                         "प्रदूषण के क्या कारण हैं?"):
            with self.subTest(question=question[:30]):
                self.assertIn("causal", viz.classify_intent(question))

    def test_why_prefers_trend_over_dot(self):
        # A why-ask about a place with dated trend evidence wants the
        # trend (timeline answers why; a locator dot does not).
        spec = decide(
            "Why is Chilika shrinking?",
            "In 1990 it was vast. In 2010 it shrank. In 2020 it "
            "shrank more.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "timeline")

    def test_statement_course_keeps_chain_not_map(self):
        # Maps claim geography (need a spatial requirement); the chain
        # only formats the author's own mention order, so an explicit
        # movement sequence still chains under a statement frame.
        spec = decide(
            "Which statement about the Ganga is correct?",
            "The Ganga flows from Haridwar via Prayagraj to Patna.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")


class SubjectNeutralityCases(unittest.TestCase):
    FACTUAL_NONES = (
        ("Who is the President of India?",
         "The President is the head of state."),
        ("Who founded the Maurya empire?",
         "Chandragupta Maurya founded it."),
        ("What is inflation?",
         "Inflation is a rise in prices over time."),
        ("What is photosynthesis?",
         "Plants convert light into food energy."),
        ("What is the UN?",
         "The UN is an international body for peace."),
        ("What is Bharatanatyam?",
         "It is a classical dance of Tamil Nadu."),
    )

    def test_factuals_stay_text_across_subjects(self):
        for question, expl in self.FACTUAL_NONES:
            with self.subTest(question=question[:30]):
                self.assertIsNone(decide(question, expl))

    def test_evidence_fires_across_subjects(self):
        cases = {
            "flowchart": (
                "How is waste segregated?",
                "1. Separate wet waste first.\n"
                "2. Keep dry waste apart.\n3. Compost the organics."),
            "comparison": (
                "How is inflation different from deflation?",
                "Compare inflation with deflation.\n"
                "• Inflation raises prices.\n"
                "• Deflation lowers prices.\n• Both matter."),
            "timeline": (
                "Arrange in order.",
                "In 1857 revolt. In 1919 massacre. In 1942 movement."),
        }
        for want, (question, expl) in cases.items():
            with self.subTest(want=want):
                spec = decide(question, expl)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, want)


class StatelessCases(unittest.TestCase):
    def test_interleaved_decisions_identical(self):
        first = decide("Where is Chilika lake?",
                       "Chilika is a lagoon on the coast.")
        for _ in range(200):
            decide("What is 7 x 8?", "It equals 56.")
            decide("Arrange in order.",
                   "In 1857 revolt. In 1919 massacre. In 1942 movement.")
        again = decide("Where is Chilika lake?",
                       "Chilika is a lagoon on the coast.")
        self.assertEqual(first.to_dict(), again.to_dict())

    def test_thousand_questions_no_quota(self):
        # Question #1 and question #1000 decide by identical
        # principles; there is no runtime counter or limit.
        probe = ("Where is Chilika lake?",
                 "Chilika is a lagoon on the coast.")
        first = decide(*probe).to_dict()
        for i in range(1000):
            decide("Filler factual %d?" % i, "Nothing visual here.")
            if i % 250 == 0:
                self.assertEqual(decide(*probe).to_dict(), first)
        self.assertEqual(decide(*probe).to_dict(), first)


if __name__ == "__main__":
    unittest.main()
