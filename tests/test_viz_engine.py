"""Phase 3 milestone 1: visual-engine unit/smoke tests (no network).

Covers the static data layer, subject/place detection, accuracy-gated
visual decisions, deterministic layouts, FakePdf drawing and a real
fpdf2 smoke render with the vendored Hind fonts.

Milestone 1 is modules + data only: these tests also pin that the
engine is NOT wired into the live PDF flow yet.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz  # PyMuPDF; already a project dependency
from fpdf import FPDF

from pdf_service import viz
from pdf_service.render import FONT_BOLD, FONT_REGULAR
from pdf_service.viz import engine, mapdraw, templates, textstyle

ROOT = Path(__file__).resolve().parent.parent


class FakePdf:
    """Recording stand-in for the fpdf2 calls viz drawing uses."""

    CHAR_FACTOR = 0.45

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._font_size = 10.0

    def set_font(self, family, style="", size=10):
        self._font_size = float(size)
        self.calls.append(("set_font", family, style, float(size)))

    def get_string_width(self, text):
        return len(text) * self._font_size * self.CHAR_FACTOR

    def set_text_color(self, r, g, b):
        self.calls.append(("set_text_color", r, g, b))

    def set_draw_color(self, r, g, b):
        self.calls.append(("set_draw_color", r, g, b))

    def set_fill_color(self, r, g, b):
        self.calls.append(("set_fill_color", r, g, b))

    def set_line_width(self, w):
        self.calls.append(("set_line_width", w))

    def set_xy(self, x, y):
        self.calls.append(("set_xy", x, y))

    def cell(self, w, h, text="", align=""):
        self.calls.append(("cell", w, h, text, align))

    def rect(self, x, y, w, h, style="D"):
        self.calls.append(("rect", x, y, w, h, style))

    def line(self, x1, y1, x2, y2):
        self.calls.append(("line", x1, y1, x2, y2))

    def ellipse(self, x, y, w, h, style="D"):
        self.calls.append(("ellipse", x, y, w, h, style))

    def polygon(self, points, style="D"):
        self.calls.append(("polygon", tuple(points), style))


def _cells(fake):
    return [c for c in fake.calls if c[0] == "cell"]


class DataLayerCases(unittest.TestCase):
    def test_subjects_schema(self):
        data = viz.load_subjects()
        self.assertEqual(data["version"], 1)
        subjects = data["subjects"]
        self.assertGreaterEqual(len(subjects), 8)
        ids = [s["id"] for s in subjects]
        self.assertEqual(len(ids), len(set(ids)))
        for entry in subjects:
            self.assertTrue(entry["label_en"])
            self.assertTrue(entry["label_hi"])
            self.assertTrue(entry["keywords_en"])
            self.assertTrue(entry["keywords_hi"])
            self.assertTrue(entry["preferred"])
            self.assertTrue(set(entry["preferred"]) <= engine.ALL_TYPES)

    def test_supported_types(self):
        self.assertEqual(set(viz.SUPPORTED_TYPES),
                         set(engine.ALL_TYPES) - {"historical_map",
                                                  "labelled_diagram"})
        self.assertEqual(len(viz.SUPPORTED_TYPES), 12)
        union = set()
        for entry in viz.load_subjects()["subjects"]:
            union.update(entry["preferred"])
        # Every supported type is reachable: preferred lists cover all but
        # mind_map, which stays reachable via the generic fallback order.
        self.assertTrue(set(viz.SUPPORTED_TYPES) - {"mind_map"} <= union)
        self.assertIn("mind_map", engine.GENERIC_ORDER)

    def test_places_schema(self):
        data = viz.load_places()
        places = data["places"]
        self.assertGreaterEqual(len(places), 70)
        ids = [p["id"] for p in places]
        self.assertEqual(len(ids), len(set(ids)))
        kinds = {"city", "river", "mountain", "lake", "monument",
                 "battlefield", "landmark"}
        for place in places:
            self.assertTrue(place["name_en"])
            self.assertTrue(place["name_hi"])
            self.assertTrue(place["match_en"])
            self.assertTrue(place["match_hi"])
            self.assertIn(place["kind"], kinds)
            self.assertTrue(-180 <= place["lon"] <= 180)
            self.assertTrue(-90 <= place["lat"] <= 90)

    def test_places_match_region_base_or_flagged(self):
        bases = viz.load_base_maps()["bases"]
        self.assertNotIn("world", bases, "no world base in milestone 1")
        for place in viz.load_places()["places"]:
            base = bases.get(place["region"])
            if base is None:
                continue  # accuracy rule: never mapped (tested elsewhere)
            self.assertTrue(engine.point_in_bbox(place["lon"], place["lat"],
                                                 base["bbox"]),
                            "%s outside its region bbox" % place["id"])

    def test_base_schema(self):
        base = viz.load_base_maps()["bases"]["india"]
        bbox = base["bbox"]
        self.assertLess(bbox[0], bbox[2])
        self.assertLess(bbox[1], bbox[3])
        self.assertTrue(base["simplified"])
        self.assertTrue(base["accuracy_note_en"])
        poly = base["polygons"][0]
        self.assertGreaterEqual(len(poly), 30)
        self.assertEqual(poly[0], poly[-1], "polygon must be closed")
        for lon, lat in poly:
            self.assertTrue(engine.point_in_bbox(lon, lat, bbox))
        self.assertGreaterEqual(len(base["islets"]), 1)

    def test_base_anchors(self):
        poly = viz.load_base_maps()["bases"]["india"]["polygons"][0]
        lons = [p[0] for p in poly]
        lats = [p[1] for p in poly]
        self.assertAlmostEqual(min(lons), 68.2, delta=0.35)   # Sir Creek
        self.assertAlmostEqual(max(lons), 96.2, delta=0.35)   # Kibithu
        self.assertAlmostEqual(min(lats), 8.1, delta=0.35)    # Kanyakumari
        self.assertAlmostEqual(max(lats), 35.6, delta=0.35)   # Siachen

    def test_data_deterministic(self):
        viz.reload_data()
        first = (viz.load_subjects(), viz.load_places(),
                 viz.load_base_maps())
        viz.reload_data()
        second = (viz.load_subjects(), viz.load_places(),
                  viz.load_base_maps())
        self.assertEqual(first, second)

    def test_invalid_data_rejected(self):
        with self.assertRaises(ValueError):
            engine._validate_subjects({"subjects": [{
                "id": "x", "label_en": "X", "label_hi": "Y",
                "keywords_en": [], "keywords_hi": [],
                "preferred": ["no_such_type"]}]})
        with self.assertRaises(ValueError):
            engine._validate_places({"places": [{"id": "x"}]})
        with self.assertRaises(ValueError):
            engine._validate_bases({"bases": {"b": {
                "label_en": "B", "label_hi": "B",
                "bbox": [0, 0, 1, 1]}}})


class ApiCases(unittest.TestCase):
    def test_public_api(self):
        self.assertEqual(viz.__version__, "0.1.0")
        for name in ("decide_visual", "safe_decide_visual",
                     "detect_subject", "find_places", "usable_places",
                     "load_subjects", "load_places", "load_base_maps"):
            self.assertTrue(callable(getattr(viz, name)), name)

    def test_wired_only_into_pdf_renderer(self):
        """Milestone 2: wired into render.py solution sections ONLY.

        The API layer, the bots and the launcher stay viz-free so the
        PDF API contract and the single-bot architecture are untouched.
        """
        wired = (ROOT / "pdf_service" / "render.py").read_text()
        self.assertIn("pdf_service.viz", wired)
        self.assertIn("_maybe_solution_visual", wired)
        for rel in ("pdf_service/app.py",
                    "quizbot/creator_bot/handlers/reports.py",
                    "quizbot/runner_bot/handlers/reports.py",
                    "pdf_service/__init__.py", "run.py"):
            text = (ROOT / rel).read_text()
            for marker in ("pdf_service.viz", "pdf_service/viz",
                           "from .viz", "from ..viz", "import viz"):
                self.assertNotIn(marker, text, f"{rel} must not import viz")


class SubjectCases(unittest.TestCase):
    def test_geography_en(self):
        self.assertEqual(viz.detect_subject(
            "The Ganga river flows through Varanasi and Patna."), "geography")

    def test_geography_hi(self):
        self.assertEqual(viz.detect_subject(
            "गंगा नदी वाराणसी के पास बहती है।"), "geography")

    def test_history_en(self):
        self.assertEqual(viz.detect_subject(
            "The Battle of Plassey in 1757 established a new rule."),
            "history")

    def test_history_hi(self):
        self.assertEqual(viz.detect_subject(
            "पानीपत का तीसरा युद्ध 1761 में हुआ था।"), "history")

    def test_polity(self):
        self.assertEqual(viz.detect_subject(
            "Parliament passed an amendment to Article 370."), "polity")

    def test_economy(self):
        self.assertEqual(viz.detect_subject(
            "RBI raised the repo rate to control inflation."), "economy")

    def test_science(self):
        self.assertEqual(viz.detect_subject(
            "Photosynthesis occurs in the leaves using chlorophyll."),
            "science")

    def test_environment(self):
        self.assertEqual(viz.detect_subject(
            "The tiger reserve protects endangered species."), "environment")

    def test_art_culture(self):
        self.assertEqual(viz.detect_subject(
            "Bharatanatyam is a classical dance of Tamil Nadu."),
            "art_culture")

    def test_ir(self):
        self.assertEqual(viz.detect_subject(
            "The BRICS summit discussed bilateral trade."), "ir")

    def test_unknown(self):
        self.assertIsNone(viz.detect_subject(
            "Xyzzy blorps are generally considered quux."))
        self.assertIsNone(viz.detect_subject(""))
        self.assertIsNone(viz.detect_subject(None))

    def test_tie_is_deterministic(self):
        self.assertEqual(viz.detect_subject("treaty"), "history")
        self.assertEqual(viz.detect_subject("treaty"),
                         viz.detect_subject("treaty"))


class PlaceCases(unittest.TestCase):
    def test_find_en(self):
        self.assertEqual([p["id"] for p in
                          viz.find_places("Where is Chilika lake located?")],
                         ["chilika"])

    def test_find_hi(self):
        self.assertEqual([p["id"] for p in
                          viz.find_places("चिल्का झील कहाँ है?")], ["chilika"])

    def test_find_multi_sorted(self):
        self.assertEqual(
            [p["id"] for p in viz.find_places("Compare Mumbai, Delhi, Agra")],
            ["agra", "delhi", "mumbai"])

    def test_unknown_place(self):
        self.assertEqual(viz.find_places("Where is the city of Xyzabc?"), [])

    def test_no_false_hit_male(self):
        self.assertEqual(
            viz.find_places("The male gamete fuses with the egg."), [])

    def test_no_false_hit_dal(self):
        self.assertEqual(viz.find_places("Dal production rose sharply."), [])

    def test_usable_requires_base(self):
        london = viz.find_places("Where is London?")
        self.assertEqual([p["id"] for p in london], ["london"])
        self.assertEqual(viz.usable_places(london), [],
                         "world has no base map -> never mapped")
        delhi = viz.find_places("Delhi")
        self.assertEqual([p["id"] for p in viz.usable_places(delhi)],
                         ["delhi"])


class DecideMapCases(unittest.TestCase):
    def test_location_map(self):
        spec = viz.decide_visual("Where is Chilika lake?")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "location_map")
        self.assertEqual(spec.subject, "geography")
        self.assertEqual(spec.payload["base"], "india")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["chilika"])

    def test_regional_map(self):
        spec = viz.decide_visual("Name the lakes: Chilika, Sambhar, Wular.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([p["id"] for p in spec.payload["places"]],
                         ["chilika", "sambhar", "wular"])

    def test_unknown_place_no_visual(self):
        self.assertIsNone(viz.decide_visual(
            "Where is the city of Xyzabc located?"))

    def test_no_base_no_visual(self):
        self.assertIsNone(viz.decide_visual("Where is London?"))

    def test_extent_question_no_map(self):
        self.assertIsNone(viz.decide_visual(
            "Describe the extent of the Mauryan empire at its peak "
            "under Ashoka the Great."))

    def test_polity_steps_beat_incidental_place(self):
        spec = viz.decide_visual(
            "How is the President of India elected?",
            explanation=("1. The electoral college is formed.\n"
                         "2. MPs and MLAs vote in New Delhi.\n"
                         "3. Votes are counted.\n"
                         "4. The winner is declared."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "flowchart")
        self.assertEqual(len(spec.payload["steps"]), 4)

    def test_location_phrasing_overrides_subject(self):
        spec = viz.decide_visual("Where in Delhi is the Supreme Court?")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "location_map")
        self.assertEqual(spec.subject, "polity")


class DecideStructureCases(unittest.TestCase):
    def test_timeline(self):
        spec = viz.decide_visual(
            "Arrange the following events in chronological order.",
            explanation=("The revolt of 1857 shook the empire. "
                         "In 1919 came a massacre. "
                         "The Quit India movement followed in 1942."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "timeline")
        self.assertEqual([e["year"] for e in spec.payload["events"]],
                         [1857, 1919, 1942])
        self.assertTrue(all(e["label"] for e in spec.payload["events"]))

    def test_two_years_not_enough(self):
        self.assertIsNone(viz.decide_visual(
            "Compare 1919 with 1942 in detail please.",
            explanation="Both years shaped the freedom movement decisively."))

    def test_flowchart_steps(self):
        spec = viz.decide_visual(
            "List the steps to file an RTI application correctly.",
            explanation=("1. Write the application.\n"
                         "2. Pay the fee.\n"
                         "3. Submit to the officer.\n"
                         "4. Collect the reply."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "flowchart")
        self.assertEqual(len(spec.payload["steps"]), 4)

    def test_process_for_systems(self):
        spec = viz.decide_visual(
            "Describe the process of urine formation in detail.",
            explanation=("1. Filtration occurs first.\n"
                         "2. Reabsorption follows next.\n"
                         "3. Secretion happens after.\n"
                         "4. Excretion completes it."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "process")

    def test_comparison(self):
        spec = viz.decide_visual(
            "What is the difference between Lok Sabha and Rajya Sabha?",
            explanation=("\u2022 Lok Sabha members are directly elected.\n"
                         "\u2022 Rajya Sabha members are indirectly elected.\n"
                         "\u2022 Both form the Parliament of India."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "comparison")
        self.assertEqual(spec.payload["left_title"], "Lok Sabha")
        self.assertEqual(spec.payload["right_title"], "Rajya Sabha")
        self.assertEqual(len(spec.payload["left_points"]), 1)
        self.assertEqual(len(spec.payload["right_points"]), 1)
        self.assertEqual(len(spec.payload["common"]), 1)

    def test_cause_effect(self):
        spec = viz.decide_visual(
            "What are the causes and effects of deforestation in detail?",
            explanation=("Causes:\n\u2022 Cutting of trees\n\u2022 Mining\n"
                         "Effects:\n\u2022 Soil erosion\n\u2022 Climate change"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "cause_effect")
        self.assertEqual(len(spec.payload["causes"]), 2)
        self.assertEqual(len(spec.payload["effects"]), 2)

    def test_cycle(self):
        spec = viz.decide_visual(
            "Explain the water cycle with its stages in detail.",
            explanation=("Stages:\nEvaporation, Condensation, "
                         "Precipitation, Collection"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "cycle")
        self.assertEqual(len(spec.payload["stages"]), 4)

    def test_classification_flat(self):
        spec = viz.decide_visual(
            "What are the three types of rocks? Explain each briefly.",
            explanation=("\u2022 Igneous rocks form from magma.\n"
                         "\u2022 Sedimentary rocks form in layers.\n"
                         "\u2022 Metamorphic rocks transform under heat."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")
        self.assertEqual(spec.payload["root"], "rocks")
        self.assertEqual(len(spec.payload["items"]), 3)

    def test_classification_groups(self):
        spec = viz.decide_visual(
            "Classify the following soils with examples in detail.",
            explanation=("\u2022 Alluvial: Punjab plains, Gangetic delta\n"
                         "\u2022 Black: Deccan plateau, Maharashtra\n"
                         "\u2022 Red: Tamil Nadu, Karnataka"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "classification")
        groups = spec.payload["groups"]
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0]["name"], "Alluvial")
        self.assertEqual(groups[0]["items"],
                         ["Punjab plains", "Gangetic delta"])

    def test_mind_map(self):
        spec = viz.decide_visual(
            "Describe the Mughal administration and its key departments.",
            explanation=("\u2022 Central\n  - Emperor\n  - Wazir\n"
                         "\u2022 Provincial\n  - Subedar\n  - Diwan"))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "mind_map")
        self.assertEqual(len(spec.payload["branches"]), 2)

    def test_concept_map(self):
        spec = viz.decide_visual(
            "What does the Indian Constitution comprise? List the parts.",
            explanation=("The Indian Constitution comprises the Preamble, "
                         "Fundamental Rights, Directive Principles and "
                         "Schedules for the welfare of all citizens."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "concept_map")
        self.assertGreaterEqual(len(spec.payload["satellites"]), 3)

    def test_infographic_fallback(self):
        spec = viz.decide_visual(
            "Write short notes on the following provisions in detail.",
            explanation=("\u2022 Provision one ensures basic safeguards for "
                         "all citizens in every state of India.\n"
                         "\u2022 Provision two lays down directive goals "
                         "for the welfare state to follow.\n"
                         "\u2022 Provision three describes emergency powers "
                         "with proper parliamentary checks.\n"
                         "\u2022 Provision four covers amendment procedures "
                         "and their constitutional limits.\n"
                         "\u2022 Provision five lists schedules, subjects "
                         "and lists of the federation.\n"
                         "\u2022 Provision six explains tribunals, services "
                         "and special provisions clearly."))
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "infographic")
        self.assertEqual(len(spec.payload["points"]), 6)

    def test_short_text_no_visual(self):
        self.assertIsNone(viz.decide_visual("What is photosynthesis?"))

    def test_labelled_demand_without_data_no_visual(self):
        self.assertIsNone(viz.decide_visual(
            "Draw a labelled diagram of the human heart showing all "
            "chambers clearly."))

    def test_never_emits_unsupported(self):
        inputs = [
            "Draw a labelled diagram of the human heart in full detail.",
            "Describe the extent of the Mauryan empire under Ashoka fully.",
            "Where is London?",
            "Where is Chilika lake?",
            ("Arrange in order please.", "In 1857 revolt. In 1919 massacre. "
             "In 1942 movement."),
        ]
        for item in inputs:
            if isinstance(item, tuple):
                spec = viz.decide_visual(item[0], explanation=item[1])
            else:
                spec = viz.decide_visual(item)
            if spec is not None:
                self.assertIn(spec.visual_type, viz.SUPPORTED_TYPES)
                self.assertNotIn(spec.visual_type,
                                 {"historical_map", "labelled_diagram"})

    def test_deterministic(self):
        kwargs = {"question": "Where is Chilika lake?"}
        first = viz.decide_visual(**kwargs).to_dict()
        second = viz.decide_visual(**kwargs).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        json.dumps(first)  # payload must be JSON-serialisable

    def test_subject_hint(self):
        spec = viz.decide_visual("Where is Chilika lake?",
                                 subject_hint="polity")
        self.assertEqual(spec.subject, "polity")
        spec = viz.decide_visual("Where is Chilika lake?",
                                 subject_hint="nonsense")
        self.assertEqual(spec.subject, "geography")

    def test_safe_wrapper(self):
        spec = viz.safe_decide_visual("Where is Chilika lake?")
        self.assertIsNotNone(spec)
        with patch("pdf_service.viz.engine.load_subjects",
                   side_effect=RuntimeError("boom")):
            self.assertIsNone(viz.safe_decide_visual("Where is Chilika?"))


class TextStyleCases(unittest.TestCase):
    def test_clean_label(self):
        self.assertEqual(textstyle.clean_label("  hello\nworld  "),
                         "hello world")
        self.assertTrue(textstyle.has_devanagari("भारत की राजधानी"))
        self.assertFalse(textstyle.has_devanagari("Capital of India"))

    def test_wrap_lines(self):
        fake = FakePdf()
        lines = textstyle.wrap_lines(fake, "one two three four", 45.0, 10.0)
        self.assertEqual(lines, ["one two", "three four"])
        lines = textstyle.wrap_lines(fake, "one two three four", 45.0, 10.0,
                                     max_lines=1)
        self.assertEqual(lines, ["one two\u2026"])

    def test_fit_size(self):
        fake = FakePdf()
        self.assertEqual(textstyle.fit_size(fake, "x" * 20, 45.0, 10.0), 6.0)
        self.assertEqual(textstyle.fit_size(fake, "abc", 45.0, 10.0), 10.0)

    def test_put_label(self):
        fake = FakePdf()
        bottom = textstyle.put_label(fake, 0, 10, 45.0, "a b c d e f", 10.0)
        self.assertAlmostEqual(bottom, 20.0)
        self.assertEqual(len(_cells(fake)), 2)

    def test_badge_label(self):
        self.assertEqual(textstyle.badge_label("timeline"), "Timeline")
        self.assertEqual(textstyle.badge_label("mystery_x"), "Mystery X")


class LayoutCases(unittest.TestCase):
    def _check(self, elements, x, y, w, h):
        boxes = [e["box"] for e in elements if e["k"] == "box"]
        self.assertTrue(boxes)
        for box in boxes:
            self.assertGreaterEqual(box.x, x - 1e-6)
            self.assertGreaterEqual(box.y, y - 1e-6)
            self.assertLessEqual(box.x + box.w, x + w + 1e-6)
            self.assertLessEqual(box.y + box.h, y + h + 1e-6)
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                self.assertFalse(
                    templates.boxes_overlap(boxes[i], boxes[j]),
                    "boxes %d and %d overlap" % (i, j))

    def test_boxes_overlap(self):
        a = templates.Box(0, 0, 10, 10)
        self.assertTrue(templates.boxes_overlap(a, templates.Box(5, 5, 9, 9)))
        self.assertFalse(templates.boxes_overlap(a, templates.Box(10, 0, 5, 5)))
        self.assertFalse(templates.boxes_overlap(a, templates.Box(20, 20, 2, 2)))

    def test_steps(self):
        elements, height = templates.layout_steps(4, 0, 0, 164)
        self.assertAlmostEqual(height, 49.0)
        self._check(elements, 0, 0, 164, height)

    def test_timeline(self):
        elements, height = templates.layout_timeline(3, 0, 0, 164)
        self.assertAlmostEqual(height, 42.0)
        self._check(elements, 0, 0, 164, height)

    def test_comparison(self):
        elements, height = templates.layout_comparison(2, 1, 1, 0, 0, 164)
        self.assertAlmostEqual(height, 39.0)
        self._check(elements, 0, 0, 164, height)

    def test_cause_effect(self):
        elements, height = templates.layout_cause_effect(3, 2, 0, 0, 164)
        self.assertAlmostEqual(height, 42.5)
        self._check(elements, 0, 0, 164, height)

    def test_cycle(self):
        elements, height = templates.layout_cycle(5, 0, 0, 164)
        self.assertAlmostEqual(height, 58.0)
        self._check(elements, 0, 0, 164, height)

    def test_concept(self):
        elements, height = templates.layout_concept(5, 0, 0, 164)
        self.assertAlmostEqual(height, 26.0)
        self._check(elements, 0, 0, 164, height)

    def test_mind(self):
        elements, height = templates.layout_mind([2, 1, 3], 0, 0, 164)
        self.assertAlmostEqual(height, 63.5)
        self._check(elements, 0, 0, 164, height)

    def test_classification_flat(self):
        elements, height = templates.layout_classification([4], 0, 0, 164)
        self.assertAlmostEqual(height, 21.0)
        self._check(elements, 0, 0, 164, height)

    def test_classification_grouped(self):
        elements, height = templates.layout_classification_grouped(
            [2, 3], 0, 0, 164)
        self.assertAlmostEqual(height, 44.0)
        self._check(elements, 0, 0, 164, height)

    def test_infographic(self):
        elements, height = templates.layout_infographic(4, 0, 0, 164)
        self.assertAlmostEqual(height, 36.5)
        self._check(elements, 0, 0, 164, height)

    def test_estimate_matches_layout(self):
        spec = viz.decide_visual(
            "List the steps to file an RTI application correctly.",
            explanation=("1. Write the application.\n2. Pay the fee.\n"
                         "3. Submit to the officer.\n4. Collect the reply."))
        self.assertEqual(templates.estimate_height_for_spec(spec, 170), 65.0)


class DrawCases(unittest.TestCase):
    def test_frame(self):
        fake = FakePdf()
        body = templates.draw_frame(fake, (10, 20, 170, 60),
                                    "Title \u092a\u0930\u0940\u0915\u094d\u0937ण",
                                    "Timeline", "note")
        self.assertEqual(body, (13.0, 30.0, 164.0))
        rects = [c for c in fake.calls if c[0] == "rect"]
        self.assertGreaterEqual(len(rects), 2)
        texts = [c[3] for c in _cells(fake)]
        self.assertTrue(any("लबासना" in t for t in texts))
        self.assertTrue(any(t == "note" for t in texts))
        with self.assertRaises(ValueError):
            templates.draw_frame(FakePdf(), (0, 0, 30, 20), "t", "b")

    def test_map_draw(self):
        fake = FakePdf()
        chilika = [p for p in viz.load_places()["places"]
                   if p["id"] == "chilika"][0]
        mapdraw.draw_map(fake, base_id="india", places=[chilika],
                         rect=(10, 10, 120, 70), title="Chilika locator")
        kinds = {c[0] for c in fake.calls}
        self.assertIn("polygon", kinds)   # land outline
        self.assertIn("ellipse", kinds)   # marker
        texts = [c[3] for c in _cells(fake)]
        self.assertTrue(any("Chilika" in t for t in texts))
        self.assertTrue(any("Not to scale" in t for t in texts))
        self.assertTrue(any("km" in t for t in texts))
        self.assertTrue(any(t == "N" for t in texts))

    def test_map_draw_deterministic(self):
        places = viz.usable_places(
            viz.find_places("Chilika lake and Varanasi city"))
        first, second = FakePdf(), FakePdf()
        for fake in (first, second):
            mapdraw.draw_map(fake, base_id="india", places=places,
                             rect=(10, 10, 120, 70))
        self.assertEqual(first.calls, second.calls)

    def test_map_overlap_markers(self):
        places = viz.usable_places(viz.find_places("Taj Mahal Agra"))
        self.assertEqual(len(places), 2)
        first, second = FakePdf(), FakePdf()
        for fake in (first, second):
            mapdraw.draw_map(fake, base_id="india", places=places,
                             rect=(10, 10, 120, 70))
        self.assertEqual(first.calls, second.calls)
        self.assertGreaterEqual(len(_cells(first)), 7)

    def test_map_errors(self):
        with self.assertRaises(ValueError):
            mapdraw.draw_map(FakePdf(), base_id="atlantis", places=[],
                             rect=(0, 0, 120, 70))
        with self.assertRaises(ValueError):
            mapdraw.draw_map(FakePdf(), base_id="india", places=[],
                             rect=(0, 0, 40, 30))

    def test_diagram_draw_all_types(self):
        cases = [
            ("Arrange in order please.",
             "In 1857 revolt. In 1919 massacre. In 1942 movement."),
            ("List the steps to file an RTI application correctly.",
             "1. Write it.\n2. Pay fee.\n3. Submit it.\n4. Collect reply."),
            ("Describe the process of urine formation in detail.",
             "1. Filtration first.\n2. Reabsorption next.\n3. Secretion "
             "then.\n4. Excretion last."),
            ("What is the difference between Lok Sabha and Rajya Sabha?",
             "\u2022 Lok Sabha is direct.\n\u2022 Rajya Sabha is indirect."),
            ("What are the causes and effects of floods in detail?",
             "Causes:\n\u2022 Heavy rain\nEffects:\n\u2022 Damage"),
            ("Explain the water cycle with its stages in detail.",
             "Stages:\nEvaporation, Condensation, Precipitation"),
            ("What are the three types of rocks? Explain each briefly.",
             "\u2022 Igneous rocks.\n\u2022 Sedimentary rocks.\n"
             "\u2022 Metamorphic rocks."),
            ("Classify the soils with examples in detail.",
             "\u2022 Alluvial: plains, delta\n\u2022 Black: plateau"),
            ("Describe the Mughal rule and departments fully.",
             "\u2022 Central\n  - Emperor\n\u2022 Provincial\n  - Subedar"),
            ("What does the cell comprise? List the parts clearly.",
             "The cell comprises the nucleus, cytoplasm and membrane "
             "for the living functions of the body."),
            ("Write short notes on provisions in full detail please.",
             "\u2022 Point one about safeguards for all citizens here.\n"
             "\u2022 Point two about goals for the welfare state here.\n"
             "\u2022 Point three about powers with proper checks here.\n"
             "\u2022 Point four about procedures and limits here now.\n"
             "\u2022 Point five about schedules and subjects here now.\n"
             "\u2022 Point six about tribunals and services here now.\n"),
        ]
        seen = set()
        for question, explanation in cases:
            spec = viz.decide_visual(question, explanation=explanation)
            self.assertIsNotNone(spec, question)
            seen.add(spec.visual_type)
            height = templates.estimate_height_for_spec(spec, 170)
            fake = FakePdf()
            templates.draw_diagram(fake, spec, (10, 10, 170, height))
            rects = [c for c in fake.calls if c[0] == "rect"]
            self.assertGreaterEqual(len(rects), 2, question)
        self.assertEqual(seen, {"timeline", "flowchart", "process",
                                "comparison", "cause_effect", "cycle",
                                "classification", "mind_map", "concept_map",
                                "infographic"})

    def test_diagram_truncation_footer(self):
        years = " ".join("In %d event." % y
                         for y in range(1800, 1920, 10))
        spec = viz.decide_visual("Arrange all of these in order please.",
                                 explanation=years)
        self.assertEqual(spec.visual_type, "timeline")
        self.assertEqual(spec.payload["shown_of"], [10, 12])
        fake = FakePdf()
        templates.draw_diagram(
            fake, spec, (10, 10, 170,
                         templates.estimate_height_for_spec(spec, 170)))
        texts = [c[3] for c in _cells(fake)]
        self.assertTrue(any("showing 10 of 12" in t for t in texts))

    def test_diagram_errors(self):
        plan = viz.decide_visual("Where is Chilika lake?")
        with self.assertRaises(ValueError):
            templates.draw_diagram(FakePdf(), plan, (10, 10, 170, 60))
        bad = engine.VisualSpec("historical_map", None, "t", {}, ())
        with self.assertRaises(ValueError):
            templates.draw_diagram(FakePdf(), bad, (10, 10, 170, 60))
        flow = viz.decide_visual(
            "List the steps to file an RTI application correctly.",
            explanation="1. a\n2. b\n3. c\n4. d")
        with self.assertRaises(ValueError):
            templates.draw_diagram(FakePdf(), flow, (10, 10, 170, 10.0))


class SmokeCases(unittest.TestCase):
    def _doc(self):
        pdf = FPDF(orientation="P", unit="mm", format="A4")
        pdf.set_auto_page_break(True, margin=20)
        pdf.set_margins(15, 14, 15)
        pdf.add_font("hind", "", str(FONT_REGULAR))
        pdf.add_font("hind", "B", str(FONT_BOLD))
        try:
            pdf.set_text_shaping(True)
        except Exception:
            pass
        pdf.add_page()
        return pdf

    @staticmethod
    def _bytes(pdf):
        out = pdf.output()
        return bytes(out) if isinstance(out, (bytes, bytearray)) \
            else out.encode("latin-1")

    @staticmethod
    def _text(data):
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc)

    def test_smoke_map_pdf(self):
        pdf = self._doc()
        places = viz.usable_places(
            viz.find_places("Chilika lake and Varanasi city"))
        self.assertEqual(len(places), 2)
        mapdraw.draw_map(pdf, base_id="india", places=places,
                         rect=(15, 15, 180, 100), title="Chilika locator")
        data = self._bytes(pdf)
        self.assertTrue(data[:4] == b"%PDF")
        text = self._text(data)
        self.assertIn("Chilika", text)
        self.assertIn("लबासना", text)
        self.assertIn("Not to scale", text)

    def test_smoke_diagram_pdf(self):
        pdf = self._doc()
        spec = viz.decide_visual(
            "List the steps to file an RTI application correctly.",
            explanation=("1. Write the application.\n2. Pay the fee.\n"
                         "3. Submit to the officer.\n4. Collect the reply."))
        height = templates.estimate_height_for_spec(spec, 180)
        templates.draw_diagram(pdf, spec, (15, 15, 180, height))
        data = self._bytes(pdf)
        self.assertTrue(data[:4] == b"%PDF")
        text = self._text(data)
        self.assertIn("लबासना", text)
        self.assertIn("Collect the reply", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
