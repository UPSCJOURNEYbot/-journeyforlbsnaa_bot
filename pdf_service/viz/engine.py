"""Decision engine: when is a visual useful, and which type?

The engine is intentionally conservative (accuracy-first):

* Maps are emitted only for places in the curated dataset that fall
  inside a region with a base map. Unknown places, regions without a
  base map, and extent/boundary questions all yield ``None``. The map
  uses the smallest base map containing every shown place.
* Routes are drawn only from sourced vertex geometry: every route
  entry needs a provenance note, dataset place references, and
  vertices inside a mappable base. A route attaches when at least
  two of its vertices fall inside the chosen base and is then drawn
  clipped to the map frame (standard atlas crop); route questions
  without a drawable sourced route keep the ordered place chain
  instead of invented lines.
* Diagram templates only re-structure text already present in the
  question/explanation (steps, bullets, years, labelled sections).
  They never assert facts of their own, and quiz *options* are never
  visualised (distractors must not be drawn as facts).
* Everything is deterministic: same input -> identical spec. No
  randomness, no clocks, no network.

Use :func:`safe_decide_visual` from any future PDF wiring: it never
raises, so the optional visual layer can never break PDF generation.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from . import textstyle

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent  # JSONs live beside the modules

# ---------------------------------------------------------------------------
# Visual types
# ---------------------------------------------------------------------------


class VisualType(str, Enum):
    LOCATION_MAP = "location_map"
    HISTORICAL_MAP = "historical_map"
    REGIONAL_MAP = "regional_map"
    PROCESS = "process"
    FLOWCHART = "flowchart"
    CONCEPT_MAP = "concept_map"
    MIND_MAP = "mind_map"
    TIMELINE = "timeline"
    COMPARISON = "comparison"
    CAUSE_EFFECT = "cause_effect"
    CYCLE = "cycle"
    LABELLED_DIAGRAM = "labelled_diagram"
    CLASSIFICATION = "classification"
    INFOGRAPHIC = "infographic"
    PANELS = "panels"
    MECHANISM = "mechanism"
    SPATIAL_CHAIN = "spatial_chain"


ALL_TYPES = frozenset(t.value for t in VisualType)

# Milestone 1: historical boundaries and per-topic scientific geometry have
# no curated data yet, so these two types are never emitted.
SUPPORTED_TYPES = frozenset(
    t for t in ALL_TYPES if t not in {"historical_map", "labelled_diagram"}
)

# Subjects whose place mentions alone justify a map. Other subjects need
# explicit location phrasing ("where", "located", ...).
MAP_SUBJECTS = frozenset(
    {"geography", "environment", "history", "art_culture", "ir"}
)

# Fallback preference order when the subject is unknown.
GENERIC_ORDER = (
    "comparison",
    "classification",
    "timeline",
    "mechanism",
    "flowchart",
    "process",
    "cycle",
    "cause_effect",
    "spatial_chain",
    "mind_map",
    "concept_map",
    "panels",
    "infographic",
)

# Generic bullet-derived visuals are always tried last (per-type and
# per-subject): an explicit comparison/timeline/etc. signal must beat a
# mere "there are bullets" fallback.
FALLBACK_TYPES = ("mind_map", "concept_map", "panels", "infographic")

# Question frames that ask for an explicit structure. When such a frame
# is present AND structure evidence exists, the structure visual answers
# the question better than a topic map, so it wins over maps ("what are
# the features of Chilika" wants feature panels, not a locator dot).
STRUCTURE_OVERRIDE_FRAMES = frozenset({
    "features", "process", "mechanism", "comparison", "chronology",
    "causal", "classify", "cycle",
})

# Thresholds / caps (all pinned by tests).
MIN_STRUCTURE_CHARS = 60
TIMELINE_MIN_YEARS = 3
STEPS_MIN = 3
CONCEPT_MIN_ITEMS = 3
CONCEPT_MAX_BULLETS = 5
INFOGRAPHIC_MIN_BULLETS = 6
INFOGRAPHIC_MIN_EXPL = 200
COMPARISON_MIN_BULLETS = 2
MAX_PLACES = 6
MAX_STEPS = 8
MAX_EVENTS = 10
MAX_POINTS = 8
MAX_SATELLITES = 8
MAX_BRANCHES = 6
MAX_BRANCH_CHILDREN = 4
MAX_GROUPS = 6
MAX_GROUP_ITEMS = 4
MAX_CAUSES = 4
MAX_EFFECTS = 4
PANELS_COUNT = 4
MECHANISM_MIN_STAGES = 3
CHAIN_MIN_LINKS = 3
MAX_LINKS = 6
MAX_TITLE_CHARS = 100
MAX_LABEL_CHARS = 80
MAX_ITEM_CHARS = 140


@dataclass(frozen=True)
class VisualSpec:
    """Immutable description of one visual (JSON-serialisable)."""

    visual_type: str
    subject: Optional[str]
    title: str
    payload: dict
    notes: tuple = field(default_factory=tuple)
    evidence: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "visual_type": self.visual_type,
            "subject": self.subject,
            "title": self.title,
            "payload": json.loads(json.dumps(self.payload)),
            "notes": list(self.notes),
            "evidence": list(self.evidence),
        }


# ---------------------------------------------------------------------------
# Data layer (local/static JSON, validated on load, cached)
# ---------------------------------------------------------------------------

_CACHE: dict[str, Any] = {}


def _read_json(name: str) -> Any:
    path = _DATA_DIR / name
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _validate_subjects(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("subjects"),
                                                   list):
        raise ValueError("subjects.json: expected {subjects: [...]}")
    seen: set[str] = set()
    for entry in data["subjects"]:
        sid = entry.get("id")
        if not sid or sid in seen:
            raise ValueError(f"subjects.json: bad/duplicate id {sid!r}")
        seen.add(sid)
        for key in ("label_en", "label_hi"):
            if not isinstance(entry.get(key), str):
                raise ValueError(f"subjects.json: {sid} missing {key}")
        for key in ("keywords_en", "keywords_hi", "preferred"):
            if not isinstance(entry.get(key), list) or not all(
                    isinstance(v, str) for v in entry[key]):
                raise ValueError(f"subjects.json: {sid} bad {key}")
        unknown = set(entry["preferred"]) - ALL_TYPES
        if unknown:
            raise ValueError(f"subjects.json: {sid} unknown types {unknown}")
    return data


def _validate_places(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("places"),
                                                   list):
        raise ValueError("geo_places.json: expected {places: [...]}")
    seen: set[str] = set()
    for entry in data["places"]:
        pid = entry.get("id")
        if not pid or pid in seen:
            raise ValueError(f"geo_places.json: bad/duplicate id {pid!r}")
        seen.add(pid)
        for key in ("name_en", "name_hi", "kind", "region"):
            if not isinstance(entry.get(key), str):
                raise ValueError(f"geo_places.json: {pid} missing {key}")
        for key in ("match_en", "match_hi"):
            if (not isinstance(entry.get(key), list) or not entry[key]
                    or not all(isinstance(v, str) for v in entry[key])):
                raise ValueError(f"geo_places.json: {pid} bad {key}")
        lon, lat = entry.get("lon"), entry.get("lat")
        if (not isinstance(lon, (int, float))
                or not isinstance(lat, (int, float))
                or not -180 <= lon <= 180 or not -90 <= lat <= 90):
            raise ValueError(f"geo_places.json: {pid} bad coordinates")
    return data


def _validate_bases(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("bases"),
                                                   dict):
        raise ValueError("geo_base.json: expected {bases: {...}}")
    for bid, base in data["bases"].items():
        bbox = base.get("bbox")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or not all(isinstance(v, (int, float)) for v in bbox)
                or not bbox[0] < bbox[2] or not bbox[1] < bbox[3]):
            raise ValueError(f"geo_base.json: {bid} bad bbox")
        polys = base.get("polygons")
        if not isinstance(polys, list) or not polys:
            raise ValueError(f"geo_base.json: {bid} needs polygons")
        for poly in polys:
            if len(poly) < 4 or poly[0] != poly[-1]:
                raise ValueError(f"geo_base.json: {bid} polygon not closed")
            for pt in poly:
                if (len(pt) != 2
                        or not all(isinstance(v, (int, float)) for v in pt)
                        or not point_in_bbox(pt[0], pt[1], bbox)):
                    raise ValueError(f"geo_base.json: {bid} bad vertex {pt}")
        for key in ("label_en", "label_hi"):
            if not isinstance(base.get(key), str):
                raise ValueError(f"geo_base.json: {bid} missing {key}")
    return data


def _validate_routes(data: Any, places_by_id: dict,
                     bases: dict) -> dict:
    """Validate geo_routes.json against the dataset places and bases.

    Unlike base polygons, route vertices are OPEN polylines (never
    closed): they trace sourced geometry such as a river course, so no
    closure is required. Every entry must cite its source.
    """
    if not isinstance(data, dict) or not isinstance(data.get("routes"),
                                                   list):
        raise ValueError("geo_routes.json: expected {routes: [...]}")
    seen: set[str] = set()
    for entry in data["routes"]:
        rid = entry.get("id")
        if not rid or rid in seen:
            raise ValueError(f"geo_routes.json: bad/duplicate id {rid!r}")
        seen.add(rid)
        if not isinstance(entry.get("name_en"), str) or not entry["name_en"]:
            raise ValueError(f"geo_routes.json: {rid} missing name_en")
        if "name_hi" in entry and not isinstance(entry["name_hi"], str):
            raise ValueError(f"geo_routes.json: {rid} bad name_hi")
        for key in ("match_en", "match_hi"):
            if (not isinstance(entry.get(key), list) or not entry[key]
                    or not all(isinstance(v, str) and v
                               for v in entry[key])):
                raise ValueError(f"geo_routes.json: {rid} bad {key}")
        place_ids = entry.get("place_ids")
        if (not isinstance(place_ids, list) or not place_ids
                or not all(isinstance(v, str) for v in place_ids)):
            raise ValueError(f"geo_routes.json: {rid} bad place_ids")
        unknown = [v for v in place_ids if v not in places_by_id]
        if unknown:
            raise ValueError(f"geo_routes.json: {rid} unknown places "
                             f"{unknown}")
        if not isinstance(entry.get("source"), str) or not entry["source"]:
            raise ValueError(f"geo_routes.json: {rid} missing source")
        verts = entry.get("vertices")
        if (not isinstance(verts, list) or len(verts) < 2
                or any(len(v) != 2 for v in verts)
                or not all(isinstance(c, (int, float))
                           for v in verts for c in v)):
            raise ValueError(f"geo_routes.json: {rid} needs >=2 vertices")
        for lon, lat in verts:
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError(f"geo_routes.json: {rid} bad vertex "
                                 f"{[lon, lat]}")
            if not any(point_in_bbox(lon, lat, base["bbox"])
                       for base in bases.values()):
                raise ValueError(f"geo_routes.json: {rid} vertex outside "
                                 f"every base {[lon, lat]}")
    return data


def load_subjects() -> dict:
    """Load + validate subjects.json (cached)."""
    if "subjects" not in _CACHE:
        _CACHE["subjects"] = _validate_subjects(_read_json("subjects.json"))
    return _CACHE["subjects"]


def load_places() -> dict:
    """Load + validate geo_places.json (cached)."""
    if "places" not in _CACHE:
        _CACHE["places"] = _validate_places(_read_json("geo_places.json"))
    return _CACHE["places"]


def load_base_maps() -> dict:
    """Load + validate geo_base.json (cached)."""
    if "bases" not in _CACHE:
        _CACHE["bases"] = _validate_bases(_read_json("geo_base.json"))
    return _CACHE["bases"]


def load_routes() -> dict:
    """Load + validate geo_routes.json (cached)."""
    if "routes" not in _CACHE:
        places = {p["id"]: p for p in load_places()["places"]}
        bases = load_base_maps()["bases"]
        _CACHE["routes"] = _validate_routes(_read_json("geo_routes.json"),
                                            places, bases)
    return _CACHE["routes"]


def reload_data() -> None:
    """Drop cached data (tests / future hot-reload)."""
    _CACHE.clear()


def point_in_bbox(lon: float, lat: float, bbox: list) -> bool:
    """Inclusive bbox test; bbox = [minlon, minlat, maxlon, maxlat]."""
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_DEVA = r"\u0900-\u097F"


def _norm(text: object) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _en_hit(keyword: str, lowered: str) -> bool:
    return re.search(r"\b" + re.escape(keyword.lower()) + r"\b", lowered,
                     flags=re.ASCII) is not None


def _hi_hit(keyword: str, text: str) -> bool:
    return re.search(r"(?<![%s])%s(?![%s])" % (_DEVA, re.escape(keyword),
                                              _DEVA), text) is not None


def detect_subject(text: object) -> Optional[str]:
    """Best-matching subject id, or None when nothing matches.

    Score = sum of matched-keyword weights (multi-word keywords weigh
    more); ties resolve in subjects.json order (deterministic).
    """
    blob = _norm(text)
    if not blob:
        return None
    lowered = blob.lower()
    best: Optional[str] = None
    best_score = 0
    for entry in load_subjects()["subjects"]:
        score = 0
        for kw in entry["keywords_en"]:
            if _en_hit(kw, lowered):
                score += len(kw.split())
        for kw in entry["keywords_hi"]:
            if _hi_hit(kw, blob):
                score += len(kw.split())
        if score > best_score:
            best_score = score
            best = entry["id"]
    return best


def subject_label(subject: Optional[str]) -> str:
    """English label for a subject id (fallback: 'General')."""
    if not subject:
        return "General"
    for entry in load_subjects()["subjects"]:
        if entry["id"] == subject:
            return entry["label_en"]
    return "General"


def find_places(text: object) -> list[dict]:
    """All dataset places mentioned in `text`, sorted by id (deduped)."""
    blob = _norm(text)
    if not blob:
        return []
    lowered = blob.lower()
    hits: dict[str, dict] = {}
    for place in load_places()["places"]:
        matched = any(_en_hit(a, lowered) for a in place["match_en"])
        if not matched:
            matched = any(_hi_hit(a, blob) for a in place["match_hi"])
        if matched:
            hits[place["id"]] = place
    return [hits[pid] for pid in sorted(hits)]


def find_routes(text: object) -> list[dict]:
    """All dataset routes mentioned in `text`, sorted by id (deduped)."""
    blob = _norm(text)
    if not blob:
        return []
    lowered = blob.lower()
    hits: dict[str, dict] = {}
    for route in load_routes()["routes"]:
        matched = any(_en_hit(a, lowered) for a in route["match_en"])
        if not matched:
            matched = any(_hi_hit(a, blob) for a in route["match_hi"])
        if matched:
            hits[route["id"]] = route
    return [hits[rid] for rid in sorted(hits)]


def usable_places(places: list[dict]) -> list[dict]:
    """Places that can actually be mapped: region has a base map and the
    point falls inside that base map's bbox. Input order preserved."""
    bases = load_base_maps()["bases"]
    out = []
    for place in places:
        base = bases.get(place.get("region", ""))
        if base and point_in_bbox(place["lon"], place["lat"],
                                  base["bbox"]):
            out.append(place)
    return out


def _bbox_area(bbox: list) -> float:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def select_base(shown: list[dict]) -> str:
    """Smallest base (by bbox area) containing every shown place.

    Falls back to the first place's own region, which is always
    mappable for usable input. Deterministic (id order breaks ties).
    """
    bases = load_base_maps()["bases"]
    covering = [bid for bid, base in bases.items()
                if all(point_in_bbox(p["lon"], p["lat"], base["bbox"])
                       for p in shown)]
    if covering:
        return min(sorted(covering),
                   key=lambda bid: _bbox_area(bases[bid]["bbox"]))
    return shown[0].get("region", "")


def _ordered_places(text: object) -> list[dict]:
    """Dataset places in first-mention order (deduped).

    Ties (same offset, e.g. overlapping aliases) resolve by place id,
    so the order is deterministic. Used for route/chain evidence where
    the mention order in the author's own text is the honest sequence.
    """
    blob = _norm(text)
    if not blob:
        return []
    lowered = blob.lower()
    hits: list[tuple[int, dict]] = []
    for place in load_places()["places"]:
        best: Optional[int] = None
        for alias in place["match_en"]:
            match = re.search(r"\b" + re.escape(alias.lower()) + r"\b",
                              lowered, flags=re.ASCII)
            if match and (best is None or match.start() < best):
                best = match.start()
        for alias in place["match_hi"]:
            match = re.search(r"(?<![%s])%s(?![%s])"
                              % (_DEVA, re.escape(alias), _DEVA), blob)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            hits.append((best, place))
    hits.sort(key=lambda item: (item[0], item[1]["id"]))
    return [place for _pos, place in hits]


# ---------------------------------------------------------------------------
# Question classifiers
# ---------------------------------------------------------------------------

_EXTENT_RES = (
    re.compile(r"\bextent\b", re.IGNORECASE),
    re.compile(r"\bspread\s+of\b", re.IGNORECASE),
    re.compile(r"\bterritor\w*\s+of\b", re.IGNORECASE),
    re.compile(r"\bboundar\w*\s+of\b", re.IGNORECASE),
    re.compile(r"\bempire\b.{0,40}\b(extend|cover|span|reach)", re.IGNORECASE),
    re.compile(r"साम्राज्य.{0,20}विस्तार"),
    re.compile(r"विस्तार.{0,20}साम्राज्य"),
)

_LOCATION_RES = (
    re.compile(r"\bwhere\b", re.IGNORECASE),
    re.compile(r"\blocati\w+\b", re.IGNORECASE),
    re.compile(r"\bcapital\s+of\b", re.IGNORECASE),
    re.compile(r"\bwhich\s+(place|city|state|country|river|lake)\b",
               re.IGNORECASE),
    re.compile(r"\bmap\b", re.IGNORECASE),
    re.compile(r"\blies\s+in\b", re.IGNORECASE),
    re.compile(r"\bsituated\b", re.IGNORECASE),
    re.compile(r"कहाँ"),
    re.compile(r"कहां"),
    re.compile(r"स्थित"),
    re.compile(r"राजधानी"),
    re.compile(r"मानचित्र"),
    re.compile(r"अवस्थित"),
)

# Extra location phrasing (Milestone A): distribution questions and
# map-marking tasks ask for a spatial visual even without "where".
_LOCATION_EXTRA_RES = (
    re.compile(r"\bdistribution\s+of\b", re.IGNORECASE),
    re.compile(r"\bmarks?\b.{0,30}\bmaps?\b", re.IGNORECASE),
    re.compile(r"\bmaps?\b.{0,30}\bmarks?\b", re.IGNORECASE),
    re.compile(r"किस\s+(राज्य|क्षेत्र|तट|जिले|भाग|देश|स्थान)"),
)


def _is_extent_question(question: str) -> bool:
    return any(rx.search(question) for rx in _EXTENT_RES)


def _has_location_phrasing(text: str) -> bool:
    return any(rx.search(text)
               for rx in _LOCATION_RES + _LOCATION_EXTRA_RES)


def _is_location_question(question: str) -> bool:
    return _has_location_phrasing(question)


# ---------------------------------------------------------------------------
# Question intent (interrogative frame; Milestone A)
#
# Intent is read ONLY from the question stem (never from options). It is
# one input to the decision -- every visual type additionally needs
# explanation-side structural evidence, so a matching keyword alone can
# never force a visual. Intents:
#
# * ``assertion`` -- "consider statements" / "match the following": the
#   question text holds unevaluated claims, so structure evidence may
#   come only from the (authoritative) explanation.
# * ``recall`` -- who/whom/led-by style person/single-fact asks: generic
#   list visuals (mind/concept/panels/infographic) are suppressed and
#   maps need explicit location phrasing (a person-seeking question
#   alone never justifies a map of a mentioned place).
# * ``features`` -- features/measures/provisions/parts asks: required
#   (together with exactly four flat items) for the 2x2 panels visual,
#   and steers numbered lists away from process/flowchart readings.
# * ``mechanism`` -- mechanism/working/pathway asks: required (together
#   with explicit ordered stages) for the mechanism visual.
# * other frames (location/route/comparison/chronology/process/causal/
#   classify/cycle) are recorded as decision evidence; the matching
#   visual types stay evidence-driven.
# ---------------------------------------------------------------------------

def _hi_word(word: str) -> str:
    """Devanagari word with script boundaries (mirrors _hi_hit)."""
    return r"(?<![%s])%s(?![%s])" % (_DEVA, re.escape(word), _DEVA)


_ROUTE_RES = (
    re.compile(r"\broute\b", re.IGNORECASE),
    re.compile(r"\bvia\b", re.IGNORECASE),
    re.compile(r"\bcorridor\b", re.IGNORECASE),
    re.compile(r"\bflows?\s+from\b", re.IGNORECASE),
    re.compile(r"\bpass(?:es|ed)?\s+through\b", re.IGNORECASE),
    re.compile(r"\bjoins?\b", re.IGNORECASE),
    re.compile(r"\bconnects?\b", re.IGNORECASE),
    re.compile(r"मार्ग"),
    re.compile(r"होकर"),
    re.compile(r"गलियारा"),
    re.compile(r"जोड़ता"),
    re.compile(r"जोड़ती"),
    re.compile(r"मिलती"),
    re.compile(r"मिलता"),
)

_COMPARISON_INTENT_RES = (
    re.compile(r"\bcompar\w*\b", re.IGNORECASE),
    re.compile(r"\bcontrast\w*\b", re.IGNORECASE),
    re.compile(r"\bdistinguish\w*\b", re.IGNORECASE),
    re.compile(r"\bdifferences?\s+between\b", re.IGNORECASE),
    re.compile(r"\bdifferent\s+from\b", re.IGNORECASE),
    re.compile(_hi_word("अंतर")),
    re.compile(_hi_word("तुलना")),
)

_CHRONOLOGY_RES = (
    re.compile(r"\bchronolog\w*\b", re.IGNORECASE),
    re.compile(r"\barrange\b.{0,40}\border\b", re.IGNORECASE),
    re.compile(r"\bcorrect\s+(order|sequence)\b", re.IGNORECASE),
    re.compile(r"\bsequence\s+of\s+(events?|reigns?|plans?|years?|"
               r"milestones?|struggles?)\b", re.IGNORECASE),
    re.compile(r"\boccurred?\s+first\b", re.IGNORECASE),
    re.compile(r"कालानुक्रम"),
    re.compile(r"कालक्रम"),
)

_PROCESS_INTENT_RES = (
    re.compile(r"\bstages?\b", re.IGNORECASE),
    re.compile(r"\bsteps?\b", re.IGNORECASE),
    re.compile(r"\bprocess\s+of\b", re.IGNORECASE),
    re.compile(r"\bprocedure\b", re.IGNORECASE),
    re.compile(r"\bformation\s+of\b", re.IGNORECASE),
    re.compile(r"\bhow\s+(is|are|was|were|does|do|can)\b", re.IGNORECASE),
    re.compile(r"चरण"),
    re.compile(r"प्रक्रम"),
    re.compile(r"प्रक्रिया"),
    re.compile(r"कैसे"),
)

_MECHANISM_INTENT_RES = (
    re.compile(r"\bmechanism\b", re.IGNORECASE),
    re.compile(r"\bhow\s+does\b.{0,50}\bwork\b", re.IGNORECASE),
    re.compile(r"\bworking\s+of\b", re.IGNORECASE),
    re.compile(r"\bpathway\b", re.IGNORECASE),
    re.compile(r"क्रियाविधि"),
    re.compile(r"कार्यप्रणाली"),
)

_CAUSAL_INTENT_RES = (
    re.compile(r"\bcauses?\s+and\s+effects?\b", re.IGNORECASE),
    re.compile(r"\beffects?\s+and\s+causes?\b", re.IGNORECASE),
    re.compile(r"\bfactors?\s+and\s+(effects?|consequences?)\b",
               re.IGNORECASE),
    re.compile(r"कारण\s+और\s+प्रभाव"),
)

_CLASSIFY_INTENT_RES = (
    re.compile(r"\btypes?\s+of\b", re.IGNORECASE),
    re.compile(r"\bkinds?\s+of\b", re.IGNORECASE),
    re.compile(r"\bclassif\w*\b", re.IGNORECASE),
    re.compile(r"\bcategor\w+\b", re.IGNORECASE),
    re.compile(r"प्रकार"),
    re.compile(r"वर्गीकरण"),
    re.compile(r"श्रेणी"),
)

_FEATURES_INTENT_RES = (
    re.compile(r"\bfeatures?\b", re.IGNORECASE),
    re.compile(r"\bcharacteristics?\b", re.IGNORECASE),
    re.compile(r"\bmeasures?\b", re.IGNORECASE),
    re.compile(r"\bprovisions?\b", re.IGNORECASE),
    re.compile(r"\bfunctions?\b", re.IGNORECASE),
    re.compile(r"\bdimensions?\b", re.IGNORECASE),
    re.compile(r"\baspects?\b", re.IGNORECASE),
    re.compile(r"\bparts?\s+of\b", re.IGNORECASE),
    re.compile(r"\bcompris\w*\b", re.IGNORECASE),
    re.compile(r"\bincludes?\b", re.IGNORECASE),
    re.compile(r"\bconsists?\s+of\b", re.IGNORECASE),
    re.compile(r"विशेषता"),
    re.compile(r"लक्षण"),
    re.compile(r"उपाय"),
    re.compile(r"प्रावधान"),
    re.compile(r"आयाम"),
    re.compile(r"शामिल"),
)

_RECALL_RES = (
    re.compile(r"^\s*who\b", re.IGNORECASE),
    re.compile(r"\bwho\s+(was|were|is|are|led|founded|wrote|discovered|"
               r"started|built|gave|issued)\b", re.IGNORECASE),
    re.compile(r"\bwhom\b", re.IGNORECASE),
    re.compile(r"\bwhose\b", re.IGNORECASE),
    re.compile(r"\bwhich\s+(king|queen|ruler|leader|president|person|poet|"
               r"author|scientist|general|saint|philosopher|sultan|"
               r"emperor)\b", re.IGNORECASE),
    re.compile(r"\bled\s+by\b", re.IGNORECASE),
    re.compile(r"\bfounded\s+by\b", re.IGNORECASE),
    re.compile(r"किसने"),
    re.compile(_hi_word("कौन")),
)

_ASSERTION_RES = (
    re.compile(r"\bconsider\s+(the\s+)?following\s+statements?\b",
               re.IGNORECASE),
    re.compile(r"\bmatch\s+the\s+following\b", re.IGNORECASE),
    re.compile(r"\bpair\s+the\s+following\b", re.IGNORECASE),
    re.compile(r"निम्नलिखित\s+कथन"),
    re.compile(r"कथनों\s+पर\s+विचार"),
    re.compile(r"सुमेलित"),
)

_INTENT_TABLE: tuple[tuple[str, tuple], ...] = (
    ("assertion", _ASSERTION_RES),
    ("recall", _RECALL_RES),
    ("location", _LOCATION_RES + _LOCATION_EXTRA_RES),
    ("route", _ROUTE_RES),
    ("comparison", _COMPARISON_INTENT_RES),
    ("chronology", _CHRONOLOGY_RES),
    ("mechanism", _MECHANISM_INTENT_RES),
    ("process", _PROCESS_INTENT_RES),
    ("causal", _CAUSAL_INTENT_RES),
    ("classify", _CLASSIFY_INTENT_RES),
    ("features", _FEATURES_INTENT_RES),
)


def classify_intent(question: object) -> tuple[str, ...]:
    """Interrogative frames detected in the question (sorted tuple).

    Multi-intent: a question may carry several frames (e.g. "stages of
    the water cycle" is both ``process`` and ``cycle``). An empty tuple
    means a general question with no specific frame. Deterministic.
    """
    text = _norm(question)
    if not text:
        return ()
    # _CYCLE_RES lives with the structure extractors below; looked up at
    # call time so definition order does not matter.
    table = list(_INTENT_TABLE) + [("cycle", _CYCLE_RES)]
    found = [name for name, pats in table
             if any(rx.search(text) for rx in pats)]
    return tuple(sorted(found))


def _intent_tag(intents: tuple[str, ...]) -> str:
    return "+".join(intents) if intents else "general"


# ---------------------------------------------------------------------------
# Structure extractors (content-derived only)
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2})\b")
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}[.)]\s+(.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[•▪·\-*+]|\d{1,2}[.)])\s+(.+?)\s*$",
                        re.MULTILINE)
_TOP_BULLET_RE = re.compile(r"^([ ]{0,1})(?:[•\-*+]|\d{1,2}[.)])\s+(.+?)\s*$")
_SUB_BULLET_RE = re.compile(r"^[ ]{2,}(?:[•\-*+]|\d{1,2}[.)])\s+(.+?)\s*$")

_ORDINALS_EN = ("first", "second", "third", "fourth", "fifth", "sixth",
                "seventh", "eighth")
_ORDINALS_HI = ("पहला", "दूसरा", "तीसरा", "चौथा", "पाँचवाँ", "पांचवां",
                "छठा", "सातवाँ", "आठवाँ")

_CMP_RES = (
    re.compile(r"differences?\s+between\s+(.+?)\s+and\s+(.+?)"
               r"(?=[.,;:?!]|$)", re.IGNORECASE),
    re.compile(r"(.{3,60}?)\s+(?:vs\.?|v/s|versus)\s+(.{3,60}?)"
               r"(?=[.,;:?!]|$)", re.IGNORECASE),
    re.compile(r"(.{2,40}?)\s+और\s+(.{2,40}?)\s+में\s+अंतर"),
    re.compile(r"(.{3,60}?)\s+different\s+from\s+(.{3,60}?)"
               r"(?=[.,;:?!]|$)", re.IGNORECASE),
    re.compile(r"compar\w*\s+(.{3,50}?)\s+with\s+(.{3,50}?)"
               r"(?=[.,;:?!]|$)", re.IGNORECASE),
)

_LEAD_QW_RE = re.compile(
    r"^(how\s+(is|are|was|were|does|do|can)|what\s+(is|are|was|were)|"
    r"why\s+(is|are)|is|are|was|were|the)\s+", re.IGNORECASE)


def _strip_lead(value: str) -> str:
    """Drop a leading interrogative ("How is X" -> "X")."""
    cleaned = _LEAD_QW_RE.sub("", value.strip()).strip()
    return cleaned or value.strip()

_CAUSE_LABELS = {
    "causes": ("causes", "reasons", "कारण"),
    "effects": ("effects", "results", "consequences", "outcome",
                "प्रभाव", "परिणाम"),
}

_CYCLE_RES = (
    # "cycle/cycles/cyclic/cyclical" only: bare "cycl*" also matched
    # "cyclone/cyclonic", which are not cycles.
    re.compile(r"\bcycl(?:e|es|ic|ical)?\b", re.IGNORECASE),
    re.compile(r"(?<![%s])चक्र(?![%s])" % (_DEVA, _DEVA)),
)

_CLASSIFY_RES = (
    re.compile(r"(?:types?|kinds?)\s+of\s+([^:;.\n?]{3,60})"
               r"(?::\s*([^.\n]{3,200}))?", re.IGNORECASE),
    re.compile(r"([^:;.\n?]{3,60}?)\s*के\s+प्रकार"
               r"(?:\s*[:：]\s*([^.\n]{3,200}))?"),
)

_CONCEPT_RES = (
    re.compile(r"([A-Z][^.,;:\n]{2,60}?)\s+"
               r"(?:includes?|comprises?|consists?\s+of)\s*:?\s*"
               r"([^.\n]{5,300})"),
    re.compile(r"([^.\n]{2,60}?)\s+में\s+([^.\n]{5,200}?)\s+शामिल\s+हैं"),
)

_PROCEDURE_WORDS = ("article", "act", "bill", "amendment", "election",
                    "procedure", "विधेयक", "संशोधन", "चुनाव", "प्रक्रिया",
                    "अनुच्छेद")
_SYSTEM_WORDS = ("process", "formation", "mechanism", "phase", "reaction",
                 "प्रक्रम", "निर्माण", "तंत्र", "अभिक्रिया")


def _extract_years(text: str) -> list[int]:
    return sorted({int(m.group(1)) for m in _YEAR_RE.finditer(text)})


def _clean_item(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    return textstyle.clean_label(text, limit)


def _numbered_items(raw: str) -> list[str]:
    return [_clean_item(m.group(1)) for m in _NUMBERED_RE.finditer(raw)]


def _bullets(raw: str) -> list[str]:
    return [_clean_item(m.group(1)) for m in _BULLET_RE.finditer(raw)
            if _clean_item(m.group(1))]


def _ordinal_steps(text: str) -> list[str]:
    """Split on first/second/third... (or पहला/दूसरा/...) markers."""
    marks: list[tuple[int, int, int]] = []  # (order, start, end)
    lowered = text.lower()
    for i, word in enumerate(_ORDINALS_EN):
        for m in re.finditer(r"\b" + word + r"\b", lowered, flags=re.ASCII):
            marks.append((i, m.start(), m.end()))
    for i, word in enumerate(_ORDINALS_HI):
        for m in re.finditer(r"(?<![%s])%s(?![%s])" % (_DEVA, re.escape(word),
                                                      _DEVA), text):
            marks.append((i + 100, m.start(), m.end()))
    marks.sort(key=lambda t: t[1])
    ordered = [m for m in marks if m[0] < 100]
    hi_marks = [m for m in marks if m[0] >= 100]
    seq = ordered if len(ordered) >= len(hi_marks) else hi_marks
    seq = sorted(seq, key=lambda t: t[1])
    # Keep only increasing ordinals.
    increasing: list[tuple[int, int, int]] = []
    for m in seq:
        if not increasing or m[0] > increasing[-1][0]:
            increasing.append(m)
    if len(increasing) < STEPS_MIN:
        return []
    steps = []
    for k, mark in enumerate(increasing):
        start = mark[2]
        end = increasing[k + 1][1] if k + 1 < len(increasing) else len(text)
        chunk = _clean_item(text[start:end], 160).lstrip(",;:.- ").strip()
        if chunk:
            steps.append(chunk)
    return steps if len(steps) >= STEPS_MIN else []


def _labeled_sections(raw: str,
                      label_map: dict[str, tuple[str, ...]]) -> dict:
    """Parse 'Label: value' sections with continuation lines.

    Returns {canonical: [items...]}. Deterministic; bullets under a label
    become separate items.
    """
    variant_to_key: dict[str, str] = {}
    for key, variants in label_map.items():
        for variant in variants:
            variant_to_key[variant.lower()] = key
    # Longest variants first so "consequences" wins over "consequence".
    ordered = sorted(variant_to_key, key=len, reverse=True)
    pattern = re.compile(
        r"^\s*(%s)\s*[:：]\s*(.*)$" % "|".join(re.escape(v)
                                              for v in ordered),
        re.IGNORECASE)
    sections: dict[str, list[str]] = {key: [] for key in label_map}
    current: Optional[str] = None
    for line in raw.splitlines():
        match = pattern.match(line.strip())
        if match:
            current = variant_to_key[match.group(1).lower()]
            value = _clean_item(match.group(2))
            if value:
                sections[current].append(value)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        bullet = re.match(r"^(?:[•▪·\-*+]|\d{1,2}[.)])\s+(.+)$", stripped)
        sections[current].append(_clean_item(bullet.group(1) if bullet
                                             else stripped))
    return {key: [v for v in vals if v] for key, vals in sections.items()}


def _split_list(chunk: str) -> list[str]:
    parts = re.split(r"[,;、]|\sand\s|\sऔर\s", chunk)
    return [_clean_item(p, 60) for p in parts if _clean_item(p, 60)]


def _timeline_payload(raw: str, norm: str) -> Optional[dict]:
    years = _extract_years(norm)
    if len(years) < TIMELINE_MIN_YEARS:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    events = []
    for year in sorted(years)[:MAX_EVENTS]:
        label = ""
        for line in lines:
            if str(year) in line:
                label = _clean_item(re.sub(r"\b%d\b" % year, "", line))
                break
        events.append({"year": year, "label": label or str(year)})
    payload: dict = {"events": events}
    if len(years) > MAX_EVENTS:
        payload["shown_of"] = [MAX_EVENTS, len(years)]
    return payload


def _steps_payload(raw: str, norm: str) -> Optional[dict]:
    items = _numbered_items(raw)[:MAX_STEPS]
    if len(items) < STEPS_MIN:
        items = _ordinal_steps(norm)[:MAX_STEPS]
    if len(items) < STEPS_MIN:
        return None
    return {"steps": [{"label": label} for label in items]}


def _steps_type(subject: Optional[str], norm: str) -> str:
    lowered = norm.lower()
    if subject == "polity" or any(w in lowered for w in _PROCEDURE_WORDS):
        return VisualType.FLOWCHART.value
    if subject in ("science", "economy", "environment") or any(
            w in lowered for w in _SYSTEM_WORDS):
        return VisualType.PROCESS.value
    return VisualType.FLOWCHART.value


def _comparison_payload(norm: str, raw: str) -> Optional[dict]:
    left = right = ""
    for pattern in _CMP_RES:
        for match in pattern.finditer(norm):
            cand_left = _strip_lead(
                _clean_item(match.group(1), 60).strip("\"' "))
            cand_right = _strip_lead(
                _clean_item(match.group(2), 60).strip("\"' "))
            if cand_left and cand_right and cand_left != cand_right:
                left, right = cand_left, cand_right
                break
        if left and right:
            break
    if not left or not right:
        return None
    bullets = _bullets(raw)
    if len(bullets) < COMPARISON_MIN_BULLETS:
        return None
    left_pts, right_pts, common = [], [], []
    for bullet in bullets:
        folded = bullet.casefold()
        in_left = left.casefold() in folded
        in_right = right.casefold() in folded
        if in_left and not in_right:
            left_pts.append(bullet)
        elif in_right and not in_left:
            right_pts.append(bullet)
        else:
            common.append(bullet)
    return {
        "left_title": left,
        "right_title": right,
        "left_points": left_pts[:4],
        "right_points": right_pts[:4],
        "common": common[:3],
    }


def _cause_effect_payload(raw: str) -> Optional[dict]:
    sections = _labeled_sections(raw, _CAUSE_LABELS)
    causes = sections["causes"][:MAX_CAUSES]
    effects = sections["effects"][:MAX_EFFECTS]
    if not causes or not effects:
        return None
    return {"causes": causes, "effects": effects}


def _cycle_payload(norm: str, raw: str,
                   intents: tuple[str, ...]) -> Optional[dict]:
    if not any(rx.search(norm) for rx in _CYCLE_RES):
        return None
    stages = _numbered_items(raw)
    if len(stages) < 3:
        sections = _labeled_sections(raw, {"stages": ("stages", "steps",
                                                     "phases", "चरण")})
        if sections["stages"]:
            stages = []
            for chunk in sections["stages"]:
                stages.extend(_split_list(chunk))
    if len(stages) < 3:
        # Bare bullets are weak cycle evidence: they count only when the
        # question itself asks about the cycle (not about features of
        # something merely cycle-named).
        if "cycle" not in intents or "features" in intents:
            return None
        stages = _bullets(raw)
    if len(stages) < 3:
        return None
    return {"stages": stages[:6]}


def _classification_payload(norm: str, raw: str) -> Optional[dict]:
    root = ""
    inline_items: list[str] = []
    for pattern in _CLASSIFY_RES:
        match = pattern.search(norm)
        if match:
            root = _clean_item(match.group(1), 60)
            if match.lastindex == 2 and match.group(2):
                inline_items = _split_list(match.group(2))
            break
    bullets = _bullets(raw)
    groups: list[dict] = []
    for bullet in bullets:
        match = re.match(r"^([^:：]{2,40})\s*[:：]\s*(.+)$", bullet)
        if match:
            items = _split_list(match.group(2))[:MAX_GROUP_ITEMS]
            if items:
                groups.append({"name": _clean_item(match.group(1), 40),
                               "items": items})
    if len(groups) >= 2:
        return {"root": root or "Classification",
                "groups": groups[:MAX_GROUPS]}
    items = inline_items or bullets
    if not root and "classif" not in norm.lower() and "वर्गीकरण" not in norm:
        return None
    if len(items) < 3:
        return None
    return {"root": root or "Classification", "items": items[:6]}


def _mind_payload(raw: str) -> Optional[dict]:
    branches: list[dict] = []
    current: Optional[dict] = None
    for line in raw.splitlines():
        top = _TOP_BULLET_RE.match(line)
        if top:
            current = {"name": _clean_item(top.group(2), 60), "children": []}
            branches.append(current)
            continue
        sub = _SUB_BULLET_RE.match(line)
        if sub and current is not None:
            item = _clean_item(sub.group(1))
            if item and len(current["children"]) < MAX_BRANCH_CHILDREN:
                current["children"].append(item)
    branches = branches[:MAX_BRANCHES]
    children = sum(len(b["children"]) for b in branches)
    if len(branches) < 2 or children < 2:
        return None
    return {"branches": branches}


def _concept_construction(norm: str) -> Optional[dict]:
    """Explicit "X comprises/includes A, B, C" hub statement, if viable."""
    for pattern in _CONCEPT_RES:
        for match in pattern.finditer(norm):
            center = _clean_item(match.group(1), 60)
            satellites = _split_list(match.group(2))
            if len(satellites) >= CONCEPT_MIN_ITEMS:
                return {"center": center,
                        "satellites": satellites[:MAX_SATELLITES]}
    return None


def _concept_payload(norm: str, raw: str, question: str,
                     skip_bullet_fallback: bool = False
                     ) -> Optional[dict]:
    construction = _concept_construction(norm)
    if construction:
        return construction
    if skip_bullet_fallback:
        # A more specific panels reading already fired for these items.
        return None
    bullets = _bullets(raw)
    if not CONCEPT_MIN_ITEMS <= len(bullets) <= CONCEPT_MAX_BULLETS:
        return None
    center = _clean_item(question, 48) or "Key Points"
    return {"center": center, "satellites": bullets[:MAX_SATELLITES]}


def _infographic_payload(raw: str, explanation: str) -> Optional[dict]:
    if len(_norm(explanation)) < INFOGRAPHIC_MIN_EXPL:
        return None
    bullets = _bullets(raw)
    if len(bullets) < INFOGRAPHIC_MIN_BULLETS:
        return None
    return {"points": bullets[:MAX_POINTS]}


_TOP_ITEM_RE = re.compile(
    r"^[ ]{0,1}(?:[•▪·\-*+]|\d{1,2}[.)])\s+(.+?)\s*$")


def _flat_items(raw: str) -> list[str]:
    """Top-level bullets/numbered items (indent of at most one space)."""
    items = []
    for line in raw.splitlines():
        match = _TOP_ITEM_RE.match(line)
        if match:
            item = _clean_item(match.group(1))
            if item:
                items.append(item)
    return items


def _has_sub_bullets(raw: str) -> bool:
    return any(_SUB_BULLET_RE.match(line) for line in raw.splitlines())


_FEATURE_LABELS = (
    "features", "feature", "characteristics", "characteristic",
    "measures", "measure", "provisions", "provision",
    "functions", "function", "dimensions", "dimension",
    "aspects", "aspect", "विशेषताएँ", "विशेषताएं", "विशेषता",
    "लक्षण", "उपाय", "प्रावधान", "आयाम",
)


def _features_frame(intents: tuple[str, ...], raw: str) -> bool:
    """True when the text asks for features/measures-type content.

    Either the question carries the ``features`` interrogative frame or
    the structure text has an explicit features-style labelled section
    ("Features:", "उपाय:", ...).
    """
    if "features" in intents:
        return True
    sections = _labeled_sections(raw, {"features": _FEATURE_LABELS})
    return bool(sections["features"])


def _panels_payload(raw: str, norm: str,
                    intents: tuple[str, ...]) -> Optional[dict]:
    """2x2 panels evidence: exactly four flat items + a features frame.

    Yields to an explicit "X comprises ..." hub statement (a hub
    reading is more faithful there) and never fires for nested lists
    (those belong to mind_map).
    """
    if "recall" in intents:
        return None
    if _has_sub_bullets(raw):
        return None
    items = _flat_items(raw)
    if len(items) != PANELS_COUNT:
        return None
    if not _features_frame(intents, raw):
        return None
    if _concept_construction(norm) is not None:
        return None
    return {"panels": [{"text": item} for item in items]}


_MECHANISM_ROLES: dict[str, tuple[str, ...]] = {
    "source": ("source", "input", "origin", "स्रोत"),
    "delivery": ("delivery", "carrier", "vector", "medium", "वाहक",
                "माध्यम"),
    "target": ("target", "destination", "site", "लक्ष्य"),
    "action": ("action", "process", "reaction", "interaction", "क्रिया",
              "अभिक्रिया"),
    "result": ("result", "output", "outcome", "effect", "परिणाम",
              "प्रभाव"),
}


def _mechanism_role_pattern() -> "re.Pattern[str]":
    variant_to_key: dict[str, str] = {}
    for key, variants in _MECHANISM_ROLES.items():
        for variant in variants:
            variant_to_key[variant.lower()] = key
    ordered = sorted(variant_to_key, key=len, reverse=True)
    return re.compile(
        r"^(?:[•▪·\-*+]\s+|\d{1,2}[.)]\s+)?(%s)\s*[:：]\s*(.+?)\s*$"
        % "|".join(re.escape(v) for v in ordered), re.IGNORECASE)


def _mechanism_segments(raw: str) -> list[dict]:
    """Ordered mechanism stages from the author's own text.

    Role-labelled lines first ("Source: ...", bare or numbered or
    bulleted), else plain numbered items, else plain top-level bullets.
    Order is always the text order; roles are only the labels the text
    itself uses. No stage or role is ever invented.
    """
    role_rx = _mechanism_role_pattern()
    variant_to_key: dict[str, str] = {}
    for key, variants in _MECHANISM_ROLES.items():
        for variant in variants:
            variant_to_key[variant.lower()] = key
    role_lines = []
    for line in raw.splitlines():
        match = role_rx.match(line.strip())
        if match:
            label = _clean_item(match.group(2))
            if label:
                role_lines.append({
                    "label": label,
                    "role": variant_to_key[match.group(1).lower()],
                    "role_text": match.group(1).strip(),
                })
    if len(role_lines) >= MECHANISM_MIN_STAGES:
        return role_lines[:MAX_STEPS]
    numbered = _numbered_items(raw)
    if len(numbered) >= MECHANISM_MIN_STAGES:
        return [{"label": label, "role": None, "role_text": ""}
                for label in numbered[:MAX_STEPS]]
    flat = _flat_items(raw)
    if len(flat) >= MECHANISM_MIN_STAGES:
        return [{"label": label, "role": None, "role_text": ""}
                for label in flat[:MAX_STEPS]]
    return []


def _mechanism_payload(raw: str,
                       intents: tuple[str, ...]) -> Optional[dict]:
    """Mechanism evidence: >=3 explicit ordered stages plus either a
    mechanism interrogative frame or >=2 distinct author-given roles."""
    segments = _mechanism_segments(raw)
    if len(segments) < MECHANISM_MIN_STAGES:
        return None
    roles = {seg["role"] for seg in segments if seg["role"]}
    if "mechanism" not in intents and len(roles) < 2:
        return None
    return {"stages": segments}


_SPATIAL_RES = (
    re.compile(r"\bvia\b", re.IGNORECASE),
    re.compile(r"\broute\b", re.IGNORECASE),
    re.compile(r"\bflows?\s+from\b", re.IGNORECASE),
    re.compile(r"\bpass(?:es|ed)?\s+through\b", re.IGNORECASE),
    re.compile(r"\bjoins?\b", re.IGNORECASE),
    re.compile(r"\bruns?\s+(from|through|to)\b", re.IGNORECASE),
    re.compile(r"\bconnects?\b", re.IGNORECASE),
    re.compile(r"\breaches?\b", re.IGNORECASE),
    re.compile(r"\bfrom\b.{0,60}\bto\b", re.IGNORECASE),
    re.compile(r"मार्ग"),
    re.compile(r"होकर"),
    re.compile(r"बहती"),
    re.compile(r"बहता"),
    re.compile(r"मिलती"),
    re.compile(r"मिलता"),
    re.compile(r"जोड़ता"),
    re.compile(r"जोड़ती"),
    re.compile(r"से\s+.+\s+तक"),
)


def _chain_links(text: str) -> list[dict]:
    """Ordered distinct places, or [] without movement phrasing."""
    if not any(rx.search(text) for rx in _SPATIAL_RES):
        return []
    links = []
    seen: set[str] = set()
    for place in _ordered_places(text):
        if place["id"] not in seen:
            seen.add(place["id"])
            links.append(place)
    return links


def _chain_payload(raw: str, expl_text: str) -> Optional[dict]:
    """Route-chain evidence: >=3 distinct curated places in mention
    order plus spatial-movement phrasing. No coordinates are involved:
    the chain order is the author's own mention order. The
    explanation's order wins when it independently evidences the
    chain (endpoint summaries in the question must not scramble the
    solution's route order); otherwise question+explanation order.
    """
    links = _chain_links(expl_text)
    if len(links) < CHAIN_MIN_LINKS:
        links = _chain_links(raw)
    if len(links) < CHAIN_MIN_LINKS:
        return None
    payload: dict = {"links": [_place_payload(p)
                               for p in links[:MAX_LINKS]]}
    if len(links) > MAX_LINKS:
        payload["shown_of"] = [MAX_LINKS, len(links)]
    return payload


def _route_covers(routes: list[dict], link_ids: list[str]) -> bool:
    """True when a sourced route connects every chain link."""
    wanted = set(link_ids)
    return any(wanted <= set(r.get("place_ids", [])) for r in routes)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _place_payload(place: dict) -> dict:
    payload = {
        "id": place["id"],
        "name_en": place["name_en"],
        "name_hi": place["name_hi"],
        "kind": place["kind"],
        "lon": place["lon"],
        "lat": place["lat"],
    }
    if place.get("at_en"):
        payload["at_en"] = place["at_en"]
        payload["at_hi"] = place.get("at_hi", "")
    return payload


def _title_for(question: str, subject: Optional[str]) -> str:
    title = textstyle.clean_label(question, MAX_TITLE_CHARS)
    if title:
        return title
    return f"{subject_label(subject)} Visual"


def decide_visual(question: object,
                  options: tuple = (),
                  explanation: object = "",
                  subject_hint: Optional[str] = None,
                  correct_answer: object = "") -> Optional[VisualSpec]:
    """Decide the single best visual for a solution, or None.

    The decision is driven by the question's interrogative intent plus
    structural evidence in the explanation (steps, bullets, years,
    labelled sections, ordered place mentions). `options` are used
    only for subject detection and `correct_answer` only to focus a
    map on the answered place -- neither ever contributes visual
    content or initiates a visual on its own. Deterministic:
    identical inputs always yield an identical spec (or identical
    None).
    """
    question_text = _norm(question)
    expl_text = _norm(explanation)
    raw = "%s\n%s" % (question or "", explanation or "")
    norm = "%s %s" % (question_text, expl_text)

    intents = classify_intent(question_text)
    if "assertion" in intents:
        # Assertion/matching questions hold unevaluated claims, so only
        # the authoritative explanation may supply structure evidence.
        struct_raw = str(explanation or "")
        struct_norm = expl_text
    else:
        struct_raw = raw
        struct_norm = norm

    subjects = load_subjects()["subjects"]
    valid_ids = {entry["id"] for entry in subjects}
    if subject_hint in valid_ids:
        subject = subject_hint
    else:
        detect_blob = question_text
        if options:
            detect_blob += " " + " ".join(str(o) for o in options)
        detect_blob += " " + expl_text
        subject = detect_subject(detect_blob)

    # -- maps (accuracy-gated) ----------------------------------------
    q_places = find_places(question_text)
    places = q_places or find_places(expl_text)
    usable = usable_places(places)
    located_q = _is_location_question(question_text)
    recall_block = (
        "recall" in intents
        and not located_q
        and not _has_location_phrasing(expl_text)
    )
    map_allowed = (
        bool(usable)
        and not _is_extent_question(question_text)
        and not recall_block
        and (subject in MAP_SUBJECTS or subject is None or located_q)
    )
    map_routes: list[dict] = []
    if map_allowed:
        assert usable  # for type-checkers; guaranteed by map_allowed
        shown = usable[:MAX_PLACES]
        base_id = select_base(shown)
        base_bbox = load_base_maps()["bases"][base_id]["bbox"]
        for route in find_routes(question_text + "\n" + expl_text):
            verts = route["vertices"]
            inside = sum(1 for lon, lat in verts
                         if point_in_bbox(lon, lat, base_bbox))
            if inside >= 2:
                map_routes.append(route)
        payload: dict = {
            "base": base_id,
            "places": [_place_payload(p) for p in shown],
            "routes": [{"id": r["id"], "name_en": r["name_en"],
                        "name_hi": r.get("name_hi", ""),
                        "vertices": [[lon, lat] for lon, lat in
                                     r["vertices"]]}
                       for r in map_routes],
        }
        notes: list[str] = []
        if len(usable) > MAX_PLACES:
            notes.append("showing %d of %d places"
                         % (MAX_PLACES, len(usable)))
        focus: Optional[str] = None
        usable_ids = {p["id"] for p in usable}
        for place in usable_places(find_places(str(correct_answer or ""))):
            if place["id"] in usable_ids:
                focus = place["id"]
                break
        payload["focus"] = focus
        visual = (VisualType.REGIONAL_MAP.value if len(usable) >= 2
                  else VisualType.LOCATION_MAP.value)
        map_spec: Optional[VisualSpec] = VisualSpec(
            visual_type=visual, subject=subject,
            title=_title_for(question_text, subject),
            payload=payload, notes=tuple(notes),
            evidence=("intent=%s" % _intent_tag(intents),
                      "places=%d" % len(usable),
                      "focus=%s" % (focus or "none"))
            + ((("routes=%d" % len(map_routes),)
                if map_routes else ())))
    else:
        map_spec = None

    # -- structure visuals (content-derived) ---------------------------
    if len(struct_norm.strip()) < MIN_STRUCTURE_CHARS:
        return map_spec

    candidates: dict[str, dict] = {}
    bits: dict[str, str] = {}
    timeline = _timeline_payload(struct_raw, struct_norm)
    if timeline:
        candidates[VisualType.TIMELINE.value] = timeline
        bits[VisualType.TIMELINE.value] = "years=%d" % len(
            timeline["events"])
    wants_flow = not ("features" in intents
                      and "process" not in intents
                      and "mechanism" not in intents)
    if wants_flow:
        steps = _steps_payload(struct_raw, struct_norm)
        if steps:
            steps_type = _steps_type(subject, struct_norm)
            candidates[steps_type] = steps
            bits[steps_type] = "steps=%d" % len(steps["steps"])
    mechanism = _mechanism_payload(struct_raw, intents)
    if mechanism:
        candidates[VisualType.MECHANISM.value] = mechanism
        roles = {s["role"] for s in mechanism["stages"] if s["role"]}
        bits[VisualType.MECHANISM.value] = "stages=%d/roles=%d" % (
            len(mechanism["stages"]), len(roles))
    comparison = _comparison_payload(struct_norm, struct_raw)
    if comparison:
        candidates[VisualType.COMPARISON.value] = comparison
        bits[VisualType.COMPARISON.value] = "sides=2/points=%d" % (
            len(comparison["left_points"])
            + len(comparison["right_points"]) + len(comparison["common"]))
    cause_effect = _cause_effect_payload(struct_raw)
    if cause_effect:
        candidates[VisualType.CAUSE_EFFECT.value] = cause_effect
        bits[VisualType.CAUSE_EFFECT.value] = "causes=%d/effects=%d" % (
            len(cause_effect["causes"]), len(cause_effect["effects"]))
    cycle = _cycle_payload(struct_norm, struct_raw, intents)
    if cycle:
        candidates[VisualType.CYCLE.value] = cycle
        bits[VisualType.CYCLE.value] = "stages=%d" % len(cycle["stages"])
    classification = _classification_payload(struct_norm, struct_raw)
    if classification:
        candidates[VisualType.CLASSIFICATION.value] = classification
        if classification.get("groups"):
            bits[VisualType.CLASSIFICATION.value] = "groups=%d" % len(
                classification["groups"])
        else:
            bits[VisualType.CLASSIFICATION.value] = "items=%d" % len(
                classification["items"])
    chain = _chain_payload(struct_raw, expl_text)
    if chain:
        candidates[VisualType.SPATIAL_CHAIN.value] = chain
        bits[VisualType.SPATIAL_CHAIN.value] = "links=%d" % len(
            chain["links"])
    panels = _panels_payload(struct_raw, struct_norm, intents)
    if panels:
        candidates[VisualType.PANELS.value] = panels
        bits[VisualType.PANELS.value] = "panels=%d" % len(
            panels["panels"])
    if "recall" not in intents:
        mind = _mind_payload(struct_raw)
        if mind:
            candidates[VisualType.MIND_MAP.value] = mind
            bits[VisualType.MIND_MAP.value] = "branches=%d/children=%d" % (
                len(mind["branches"]),
                sum(len(b["children"]) for b in mind["branches"]))
        concept = _concept_payload(struct_norm, struct_raw, question_text,
                                   skip_bullet_fallback=panels is not None)
        if concept:
            candidates[VisualType.CONCEPT_MAP.value] = concept
            if _concept_construction(struct_norm) is not None:
                bits[VisualType.CONCEPT_MAP.value] = "satellites=%d" % len(
                    concept["satellites"])
            else:
                bits[VisualType.CONCEPT_MAP.value] = "bullets=%d" % len(
                    concept["satellites"])
        infographic = _infographic_payload(struct_raw,
                                           str(explanation or ""))
        if infographic:
            candidates[VisualType.INFOGRAPHIC.value] = infographic
            bits[VisualType.INFOGRAPHIC.value] = "points=%d" % len(
                infographic["points"])

    if not candidates:
        return map_spec
    preferred: list[str] = []
    if subject:
        for entry in subjects:
            if entry["id"] == subject:
                preferred = [t for t in entry["preferred"]
                             if t in SUPPORTED_TYPES]
                break
    order = [t for t in preferred if t not in FALLBACK_TYPES]
    order += [t for t in GENERIC_ORDER
              if t not in preferred and t not in FALLBACK_TYPES
              and t in SUPPORTED_TYPES]
    order += [t for t in FALLBACK_TYPES if t in SUPPORTED_TYPES]
    for visual_type in order:
        if visual_type in candidates:
            struct_spec = VisualSpec(
                visual_type=visual_type, subject=subject,
                title=_title_for(question_text, subject),
                payload=candidates[visual_type], notes=(),
                evidence=("intent=%s" % _intent_tag(intents),
                          bits[visual_type]))
            if map_spec is not None and not (
                    set(intents) & STRUCTURE_OVERRIDE_FRAMES):
                if (visual_type == VisualType.SPATIAL_CHAIN.value
                        and "route" in intents
                        and "location" not in intents
                        and chain is not None
                        and not _route_covers(
                            map_routes,
                            [link["id"] for link in chain["links"]])):
                    # A route ask wants the ordered journey: dots on a
                    # map cannot show it, so the chain wins -- unless
                    # the question is an explicit where-ask or a
                    # sourced route covers the same links (then the
                    # map draws the real geometry).
                    return struct_spec
                return map_spec
            return struct_spec
    return map_spec


def safe_decide_visual(question: object,
                       options: tuple = (),
                       explanation: object = "",
                       subject_hint: Optional[str] = None,
                       correct_answer: object = ""
                       ) -> Optional[VisualSpec]:
    """Wrapper that never raises: returns None on any internal error.

    Future PDF wiring must use this so visuals stay strictly optional.
    """
    try:
        return decide_visual(question, options, explanation, subject_hint,
                             correct_answer)
    except Exception:
        logger.exception("Visual decision failed; continuing without visual")
        return None
