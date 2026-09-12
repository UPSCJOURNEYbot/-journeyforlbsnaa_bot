"""Decision engine: when is a visual useful, and which type?

The engine is intentionally conservative (accuracy-first):

* Maps are emitted only for places in the curated dataset that fall
  inside a region with a base map. Unknown places, regions without a
  base map, and extent/boundary questions all yield ``None``.
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
    "flowchart",
    "process",
    "cycle",
    "cause_effect",
    "mind_map",
    "concept_map",
    "infographic",
)

# Generic bullet-derived visuals are always tried last (per-type and
# per-subject): an explicit comparison/timeline/etc. signal must beat a
# mere "there are bullets" fallback.
FALLBACK_TYPES = ("mind_map", "concept_map", "infographic")

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

    def to_dict(self) -> dict:
        return {
            "visual_type": self.visual_type,
            "subject": self.subject,
            "title": self.title,
            "payload": json.loads(json.dumps(self.payload)),
            "notes": list(self.notes),
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


def _is_extent_question(question: str) -> bool:
    return any(rx.search(question) for rx in _EXTENT_RES)


def _is_location_question(question: str) -> bool:
    return any(rx.search(question) for rx in _LOCATION_RES)


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
)

_CAUSE_LABELS = {
    "causes": ("causes", "reasons", "कारण"),
    "effects": ("effects", "results", "consequences", "outcome",
                "प्रभाव", "परिणाम"),
}

_CYCLE_RES = (
    re.compile(r"\bcycl\w*\b", re.IGNORECASE),
    re.compile(r"चक्र"),
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
            cand_left = _clean_item(match.group(1), 60).strip("\"' ")
            cand_right = _clean_item(match.group(2), 60).strip("\"' ")
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


def _cycle_payload(norm: str, raw: str) -> Optional[dict]:
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


def _concept_payload(norm: str, raw: str, question: str) -> Optional[dict]:
    for pattern in _CONCEPT_RES:
        for match in pattern.finditer(norm):
            center = _clean_item(match.group(1), 60)
            satellites = _split_list(match.group(2))
            if len(satellites) >= CONCEPT_MIN_ITEMS:
                return {"center": center,
                        "satellites": satellites[:MAX_SATELLITES]}
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
                  subject_hint: Optional[str] = None) -> Optional[VisualSpec]:
    """Decide the single best visual for a solution, or None.

    `options` are used only for subject detection -- never for visual
    content. Deterministic: identical inputs always yield an identical
    spec (or identical None).
    """
    question_text = _norm(question)
    expl_text = _norm(explanation)
    raw = "%s\n%s" % (question or "", explanation or "")
    norm = "%s %s" % (question_text, expl_text)

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
    map_allowed = (
        bool(usable)
        and not _is_extent_question(question_text)
        and (subject in MAP_SUBJECTS or subject is None
             or _is_location_question(question_text))
    )
    if map_allowed:
        assert usable  # for type-checkers; guaranteed by map_allowed
        shown = usable[:MAX_PLACES]
        payload: dict = {
            "base": shown[0]["region"],
            "places": [_place_payload(p) for p in shown],
        }
        notes: list[str] = []
        if len(usable) > MAX_PLACES:
            notes.append("showing %d of %d places"
                         % (MAX_PLACES, len(usable)))
        visual = (VisualType.REGIONAL_MAP.value if len(usable) >= 2
                  else VisualType.LOCATION_MAP.value)
        return VisualSpec(visual_type=visual, subject=subject,
                          title=_title_for(question_text, subject),
                          payload=payload, notes=tuple(notes))

    # -- structure visuals (content-derived) ---------------------------
    if len(norm.strip()) < MIN_STRUCTURE_CHARS:
        return None

    candidates: dict[str, dict] = {}
    timeline = _timeline_payload(raw, norm)
    if timeline:
        candidates[VisualType.TIMELINE.value] = timeline
    steps = _steps_payload(raw, norm)
    if steps:
        candidates[_steps_type(subject, norm)] = steps
    comparison = _comparison_payload(norm, raw)
    if comparison:
        candidates[VisualType.COMPARISON.value] = comparison
    cause_effect = _cause_effect_payload(raw)
    if cause_effect:
        candidates[VisualType.CAUSE_EFFECT.value] = cause_effect
    cycle = _cycle_payload(norm, raw)
    if cycle:
        candidates[VisualType.CYCLE.value] = cycle
    classification = _classification_payload(norm, raw)
    if classification:
        candidates[VisualType.CLASSIFICATION.value] = classification
    mind = _mind_payload(raw)
    if mind:
        candidates[VisualType.MIND_MAP.value] = mind
    concept = _concept_payload(norm, raw, question_text)
    if concept:
        candidates[VisualType.CONCEPT_MAP.value] = concept
    infographic = _infographic_payload(raw, str(explanation or ""))
    if infographic:
        candidates[VisualType.INFOGRAPHIC.value] = infographic

    if not candidates:
        return None
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
            return VisualSpec(
                visual_type=visual_type, subject=subject,
                title=_title_for(question_text, subject),
                payload=candidates[visual_type], notes=())
    return None


def safe_decide_visual(question: object,
                       options: tuple = (),
                       explanation: object = "",
                       subject_hint: Optional[str] = None
                       ) -> Optional[VisualSpec]:
    """Wrapper that never raises: returns None on any internal error.

    Future PDF wiring must use this so visuals stay strictly optional.
    """
    try:
        return decide_visual(question, options, explanation, subject_hint)
    except Exception:
        logger.exception("Visual decision failed; continuing without visual")
        return None
