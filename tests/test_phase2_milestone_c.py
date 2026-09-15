"""Phase 2 milestone C: sourced river routes on the map.

Nine Natural Earth (public domain, v5.0) river courses vendored via
the deterministic dev-time builder; Mahanadi stays an honest gap
(absent from NE 50m/10m). Routes attach when mentioned and drawable
(>=2 vertices inside the chosen base) and draw clipped to the map
frame. Offline and deterministic throughout.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from pdf_service.viz import engine as viz
from pdf_service.viz import mapdraw
from tests.test_phase2_milestone_b import FakePDF

VIZ_DIR = Path(__file__).resolve().parent.parent / "pdf_service" / "viz"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "c_build_routes", VIZ_DIR / "tools" / "build_routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()

# route id -> (vertices, termini, place_ids): full regression pins.
PINNED = {
    "brahmaputra": (5, [90.6, 23.5], [95.4, 28.0],
                    ["brahmaputra", "dhaka", "guwahati", "itanagar"]),
    "ganga": (9, [79.8, 30.9], [89.4, 21.7],
              ["dehradun", "ganga", "haridwar", "nandadevi", "patna",
               "prayagraj", "varanasi"]),
    "godavari": (7, [73.7, 20.0], [81.7, 16.3], ["godavari"]),
    "indus": (13, [67.5, 24.0], [79.4, 32.7], ["indus", "leh"]),
    "kaveri": (5, [75.7, 12.3], [78.7, 10.9], ["kaveri"]),
    "krishna": (8, [73.8, 18.0], [80.9, 15.8], ["krishna"]),
    "narmada": (7, [73.1, 21.8], [81.6, 22.8], ["narmada"]),
    "tapi": (5, [72.7, 21.1], [78.1, 21.7], ["tapi"]),
    "yamuna": (5, [78.3, 30.9], [81.9, 25.4],
               ["agra", "dehradun", "delhi", "panipat", "prayagraj",
                "tajmahal", "yamuna"]),
}


def decide(question, explanation="", **kwargs):
    return viz.safe_decide_visual(question, (), explanation, **kwargs)


class RouteDataCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = {r["id"]: r
                      for r in viz.load_routes()["routes"]}
        cls.places = {p["id"]: p
                      for p in viz.load_places()["places"]}
        cls.india = viz.load_base_maps()["bases"]["india"]["bbox"]

    def test_nine_rivers_no_mahanadi(self):
        self.assertEqual(sorted(self.routes), sorted(PINNED))
        self.assertNotIn("mahanadi", self.routes)

    def test_vertices_termini_places_pinned(self):
        for rid, (count, start, end, places) in PINNED.items():
            route = self.routes[rid]
            self.assertEqual(len(route["vertices"]), count, rid)
            self.assertEqual(route["vertices"][0], start, rid)
            self.assertEqual(route["vertices"][-1], end, rid)
            self.assertEqual(route["place_ids"], places, rid)

    def test_sources_are_traceable(self):
        for rid, route in self.routes.items():
            src = route["source"]
            self.assertTrue(src.startswith("Natural Earth 5.0 ne_"),
                            src)
            self.assertIn("main course, %d vertices"
                          % len(route["vertices"]), src)

    def test_names_copied_from_places(self):
        for rid, route in self.routes.items():
            place = self.places[rid]
            self.assertEqual(route["name_en"], place["name_en"])
            self.assertEqual(route["name_hi"], place["name_hi"])
            self.assertEqual(route["match_en"], place["match_en"])
            self.assertEqual(route["match_hi"], place["match_hi"])

    def test_only_indus_leaves_the_india_frame(self):
        for rid, route in self.routes.items():
            outside = [v for v in route["vertices"]
                       if not viz.point_in_bbox(v[0], v[1],
                                                self.india)]
            if rid == "indus":
                self.assertEqual(len(outside), 2, rid)
            else:
                self.assertEqual(outside, [], rid)

    def test_find_routes_hindi_real_data(self):
        self.assertEqual([r["id"] for r in viz.find_routes(
            "गंगा कहाँ बहती है?")], ["ganga"])


class BuilderUnitCases(unittest.TestCase):
    def test_fork_takes_longest_branch(self):
        segs = [[(0.0, 0.0), (5.0, 0.0)],
                [(5.0, 0.0), (9.0, 0.0)],
                [(5.0, 0.0), (5.0, 2.0)]]
        points, used, dropped = BUILDER.longest_course(segs)
        self.assertEqual(used, 2)
        self.assertEqual(dropped, [])
        self.assertEqual(points[0], [0.0, 0.0])
        self.assertEqual(points[-1], [9.0, 0.0])

    def test_tie_break_is_deterministic(self):
        segs = [[(0.0, 0.0), (4.0, 0.0)],
                [(0.0, 0.0), (0.0, 4.0)]]
        first = BUILDER.longest_course(segs)
        second = BUILDER.longest_course(list(reversed(segs)))
        self.assertEqual(first[0], second[0])

    def test_orientation_starts_west(self):
        segs = [[(9.0, 1.0), (3.0, 1.0)]]
        points, used, _dropped = BUILDER.longest_course(segs)
        self.assertEqual(used, 1)
        self.assertEqual(points, [[3.0, 1.0], [9.0, 1.0]])

    def test_detached_piece_reported(self):
        segs = [[(0.0, 0.0), (6.0, 0.0)],
                [(20.0, 20.0), (21.0, 20.0)]]
        points, used, dropped = BUILDER.longest_course(segs)
        self.assertEqual(used, 1)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0], (20.0, 20.0, 21.0, 20.0))
        self.assertEqual(points[-1], [6.0, 0.0])

    def test_loop_without_endpoints_is_none(self):
        segs = [[(0.0, 0.0), (1.0, 0.0)],
                [(1.0, 0.0), (1.0, 1.0)],
                [(1.0, 1.0), (0.0, 0.0)]]
        points, used, _dropped = BUILDER.longest_course(segs)
        self.assertIsNone(points)
        self.assertEqual(used, 0)

    def test_degenerate_segments_skipped(self):
        points, used, _dropped = BUILDER.longest_course(
            [[(0.0, 0.0)], [(2.0, 2.0), (5.0, 2.0)]])
        self.assertEqual(used, 1)
        self.assertEqual(points, [[2.0, 2.0], [5.0, 2.0]])

    def test_course_distance(self):
        line = [[0.0, 0.0], [4.0, 0.0]]
        self.assertEqual(BUILDER.course_distance(2.0, 0.0, line), 0.0)
        self.assertEqual(BUILDER.course_distance(2.0, 3.0, line), 3.0)


class ClipCases(unittest.TestCase):
    BBOX = [2.0, -1.0, 8.0, 1.0]

    def test_clip_inside_outside_crossing(self):
        inside = mapdraw._clip_segment(3.0, 0.0, 5.0, 0.0, self.BBOX)
        self.assertEqual(inside, ((3.0, 0.0), (5.0, 0.0)))
        self.assertIsNone(mapdraw._clip_segment(20.0, 0.0, 30.0, 0.0,
                                               self.BBOX))
        crossed = mapdraw._clip_segment(0.0, 0.0, 10.0, 0.0, self.BBOX)
        self.assertEqual(crossed, ((2.0, 0.0), (8.0, 0.0)))

    def test_clip_degenerate(self):
        self.assertEqual(
            mapdraw._clip_segment(3.0, 0.0, 3.0, 0.0, self.BBOX),
            ((3.0, 0.0), (3.0, 0.0)))
        self.assertIsNone(
            mapdraw._clip_segment(30.0, 0.0, 30.0, 0.0, self.BBOX))

    def test_runs_fragment_on_reentry(self):
        runs = mapdraw._clip_runs(
            [[0.0, 0.0], [4.0, 0.0], [20.0, 0.0], [30.0, 0.0],
             [6.0, 0.0], [4.0, 0.0]], self.BBOX)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][0], (2.0, 0.0))
        self.assertEqual(runs[0][-1], (8.0, 0.0))
        self.assertEqual(runs[1][0], (8.0, 0.0))

    def test_runs_never_chord_outside_bulge(self):
        # A V dipping outside must not gain a shortcut segment: the
        # two clipped legs stay separate runs.
        runs = mapdraw._clip_runs(
            [[4.0, 0.0], [20.0, 5.0], [4.0, 0.5]], self.BBOX)
        self.assertEqual(len(runs), 2)

    def test_runs_fully_outside_is_empty(self):
        self.assertEqual(
            mapdraw._clip_runs([[20.0, 0.0], [30.0, 0.0]], self.BBOX),
            [])

    def _drawn(self, routes):
        fake = FakePDF()
        mapdraw.draw_map(fake, base_id="india", places=[],
                         rect=(10.0, 10.0, 170.0, 80.0), routes=routes)
        return fake.calls

    def test_clipped_end_gets_no_dot(self):
        calls = self._drawn([{"id": "r", "vertices": [[60.0, 25.0],
                                                      [70.0, 25.0],
                                                      [75.0, 25.0]]}])
        lines = [c for c in calls
                 if c[0] == "line" and tuple(c[1][:3]) == mapdraw.ROUTE]
        self.assertTrue(lines)  # the inside stretch still draws
        dots = [c for c in calls if c[0] == "ellipse"
                and tuple(c[1][:3]) == mapdraw.ROUTE]
        self.assertEqual(len(dots), 1)  # inside terminus only

    def test_stub_route_skipped(self):
        calls = self._drawn([{"id": "r", "vertices": [[60.0, 25.0],
                                                      [61.0, 25.0],
                                                      [75.0, 25.0]]}])
        lines = [c for c in calls
                 if c[0] == "line" and tuple(c[1][:3]) == mapdraw.ROUTE]
        self.assertEqual(lines, [])

    def test_inside_route_draws_two_dots(self):
        calls = self._drawn([{"id": "r", "vertices": [[70.0, 25.0],
                                                      [75.0, 25.0]]}])
        dots = [c for c in calls if c[0] == "ellipse"
                and tuple(c[1][:3]) == mapdraw.ROUTE]
        self.assertEqual(len(dots), 2)


class AttachGateCases(unittest.TestCase):
    def test_real_indus_attaches_despite_mouth_outside(self):
        spec = decide("Where does the Indus flow?",
                      "It flows via Leh down to the plains.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.payload["base"], "india")
        self.assertEqual([r["id"] for r in spec.payload["routes"]],
                         ["indus"])

    def test_single_inside_vertex_skipped(self):
        route = {"id": "stub", "name_en": "Stub", "name_hi": "",
                 "match_en": ["stub river"], "match_hi": ["स्टब"],
                 "place_ids": ["delhi"], "source": "test",
                 "vertices": [[77.0, 28.0], [200.0, 0.0],
                              [201.0, 1.0]]}
        with patch.object(viz, "load_routes",
                          return_value={"routes": [route]}):
            spec = decide("Where is Delhi?",
                          "The Stub river is far away.")
        self.assertEqual(spec.payload["routes"], [])

    def test_two_inside_vertices_attach(self):
        route = {"id": "ok", "name_en": "Ok", "name_hi": "",
                 "match_en": ["ok river"], "match_hi": ["ओके"],
                 "place_ids": ["delhi"], "source": "test",
                 "vertices": [[77.0, 28.0], [78.0, 28.0],
                              [200.0, 0.0]]}
        with patch.object(viz, "load_routes",
                          return_value={"routes": [route]}):
            spec = decide("Where is Delhi?",
                          "The Ok river is near.")
        self.assertEqual([r["id"] for r in spec.payload["routes"]],
                         ["ok"])

    def test_mahanadi_gap_is_dots_only(self):
        spec = decide("Where does the Mahanadi flow?",
                      "It flows east to the Bay of Bengal.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "location_map")
        self.assertEqual(spec.payload["routes"], [])

    def test_bare_krishna_stays_unmatched(self):
        # The dataset requires "Krishna river" (deity disambiguation);
        # routes reuse the same aliases, so both stay silent here.
        self.assertIsNone(decide("Where does the Krishna flow?"))
        spec = decide("Where does the Krishna river flow?")
        self.assertEqual([r["id"] for r in spec.payload["routes"]],
                         ["krishna"])


class CoverArbitrationCases(unittest.TestCase):
    def test_covering_route_map_wins_english(self):
        spec = decide("Trace the Ganga from Haridwar to Patna via "
                      "Prayagraj.",
                      "The Ganga flows from Haridwar via Prayagraj "
                      "to Patna.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([r["id"] for r in spec.payload["routes"]],
                         ["ganga"])
        self.assertIn("routes=1", spec.evidence)

    def test_covering_route_map_wins_hindi(self):
        spec = decide("हरिद्वार से पटना तक गंगा का मार्ग बताइए।",
                      "गंगा हरिद्वार से प्रयागराज होकर पटना तक "
                      "बहती है।")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "regional_map")
        self.assertEqual([r["id"] for r in spec.payload["routes"]],
                         ["ganga"])

    def test_non_covering_chain_still_wins(self):
        spec = decide("Trace the trade route from Delhi to Ahmedabad "
                      "via Jaipur.",
                      "Goods moved from Delhi to Ahmedabad via Jaipur; "
                      "the Ganga valley trade was separate.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")

    def test_routeless_river_keeps_chain(self):
        # Mahanadi has no sourced route, so no map can cover the
        # chain: the ordered links still visualise as a chain.
        spec = decide("Trace the Mahanadi trade link from Delhi to "
                      "Ahmedabad via Jaipur.",
                      "Goods moved from Delhi to Ahmedabad via Jaipur; "
                      "the Mahanadi delta received them.")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.visual_type, "spatial_chain")


class RenderWiringCases(unittest.TestCase):
    def _drawings(self, questions):
        import tempfile

        import fitz

        from pdf_service.render import render_testseries_pdf
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "c.pdf")
            render_testseries_pdf(
                questions, exam_title="C", tagline="Test Series",
                quiz_names=["QZ"], solution_display="end",
                output_path=out)
            with fitz.open(out) as doc:
                return (sum(len(p.get_drawings()) for p in doc),
                        "\n".join(p.get_text() for p in doc))

    def _q(self, stem, expl):
        return {"question": stem,
                "options": ["A1", "A2", "A3", "A4"],
                "correct_option_id": 0, "explanation": expl}

    def test_route_reaches_the_pdf(self):
        with_route, text = self._drawings([
            self._q("Where does the Ganga flow?",
                    "It flows via Varanasi and Patna.")])
        without, _plain = self._drawings([
            self._q("Where is Chilika lake?",
                    "Chilika is a brackish lagoon.")])
        self.assertIn("Not to scale", text)
        self.assertGreater(with_route, without)


if __name__ == "__main__":
    unittest.main(verbosity=2)
