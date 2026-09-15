"""Rule-13 adversarial visual verification: regression pins.

Pins the defects found by adversarial probing of the decision engine:
no fictional comparison sides, no side-bleed across sentence ends,
Devanagari-aware side attribution, Hindi अंतर्राष्ट्रीय/तुलनात्मक
guards on side extraction, Hinglish side frames (aur-antar, ki-tulna,
compare-and, ke-types), and roman interrogative parity (kahan/kaise/
kyon/kaun/kab/kram/naam/kis-disha-mein) with EN/Devanagari. Offline
and deterministic throughout.
"""

from __future__ import annotations

import unittest

from pdf_service.viz import engine as viz


def decide(question, explanation=""):
    return viz.safe_decide_visual(question, (), explanation)


class NoFictionalSidesCases(unittest.TestCase):
    """A sided comparison needs bullets attributed to a side."""

    def test_unattributed_bullets_yield_no_sided_comparison(self):
        spec = decide("How is X different from Y?",
                      "• Apples are red.\n• Oranges are orange.\n"
                      "• Both are fruits.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")
        self.assertNotIn("left_title", spec.payload)

    def test_substring_lookalikes_do_not_attribute(self):
        # "next"/"text" contain x/t but never mention side X.
        spec = decide("How is X different from Y?",
                      "• The next step matters.\n• Text is dense here.\n"
                      "• Both need extremes.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")


class SidesBleedCases(unittest.TestCase):
    """Sides never span sentence ends or bullet markers."""

    def test_short_side_does_not_eat_explanation(self):
        spec = decide("Is X vs Y good?",
                      "• Apples are red.\n• Oranges are orange.\n"
                      "• Both are fruits.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")

    def test_period_inside_name_preserved(self):
        spec = decide("How is Boston different from St. Louis?",
                      "• Boston is coastal.\n• St. Louis is inland.\n"
                      "• Both are cities.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertEqual(spec.payload["right_title"], "St. Louis")

    def test_stopwords_do_not_attribute(self):
        # Right side bleeds a verb phrase ("dogs are small"): "are"
        # must not attribute bullets; side words still count. (Single
        # letters like "A" stay skipped: they collide with articles.)
        spec = decide("Differences between cats and dogs are small.",
                      "• Cats are tiny here.\n• Dogs are small too.\n"
                      "• Both are minor.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertEqual(spec.payload["left_points"],
                         ["Cats are tiny here."])
        self.assertEqual(spec.payload["right_points"],
                         ["Dogs are small too."])
        self.assertEqual(spec.payload["common"], ["Both are minor."])


class SharedWordCases(unittest.TestCase):
    """Words shared by both sides cannot attribute either side."""

    def test_shared_word_does_not_poison_attribution(self):
        spec = decide("What is the difference between Lok Sabha and "
                      "Rajya Sabha?",
                      "• Lok Sabha members are directly elected.\n"
                      "• Rajya Sabha members are indirectly elected.\n"
                      "• Both form the Parliament of India.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertEqual(spec.payload["left_points"],
                         ["Lok Sabha members are directly elected."])
        self.assertEqual(spec.payload["right_points"],
                         ["Rajya Sabha members are indirectly elected."])
        self.assertEqual(spec.payload["common"],
                         ["Both form the Parliament of India."])

    def test_indistinguishable_sides_fall_back(self):
        # Same words on both sides: no bullet can name one side.
        spec = decide("How is Lake Chilika different from Chilika Lake?",
                      "• Lake Chilika is vast.\n"
                      "• Chilika Lake is brackish.\n"
                      "• Both are wetlands here.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")


class HindiGuardCases(unittest.TestCase):
    """अंतर्राष्ट्रीय/तुलनात्मक never mint comparison sides."""

    def test_antarrashtriya_has_no_sides(self):
        spec = decide("भारत और चीन में अंतर्राष्ट्रीय व्यापार होता है?",
                      "• भारत निर्यात करता है।\n• चीन आयात करता है।\n"
                      "• दोनों लाभान्वित हैं।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")

    def test_tulnatmak_has_no_sides(self):
        spec = decide("राम और श्याम का तुलनात्मक अध्ययन क्या है?",
                      "• राम लंबा है।\n• श्याम छोटा है।\n• दोनों मित्र हैं।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")

    def test_devanagari_sides_attribute_despite_vowel_signs(self):
        # गंगा ends in ा (a mark, not a word char): attribution must
        # still match it.
        spec = decide("गंगा और यमुना में अंतर बताइए?",
                      "• गंगा लंबी है।\n• यमुना सहायक है।\n"
                      "• दोनों पवित्र हैं।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertEqual(spec.payload["left_points"], ["गंगा लंबी है।"])
        self.assertEqual(spec.payload["right_points"], ["यमुना सहायक है।"])


class HinglishSidesCases(unittest.TestCase):
    """Roman side frames extract sides; sideless frames do not."""

    BULLETS = ("• Ram is tall and fast.\n• Shyam is short and slow.\n"
               "• Both are friends forever.")

    def test_aur_antar_extracts_sides(self):
        for question in ("Ram aur Shyam mein kya antar hai?",
                         "Ram aur Shyam men antar batao?",
                         "Ram aur Shyam ka antar batao?"):
            with self.subTest(question=question):
                spec = decide(question, self.BULLETS)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, "comparison")
                self.assertIn("comparison", spec.evidence[0])

    def test_ki_tulna_extracts_sides(self):
        spec = decide("Ram aur Shyam ki tulna karo?", self.BULLETS)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")

    def test_compare_and_extracts_sides(self):
        spec = decide("Compare Ram and Shyam.", self.BULLETS)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertIn("comparison", spec.evidence[0])

    def test_vs_fires_comparison_intent(self):
        self.assertIn("comparison",
                      viz.classify_intent("Ganga vs Yamuna: which is longer?"))
        spec = decide("Ganga vs Yamuna: which is longer?",
                      "• Ganga grew since 1857.\n• Yamuna changed in 1919.\n"
                      "• Both revered since 1942.")
        self.assertIsNotNone(spec)
        # The comparison ask beats the incidental years.
        self.assertEqual(spec.visual_type, "comparison")

    def test_compare_following_has_no_sides(self):
        spec = decide("Compare the following statements.",
                      "• Point one here.\n• Point two here.\n"
                      "• Point three here.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")

    def test_ke_types_classifies(self):
        spec = decide("Rocks ke types batao.",
                      "• Igneous rocks hain.\n• Sedimentary rocks hain.\n"
                      "• Metamorphic rocks hain.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")
        self.assertIn("classify", spec.evidence[0])

    def test_ke_prakar_classifies(self):
        spec = decide("Rocks ke prakar batao.",
                      "• Igneous rocks hain.\n• Sedimentary rocks hain.\n"
                      "• Metamorphic rocks hain.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")

    def test_types_verb_is_not_classification(self):
        self.assertIsNone(
            decide("What does Luke do?",
                   "Luke types quickly and well every day now."))


class RomanInterrogativeCases(unittest.TestCase):
    """Roman Hinglish frames decide like EN/Devanagari equivalents."""

    def test_kahan_is_location(self):
        spec = decide("Sambhar kahan hai?",
                      "Sambhar Rajasthan mein sthit hai.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "location_map")
        self.assertIn("location", spec.evidence[0])

    def test_kaise_is_process(self):
        spec = decide("Barsaat kaise hoti hai?",
                      "1. Vashpikaran hota hai.\n2. Sanghanan hota hai.\n"
                      "3. Varshan hota hai.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "flowchart")
        self.assertIn("process", spec.evidence[0])

    def test_kyon_matches_why_race(self):
        expl = ("Causes: 1857 anger, 1919 fear, 1942 resolve grew.\n"
                "Effects: 1857 revolt, 1919 massacre, 1942 movement.")
        roman = decide("1857 ka vidroh kyon hua tha?", expl)
        english = decide("Why did the revolt of 1857 happen?", expl)
        self.assertIsNotNone(roman)
        self.assertIsNotNone(english)
        self.assertEqual(roman.visual_type, "cause_effect")
        self.assertEqual(english.visual_type, roman.visual_type)

    def test_kaun_blocks_like_who(self):
        expl = ("• Shah Jahan ne banwaya.\n• Ustad Ahmad tha.\n"
                "• Agra mein hai.")
        self.assertIsNone(
            decide("Taj Mahal kaun banwaya tha?", expl))
        self.assertIsNone(
            decide("Who built the Taj Mahal?", expl))

    def test_kab_kram_are_chronology(self):
        expl = ("1857 mein vidroh. 1919 mein hatyakand. "
                "1942 mein andolan.")
        for question in ("Yuddh kab hua tha?", "Ghatna kram batao."):
            with self.subTest(question=question):
                spec = decide(question, expl)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, "timeline")
                self.assertIn("chronology", spec.evidence[0])

    def test_naam_is_set_ask(self):
        spec = decide("Jheelon ke naam batao: Chilika aur Sambhar.",
                      "Chilika Odisha mein hai. Sambhar Rajasthan mein hai.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual(len(spec.payload["places"]), 2)

    def test_kis_disha_roman_is_relation(self):
        spec = decide("Patna kis disha mein hai?",
                      "Patna Delhi ke poorab mein hai.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["patna", "delhi"])


class EvidenceHonestyCases(unittest.TestCase):
    """Comparison evidence claims match attributed content."""

    def test_reported_points_are_attributed(self):
        spec = decide("Ram aur Shyam mein kya antar hai?",
                      "• Ram is tall and fast.\n• Shyam is short and slow.\n"
                      "• Both are friends forever.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertTrue(spec.payload["left_points"])
        self.assertTrue(spec.payload["right_points"])
        total = (len(spec.payload["left_points"])
                 + len(spec.payload["right_points"])
                 + len(spec.payload["common"]))
        self.assertEqual(spec.evidence[1], "sides=2/points=%d" % total)


if __name__ == "__main__":
    unittest.main()
