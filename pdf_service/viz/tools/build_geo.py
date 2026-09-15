#!/usr/bin/env python3
"""Build vendored world/africa base maps + country places (DEV-TIME ONLY).

Provenance: ``countries.geo.json`` from johan/world.geo.json
(public domain, UNLICENSE), fetched once as a codeload tarball. This
script is never run at runtime: its outputs are merged into the
committed ``geo_base.json`` / ``geo_places.json`` (Phase 2, Rule 8 --
no runtime dependency on external data).

Pipeline (stdlib only, fully deterministic):

1. Load every country polygon (ISO_A3 feature ids), exterior rings
   only (holes are dropped: these are deliberately coarse teaching
   outlines, always captioned "Simplified outline - Not to scale").
2. Simplify rings with Douglas-Peucker, round to 0.1 degree (the
   precision the committed JSONs document), force closure, drop
   degenerate rings (< 4 points).
3. ``world`` base = all simplified polygons; ``africa`` base = the
   African Union ISO_A3 set. The existing ``india`` entry is
   preserved untouched.
4. Country places: marker = mean of the largest ring's vertices from
   the UNSIMPLIFIED source polygon; when that falls outside the ring,
   the nearest boundary vertex is used instead, so a marker always
   sits on/inside its own country. Coordinates are computed, never
   hand-written.
5. Merge + validate with the engine validators, then write.

Usage: python3 pdf_service/viz/tools/build_geo.py /path/to/world.tgz
"""

from __future__ import annotations

import json
import math
import sys
import tarfile
from pathlib import Path

EPS_WORLD = 1.0
EPS_AFRICA = 0.5

AFRICA_A3 = frozenset({
    "DZA", "AGO", "BEN", "BWA", "BFA", "BDI", "CMR", "CPV", "CAF",
    "TCD", "COM", "COG", "COD", "CIV", "DJI", "EGY", "GNQ", "ERI",
    "ETH", "GAB", "GMB", "GHA", "GIN", "GNB", "KEN", "LSO", "LBR",
    "LBY", "MDG", "MWI", "MLI", "MRT", "MAR", "MOZ", "NAM", "NER",
    "NGA", "RWA", "STP", "SEN", "SYC", "SLE", "SOM", "ZAF", "SSD",
    "SDN", "SWZ", "TZA", "TGO", "TUN", "UGA", "ZMB", "ZWE", "ESH",
})

# id, ISO_A3, name_en, name_hi, match_en, match_hi.
COUNTRIES = (
    ("egypt", "EGY", "Egypt", "मिस्र", ["egypt"], ["मिस्र"]),
    ("sudan", "SDN", "Sudan", "सूडान", ["sudan"], ["सूडान"]),
    ("south_sudan", "SSD", "South Sudan", "दक्षिण सूडान",
     ["south sudan"], ["दक्षिण सूडान"]),
    ("ethiopia", "ETH", "Ethiopia", "इथियोपिया", ["ethiopia"],
     ["इथियोपिया"]),
    ("uganda", "UGA", "Uganda", "युगांडा", ["uganda"], ["युगांडा"]),
    ("kazakhstan", "KAZ", "Kazakhstan", "कजाकिस्तान", ["kazakhstan"],
     ["कजाकिस्तान"]),
    ("uzbekistan", "UZB", "Uzbekistan", "उज्बेकिस्तान", ["uzbekistan"],
     ["उज्बेकिस्तान"]),
    ("turkmenistan", "TKM", "Turkmenistan", "तुर्कमेनिस्तान",
     ["turkmenistan"], ["तुर्कमेनिस्तान"]),
    ("kyrgyzstan", "KGZ", "Kyrgyzstan", "किर्गिस्तान", ["kyrgyzstan"],
     ["किर्गिस्तान"]),
    ("tajikistan", "TJK", "Tajikistan", "ताजिकिस्तान", ["tajikistan"],
     ["ताजिकिस्तान"]),
    ("afghanistan", "AFG", "Afghanistan", "अफगानिस्तान",
     ["afghanistan"], ["अफगानिस्तान"]),
    ("saudi_arabia", "SAU", "Saudi Arabia", "सऊदी अरब",
     ["saudi arabia", "saudi"], ["सऊदी अरब"]),
    ("iran", "IRN", "Iran", "ईरान", ["iran"], ["ईरान"]),
    ("iraq", "IRQ", "Iraq", "इराक", ["iraq"], ["इराक"]),
    ("israel", "ISR", "Israel", "इजराइल", ["israel"], ["इजराइल"]),
    ("turkey", "TUR", "Turkey", "तुर्की",
     ["turkey", "turkiye", "türkiye"], ["तुर्की", "तुर्किये"]),
    ("myanmar", "MMR", "Myanmar", "म्यांमार", ["myanmar", "burma"],
     ["म्यांमार", "बर्मा"]),
    ("thailand", "THA", "Thailand", "थाईलैंड", ["thailand"],
     ["थाईलैंड"]),
    ("vietnam", "VNM", "Vietnam", "वियतनाम", ["vietnam"],
     ["वियतनाम"]),
    ("indonesia", "IDN", "Indonesia", "इंडोनेशिया", ["indonesia"],
     ["इंडोनेशिया"]),
    ("china", "CHN", "China", "चीन", ["china"], ["चीन"]),
    ("japan", "JPN", "Japan", "जापान", ["japan"], ["जापान"]),
    ("mongolia", "MNG", "Mongolia", "मंगोलिया", ["mongolia"],
     ["मंगोलिया"]),
    ("south_africa", "ZAF", "South Africa", "दक्षिण अफ्रीका",
     ["south africa"], ["दक्षिण अफ्रीका"]),
    ("nigeria", "NGA", "Nigeria", "नाइजीरिया", ["nigeria"],
     ["नाइजीरिया"]),
    ("kenya", "KEN", "Kenya", "केन्या", ["kenya"], ["केन्या"]),
    ("libya", "LBY", "Libya", "लीबिया", ["libya"], ["लीबिया"]),
    ("algeria", "DZA", "Algeria", "अल्जीरिया", ["algeria"],
     ["अल्जीरिया"]),
    ("morocco", "MAR", "Morocco", "मोरक्को", ["morocco"],
     ["मोरक्को"]),
    ("usa", "USA", "United States", "संयुक्त राज्य अमेरिका",
     ["usa", "united states", "america"],
     ["संयुक्त राज्य अमेरिका", "अमेरिका"]),
    ("russia", "RUS", "Russia", "रूस", ["russia"], ["रूस"]),
    ("uk", "GBR", "United Kingdom", "यूनाइटेड किंगडम",
     ["uk", "united kingdom", "britain", "great britain"],
     ["यूनाइटेड किंगडम", "ब्रिटेन"]),
    ("france", "FRA", "France", "फ्रांस", ["france"], ["फ्रांस"]),
    ("australia", "AUS", "Australia", "ऑस्ट्रेलिया", ["australia"],
     ["ऑस्ट्रेलिया"]),
    ("brazil", "BRA", "Brazil", "ब्राजील", ["brazil"], ["ब्राजील"]),
)


def _perp_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    ratio = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    ratio = max(0.0, min(1.0, ratio))
    return math.hypot(px - (ax + ratio * dx), py - (ay + ratio * dy))


def simplify(points, eps):
    """Douglas-Peucker on an open point chain (endpoints kept)."""
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        ax, ay = points[start]
        bx, by = points[end]
        best_i, best_d = -1, eps
        for i in range(start + 1, end):
            dist = _perp_dist(points[i][0], points[i][1], ax, ay, bx, by)
            if dist > best_d:
                best_d, best_i = dist, i
        if best_i >= 0:
            keep[best_i] = True
            stack.append((start, best_i))
            stack.append((best_i, end))
    return [pt for pt, flag in zip(points, keep) if flag]


def _ring_area(ring):
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def _point_in_ring(lon, lat, ring):
    inside = False
    prev_lon, prev_lat = ring[-1]
    for lon_i, lat_i in ring:
        if (lat_i > lat) != (prev_lat > lat):
            intersect = (prev_lon + (lat - prev_lat)
                         * (lon_i - prev_lon) / (lat_i - prev_lat))
            if lon < intersect:
                inside = not inside
        prev_lon, prev_lat = lon_i, lat_i
    return inside


def _nearest_vertex(lon, lat, ring):
    return min(ring, key=lambda pt: (pt[0] - lon) ** 2 + (pt[1] - lat) ** 2)


def marker_for(rings):
    """Honest marker: vertex-mean of the largest ring, pulled back to
    the nearest boundary vertex when the mean falls outside."""
    largest = max(rings, key=lambda ring: abs(_ring_area(ring)))
    lon = sum(pt[0] for pt in largest) / len(largest)
    lat = sum(pt[1] for pt in largest) / len(largest)
    if not _point_in_ring(lon, lat, largest):
        lon, lat = _nearest_vertex(lon, lat, largest)
    return round(lon, 1), round(lat, 1)


def _rounded(ring):
    return [[round(lon, 1), round(lat, 1)] for lon, lat in ring]


def _split_jumps(ring):
    """Break a ring at antimeridian jumps (>300 deg neighbours).

    The ring is opened and rotated to start just after its largest
    jump, so every part's endpoints are a jump pair: closing each
    part then only bridges the small meridian gap instead of
    streaking across the map.
    """
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    jumps = [i for i in range(len(pts))
             if abs(pts[(i + 1) % len(pts)][0] - pts[i][0]) > 300]
    if not jumps:
        return [pts]
    widest = max(jumps, key=lambda i: abs(
        pts[(i + 1) % len(pts)][0] - pts[i][0]))
    ordered = pts[widest + 1:] + pts[:widest + 1]
    parts, current = [], [ordered[0]]
    for prev, pt in zip(ordered, ordered[1:]):
        if abs(pt[0] - prev[0]) > 300:
            parts.append(current)
            current = [pt]
        else:
            current.append(pt)
    parts.append(current)
    return parts


def _side(lon):
    return 180.0 if lon >= 0 else -180.0


def _simplify_ring(ring, eps):
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    raw = [(float(x), float(y)) for x, y in ring]
    had_jump = any(abs(b[0] - a[0]) > 300 for a, b in zip(raw, raw[1:]))
    out = []
    for part in _split_jumps(raw):
        if len(part) < 2:
            continue
        simple = _rounded(simplify(part, eps))
        if had_jump and len(simple) >= 2:
            # Antimeridian ring (in this source: only Antarctica's
            # coast, which encircles the pole): close via the pole so
            # the closure runs along the map edge instead of
            # streaking across the map.
            mean_lat = sum(pt[1] for pt in simple) / len(simple)
            pole = 90.0 if mean_lat >= 0 else -90.0
            simple.append([_side(simple[-1][0]), pole])
            simple.append([_side(simple[0][0]), pole])
            simple.append(list(simple[0]))
        else:
            simple[-1] = list(simple[0])
        if len(simple) >= 4:
            out.append(simple)
    return out


def load_countries(tarball):
    with tarfile.open(tarball, "r:gz") as tar:
        member = next(m for m in tar.getmembers()
                      if m.name.endswith("countries.geo.json"))
        data = json.load(tar.extractfile(member))
    out = {}
    for feature in data["features"]:
        code = feature["id"]
        geom = feature["geometry"]
        rings = []
        if geom["type"] == "Polygon":
            rings = [geom["coordinates"][0]]
        elif geom["type"] == "MultiPolygon":
            rings = [poly[0] for poly in geom["coordinates"]]
        # Extend (never overwrite): the source reuses "-99" twice.
        out.setdefault(code, []).extend(
            [[(float(x), float(y)) for x, y in ring] for ring in rings])
    return out


def build_base(rings_by_code, codes, eps):
    polys, dropped = [], 0
    for code in sorted(codes):
        for ring in rings_by_code[code]:
            simple = _simplify_ring(ring, eps)
            if simple:
                polys.extend(simple)
            else:
                dropped += 1
    return polys, dropped


def africa_bbox(polys):
    lons = [pt[0] for poly in polys for pt in poly]
    lats = [pt[1] for poly in polys for pt in poly]
    pad = 2.0
    return [math.floor((min(lons) - pad) * 10) / 10,
            math.floor((min(lats) - pad) * 10) / 10,
            math.ceil((max(lons) + pad) * 10) / 10,
            math.ceil((max(lats) + pad) * 10) / 10]


def main(argv):
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo))
    from pdf_service.viz import engine as viz_engine

    tarball = argv[1]
    countries = load_countries(tarball)
    print("source countries: %d" % len(countries))

    world_polys, world_dropped = build_base(countries, countries, EPS_WORLD)
    # "-99" is reused by the source for Somaliland and Northern Cyprus;
    # the surviving ring(s) sit at their true locations. Somaliland
    # belongs on the Africa outline; a sub-degree Northern Cyprus remnant
    # (if it survives simplification) is geographically in place too.
    africa_codes = sorted((set(countries) & AFRICA_A3)
                          | ({"-99"} if "-99" in countries else set()))
    missing_africa = sorted(AFRICA_A3 - set(countries))
    africa_polys, africa_dropped = build_base(countries, africa_codes,
                                              EPS_AFRICA)
    world_verts = sum(len(p) for p in world_polys)
    africa_verts = sum(len(p) for p in africa_polys)
    print("world: %d polys, %d verts, %d dropped rings"
          % (len(world_polys), world_verts, world_dropped))
    print("africa: %d polys, %d verts, %d dropped rings, %d countries"
          % (len(africa_polys), africa_verts, africa_dropped,
             len(africa_codes)))
    if missing_africa:
        print("africa codes absent from source: %s"
              % ",".join(missing_africa))

    # NOTE: run once on a tree whose JSONs lack built data. To
    # rebuild, restore pdf_service/viz/geo_base.json and
    # pdf_service/viz/geo_places.json from git first: the place merge
    # below is append-only and refuses duplicate ids.
    jumps = poles = 0
    for poly in world_polys:
        for i in range(len(poly) - 1):
            if abs(poly[i + 1][0] - poly[i][0]) > 300:
                if (abs(abs(poly[i][1]) - 90.0) < 1e-9
                        and abs(abs(poly[i + 1][1]) - 90.0) < 1e-9):
                    poles += 1  # by design: closure along the map edge
                else:
                    jumps += 1
    print("antimeridian jumps in world rings: %d (%d pole closures)"
          % (jumps, poles))

    places = []
    missing_cty = []
    for pid, code, en, hi, men, mhi in COUNTRIES:
        if code not in countries:
            missing_cty.append(code)
            continue
        lon, lat = marker_for(countries[code])
        places.append({"id": pid, "name_en": en, "name_hi": hi,
                       "kind": "country", "region": "world",
                       "match_en": men, "match_hi": mhi,
                       "lon": lon, "lat": lat})
        print("  %-12s %s -> (%s, %s)" % (pid, code, lon, lat))
    if missing_cty:
        print("country codes absent from source: %s"
              % ",".join(missing_cty))

    bases_path = repo / "pdf_service" / "viz" / "geo_base.json"
    places_path = repo / "pdf_service" / "viz" / "geo_places.json"
    base_data = json.loads(bases_path.read_text(encoding="utf-8"))
    place_data = json.loads(places_path.read_text(encoding="utf-8"))
    old_india = base_data["bases"]["india"]
    base_data["bases"] = {
        "india": old_india,
        "world": {
            "label_en": "World",
            "label_hi": "विश्व",
            "bbox": [-180.0, -90.0, 180.0, 90.0],
            "graticule_step": 30,
            "simplified": True,
            "accuracy_note_en": "Simplified outline - Not to scale",
            "accuracy_note_hi": "सरलीकृत रेखा-मानचित्र - पैमाने पर नहीं",
            "polygons": world_polys,
            "islets": [],
        },
        "africa": {
            "label_en": "Africa",
            "label_hi": "अफ्रीका",
            "bbox": africa_bbox(africa_polys),
            "graticule_step": 10,
            "simplified": True,
            "accuracy_note_en": "Simplified outline - Not to scale",
            "accuracy_note_hi": "सरलीकृत रेखा-मानचित्र - पैमाने पर नहीं",
            "polygons": africa_polys,
            "islets": [],
        },
    }
    have = {p["id"] for p in place_data["places"]}
    dupes = [p["id"] for p in places if p["id"] in have]
    assert not dupes, "place id collision: %s" % dupes
    place_data["places"].extend(sorted(places, key=lambda p: p["id"]))

    viz_engine._validate_bases(base_data)
    viz_engine._validate_places(place_data)
    viz_engine.reload_data()

    for path, data in ((bases_path, base_data), (places_path, place_data)):
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False)
                        + "\n", encoding="utf-8")
    print("wrote %s (%d bytes)" % (bases_path, bases_path.stat().st_size))
    print("wrote %s (%d bytes)" % (places_path, places_path.stat().st_size))


if __name__ == "__main__":
    main(sys.argv)
