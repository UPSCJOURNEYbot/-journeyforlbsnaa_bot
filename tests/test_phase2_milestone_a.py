"""Phase 2 Milestone A: intent-driven visual decisions.

Covers the Milestone A engine upgrade: interrogative intent
classification (English/Hindi), evidence gates for the three new
visual types (panels/mechanism/spatial_chain), the Rule-13
semantic-differentiation battery (same keywords, different intents ->
different visuals), correct-answer map focus, geometry guarantees
(no-overlap/in-bounds/exact height) for the new templates, and
representative real-PDF renders. All offline and deterministic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import fitz  # PyMuPDF; already a project dependency

from pdf_service.render import render_testseries_pdf
from pdf_service.viz import engine as viz
from pdf_service.viz import templates, textstyle
from pdf_service.viz.templates import Box, boxes_overlap


HEART_4 = ("• Right atrium receives used blood.\n"
           "• Right ventricle pumps to the lungs.\n"
           "• Left atrium receives fresh blood.\n"
           "• Left ventricle pumps to the body.")

CHILIKA_FEATURES_4 = ("Chilika impresses every visitor.\n"
                      "• Chilika is a brackish water lagoon.\n"
                      "• Chilika hosts migratory birds in winter.\n"
                      "• Chilika supports fishing livelihoods.\n"
                      "• Chilika faces siltation pressures.")

NUMBERED_4 = ("1. Write the application.\n"
              "2. Pay the fee.\n"
              "3. Submit to the officer.\n"
              "4. Collect the reply.")

VACCINE_ROLES = ("Source: weakened germ\n"
                 "Delivery: syringe needle\n"
                 "Target: immune cells\n"
                 "Result: lasting protection")

WORLD_ROUTE = ("The route runs from London via Moscow to Beijing, "
               "then onward to Tokyo.")

STATEMENTS_Q = ("Consider the following statements:\n"
                "1. The monsoon brings most of the annual rainfall.\n"
                "2. The retreating monsoon waters the south-east coast.\n"
                "3. El Nino can weaken the monsoon rains.")


def decide(question, explanation="", **kwargs):
    return viz.safe_decide_visual(question, (), explanation, **kwargs)


def vtype(question, explanation="", **kwargs):
    spec = decide(question, explanation, **kwargs)
    return spec.visual_type if spec else None


class IntentCases(unittest.TestCase):
    def test_frames_en(self):
        frames = {
            "Where is Chilika lake?": "location",
            "Trace the route from Delhi to Mumbai via Jaipur.": "route",
            "How is X different from Y?": "comparison",
            "Arrange the events in chronological order.": "chronology",
            "List the steps to file an RTI plea.": "process",
            "Explain the mechanism of vaccination.": "mechanism",
            "What are the causes and effects of floods?": "causal",
            "What are the three types of rocks?": "classify",
            "Explain the water cycle with stages.": "cycle",
            "What are the features of the heart?": "features",
            "Who led the Dandi March?": "recall",
            "Consider the following statements.": "assertion",
        }
        for question, frame in frames.items():
            with self.subTest(question=question):
                self.assertIn(frame, viz.classify_intent(question))

    def test_frames_hi(self):
        frames = {
            "चिल्का झील कहाँ स्थित है?": "location",
            "लंदन से बीजिंग तक का मार्ग बताइए।": "route",
            "लोकसभा और राज्यसभा में अंतर क्या है?": "comparison",
            "घटनाओं को कालानुक्रम में लगाइए।": "chronology",
            "टीकाकरण के चरण लिखिए।": "process",
            "टीकाकरण की क्रियाविधि समझाइए।": "mechanism",
            "बाढ़ के कारण और प्रभाव क्या हैं?": "causal",
            "चट्टानों के प्रकार बताइए।": "classify",
            "जल चक्र समझाइए।": "cycle",
            "हृदय की विशेषताएँ लिखिए।": "features",
            "दांडी मार्च का नेतृत्व किसने किया?": "recall",
            "निम्नलिखित कथनों पर विचार कीजिए।": "assertion",
        }
        for question, frame in frames.items():
            with self.subTest(question=question):
                self.assertIn(frame, viz.classify_intent(question))

    def test_multi_intent(self):
        intents = viz.classify_intent(
            "What are the stages of the water cycle?")
        self.assertIn("process", intents)
        self.assertIn("cycle", intents)
        self.assertEqual(tuple(sorted(intents)), intents)

    def test_general_empty(self):
        self.assertEqual(viz.classify_intent(""), ())
        self.assertEqual(viz.classify_intent("Tell me about pottery."), ())

    def test_no_false_cycle(self):
        # "cyclone/cyclonic" and "चक्रवर्ती" are not cycles.
        self.assertNotIn("cycle", viz.classify_intent(
            "What is a cyclonic storm?"))
        self.assertNotIn("cycle", viz.classify_intent(
            "चक्रवर्ती सम्राट कौन था?"))

    def test_no_false_difference(self):
        # "international" (अंतर्राष्ट्रीय) is not a comparison ask.
        self.assertNotIn("comparison", viz.classify_intent(
            "Which body handles अंतर्राष्ट्रीय disputes?"))


class PanelsCases(unittest.TestCase):
    def test_panels_en(self):
        spec = decide("Name the parts of the human heart.",
                      "Learn the parts in order.\n" + HEART_4)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "panels")
        self.assertEqual(len(spec.payload["panels"]), 4)
        self.assertEqual(spec.evidence[1], "panels=4")
        self.assertIn("features", spec.evidence[0])

    def test_panels_hi(self):
        spec = decide("मानव हृदय की चार विशेषताएँ लिखिए।",
                      ("हृदय के भाग सीखिए।\n"
                       "• दायां अलिंद अशुद्ध रक्त लेता है।\n"
                       "• दायां निलय रक्त फेफड़ों तक भेजता है।\n"
                       "• बायां अलिंद शुद्ध रक्त लेता है।\n"
                       "• बायां निलय रक्त शरीर तक भेजता है।"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "panels")

    def test_panels_hinglish(self):
        spec = decide("मानव हृदय की चार features बताइए।",
                      "Learn the parts in order.\n" + HEART_4)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "panels")

    def test_panels_numbered_features(self):
        # Numbered features are parallel items, not process stages.
        spec = decide("What are the four features of the heart?",
                      ("1. Two upper chambers.\n"
                       "2. Two lower chambers.\n"
                       "3. Four valves in total.\n"
                       "4. A thick left wall."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "panels")

    def test_panels_needs_exactly_four(self):
        three = ("What are the features of the heart?",
                 "• Two atria on top.\n• Two ventricles below.\n"
                 "• Four valves inside.")
        five = ("What are the features of the heart?",
                "• Two atria on top.\n• Two ventricles below.\n"
                "• Four valves inside.\n• One septum wall.\n"
                "• Thick left muscle.")
        self.assertEqual(vtype(*three), "concept_map")
        self.assertEqual(vtype(*five), "concept_map")

    def test_panels_yields_to_construction(self):
        spec = decide(
            "What are the features of the Indian Constitution?",
            ("The Indian Constitution comprises the Preamble, "
             "Fundamental Rights, Directive Principles and Schedules "
             "for all citizens.\n"
             "• Point one is noted here.\n"
             "• Point two is noted here.\n"
             "• Point three is noted here.\n"
             "• Point four is noted here."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")

    def test_panels_flat_only(self):
        self.assertEqual(
            vtype("What are the features of Mughal rule?",
                  "• Central\n  - Emperor\n  - Wazir\n"
                  "• Provincial\n  - Subedar\n  - Diwan"),
            "mind_map")

    def test_panels_recall_veto(self):
        self.assertIsNone(decide(
            "Who listed the features of this tomb?",
            "• High dome over the hall.\n"
            "• Marble inlay on walls.\n"
            "• Large gateway outside.\n"
            "• Gardens all around."))

    def test_panels_grounded(self):
        question = "Name the parts of the human heart."
        expl = "Learn the parts in order.\n" + HEART_4
        spec = decide(question, expl)
        blob = question + "\n" + expl
        for panel in spec.payload["panels"]:
            self.assertIn(panel["text"], blob)

    def test_panels_beats_topic_map(self):
        # A features ask wants feature panels, not a locator dot.
        spec = decide("What are the features of Chilika lake?",
                      CHILIKA_FEATURES_4)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "panels")


class MechanismCases(unittest.TestCase):
    def test_mechanism_roles_en(self):
        spec = decide("Explain the mechanism of vaccination.",
                      VACCINE_ROLES)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "mechanism")
        stages = spec.payload["stages"]
        self.assertEqual([s["role"] for s in stages],
                         ["source", "delivery", "target", "result"])
        self.assertEqual([s["role_text"] for s in stages],
                         ["Source", "Delivery", "Target", "Result"])
        self.assertEqual(spec.evidence[1], "stages=4/roles=4")

    def test_mechanism_hi(self):
        spec = decide("टीकाकरण की क्रियाविधि समझाइए।",
                      ("स्रोत: कमजोर रोगाणु\n"
                       "वाहक: सिरिंज की सुई\n"
                       "लक्ष्य: प्रतिरक्षा कोशिकाएँ\n"
                       "परिणाम: स्थायी सुरक्षा"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "mechanism")
        self.assertEqual(
            [s["role"] for s in spec.payload["stages"]],
            ["source", "delivery", "target", "result"])

    def test_mechanism_hinglish(self):
        spec = decide("Explain the working of vaccination. "
                      "टीकाकरण कैसे काम करता है?",
                      ("Source: weakened germ\n"
                       "Target: immune cells\n"
                       "Result: lasting protection"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "mechanism")

    def test_mechanism_intent_with_numbered(self):
        spec = decide("Explain the mechanism of blood clotting.",
                      ("1. Platelets gather at the cut.\n"
                       "2. Fibrin threads form a mesh.\n"
                       "3. Cells trap inside the mesh.\n"
                       "4. A firm clot seals the cut."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "mechanism")
        self.assertTrue(all(s["role"] is None
                            for s in spec.payload["stages"]))

    def test_mechanism_needs_evidence(self):
        self.assertIsNone(decide(
            "Explain the mechanism of the monsoon in brief.",
            "Winds shift with the seasons every year."))
        # A single repeated role is a list, not a mechanism.
        self.assertIsNone(decide(
            "Tell me about these related inputs.",
            "Source: sunlight\nSource: water\nSource: soil nutrients."))

    def test_mechanism_order_preserved(self):
        # Stage order is the text order; never re-sorted into a
        # canonical role sequence.
        spec = decide("Explain the mechanism of this device.",
                      "Result: clean water\n"
                      "Source: muddy water\n"
                      "Target: filter bed")
        self.assertIsNotNone(spec)
        self.assertEqual(
            [s["label"] for s in spec.payload["stages"]],
            ["clean water", "muddy water", "filter bed"])

    def test_mechanism_grounded(self):
        spec = decide("Explain the mechanism of vaccination.",
                      VACCINE_ROLES)
        for stage in spec.payload["stages"]:
            self.assertIn(stage["label"], VACCINE_ROLES)
            self.assertIn(stage["role_text"], VACCINE_ROLES)

    def test_plain_process_stays_process(self):
        self.assertEqual(
            vtype("Describe the process of urine formation in detail.",
                  ("1. Filtration occurs first.\n"
                   "2. Reabsorption follows next.\n"
                   "3. Secretion happens after.\n"
                   "4. Excretion completes it.")),
            "process")


class ChainCases(unittest.TestCase):
    def test_chain_world_en(self):
        spec = decide("Trace the route from London to Tokyo.",
                      WORLD_ROUTE)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")
        self.assertEqual(
            [link["id"] for link in spec.payload["links"]],
            ["london", "moscow", "beijing", "tokyo"])
        self.assertEqual(spec.evidence[1], "links=4")

    def test_chain_hi(self):
        spec = decide("लंदन से बीजिंग तक का मार्ग बताइए।",
                      "यह मार्ग लंदन से शुरू होकर मॉस्को होकर "
                      "बीजिंग जाता है।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")
        self.assertEqual(
            [link["id"] for link in spec.payload["links"]],
            ["london", "moscow", "beijing"])

    def test_chain_hinglish(self):
        spec = decide("Trace the route from London to Tokyo.",
                      "Route London se Tokyo via Moscow and Beijing "
                      "jaata hai.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")
        # Mention order is the honest order, as written.
        self.assertEqual(
            [link["id"] for link in spec.payload["links"]],
            ["london", "tokyo", "moscow", "beijing"])

    def test_chain_india_when_map_blocked(self):
        # A person-seeking question never justifies a map, but the
        # ordered route in the explanation is still visualisable.
        spec = decide("Who led the famous march?",
                      "The march went from Ahmedabad to Delhi "
                      "via Jaipur.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")
        self.assertEqual(
            [link["id"] for link in spec.payload["links"]],
            ["ahmedabad", "delhi", "jaipur"])

    def test_chain_india_nonmap_subject(self):
        spec = decide("Trace the trade journey of goods from Delhi "
                      "to Mumbai via Ahmedabad.",
                      "Goods moved from Delhi to Mumbai via Ahmedabad "
                      "in caravans.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")

    def test_chain_dedupes_first_mention_wins(self):
        spec = decide("Who travelled this circuit?",
                      "The circuit ran from Delhi to Jaipur via Delhi "
                      "to Ahmedabad.")
        self.assertIsNotNone(spec)
        self.assertEqual(
            [link["id"] for link in spec.payload["links"]],
            ["delhi", "jaipur", "ahmedabad"])

    def test_chain_gates(self):
        # Two known places are not a chain.
        self.assertIsNone(decide(
            "Trace the route from London to Paris.",
            "The route runs from London to Paris via Calais."))
        # Places without movement phrasing are not a chain.
        self.assertIsNone(decide(
            "Name three world capitals.",
            "London, Paris and Tokyo are famous capitals."))

    def test_chain_grounded_in_dataset(self):
        spec = decide("Trace the route from London to Tokyo.",
                      WORLD_ROUTE)
        by_id = {p["id"]: p for p in viz.load_places()["places"]}
        for link in spec.payload["links"]:
            self.assertIn(link["id"], by_id)
            self.assertEqual(link["name_en"], by_id[link["id"]]["name_en"])

    def test_map_beats_chain_for_where(self):
        spec = decide("Where are Delhi, Jaipur and Ahmedabad on the "
                      "trade route via the plains?", "")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")


class Rule13Cases(unittest.TestCase):
    """Same keywords, different intents -> different visuals."""

    def test_five_intents_same_entity(self):
        got = {
            "where": vtype(
                "Where is Chilika lake located?",
                "Chilika is a brackish lagoon located on the coast."),
            "stages": vtype(
                "What are the stages of Chilika lake formation?",
                ("1. A bay gets enclosed by sand.\n"
                 "2. Fresh water mixes with sea.\n"
                 "3. Plants colonise the shallows.\n"
                 "4. A lagoon ecosystem forms.")),
            "features": vtype("What are the features of Chilika lake?",
                              CHILIKA_FEATURES_4),
            "compare": vtype(
                "How is Chilika lake different from Sambhar lake?",
                ("• Chilika is a brackish lagoon.\n"
                 "• Sambhar is a salt lake.\n"
                 "• Both are wetlands.")),
            "who": vtype(
                "Who was the king near Chilika lake?",
                "A king once ruled the region around Chilika."),
        }
        self.assertEqual(got["where"], "location_map")
        self.assertEqual(got["stages"], "process")
        self.assertEqual(got["features"], "panels")
        self.assertEqual(got["compare"], "comparison")
        self.assertIsNone(got["who"])
        self.assertEqual(len(set(got.values())), 5)

    def test_numbered_steps_vs_statements(self):
        self.assertEqual(
            vtype("List the steps to file an RTI application correctly.",
                  "Follow these points in order.\n" + NUMBERED_4),
            "flowchart")
        self.assertIsNone(decide(
            STATEMENTS_Q,
            "All three statements correctly describe the system."))

    def test_cycle_word_three_ways(self):
        self.assertEqual(
            vtype("Explain the water cycle with its stages in detail.",
                  "Stages:\nEvaporation, Condensation, Precipitation, "
                  "Collection"),
            "cycle")
        self.assertEqual(
            vtype("What are the features of the new cycle policy?",
                  ("• Faster permits for green units.\n"
                   "• Tax rebates for solar plants.\n"
                   "• Skill centres in each block.\n"
                   "• Yearly audit of outcomes.")),
            "panels")
        self.assertIsNone(decide(
            "Explain the water cycle in brief.",
            "The water cycle moves water around. Evaporation lifts it "
            "up. Rain brings it down. Rivers carry it on."))

    def test_process_mechanism_recall(self):
        numbered = ("1. Filtration occurs first.\n"
                    "2. Reabsorption follows next.\n"
                    "3. Secretion happens after.\n"
                    "4. Excretion completes it.")
        self.assertEqual(
            vtype("Describe the process of urine formation in detail.",
                  numbered),
            "process")
        self.assertEqual(
            vtype("Explain the mechanism of urine formation in detail.",
                  ("Source: blood plasma\n"
                   "Action: filtration work\n"
                   "Target: kidney tubules\n"
                   "Result: urine output")),
            "mechanism")
        self.assertIsNone(decide(
            "Who discovered the process of blood circulation?",
            "William Harvey discovered it long ago. Blood flows in a "
            "loop around the body every single minute."))

    def test_compare_vs_locate(self):
        self.assertEqual(
            vtype("How is Chilika lake different from Sambhar lake?",
                  ("• Chilika is a brackish lagoon.\n"
                   "• Sambhar is a salt lake.\n"
                   "• Both are wetlands.")),
            "comparison")
        self.assertEqual(
            vtype("Where are Chilika and Sambhar lakes?", ""),
            "regional_map")

    def test_route_ask_vs_where_ask(self):
        self.assertEqual(
            vtype("Trace the trade journey of goods from Delhi to "
                  "Mumbai via Ahmedabad.",
                  "Goods moved from Delhi to Mumbai via Ahmedabad "
                  "in caravans."),
            "spatial_chain")
        self.assertEqual(
            vtype("Where are Delhi, Mumbai and Ahmedabad?", ""),
            "regional_map")

    def test_matching_stays_text_only(self):
        self.assertIsNone(decide(
            "Match the following articles with their subjects:\n"
            "1. Article 14\n2. Article 19\n3. Article 21",
            "Article 14 is equality, 19 is freedom, 21 is life."))
        # ... but matching place lists still earns an honest map.
        self.assertEqual(
            vtype("Match the following rivers with their states:\n"
                  "1. Ganga\n2. Godavari\n3. Narmada",
                  "Ganga flows north, Godavari flows south, Narmada "
                  "flows west."),
            "regional_map")

    def test_assertion_uses_explanation_evidence(self):
        # Authoritative explanation years still earn a timeline.
        self.assertEqual(
            vtype("Consider the following statements about our past.",
                  "In 1857 the revolt shook the empire. In 1919 came a "
                  "massacre. In 1942 the Quit India call echoed."),
            "timeline")


class FocusCases(unittest.TestCase):
    QUESTION = "Where are Chilika and Sambhar lakes located?"

    def test_focus_from_answer(self):
        spec = decide(self.QUESTION, "Both lakes attract birds.",
                      correct_answer="Sambhar Lake")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual(spec.payload["focus"], "sambhar")
        # Focus never reorders the deterministic place list.
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["chilika", "sambhar"])

    def test_focus_none_without_answer(self):
        spec = decide(self.QUESTION, "Both lakes attract birds.")
        self.assertEqual(spec.payload["focus"], None)

    def test_answer_cannot_initiate_map(self):
        self.assertIsNone(decide(
            "Which lake is famous for migratory birds?",
            "Many lakes attract birds in winter.",
            correct_answer="Chilika Lake"))


class EvidenceCases(unittest.TestCase):
    def test_evidence_present(self):
        specs = [
            decide("Where is Chilika lake?", "Chilika is a lagoon."),
            decide("Explain the mechanism of vaccination.", VACCINE_ROLES),
            decide("Arrange the events in chronological order.",
                   "In 1857 revolt. In 1919 massacre. In 1942 movement."),
        ]
        for spec in specs:
            with self.subTest(spec=spec.visual_type):
                self.assertTrue(spec.evidence)
                self.assertTrue(spec.evidence[0].startswith("intent="))

    def test_evidence_deterministic_json(self):
        kwargs = {"question": "Where is Chilika lake?",
                  "explanation": "Chilika is a lagoon."}
        first = decide(**kwargs).to_dict()
        second = decide(**kwargs).to_dict()
        self.assertEqual(first, second)
        self.assertIn("evidence", first)
        json.dumps(first)


class _FakePdf:
    """Duck-typed stand-in recording nothing (geometry is exact)."""

    def set_fill_color(self, *args):
        pass

    def set_draw_color(self, *args):
        pass

    def set_text_color(self, *args):
        pass

    def set_line_width(self, *args):
        pass

    def rect(self, *args, **kwargs):
        pass

    def set_font(self, *args, **kwargs):
        pass

    def set_xy(self, *args):
        pass

    def cell(self, *args, **kwargs):
        pass

    def get_string_width(self, text):
        return len(text or "") * 1.2

    def line(self, *args):
        pass

    def polygon(self, *args, **kwargs):
        pass

    def ellipse(self, *args, **kwargs):
        pass


def _contains(outer: Box, inner: Box, eps: float = 1e-6) -> bool:
    return (inner.x + eps >= outer.x and inner.y + eps >= outer.y
            and inner.x + inner.w <= outer.x + outer.w + eps
            and inner.y + inner.h <= outer.y + outer.h + eps)


class GeometryCases(unittest.TestCase):
    def _assert_layout(self, elements, x, y, w, h):
        boxes = [el["box"] for el in elements if el["k"] == "box"]
        self.assertTrue(boxes)
        for box in boxes:
            self.assertGreaterEqual(box.x, x - 1e-6)
            self.assertGreaterEqual(box.y, y - 1e-6)
            self.assertLessEqual(box.x + box.w, x + w + 1e-6)
            self.assertLessEqual(box.y + box.h, y + h + 1e-6)
        for i, first in enumerate(boxes):
            for second in boxes[i + 1:]:
                if _contains(first, second) or _contains(second, first):
                    continue  # deliberate parent/child nesting
                self.assertFalse(boxes_overlap(first, second),
                                 "overlap: %r vs %r" % (first, second))
        for el in elements:
            if el["k"] != "box" or not el["text"]:
                continue
            need = (1.2 + el.get("max_lines", 2) * el.get("size", 8.5)
                    * textstyle.LINE_FACTOR)
            self.assertLessEqual(need, el["box"].h + 1e-6,
                                 "text cannot fit: %r" % (el,))

    def test_panels_geometry(self):
        elements, height = templates.layout_panels(4, 0.0, 0.0, 164.0)
        self.assertAlmostEqual(height, 2 * 24.0 + 2.5)
        self._assert_layout(elements, 0.0, 0.0, 164.0, height)

    def test_mechanism_geometry(self):
        for flags in ([True, True, True, True],
                      [False, False, False],
                      [True, False, True, False, True]):
            elements, height = templates.layout_mechanism(
                flags, 0.0, 0.0, 164.0)
            self._assert_layout(elements, 0.0, 0.0, 164.0, height)

    def test_chain_geometry(self):
        elements, height = templates.layout_chain(
            4, [True, True, False, True], 0.0, 0.0, 164.0)
        self._assert_layout(elements, 0.0, 0.0, 164.0, height)
        elements, height = templates.layout_chain(
            3, [False, False, False], 0.0, 0.0, 164.0)
        self._assert_layout(elements, 0.0, 0.0, 164.0, height)

    def _assert_estimate_tight(self, spec):
        width = 150.0
        need = templates.estimate_height_for_spec(spec, width)
        self.assertGreater(need, 0)
        templates.draw_diagram(_FakePdf(), spec, (0.0, 0.0, width, need))
        with self.assertRaises(ValueError):
            templates.draw_diagram(_FakePdf(), spec,
                                   (0.0, 0.0, width, need - 0.5))

    def test_estimates_tight(self):
        specs = [
            decide("Name the parts of the human heart.",
                   "Learn the parts.\n" + HEART_4),
            decide("Explain the mechanism of vaccination.",
                   VACCINE_ROLES),
            decide("Trace the route from London to Tokyo.", WORLD_ROUTE),
            decide("List the steps to file an RTI plea.",
                   "Follow the points.\n" + NUMBERED_4),
        ]
        for spec in specs:
            with self.subTest(spec=spec.visual_type):
                self._assert_estimate_tight(spec)


class RealPdfCases(unittest.TestCase):
    def _questions(self):
        return [
            {"question": "Name the parts of the human heart.",
             "options": ["Four chambers", "Two chambers",
                         "One chamber", "No chamber"],
             "correct_option_id": 0,
             "explanation": "Learn the parts in order.\n" + HEART_4},
            {"question": "Explain the mechanism of vaccination.",
             "options": ["Source to result", "Only source",
                         "Only result", "No stages"],
             "correct_option_id": 0,
             "explanation": VACCINE_ROLES},
            {"question": "Trace the route from London to Tokyo.",
             "options": ["London first", "Tokyo first",
                         "Moscow first", "Beijing first"],
             "correct_option_id": 0,
             "explanation": WORLD_ROUTE},
            {"question": "मानव हृदय की चार विशेषताएँ लिखिए।",
             "options": ["चार भाग", "दो भाग", "एक भाग", "कोई भाग नहीं"],
             "correct_option_id": 0,
             "explanation": ("हृदय के भाग सीखिए।\n"
                             "• दायां अलिंद अशुद्ध रक्त लेता है।\n"
                             "• दायां निलय रक्त फेफड़ों तक भेजता है।\n"
                             "• बायां अलिंद शुद्ध रक्त लेता है।\n"
                             "• बायां निलय रक्त शरीर तक भेजता है।")},
            {"question": "What is 7 x 8?",
             "options": ["54", "56", "58", "60"],
             "correct_option_id": 1,
             "explanation": "It equals 56."},
        ]

    def _render(self, out, display):
        return render_testseries_pdf(
            self._questions(), exam_title="Milestone A Test",
            tagline="Test Series", quiz_names=["QZ"],
            solution_display=display, output_path=out)

    def _inspect(self, path):
        with fitz.open(path) as doc:
            self.assertGreater(doc.page_count, 0)
            full = "\n".join(page.get_text() for page in doc)
            drawings = sum(len(page.get_drawings()) for page in doc)
            for page in doc:
                text = page.get_text()
                self.assertTrue(text.strip())
                width, height = page.rect.width, page.rect.height
                for x0, y0, x1, y1, _t, _b, _ty in page.get_text("blocks"):
                    self.assertGreaterEqual(x0, -1)
                    self.assertGreaterEqual(y0, -1)
                    self.assertLessEqual(x1, width + 1)
                    self.assertLessEqual(y1, height + 1)
        return full, drawings

    def test_new_types_render_end(self):
        with tempfile.TemporaryDirectory(prefix="p2a-") as tmp:
            out = str(Path(tmp) / "a-end.pdf")
            info = self._render(out, "end")
            self.assertEqual(info["questions"], 5)
            full, drawings = self._inspect(out)
            self.assertGreater(drawings, 0)
            self.assertNotIn("__", full)
            for badge in ("Four Panels", "Mechanism", "Route Chain"):
                self.assertIn(badge, full)
            for label in ("Right atrium", "SOURCE", "London",
                          "दायां अलिंद"):
                self.assertIn(label, full)

    def test_new_types_render_inline(self):
        with tempfile.TemporaryDirectory(prefix="p2a-") as tmp:
            out = str(Path(tmp) / "a-inline.pdf")
            self._render(out, "inline")
            full, drawings = self._inspect(out)
            self.assertGreater(drawings, 0)
            for badge in ("Four Panels", "Mechanism", "Route Chain"):
                self.assertIn(badge, full)

    def test_render_deterministic_text(self):
        with tempfile.TemporaryDirectory(prefix="p2a-") as tmp:
            first = str(Path(tmp) / "a.pdf")
            second = str(Path(tmp) / "b.pdf")
            self._render(first, "end")
            self._render(second, "end")
            with fitz.open(first) as doc:
                text_a = "\n".join(page.get_text() for page in doc)
            with fitz.open(second) as doc:
                text_b = "\n".join(page.get_text() for page in doc)
            self.assertEqual(text_a, text_b)


if __name__ == "__main__":
    unittest.main()
