"""Phase 2 milestone D: Rule-11 acceptance matrix.

Every supported visual type is pinned on each Rule-11 dimension:
decision rule (canonical EN + HI inputs), evidence, exact height,
geometry (in-bounds, no partial overlaps, text fits), and real-PDF
rendering. Offline and deterministic throughout.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pdf_service.viz import engine as viz
from pdf_service.viz import mapdraw, templates
from pdf_service.viz.engine import VisualSpec
from pdf_service.viz.textstyle import BADGES
from tests.test_phase2_milestone_b import FakePDF

WORLD_ROUTE = ("The route runs from London via Moscow to Beijing, "
               "then onward to Tokyo.")

LONG = ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed "
        "do eiusmod tempor incididunt %d")

BODY_W = 164.0  # production body width (170 - 2 * FRAME_PAD)

# Canonical firing inputs: (question, explanation) per type.
EN_MATRIX = {
    "location_map": ("Where is Chilika lake?",
                     "Chilika is a brackish lagoon on the coast."),
    "regional_map": ("Name the lakes: Chilika, Sambhar, Wular.", ""),
    "timeline": ("Arrange in chronological order.",
                 "In 1857 revolt. In 1919 massacre. In 1942 movement."),
    "process": ("Explain the formation of rain.",
                "1. Evaporation rises high.\n"
                "2. Condensation forms clouds.\n"
                "3. Precipitation falls down."),
    "flowchart": ("What is the election procedure?",
                  "1. Notification is issued.\n"
                  "2. Nominations are filed.\n"
                  "3. Votes are counted."),
    "comparison": ("What are the differences between X and Y?",
                   "Compare X with Y.\n"
                   "• X is fast and small.\n"
                   "• Y is slow and large.\n• Both are useful."),
    "cause_effect": ("What are the causes and effects of floods?",
                     "Causes: heavy rain.\nEffects: crop loss."),
    "cycle": ("Describe the water cycle stages.",
              "The water cycle stages are:\n"
              "stages: evaporation, condensation, precipitation."),
    "classification": ("What are the types of rocks?",
                       "Types of rocks: igneous, sedimentary, "
                       "metamorphic."),
    "concept_map": ("What does the heart comprise?",
                    "The heart comprises atria, ventricles and valves "
                    "for pumping blood."),
    "mind_map": ("Describe the system.",
                 "• Branch alpha\n  • detail one\n  • detail two\n"
                 "• Branch beta\n  • detail three\n  • detail four"),
    "infographic": ("Revise the provisions.",
                    "Read carefully.\n"
                    "• Point one text here for length.\n"
                    "• Point two text here for length.\n"
                    "• Point three text here for length.\n"
                    "• Point four text here for length.\n"
                    "• Point five text here for length.\n"
                    "• Point six text here for length yes."),
    "panels": ("What are the features of the lake?",
               "• Feature one here.\n• Feature two here.\n"
               "• Feature three here.\n• Feature four here."),
    "mechanism": ("Explain the mechanism of vaccination.",
                  "Source: weakened germ\nDelivery: syringe needle\n"
                  "Target: immune cells"),
    "spatial_chain": ("Trace the route from London to Tokyo.",
                      WORLD_ROUTE),
}

HI_MATRIX = {
    "location_map": ("सांभर झील कहाँ स्थित है?",
                     "सांभर झील राजस्थान में स्थित खारे पानी की झील है।"),
    "regional_map": ("चिल्का, सांभर और वुलर झीलें कहाँ हैं?", ""),
    "timeline": ("घटनाओं को क्रम में लगाइए।",
                 "1857 में विद्रोह हुआ। 1919 में हत्याकांड हुआ। "
                 "1942 में आंदोलन हुआ।"),
    "process": ("वर्षा का निर्माण कैसे होता है?",
                "1. वाष्पीकरण होता है।\n"
                "2. संघनन से बादल बनते हैं।\n3. वर्षण होता है।"),
    "flowchart": ("चुनाव प्रक्रिया क्या है?",
                  "1. अधिसूचना जारी होती है।\n"
                  "2. नामांकन दाखिल होते हैं।\n"
                  "3. मतों की गिनती होती है।"),
    "comparison": ("गंगा और यमुना में अंतर बताइए।",
                   "• गंगा लंबी नदी है।\n• यमुना सहायक नदी है।\n"
                   "• दोनों पवित्र हैं।"),
    "cause_effect": ("बाढ़ के कारण और प्रभाव क्या हैं?",
                     "कारण: भारी वर्षा होती है।\n"
                     "प्रभाव: फसल नष्ट होती है।"),
    "cycle": ("जल चक्र के चरण विस्तार से बताइए।",
              "जल चक्र के चरण इस प्रकार हैं:\n"
              "चरण: वाष्पीकरण, संघनन, वर्षण।"),
    "classification": ("चट्टानों के प्रकार बताइए।",
                       "• आग्नेय चट्टानें।\n• अवसादी चट्टानें।\n"
                       "• रूपांतरित चट्टानें।"),
    "concept_map": ("हृदय में क्या शामिल है?",
                    "हृदय में आलिंद, निलय और कपाट शामिल हैं।"),
    "mind_map": ("तंत्र का वर्णन कीजिए।",
                 "• शाखा एक\n  • बिंदु एक\n  • बिंदु दो\n"
                 "• शाखा दो\n  • बिंदु तीन\n  • बिंदु चार"),
    "infographic": ("प्रावधानों को विस्तार से दोहराइए।",
                    "ध्यान से प्रत्येक बिंदु पढ़ें।\n"
                    "• पहला महत्वपूर्ण बिंदु यहाँ विस्तार से लिखा है।\n"
                    "• दूसरा महत्वपूर्ण बिंदु यहाँ विस्तार से लिखा है।\n"
                    "• तीसरा महत्वपूर्ण बिंदु यहाँ विस्तार से लिखा है।\n"
                    "• चौथा महत्वपूर्ण बिंदु यहाँ विस्तार से लिखा है।\n"
                    "• पाँचवाँ महत्वपूर्ण बिंदु यहाँ विस्तार से लिखा है।\n"
                    "• छठा महत्वपूर्ण बिंदु यहाँ विस्तार से लिखा है।"),
    "panels": ("झील की विशेषताएँ बताइए।",
               "• पहली विशेषता यहाँ।\n• दूसरी विशेषता यहाँ।\n"
               "• तीसरी विशेषता यहाँ।\n• चौथी विशेषता यहाँ।"),
    "mechanism": ("टीकाकरण की क्रियाविधि समझाइए।",
                  "स्रोत: कमजोर रोगाणु\nवाहक: सिरिंज की सुई\n"
                  "लक्ष्य: प्रतिरक्षा कोशिकाएँ"),
    "spatial_chain": ("लंदन से बीजिंग तक का मार्ग बताइए।",
                      "यह मार्ग लंदन से शुरू होकर मॉस्को होकर "
                      "बीजिंग जाता है।"),
}

HINGLISH = {
    "timeline": ("Events ko order mein lagao.",
                 "1857 mein revolt hua. 1919 mein massacre hua. "
                 "1942 mein movement hua."),
    "classification": ("Types of rocks batao.",
                       "• Igneous rocks hain.\n"
                       "• Sedimentary rocks hain.\n"
                       "• Metamorphic rocks hain."),
    "panels": ("Lake ke features batao.",
               "• Pehla feature yahan hai.\n"
               "• Dusra feature yahan hai.\n"
               "• Tisra feature yahan hai.\n"
               "• Chautha feature yahan hai."),
}

DIAGRAM_TYPES = [t for t in EN_MATRIX if t not in ("location_map",
                                                   "regional_map")]

# Maximal payloads per diagram type (engine caps, long texts).
MAXIMAL = {
    "process": {"steps": [{"label": LONG % i} for i in range(8)]},
    "flowchart": {"steps": [{"label": LONG % i} for i in range(8)]},
    "timeline": {"events": [{"year": 1900 + i, "label": LONG % i}
                            for i in range(10)]},
    "comparison": {"left_title": "Left Side Title Here",
                   "right_title": "Right Side Title Here",
                   "left_points": [LONG % i for i in range(4)],
                   "right_points": [LONG % i for i in range(4)],
                   "common": [LONG % i for i in range(3)]},
    "cause_effect": {"topic": "Why Things Happen",
                     "causes": [LONG % i for i in range(4)],
                     "effects": [LONG % i for i in range(4)]},
    "cycle": {"stages": [LONG % i for i in range(6)]},
    "concept_map": {"center": "Central Concept",
                    "satellites": [LONG % i for i in range(8)]},
    "mind_map": {"center": "Central Idea",
                 "branches": [{"name": "Branch %d" % i,
                               "children": ["child %d-%d long text" % (i, c)
                                            for c in range(4)]}
                              for i in range(6)]},
    "classification": {"root": "Root Category",
                       "items": ["item %d with some text" % i
                                 for i in range(6)]},
    "classification_g": {"root": "Root",
                         "groups": [{"name": "Group %d" % i,
                                     "items": ["it %d-%d" % (i, j)
                                              for j in range(4)]}
                                    for i in range(6)]},
    "infographic": {"points": [LONG % i for i in range(8)]},
    "panels": {"panels": [{"text": LONG % i * 2} for i in range(4)]},
    "mechanism": {"stages": [{"label": LONG % i, "role": "source",
                              "role_text": "Source"} for i in range(8)]},
    "spatial_chain": {"links": [{"name_en": "Linktown %d" % i,
                                 "name_hi": "कड़ी %d" % i}
                                for i in range(6)]},
}

# Row gap absorbing text overhang per layout (0 = fits its own box).
TEXT_GAPS = {"comparison": 2.0, "cause_effect": 2.5,
             "infographic": 1.5, "classification": 4.0}


def decide(question, explanation=""):
    return viz.safe_decide_visual(question, (), explanation)


def make_spec(vtype, payload):
    return VisualSpec(visual_type=vtype, subject="geography",
                      title="Test Title for the Diagram Frame",
                      payload=payload)


class RuleMatrixCases(unittest.TestCase):
    def test_english_matrix(self):
        self.assertEqual(set(EN_MATRIX), viz.SUPPORTED_TYPES)
        for vtype, (question, expl) in EN_MATRIX.items():
            with self.subTest(vtype=vtype):
                spec = decide(question, expl)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, vtype)

    def test_hindi_matrix(self):
        self.assertEqual(set(HI_MATRIX), viz.SUPPORTED_TYPES)
        for vtype, (question, expl) in HI_MATRIX.items():
            with self.subTest(vtype=vtype):
                spec = decide(question, expl)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, vtype)

    def test_hinglish_samples(self):
        for vtype, (question, expl) in HINGLISH.items():
            with self.subTest(vtype=vtype):
                spec = decide(question, expl)
                self.assertIsNotNone(spec)
                self.assertEqual(spec.visual_type, vtype)

    def test_classification_bullets_variant(self):
        spec = decide("Classify the rocks with examples.",
                      "Types of rocks:\n• Igneous\n• Sedimentary\n"
                      "• Metamorphic")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")

    def test_classification_hindi_header_bullets(self):
        spec = decide("चट्टानों के प्रकार बताइए।",
                      "चट्टानों के प्रकार:\n• आग्नेय चट्टानें।\n"
                      "• अवसादी चट्टानें।\n• रूपांतरित चट्टानें।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")

    def test_numbered_cycle_stages_read_linear(self):
        # Bare numbered steps are faithful as a flowchart even when
        # the question names a cycle (no circularity asserted).
        spec = decide("Describe the water cycle stages.",
                      "The water cycle works thus:\n"
                      "1. Evaporation happens.\n"
                      "2. Condensation happens.\n"
                      "3. Precipitation happens.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "flowchart")

    def test_plain_steps_default_flowchart(self):
        spec = decide("What are the steps?",
                      "1. First do this thing.\n"
                      "2. Then do that thing.\n3. Finally finish it.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "flowchart")


class EvidenceCases(unittest.TestCase):
    def test_all_types_carry_intent_evidence(self):
        for vtype, (question, expl) in EN_MATRIX.items():
            with self.subTest(vtype=vtype):
                first = decide(question, expl)
                second = decide(question, expl)
                self.assertTrue(first.evidence)
                self.assertTrue(first.evidence[0].startswith("intent="))
                self.assertEqual(first.to_dict(), second.to_dict())


class HeightContractCases(unittest.TestCase):
    def test_estimate_matches_layout_exactly(self):
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                spec = make_spec(vtype, MAXIMAL[vtype])
                _els, body_h = templates._layout_for_spec(
                    spec, 0.0, 0.0, BODY_W)
                want = round(templates.TITLE_H + 2.0 + body_h + 2.0
                             + templates.BRAND_H, 1)
                self.assertEqual(
                    templates.estimate_height_for_spec(spec, BODY_W + 6.0),
                    want)

    def test_drawn_content_never_exceeds_body(self):
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                spec = make_spec(vtype, MAXIMAL[vtype])
                els, body_h = templates._layout_for_spec(
                    spec, 0.0, 0.0, BODY_W)
                bottoms = [e["box"].y + e["box"].h for e in els
                           if e["k"] == "box"]
                bottoms += [max(e["p1"][1], e["p2"][1]) for e in els
                            if e["k"] in ("line", "arrow")]
                bottoms += [e["c"][1] + e["r"] for e in els
                            if e["k"] == "dot"]
                self.assertLessEqual(max(bottoms), body_h + 1e-9)

    def test_draw_at_exact_estimate_all_types(self):
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                spec = make_spec(vtype, MAXIMAL[vtype])
                est = templates.estimate_height_for_spec(spec, BODY_W + 6.0)
                templates.draw_diagram(
                    FakePDF(), spec, (10.0, 10.0, BODY_W + 6.0, est))

    def test_grouped_classification_height(self):
        spec = make_spec("classification", MAXIMAL["classification_g"])
        els, body_h = templates._layout_for_spec(spec, 0.0, 0.0, BODY_W)
        bottom = max(e["box"].y + e["box"].h for e in els
                     if e["k"] == "box")
        self.assertLessEqual(bottom, body_h + 1e-9)
        est = templates.estimate_height_for_spec(spec, BODY_W + 6.0)
        templates.draw_diagram(FakePDF(), spec,
                               (10.0, 10.0, BODY_W + 6.0, est))

    def test_maps_draw_on_all_bases(self):
        places = viz.load_places()["places"]
        by_region: dict[str, list] = {}
        for place in places:
            by_region.setdefault(place["region"], []).append(place)
        by_region["africa"] = [
            {"id": "t", "name_en": "T", "name_hi": "ट", "kind": "city",
             "lon": 20.0, "lat": 10.0}]
        indus = viz.load_routes()["routes"][3]
        for base_id, shown in (("india", by_region["india"][:6]),
                               ("world", by_region["world"][:6]),
                               ("africa", by_region["africa"])):
            with self.subTest(base=base_id):
                height = mapdraw.suggest_height(base_id, 170.0)
                routes = [indus] if base_id == "india" else []
                mapdraw.draw_map(FakePDF(), base_id=base_id,
                                 places=shown,
                                 rect=(10.0, 10.0, 170.0, height),
                                 title="T", routes=routes)


class GeometryCases(unittest.TestCase):
    def _boxes(self, vtype):
        spec = make_spec(vtype, MAXIMAL[vtype])
        els, body_h = templates._layout_for_spec(spec, 0.0, 0.0, BODY_W)
        return ([e["box"] for e in els if e["k"] == "box"], body_h,
                [e for e in els if e["k"] != "box"])

    @staticmethod
    def _contains(outer, inner):
        return (outer.x <= inner.x + 1e-9
                and outer.y <= inner.y + 1e-9
                and inner.x + inner.w <= outer.x + outer.w + 1e-9
                and inner.y + inner.h <= outer.y + outer.h + 1e-9)

    def test_boxes_in_bounds(self):
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                boxes, body_h, _rest = self._boxes(vtype)
                for box in boxes:
                    self.assertGreaterEqual(box.x, -1e-9)
                    self.assertGreaterEqual(box.y, -1e-9)
                    self.assertLessEqual(box.x + box.w, BODY_W + 1e-9)
                    self.assertLessEqual(box.y + box.h, body_h + 1e-9)

    def test_no_partial_box_overlaps(self):
        # Panels nest chips inside panel boxes (containment by
        # design); every other pair must stay disjoint.
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                boxes, _h, _rest = self._boxes(vtype)
                for i in range(len(boxes)):
                    for j in range(i + 1, len(boxes)):
                        if not templates.boxes_overlap(boxes[i], boxes[j]):
                            continue
                        nested = (self._contains(boxes[i], boxes[j])
                                  or self._contains(boxes[j], boxes[i]))
                        self.assertTrue(
                            nested, "%s boxes %d/%d partially overlap"
                            % (vtype, i, j))

    def test_connectors_stay_in_container(self):
        pad = templates.FRAME_PAD
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                _boxes, body_h, rest = self._boxes(vtype)
                for element in rest:
                    if element["k"] in ("line", "arrow"):
                        pts = [element["p1"], element["p2"]]
                    else:
                        cx, cy = element["c"]
                        pts = [(cx - element["r"], cy - element["r"]),
                               (cx + element["r"], cy + element["r"])]
                    for px, py in pts:
                        self.assertGreaterEqual(px, -pad - 1e-9)
                        self.assertLessEqual(px, BODY_W + pad + 1e-9)
                        self.assertGreaterEqual(py, -pad - 1e-9)
                        self.assertLessEqual(py, body_h + pad + 1e-9)

    def test_text_fits_box_plus_gap(self):
        for vtype in DIAGRAM_TYPES:
            with self.subTest(vtype=vtype):
                spec = make_spec(vtype, MAXIMAL[vtype])
                els, _h = templates._layout_for_spec(spec, 0.0, 0.0, BODY_W)
                gap = TEXT_GAPS.get(vtype, 0.0)
                for element in els:
                    if element["k"] != "box" or not element["text"]:
                        continue
                    need = (element.get("size", 8.5)
                            * 0.5 * element.get("max_lines", 2)
                            + 1.2 - element["box"].h)
                    self.assertLessEqual(
                        need, gap + 1e-9,
                        "%s text overhang %.1fmm" % (vtype, need))

    def test_year_chip_single_line(self):
        els, _h = templates.layout_timeline(3, 0.0, 0.0, BODY_W)
        chips = [e for e in els if e["k"] == "box"
                 and e["text"].startswith("__YEAR__")]
        self.assertEqual(len(chips), 3)
        for chip in chips:
            self.assertEqual(chip["max_lines"], 1)

    def test_map_labels_stay_in_fitted_box(self):
        base = mapdraw.get_base("world")
        box = mapdraw._fitted_box(
            base["bbox"], (10.0, 10.0, 170.0, 118.0))
        places = [p for p in viz.load_places()["places"]
                  if p["region"] == "world"][:6]
        markers = [mapdraw.project(p["lon"], p["lat"], base["bbox"], box)
                   for p in places]
        labels = [([p["name_en"]], [7.0]) for p in places]
        placed = mapdraw._place_labels(FakePDF(), markers, labels, box)
        self.assertEqual(len(placed), 6)
        for i, (lx, ly, lw, lh) in enumerate(placed):
            with self.subTest(place=places[i]["id"]):
                self.assertGreaterEqual(lx, box[0] - 1e-9)
                self.assertGreaterEqual(ly, box[1] - 1e-9)
                self.assertLessEqual(lx + lw, box[0] + box[2] + 1e-9)
                self.assertLessEqual(ly + lh, box[1] + box[3] + 1e-9)


class PdfRenderCases(unittest.TestCase):
    def _q(self, stem, expl):
        return {"question": stem,
                "options": ["A1", "A2", "A3", "A4"],
                "correct_option_id": 0, "explanation": expl}

    def test_all_types_render_badges(self):
        import tempfile

        import fitz

        from pdf_service.render import render_testseries_pdf
        questions = [self._q(question, expl)
                     for _vtype, (question, expl) in EN_MATRIX.items()]
        questions.append(self._q(*HI_MATRIX["location_map"]))
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "d.pdf")
            render_testseries_pdf(
                questions, exam_title="D", tagline="Test Series",
                quiz_names=["QZ"], solution_display="end",
                output_path=out)
            with fitz.open(out) as doc:
                text = "\n".join(p.get_text() for p in doc)
        for vtype in DIAGRAM_TYPES:
            self.assertIn(BADGES[vtype], text, vtype)
        self.assertEqual(text.count("Not to scale"), 3)
        self.assertIn("सांभर", text)


if __name__ == "__main__":
    unittest.main()