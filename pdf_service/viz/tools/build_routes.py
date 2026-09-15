"""Dev-time route builder: Natural Earth rivers -> geo_routes.json.

Reads Natural Earth river GeoJSONs (public domain, v5.0) plus the
repo's geo_places.json, and REWRITES pdf_service/viz/geo_routes.json
wholesale (rerunnable: output depends only on inputs).

Per-river rules (all deterministic, all reported on stdout):

* SOURCE PIN: each dataset river maps to one NE file and one NE
  feature name (exact lowercase match on ``name_en``/``name``).
* MAIN COURSE: same-name segments sharing endpoints form a graph;
  the route is the longest simple path between degree-1 nodes
  (tributaries, distributaries and detached pieces are excluded, and
  reported). Length ties break on the coordinate sequence.
  Orientation starts at the lexicographically smaller endpoint; flow
  direction is NOT claimed from geometry alone.
* SIMPLIFY: Douglas-Peucker eps 0.25 degrees, vertices rounded to
  0.1 (mirrors the helpers in build_geo.py).
* PLACES: the river's own place id plus dataset places within 0.5
  degrees of the simplified course (point-to-segment), sorted by id.
  Names and match aliases are copied verbatim from the dataset place.

Run from the repository root::

    python3 pdf_service/viz/tools/build_routes.py 50m.geojson sr.geojson \\
        10m.geojson

This module is NEVER imported at runtime (see the Milestone B/C
offline tests).
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# dataset river id -> (input file key, Natural Earth feature name)
RIVERS = {
    "ganga": ("p50", "ganges"),
    "yamuna": ("sr50", "yamuna"),
    "brahmaputra": ("p50", "brahmaputra"),
    "indus": ("p50", "indus"),
    "godavari": ("sr50", "godavari"),
    "krishna": ("sr50", "krishna"),
    "narmada": ("sr50", "narmada"),
    "tapi": ("t10", "tapi"),
    "kaveri": ("t10", "cauvery"),
}

# Canonical upstream filenames (what the inputs were downloaded as).
FILE_LABELS = {
    "p50": "ne_50m_rivers_lake_centerlines.geojson",
    "sr50": "ne_50m_rivers_lake_centerlines_scale_rank.geojson",
    "t10": "ne_10m_rivers_lake_centerlines.geojson",
}

KNOWN_GAPS = {
    # Present in geo_places.json but in no NE 50m/10m river file (v5.0).
    "mahanadi": "absent from Natural Earth 50m/10m rivers (v5.0)",
}

EPS = 0.25  # DP simplification tolerance, degrees
PLACE_TOL = 0.5  # place-attachment tolerance, degrees
SELF_TOL = 2.0  # sanity bound for the river's own place marker


def _perp_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    along = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    if along <= 0:
        return math.hypot(px - ax, py - ay)
    if along >= 1:
        return math.hypot(px - bx, py - by)
    return abs((px - ax) * dy - (py - ay) * dx) / math.hypot(dx, dy)


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


def _rounded(line):
    return [[round(lon, 1), round(lat, 1)] for lon, lat in line]


def _lines_of(feature):
    coords = feature["geometry"]["coordinates"]
    if feature["geometry"]["type"] == "MultiLineString":
        return coords
    return [coords]


def _node(pt):
    return (round(pt[0], 3), round(pt[1], 3))


def _seg_len(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(pts, pts[1:]))


def longest_course(segments):
    """Longest simple endpoint-to-endpoint path (main course).

    Returns (points, used_segments, dropped_components) where points
    is the stitched polyline starting at the lexicographically
    smaller endpoint, used_segments counts consumed input segments,
    and dropped_components lists spans of unconnected leftovers.
    """
    adj: dict[tuple, list] = {}
    for idx, seg in enumerate(segments):
        if len(seg) < 2:
            continue
        a, b = _node(seg[0]), _node(seg[-1])
        length = _seg_len(seg)
        adj.setdefault(a, []).append((b, idx, seg, length))
        adj.setdefault(b, []).append((a, idx, seg, length))
    for node in adj:
        adj[node].sort(key=lambda e: (e[0], e[1]))

    degree = {node: len(edges) for node, edges in adj.items()}
    endpoints = sorted(n for n, d in degree.items() if d == 1)
    if not endpoints:
        return None, 0, []

    best = None  # (length, coord_key, points, used)

    def visit(node, seen, points, length, used):
        nonlocal best
        if len(seen) > 1 and degree[node] == 1:
            key = (round(length, 6),
                   tuple((round(x, 3), round(y, 3))
                         for x, y in points))
            if best is None or key > best[0]:
                best = (key, list(points), set(used))
        for nxt, idx, seg, leng in adj[node]:
            if nxt in seen:
                continue
            step = seg if _node(seg[0]) == node else list(reversed(seg))
            visit(nxt, seen | {nxt}, points + [tuple(p) for p in step[1:]],
                  length + leng, used | {idx})

    for start in endpoints:
        for nxt, idx, seg, leng in adj[start]:
            step = seg if _node(seg[0]) == start else list(reversed(seg))
            visit(nxt, {start, nxt}, [tuple(pt) for pt in step], leng,
                  {idx})
    if best is None:
        return None, 0, []
    _, points, used = best
    oriented = [[float(x), float(y)] for x, y in points]
    if oriented[0] > oriented[-1]:
        oriented.reverse()

    comp_of: dict[int, int] = {}
    for idx, seg in enumerate(segments):
        if len(seg) >= 2:
            comp_of[idx] = idx
    # Union-find over shared endpoints to describe leftovers.
    parent = dict(comp_of)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_node: dict[tuple, list] = {}
    for idx, seg in enumerate(segments):
        if len(seg) < 2:
            continue
        for end in (_node(seg[0]), _node(seg[-1])):
            by_node.setdefault(end, []).append(idx)
    for members in by_node.values():
        for other in members[1:]:
            parent[find(members[0])] = find(other)
    valid = [j for j, s in enumerate(segments) if len(s) >= 2]
    used_roots = {find(j) for j in valid if j in used}
    dropped = []
    seen_roots = set()
    for idx in valid:
        if idx in used:
            continue
        root = find(idx)
        if root in used_roots or root in seen_roots:
            continue
        seen_roots.add(root)
        pts = [pt for j in valid if j not in used and find(j) == root
               for pt in segments[j]]
        dropped.append((min(p[0] for p in pts), min(p[1] for p in pts),
                        max(p[0] for p in pts), max(p[1] for p in pts)))
    dropped.sort()
    return oriented, len(used), dropped


def _point_seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    along = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    along = max(0.0, min(1.0, along))
    return math.hypot(px - (ax + along * dx), py - (ay + along * dy))


def course_distance(lon, lat, line):
    return min(_point_seg_dist(lon, lat, a[0], a[1], b[0], b[1])
               for a, b in zip(line, line[1:]))


def main(argv):
    if len(argv) != 4:
        print("usage: build_routes.py <50m.geojson> <sr50.geojson> "
              "<10m.geojson>")
        return 2
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo))
    from pdf_service.viz import engine as viz_engine

    inputs = dict(zip(("p50", "sr50", "t10"), argv[1:]))
    files = {key: json.loads(Path(path).read_text(encoding="utf-8"))
             for key, path in inputs.items()}
    root = Path(__file__).resolve().parent.parent
    places = {p["id"]: p for p in json.loads(
        (root / "geo_places.json").read_text(encoding="utf-8"))["places"]}
    bases = json.loads(
        (root / "geo_base.json").read_text(encoding="utf-8"))["bases"]

    routes = []
    for rid, (key, ne_name) in sorted(RIVERS.items()):
        feats = [f for f in files[key]["features"]
                 if (f["properties"].get("name_en")
                     or f["properties"].get("name") or "")
                 .strip().lower() == ne_name]
        if not feats:
            print("SKIP %s: %r not in %s" % (rid, ne_name, inputs[key]))
            continue
        segments = [[(float(x), float(y)) for x, y in seg]
                    for f in feats for seg in _lines_of(f)]
        course, used, dropped = longest_course(segments)
        if course is None or len(course) < 2:
            print("SKIP %s: no stitched course" % rid)
            continue
        simple = _rounded(simplify(course, EPS))
        own = places[rid]
        self_dist = course_distance(own["lon"], own["lat"], simple)
        if self_dist > SELF_TOL:
            print("SKIP %s: own marker %.2f deg off course"
                  % (rid, self_dist))
            continue
        near = [(pid, course_distance(p["lon"], p["lat"], simple))
                for pid, p in places.items()]
        attached = sorted(pid for pid, dist in near
                          if pid == rid or dist <= PLACE_TOL)
        routes.append({
            "id": rid,
            "name_en": own["name_en"],
            "name_hi": own["name_hi"],
            "match_en": list(own["match_en"]),
            "match_hi": list(own["match_hi"]),
            "place_ids": attached,
            "source": ("Natural Earth 5.0 %s feature %r "
                       "(main course, %d vertices)"
                       % (FILE_LABELS[key], ne_name, len(simple))),
            "vertices": simple,
        })
        dists = {pid: dist for pid, dist in near if pid in attached}
        print("%-12s %-28s segs=%d/%d raw=%d verts=%d "
              "self=%.2f places=%s" % (
                  rid, "%s:%s" % (key, ne_name), used, len(segments),
                  len(course), len(simple), self_dist,
                  ",".join("%s(%.2f)" % (pid, dists[pid])
                           for pid in attached)))
        for span in dropped:
            print("    dropped component lon %.1f-%.1f lat %.1f-%.1f"
                  % (span[0], span[2], span[1], span[3]))
    for rid, reason in sorted(KNOWN_GAPS.items()):
        print("GAP %-9s %s" % (rid, reason))

    data = {"routes": routes}
    viz_engine._validate_routes(data, places, bases)
    out = root / "geo_routes.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print("wrote %s (%d bytes, %d routes)"
          % (out, out.stat().st_size, len(routes)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
