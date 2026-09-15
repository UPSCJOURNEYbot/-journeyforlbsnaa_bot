"""Phase 2 milestone B: world + africa bases, smallest-scale selection,
and the sourced-routes layer (data-gated: the routes file ships empty).

Offline and deterministic throughout: no network, no clocks, fixed
pins for every derived number.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from pdf_service.viz import engine as viz
from pdf_service.viz import mapdraw

VIZ_DIR = Path(__file__).resolve().parent.parent / "pdf_service" / "viz"

NEW_COUNTRY_IDS = [
    'afghanistan', 'algeria', 'australia', 'brazil', 'china', 'egypt',
    'ethiopia', 'france', 'indonesia', 'iran', 'iraq', 'israel', 'japan',
    'kazakhstan', 'kenya', 'kyrgyzstan', 'libya', 'mongolia', 'morocco',
    'myanmar', 'nigeria', 'russia', 'saudi_arabia', 'south_africa',
    'south_sudan', 'sudan', 'tajikistan', 'thailand', 'turkey',
    'turkmenistan', 'uganda', 'uk', 'usa', 'uzbekistan', 'vietnam',
]

WORLD_ROUTE = ("The route runs from London via Moscow to Beijing, "
               "then onward to Tokyo.")

# Synthetic routes for attachment/arbitration tests (patched in; the
# shipped routes file is empty). Vertices sit inside real base bboxes
# and place_ids resolve in the real dataset.
CORRIDOR = {
    "id": "test-corridor", "name_en": "Test Corridor",
    "name_hi": "टेस्ट गलियारा",
    "match_en": ["test corridor"], "match_hi": ["टेस्ट गलियारा"],
    "place_ids": ["delhi", "jaipur", "ahmedabad"],
    "source": "synthetic fixture (unit test)",
    "vertices": [[77.2, 28.6], [75.8, 26.9], [72.6, 23.0]],
}
EXPRESS = {
    "id": "test-express", "name_en": "Test Express", "name_hi": "",
    "match_en": ["test express"], "match_hi": ["टेस्ट एक्सप्रेस"],
    "place_ids": ["london", "moscow", "beijing", "tokyo"],
    "source": "synthetic fixture (unit test)",
    "vertices": [[-0.1, 51.5], [37.6, 55.8], [116.4, 39.9],
                 [139.7, 35.7]],
}


def decide(question, explanation="", **kwargs):
    return viz.safe_decide_visual(question, (), explanation, **kwargs)


def patched_routes(*routes):
    return patch.object(viz, "load_routes",
                        return_value={"routes": list(routes)})


class SelectBaseCases(unittest.TestCase):
    def test_india_pair_stays_india(self):
        spec = decide("Where are Delhi and Jaipur?")
        self.assertEqual(spec.payload["base"], "india")

    def test_world_pair_uses_world(self):
        spec = decide("Where are London and Tokyo?")
        self.assertEqual(spec.payload["base"], "world")

    def test_mixed_pair_escalates_to_world(self):
        # Regression: before select_base, the first place's region
        # won and London rendered off the India map.
        spec = decide("Where are London and Delhi located?")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.payload["base"], "world")
        self.assertEqual(spec.visual_type, "regional_map")

    def test_cairo_uses_smallest_base(self):
        spec = decide("Where is Cairo located?")
        self.assertEqual(spec.payload["base"], "africa")

    def test_africa_pair_uses_africa(self):
        spec = decide("Where are Cairo and Lagos?")
        self.assertEqual(spec.payload["base"], "africa")

    def test_africa_plus_india_escalates(self):
        spec = decide("Where are Cairo and Delhi?")
        self.assertEqual(spec.payload["base"], "world")

    def test_fallback_is_first_region(self):
        india_only = {"india": viz.load_base_maps()["bases"]["india"]}
        london = viz.find_places("London")
        with patch.object(viz, "load_base_maps",
                          return_value={"bases": india_only}):
            self.assertEqual(viz.select_base(london), "world")


class RoutesFileCases(unittest.TestCase):
    def test_shipped_file_is_empty_but_valid(self):
        self.assertEqual(viz.load_routes(), {"routes": []})

    def test_map_payload_always_has_routes_key(self):
        spec = decide("Where is London?")
        self.assertEqual(spec.payload["routes"], [])
        self.assertNotIn("routes=", "".join(spec.evidence))

    def _validated(self, entry):
        places = {p["id"]: p for p in viz.load_places()["places"]}
        bases = viz.load_base_maps()["bases"]
        return viz._validate_routes({"routes": [entry]}, places, bases)

    def test_validator_accepts_open_polyline(self):
        data = self._validated(CORRIDOR)
        self.assertEqual(data["routes"][0]["id"], "test-corridor")

    def test_validator_rejects(self):
        bad = dict(CORRIDOR)
        bad["vertices"] = [[77.2, 28.6]]  # single vertex
        with self.assertRaises(ValueError):
            self._validated(bad)
        bad = dict(CORRIDOR)
        bad["source"] = ""  # provenance is mandatory
        with self.assertRaises(ValueError):
            self._validated(bad)
        bad = dict(CORRIDOR)
        bad["place_ids"] = ["delhi", "atlantis"]  # unknown place
        with self.assertRaises(ValueError):
            self._validated(bad)
        bad = dict(CORRIDOR)
        bad["vertices"] = [[77.2, 28.6], [999.0, 0.0]]  # bad coords
        with self.assertRaises(ValueError):
            self._validated(bad)
        bad = dict(CORRIDOR)
        bad["match_en"] = []  # unmatchable
        with self.assertRaises(ValueError):
            self._validated(bad)


class RouteMatchingCases(unittest.TestCase):
    def test_find_routes_en_hi(self):
        with patched_routes(CORRIDOR, EXPRESS):
            self.assertEqual(
                [r["id"] for r in viz.find_routes(
                    "Take the Test Corridor south.")],
                ["test-corridor"])
            self.assertEqual(
                [r["id"] for r in viz.find_routes("टेस्ट गलियारा लें।")],
                ["test-corridor"])
            self.assertEqual(viz.find_routes("Take the train south."), [])

    def test_decide_attaches_mentioned_route(self):
        with patched_routes(CORRIDOR):
            spec = decide("Where are Delhi and Jaipur?",
                          "Both lie on the Test Corridor route.")
        self.assertEqual(
            [r["id"] for r in spec.payload["routes"]], ["test-corridor"])
        self.assertIn("routes=1", spec.evidence)
        self.assertEqual(
            spec.payload["routes"][0]["vertices"], CORRIDOR["vertices"])

    def test_unmentioned_route_not_attached(self):
        with patched_routes(CORRIDOR):
            spec = decide("Where are Delhi and Jaipur?")
        self.assertEqual(spec.payload["routes"], [])

    def test_route_outside_base_not_attached(self):
        # The express spans the world; an India-only map cannot draw it.
        with patched_routes(EXPRESS):
            spec = decide("Where are Delhi and Jaipur?",
                          "Ignore the Test Express here.")
        self.assertEqual(spec.payload["base"], "india")
        self.assertEqual(spec.payload["routes"], [])


class RouteArbitrationCases(unittest.TestCase):
    def test_covering_route_lets_map_win(self):
        expl = WORLD_ROUTE + " This Test Express connects them."
        with patched_routes(EXPRESS):
            spec = decide("Trace the route from London to Tokyo.", expl)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual(
            [r["id"] for r in spec.payload["routes"]], ["test-express"])

    def test_non_covering_route_chain_still_wins(self):
        expl = WORLD_ROUTE + " The Test Corridor is elsewhere."
        with patched_routes(CORRIDOR):
            spec = decide("Trace the route from London to Tokyo.", expl)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")
        self.assertEqual(
            [link["id"] for link in spec.payload["links"]],
            ["london", "moscow", "beijing", "tokyo"])

    def test_route_ask_without_route_keeps_chain(self):
        spec = decide("Trace the route from London to Tokyo.",
                      WORLD_ROUTE)
        self.assertEqual(spec.visual_type, "spatial_chain")

    def test_where_ask_keeps_map_over_chain(self):
        spec = decide("Where are Delhi, Jaipur and Ahmedabad on the "
                      "trade route via the plains?", "")
        self.assertEqual(spec.visual_type, "regional_map")


class FakePDF:
    """Minimal recording stand-in for the fpdf calls draw_map uses."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.color: tuple = (0, 0, 0)

    def _rec(self, name, *args):
        self.calls.append((name, args))

    def set_draw_color(self, *a):
        self.color = tuple(a)
        self._rec("draw", *a)

    def set_fill_color(self, *a):
        self._rec("fill", *a)

    def set_text_color(self, *a):
        pass

    def set_line_width(self, w):
        self._rec("width", w)

    def set_font(self, *a):
        pass

    def set_xy(self, *a):
        pass

    def get_string_width(self, text):
        return len(text) * 1.5

    def rect(self, *a, **k):
        self._rec("rect", *a)

    def polygon(self, *a, **k):
        self._rec("polygon", len(a[0]))

    def ellipse(self, *a, **k):
        self._rec("ellipse", *a)

    def line(self, *a):
        self._rec("line", *(self.color + tuple(a)))

    def cell(self, *a, **k):
        pass


class MapdrawRoutesCases(unittest.TestCase):
    RECT = (10.0, 10.0, 170.0, 78.0)

    def _draw(self, routes):
        fake = FakePDF()
        mapdraw.draw_map(fake, base_id="world", places=[], rect=self.RECT,
                         routes=routes)
        return fake.calls

    def test_country_label(self):
        self.assertEqual(mapdraw.KIND_LABELS["country"], "Country")

    def _route_lines(self, calls):
        return [c for c in calls
                if c[0] == "line" and tuple(c[1][:3]) == mapdraw.ROUTE]

    def test_route_draws_segments_and_dots(self):
        calls = self._draw([{"id": "r", "vertices": EXPRESS["vertices"]}])
        # 3 segments for 4 vertices (graticule lines differ in colour).
        self.assertEqual(len(self._route_lines(calls)), 3)
        dots = [c for c in calls if c[0] == "ellipse"]
        self.assertEqual(len(dots), 2)  # termini only, no places

    def test_route_outside_bbox_skipped_whole(self):
        routes = [{"id": "r",
                   "vertices": [[0.0, 0.0], [200.0, 0.0]]}]  # off-base
        calls = self._draw(routes)
        self.assertEqual(self._route_lines(calls), [])
        self.assertEqual([c for c in calls if c[0] == "ellipse"], [])

    def test_degenerate_route_skipped(self):
        calls = self._draw([{"id": "r", "vertices": [[0.0, 0.0]]}])
        self.assertEqual(self._route_lines(calls), [])

    def test_routes_default_to_none(self):
        fake = FakePDF()
        mapdraw.draw_map(fake, base_id="india", places=[], rect=self.RECT)
        self.assertTrue(fake.calls)

    def test_unknown_base_still_rejected(self):
        with self.assertRaises(ValueError):
            mapdraw.draw_map(FakePDF(), base_id="atlantis", places=[],
                             rect=self.RECT)


class DataInvariantCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bases = viz.load_base_maps()["bases"]
        cls.places = viz.load_places()["places"]

    def test_base_set_and_counts(self):
        self.assertEqual(set(self.bases), {"india", "world", "africa"})
        world = self.bases["world"]["polygons"]
        africa = self.bases["africa"]["polygons"]
        self.assertEqual((len(world), sum(len(p) for p in world)),
                         (186, 1757))
        self.assertEqual((len(africa), sum(len(p) for p in africa)),
                         (52, 586))
        self.assertEqual(
            {bid: self.bases[bid]["graticule_step"]
             for bid in ("india", "world", "africa")},
            {"india": 5, "world": 30, "africa": 10})

    def test_no_streak_segments(self):
        # No consecutive-vertex jump except the single by-design
        # Antarctica pole closure along the map edge.
        for bid in ("world", "africa"):
            for poly in self.bases[bid]["polygons"]:
                self.assertEqual(poly[0], poly[-1])
                for prev, cur in zip(poly, poly[1:]):
                    if abs(cur[0] - prev[0]) <= 300:
                        continue
                    pole = (abs(abs(prev[1]) - 90.0) < 1e-9
                            and abs(abs(cur[1]) - 90.0) < 1e-9)
                    self.assertTrue(
                        pole, "%s has a streak segment %s -> %s"
                        % (bid, prev, cur))

    def test_places_count_and_new_ids(self):
        self.assertEqual(len(self.places), 114)
        old_ids = {p["id"] for p in self.places[:79]}
        new_ids = sorted(p["id"] for p in self.places[79:])
        self.assertEqual(new_ids, NEW_COUNTRY_IDS)
        self.assertFalse(old_ids & set(new_ids))
        by_id = {p["id"]: p for p in self.places}
        for pid in NEW_COUNTRY_IDS:
            self.assertEqual(by_id[pid]["kind"], "country")
            self.assertEqual(by_id[pid]["region"], "world")

    def test_new_aliases_do_not_collide(self):
        old_alias = set()
        for place in self.places[:79]:
            old_alias.update(a.lower() for a in place["match_en"])
        for place in self.places[79:]:
            for alias in place["match_en"]:
                self.assertNotIn(alias.lower(), old_alias, alias)

    def test_spot_centroids(self):
        by_id = {p["id"]: p for p in self.places}
        self.assertEqual(
            (by_id["egypt"]["lon"], by_id["egypt"]["lat"]), (31.5, 28.2))
        self.assertEqual(
            (by_id["uk"]["lon"], by_id["uk"]["lat"]), (-2.9, 54.0))
        self.assertEqual(
            (by_id["usa"]["lon"], by_id["usa"]["lat"]), (-90.2, 38.3))


class HeightCases(unittest.TestCase):
    def test_suggest_height_pins(self):
        self.assertEqual(mapdraw.suggest_height("world", 170.0), 78.2)
        self.assertEqual(mapdraw.suggest_height("africa", 170.0), 80.0)
        self.assertEqual(mapdraw.suggest_height("india", 170.0), 80.0)


class RenderWiringCases(unittest.TestCase):
    def _pdf_text(self, questions):
        import tempfile

        from pdf_service.render import render_testseries_pdf
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "b.pdf")
            render_testseries_pdf(
                questions, exam_title="B", tagline="Test Series",
                quiz_names=["QZ"], solution_display="end",
                output_path=out)
            import fitz
            with fitz.open(out) as doc:
                return "\n".join(p.get_text() for p in doc)

    def _q(self, stem, expl):
        return {"question": stem,
                "options": ["A1", "A2", "A3", "A4"],
                "correct_option_id": 0, "explanation": expl}

    def test_world_map_renders(self):
        text = self._pdf_text([
            self._q("Where is London located?",
                    "London is the capital of the UK.")])
        self.assertIn("Not to scale", text)
        self.assertIn("London", text)

    def test_mixed_map_lists_both_places(self):
        text = self._pdf_text([
            self._q("Where are London and Delhi located?",
                    "London and Delhi are both national capitals.")])
        self.assertIn("Not to scale", text)
        self.assertIn("London", text)
        self.assertIn("Delhi", text)


class OfflineCases(unittest.TestCase):
    def test_runtime_modules_have_no_network_imports(self):
        for name in ("engine.py", "mapdraw.py"):
            src = (VIZ_DIR / name).read_text(encoding="utf-8")
            for token in ("urllib", "socket", "http.client", "requests"):
                self.assertNotIn(token, src, name)

    def test_builder_is_stdlib_only(self):
        import ast
        tree = ast.parse((VIZ_DIR / "tools" / "build_geo.py").read_text(
            encoding="utf-8"))
        # pdf_service = first-party local import (validator reuse);
        # everything else must be stdlib (no network, no third party).
        allowed = {"__future__", "argparse", "io", "json", "math",
                   "pathlib", "pdf_service", "sys", "tarfile"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed,
                                  alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertTrue(
                    (node.module or "").split(".")[0] in allowed
                    or node.level > 0, node.module)


if __name__ == "__main__":
    unittest.main(verbosity=2)
