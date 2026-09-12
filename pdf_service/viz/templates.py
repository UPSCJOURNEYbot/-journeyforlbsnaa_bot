"""Deterministic diagram layouts + fpdf2 drawing for non-map visuals.

Every template has two halves:

* ``layout_*`` -- pure functions: item counts + body width -> element
  list + exact height. Unit-testable without any PDF object.
* ``draw_diagram`` -- renders a :class:`VisualSpec` into a rect on a
  document (real fpdf2 or a duck-typed fake) using only fixed metrics,
  so :func:`estimate_height_for_spec` is exactly the drawn height and a
  future caller can guarantee page-break safety.

Elements are plain dicts (``box`` / ``line`` / ``arrow`` / ``dot``) so
tests can assert geometry (no overlap, in-bounds) directly.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ..render import BRAND
from . import textstyle
from .engine import VisualSpec

NAVY = (20, 40, 90)
INK = (30, 30, 30)
MUTED = (110, 110, 110)
LINE = (150, 160, 175)
FILL = (245, 247, 250)
ACCENT = (22, 101, 52)
ACCENT_BG = (231, 244, 233)
WHITE = (255, 255, 255)

TITLE_H = 8.0
BRAND_H = 4.0
FRAME_PAD = 3.0
GAP = 2.5


class Box(NamedTuple):
    x: float
    y: float
    w: float
    h: float


def boxes_overlap(a: Box, b: Box, margin: float = 0.0) -> bool:
    """True when boxes a/b intersect (with optional extra margin)."""
    return not (a.x + a.w + margin <= b.x or b.x + b.w + margin <= a.x
                or a.y + a.h + margin <= b.y or b.y + b.h + margin <= a.y)


def _box(x: float, y: float, w: float, h: float, text: str = "",
         size: float = 8.5, bold: bool = False, fill: Any = FILL,
         line: Any = LINE, color: Any = INK, align: str = "C",
         max_lines: int = 2) -> dict:
    return {"k": "box", "box": Box(x, y, w, h), "text": text, "size": size,
            "bold": bold, "fill": fill, "line": line, "color": color,
            "align": align, "max_lines": max_lines}


def _line(p1: tuple, p2: tuple, color: Any = LINE,
          lw: float = 0.3) -> dict:
    return {"k": "line", "p1": p1, "p2": p2, "color": color, "lw": lw}


def _arrow(p1: tuple, p2: tuple, color: Any = NAVY,
           lw: float = 0.35) -> dict:
    return {"k": "arrow", "p1": p1, "p2": p2, "color": color, "lw": lw}


def _dot(center: tuple, r: float, fill: Any = NAVY) -> dict:
    return {"k": "dot", "c": center, "r": r, "fill": fill}


# ---------------------------------------------------------------------------
# Layouts (pure)
# ---------------------------------------------------------------------------

def layout_steps(n: int, x: float, y: float, w: float,
                 numbered: bool = True) -> tuple[list[dict], float]:
    """Vertical step boxes with down-arrows. Returns (elements, height)."""
    box_h, arrow = 10.0, 3.0
    elements: list[dict] = []
    yy = y
    for i in range(max(0, n)):
        elements.append(_box(x, yy, w, box_h, "__TEXT__%d" % i))
        if numbered:
            elements[-1]["number"] = i + 1
        yy += box_h
        if i < n - 1:
            elements.append(_arrow((x + w / 2, yy + 0.4),
                                   (x + w / 2, yy + arrow - 0.4)))
            yy += arrow
    return elements, (yy - y) if n else 0.0


def layout_timeline(n: int, x: float, y: float, w: float
                    ) -> tuple[list[dict], float]:
    """Vertical spine, alternating-side event boxes + year chips."""
    row_h, gap, side = 12.0, 3.0, 9.0
    cx = x + w / 2.0
    box_w = (w - 2 * side) / 2.0
    elements: list[dict] = []
    yy = y
    for i in range(max(0, n)):
        right = (i % 2 == 0)
        bx = cx + side if right else cx - side - box_w
        elements.append(_box(bx, yy, box_w, row_h, "__TEXT__%d" % i,
                             align="L" if right else "R"))
        elements.append(_box(cx - 8.0, yy + 1.0, 16.0, 5.0,
                             "__YEAR__%d" % i, size=6.5, bold=True,
                             fill=NAVY, line=NAVY, color=WHITE))
        dot_x = bx if right else bx + box_w
        elements.append(_dot((dot_x, yy + row_h / 2.0), 1.4))
        elements.append(_line((cx, yy + row_h / 2.0),
                              (dot_x, yy + row_h / 2.0), color=NAVY,
                              lw=0.3))
        yy += row_h + gap
    if n:
        elements.append(_line((cx, y), (cx, yy - gap), color=NAVY, lw=0.5))
        return elements, yy - gap - y
    return elements, 0.0


def layout_comparison(n_left: int, n_right: int, n_common: int, x: float,
                      y: float, w: float) -> tuple[list[dict], float]:
    """Two-column headers + rows, then an optional shared strip."""
    header_h, row_h, gap = 8.0, 9.0, 2.0
    col_w = (w - gap) / 2.0
    elements = [
        _box(x, y, col_w, header_h, "__LEFT__", bold=True, fill=NAVY,
             line=NAVY, color=WHITE),
        _box(x + col_w + gap, y, col_w, header_h, "__RIGHT__", bold=True,
             fill=ACCENT, line=ACCENT, color=WHITE),
    ]
    yy = y + header_h + gap
    rows = max(n_left, n_right)
    for i in range(rows):
        if i < n_left:
            elements.append(_box(x, yy, col_w, row_h, "__L%d" % i,
                                 align="L", max_lines=2))
        if i < n_right:
            elements.append(_box(x + col_w + gap, yy, col_w, row_h,
                                 "__R%d" % i, align="L", max_lines=2))
        yy += row_h + gap
    if n_common:
        if rows:
            yy -= gap
            yy += gap
        for i in range(n_common):
            elements.append(_box(x, yy, w, 7.0, "__C%d" % i, size=7.5,
                                 fill=ACCENT_BG, align="L", max_lines=1))
            yy += 7.0 + gap
    return elements, (yy - gap - y) if (rows or n_common) else header_h


def layout_cause_effect(n_causes: int, n_effects: int, x: float, y: float,
                        w: float) -> tuple[list[dict], float]:
    """Fishbone-lite: causes above a spine, effects below, topic centre."""
    box_h, gap, per_row = 9.0, 2.5, 2
    col_w = (w - gap) / per_row
    elements: list[dict] = []
    yy = y
    cause_rows = (max(0, n_causes) + per_row - 1) // per_row
    for r in range(cause_rows):
        for c in range(per_row):
            i = r * per_row + c
            if i >= n_causes:
                break
            elements.append(_box(x + c * (col_w + gap), yy, col_w, box_h,
                                 "__CAUSE__%d" % i, align="L",
                                 max_lines=2))
        yy += box_h + gap
    spine_y = yy + 4.0
    topic_w = min(64.0, w * 0.6)
    elements.append(_line((x, spine_y), (x + w, spine_y), color=NAVY,
                          lw=0.5))
    elements.append(_arrow((x + w - 0.5, spine_y), (x + w + 0.4, spine_y)))
    elements.append(_box(x + (w - topic_w) / 2.0, spine_y - 4.0, topic_w,
                         8.0, "__TOPIC__", bold=True, fill=NAVY,
                         line=NAVY, color=WHITE))
    for i in range(n_causes):
        bx = x + (i % per_row) * (col_w + gap)
        elements.append(_line((bx + col_w / 2.0, yy - gap),
                              (x + w * (0.25 + 0.5 * (i / max(1, n_causes - 1)
                                                      if n_causes > 1
                                                      else 0.5)),
                               spine_y), color=LINE, lw=0.3))
    yy = spine_y + 4.0 + gap
    effect_rows = (max(0, n_effects) + per_row - 1) // per_row
    for r in range(effect_rows):
        for c in range(per_row):
            i = r * per_row + c
            if i >= n_effects:
                break
            elements.append(_box(x + c * (col_w + gap), yy, col_w, box_h,
                                 "__EFFECT__%d" % i, align="L",
                                 fill=ACCENT_BG, max_lines=2))
        yy += box_h + gap
    return elements, yy - gap - y


def layout_cycle(n: int, x: float, y: float, w: float
                 ) -> tuple[list[dict], float]:
    """Nodes evenly spaced on an ellipse with circular arrows."""
    import math

    height = min(58.0, max(32.0, w * 0.5))
    node_w, node_h = 30.0, 11.0
    cx, cy = x + w / 2.0, y + height / 2.0
    rx = max(8.0, w / 2.0 - node_w / 2.0 - 2.0)
    ry = max(6.0, height / 2.0 - node_h / 2.0 - 2.0)
    elements: list[dict] = []
    centres = []
    for i in range(max(0, n)):
        angle = -math.pi / 2.0 + 2 * math.pi * i / max(1, n)
        px = cx + rx * math.cos(angle)
        py = cy + ry * math.sin(angle)
        centres.append((px, py))
        elements.append(_box(px - node_w / 2.0, py - node_h / 2.0, node_w,
                             node_h, "__TEXT__%d" % i, size=7.5,
                             max_lines=2))
    for i in range(n):
        p1, p2 = centres[i], centres[(i + 1) % n]
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        dist = math.hypot(dx, dy) or 1.0
        trim = 11.0
        q1 = (p1[0] + dx / dist * trim, p1[1] + dy / dist * trim)
        q2 = (p2[0] - dx / dist * (trim + 1.0),
              p2[1] - dy / dist * (trim + 1.0))
        elements.append(_arrow(q1, q2))
    return elements, height


def layout_concept(n: int, x: float, y: float, w: float
                   ) -> tuple[list[dict], float]:
    """Centre box with satellites in deterministic grid slots."""
    sat_w = min(38.0, (w - 2 * GAP) / 3.0)
    sat_h, row_gap = 10.0, 6.0
    slots = [  # (col 0..2, row 0..2) in fill order
        (0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2),
    ]
    rows_used = {1}
    for i in range(min(n, len(slots))):
        rows_used.add(slots[i][1])
    top = min(rows_used)
    rows = sorted(rows_used)
    row_y = {r: y + (r - top) * (sat_h + row_gap) for r in rows}
    height = (max(rows) - top) * (sat_h + row_gap) + sat_h
    col_x = [x, x + (w - sat_w) / 2.0, x + w - sat_w]
    mid_y = row_y[1] if 1 in row_y else y
    centre = Box(x + (w - 40.0) / 2.0, mid_y, 40.0, sat_h)
    elements = [_box(*centre, "__CENTER__", bold=True, fill=NAVY,
                     line=NAVY, color=WHITE)]
    cx, cy = centre.x + centre.w / 2.0, centre.y + centre.h / 2.0
    for i in range(min(n, len(slots))):
        col, row = slots[i]
        sx = col_x[col] if not (row == 1 and col == 1) else col_x[col]
        sy = row_y[row]
        elements.append(_box(sx, sy, sat_w, sat_h, "__TEXT__%d" % i,
                             size=7.5, max_lines=2))
        elements.append(_line((cx, cy), (sx + sat_w / 2.0,
                                        sy + sat_h / 2.0)))
    return elements, height


def layout_mind(branches: list[int], x: float, y: float, w: float
                ) -> tuple[list[dict], float]:
    """Centre box; branches alternate left/right with stacked children."""
    name_w, name_h, child_h, gap = 34.0, 8.0, 7.0, 2.0
    left_x, right_x = x, x + w - name_w
    centre = Box(x + (w - 36.0) / 2.0, y, 36.0, 10.0)
    elements = [_box(*centre, "__CENTER__", bold=True, fill=NAVY,
                     line=NAVY, color=WHITE)]
    cx, cy = centre.x + centre.w / 2.0, centre.y + centre.h / 2.0
    col_y = [y, y]
    for i, n_children in enumerate(branches):
        side = i % 2  # 0 = left, 1 = right
        bx = left_x if side == 0 else right_x
        by = col_y[side]
        elements.append(_box(bx, by, name_w, name_h, "__B%d" % i,
                             bold=True, fill=ACCENT_BG, size=7.5,
                             max_lines=1))
        elements.append(_line((cx, cy), (bx + name_w / 2.0, by + name_h
                                                            / 2.0)))
        yy = by + name_h + 1.5
        for c in range(n_children):
            elements.append(_box(bx, yy, name_w, child_h, "__B%dC%d" % (i, c),
                                 size=7.0, max_lines=1))
            yy += child_h + 1.5
        col_y[side] = yy + gap
    height = max(col_y[0], col_y[1], y + centre.h) - gap - y
    return elements, max(height, centre.h)


def layout_classification(levels: list[int], x: float, y: float, w: float,
                          root_w: float = 0.0) -> tuple[list[dict], float]:
    """Root box with evenly split child rows (levels = counts per row)."""
    root_h, row_h, gap = 9.0, 8.0, 4.0
    rw = root_w or min(64.0, w * 0.6)
    elements = [_box(x + (w - rw) / 2.0, y, rw, root_h, "__ROOT__",
                     bold=True, fill=NAVY, line=NAVY, color=WHITE)]
    yy = y + root_h + gap
    prev_centres = [x + w / 2.0]
    for level, count in enumerate(levels):
        if count <= 0:
            continue
        cw = (w - (count - 1) * GAP) / count
        centres = []
        for i in range(count):
            bx = x + i * (cw + GAP)
            elements.append(_box(bx, yy, cw, row_h, "__L%dI%d" % (level, i),
                                 size=7.5, max_lines=2))
            centres.append(bx + cw / 2.0)
        parent = prev_centres[0] if len(prev_centres) == 1 else None
        for cx in centres:
            anchor = parent if parent is not None else x + w / 2.0
            elements.append(_line((anchor, yy - gap), (cx, yy)))
        prev_centres = centres
        yy += row_h + gap
    return elements, yy - gap - y


def layout_classification_grouped(counts: list[int], x: float, y: float,
                                  w: float) -> tuple[list[dict], float]:
    """Root box with labelled group columns (counts = items per group)."""
    root_h, name_h, item_h, gap = 9.0, 7.0, 7.0, 2.5
    groups = max(1, len(counts))
    col_w = (w - (groups - 1) * GAP) / groups
    rw = min(64.0, w * 0.6)
    elements = [_box(x + (w - rw) / 2.0, y, rw, root_h, "__ROOT__",
                     bold=True, fill=NAVY, line=NAVY, color=WHITE)]
    yy = y + root_h + gap
    max_items = max(counts) if counts else 0
    for i, count in enumerate(counts):
        bx = x + i * (col_w + GAP)
        elements.append(_box(bx, yy, col_w, name_h, "__L0I%d" % i,
                             bold=True, fill=ACCENT_BG, size=7.5,
                             max_lines=1))
        elements.append(_line((x + w / 2.0, y + root_h),
                              (bx + col_w / 2.0, yy)))
        iy = yy + name_h + 1.5
        for j in range(count):
            elements.append(_box(bx, iy, col_w, item_h, "__G%dJ%d" % (i, j),
                                 size=7.0, max_lines=1))
            iy += item_h + 1.5
    total = root_h + gap + name_h + 1.5
    if max_items:
        total += max_items * (item_h + 1.5) - 1.5
    return elements, total


def layout_infographic(n: int, x: float, y: float, w: float
                       ) -> tuple[list[dict], float]:
    """Numbered point rows with accent chips."""
    row_h, gap = 8.0, 1.5
    elements: list[dict] = []
    yy = y
    for i in range(max(0, n)):
        elements.append(_box(x, yy, 6.0, 6.0, str(i + 1), size=7.0,
                             bold=True, fill=NAVY, line=NAVY, color=WHITE,
                             max_lines=1))
        elements.append(_box(x + 8.0, yy, w - 8.0, row_h, "__TEXT__%d" % i,
                             size=8.0, align="L", fill=None, line=None,
                             max_lines=2))
        yy += row_h + gap
    return elements, (yy - gap - y) if n else 0.0


# ---------------------------------------------------------------------------
# Measuring + frame + dispatch
# ---------------------------------------------------------------------------


def _layout_for_spec(spec: VisualSpec, x: float, y: float, w: float
                     ) -> tuple[list[dict], float]:
    """Pure layout for a spec (placeholder texts resolved by caller)."""
    payload = spec.payload
    kind = spec.visual_type
    if kind in ("process", "flowchart"):
        return layout_steps(len(payload.get("steps", [])), x, y, w)
    if kind == "timeline":
        return layout_timeline(len(payload.get("events", [])), x, y, w)
    if kind == "comparison":
        return layout_comparison(len(payload.get("left_points", [])),
                                 len(payload.get("right_points", [])),
                                 len(payload.get("common", [])), x, y, w)
    if kind == "cause_effect":
        return layout_cause_effect(len(payload.get("causes", [])),
                                   len(payload.get("effects", [])), x, y, w)
    if kind == "cycle":
        return layout_cycle(len(payload.get("stages", [])), x, y, w)
    if kind == "concept_map":
        return layout_concept(len(payload.get("satellites", [])), x, y, w)
    if kind == "mind_map":
        return layout_mind([len(b.get("children", []))
                            for b in payload.get("branches", [])], x, y, w)
    if kind == "classification":
        groups = payload.get("groups", [])
        if groups:
            return layout_classification_grouped(
                [len(g.get("items", [])) for g in groups], x, y, w)
        return layout_classification([len(payload.get("items", []))], x, y, w)
    if kind == "infographic":
        return layout_infographic(len(payload.get("points", [])), x, y, w)
    raise ValueError(f"No template for visual type: {kind!r}")


def estimate_height_for_spec(spec: VisualSpec, width: float) -> float:
    """Exact total container height for `spec` at `width` (mm)."""
    body_w = width - 2 * FRAME_PAD
    _elements, body_h = _layout_for_spec(spec, 0.0, 0.0, body_w)
    return round(TITLE_H + 2.0 + body_h + 2.0 + BRAND_H, 1)


def _resolve_texts(spec: VisualSpec, elements: list[dict]) -> None:
    """Replace __PLACEHOLDER__ box texts with payload content (in place)."""
    payload = spec.payload
    kind = spec.visual_type
    mapping: dict[str, str] = {}
    if kind in ("process", "flowchart"):
        for i, step in enumerate(payload.get("steps", [])):
            mapping["__TEXT__%d" % i] = "%d. %s" % (
                i + 1, textstyle.clean_label(step.get("label", ""), 90))
    elif kind == "timeline":
        for i, event in enumerate(payload.get("events", [])):
            mapping["__TEXT__%d" % i] = textstyle.clean_label(
                event.get("label", ""), 90)
            mapping["__YEAR__%d" % i] = str(event.get("year", ""))
    elif kind == "comparison":
        mapping["__LEFT__"] = textstyle.clean_label(
            payload.get("left_title", ""), 40)
        mapping["__RIGHT__"] = textstyle.clean_label(
            payload.get("right_title", ""), 40)
        for i, point in enumerate(payload.get("left_points", [])):
            mapping["__L%d" % i] = textstyle.clean_label(point, 90)
        for i, point in enumerate(payload.get("right_points", [])):
            mapping["__R%d" % i] = textstyle.clean_label(point, 90)
        for i, point in enumerate(payload.get("common", [])):
            mapping["__C%d" % i] = textstyle.clean_label(point, 120)
    elif kind == "cause_effect":
        mapping["__TOPIC__"] = textstyle.clean_label(
            payload.get("topic", "") or spec.title, 48)
        for i, cause in enumerate(payload.get("causes", [])):
            mapping["__CAUSE__%d" % i] = textstyle.clean_label(cause, 80)
        for i, effect in enumerate(payload.get("effects", [])):
            mapping["__EFFECT__%d" % i] = textstyle.clean_label(effect, 80)
    elif kind == "cycle":
        for i, stage in enumerate(payload.get("stages", [])):
            mapping["__TEXT__%d" % i] = textstyle.clean_label(stage, 60)
    elif kind == "concept_map":
        mapping["__CENTER__"] = textstyle.clean_label(
            payload.get("center", ""), 48)
        for i, sat in enumerate(payload.get("satellites", [])):
            mapping["__TEXT__%d" % i] = textstyle.clean_label(sat, 70)
    elif kind == "mind_map":
        mapping["__CENTER__"] = textstyle.clean_label(
            payload.get("center", "") or spec.title, 40)
        for i, branch in enumerate(payload.get("branches", [])):
            mapping["__B%d" % i] = textstyle.clean_label(
                branch.get("name", ""), 50)
            for c, child in enumerate(branch.get("children", [])):
                mapping["__B%dC%d" % (i, c)] = textstyle.clean_label(
                    child, 50)
    elif kind == "classification":
        mapping["__ROOT__"] = textstyle.clean_label(
            payload.get("root", ""), 60)
        groups = payload.get("groups", [])
        if groups:
            for i, group in enumerate(groups):
                mapping["__L0I%d" % i] = textstyle.clean_label(
                    group.get("name", ""), 50)
                for j, item in enumerate(group.get("items", [])):
                    mapping["__G%dJ%d" % (i, j)] = textstyle.clean_label(
                        item, 50)
        else:
            for i, item in enumerate(payload.get("items", [])):
                mapping["__L0I%d" % i] = textstyle.clean_label(item, 60)
    elif kind == "infographic":
        for i, point in enumerate(payload.get("points", [])):
            mapping["__TEXT__%d" % i] = textstyle.clean_label(point, 120)
    for element in elements:
        if element["k"] == "box" and element["text"] in mapping:
            element["text"] = mapping[element["text"]]


def _draw_elements(pdf: Any, elements: list[dict]) -> None:
    for element in elements:
        kind = element["k"]
        if kind == "box":
            box = element["box"]
            if element.get("fill") is not None:
                pdf.set_fill_color(*element["fill"])
            if element.get("line") is not None:
                pdf.set_draw_color(*element["line"])
                pdf.set_line_width(0.3)
            style = ("DF" if element.get("fill") is not None
                     and element.get("line") is not None
                     else "F" if element.get("fill") is not None
                     else "D" if element.get("line") is not None else None)
            if style:
                pdf.rect(box.x, box.y, box.w, box.h, style=style)
            if element.get("text"):
                textstyle.put_label(
                    pdf, box.x + 1.5, box.y + 1.2, box.w - 3.0,
                    element["text"], element.get("size", 8.5),
                    bold=bool(element.get("bold")),
                    align=element.get("align", "C"),
                    color=element.get("color", INK),
                    max_lines=element.get("max_lines", 2))
        elif kind == "line":
            pdf.set_draw_color(*element.get("color", LINE))
            pdf.set_line_width(element.get("lw", 0.3))
            p1, p2 = element["p1"], element["p2"]
            pdf.line(p1[0], p1[1], p2[0], p2[1])
        elif kind == "arrow":
            import math

            pdf.set_draw_color(*element.get("color", NAVY))
            pdf.set_fill_color(*element.get("color", NAVY))
            pdf.set_line_width(element.get("lw", 0.35))
            p1, p2 = element["p1"], element["p2"]
            pdf.line(p1[0], p1[1], p2[0], p2[1])
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            dist = math.hypot(dx, dy) or 1.0
            ux, uy = dx / dist, dy / dist
            s = 2.2
            pdf.polygon([(p2[0], p2[1]),
                         (p2[0] - ux * s - uy * s * 0.6,
                          p2[1] - uy * s + ux * s * 0.6),
                         (p2[0] - ux * s + uy * s * 0.6,
                          p2[1] - uy * s - ux * s * 0.6)], style="F")
        elif kind == "dot":
            pdf.set_fill_color(*element.get("fill", NAVY))
            cx, cy = element["c"]
            r = element["r"]
            pdf.ellipse(cx - r, cy - r, r * 2, r * 2, style="F")


def draw_frame(pdf: Any, rect: tuple, title: str, badge: str,
               footer_note: str = "") -> tuple:
    """Draw the bordered container + title bar + brand strip.

    Returns the body rect (x, y, w) available for content.
    """
    x, y, w, h = rect
    if w < 2 * FRAME_PAD + 40:
        raise ValueError("Diagram rect too narrow: %.1f mm" % w)
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.5)
    pdf.rect(x, y, w, h, style="D")
    pdf.set_fill_color(*NAVY)
    pdf.rect(x, y, w, TITLE_H, style="F")
    pdf.set_text_color(*WHITE)
    pdf.set_font(textstyle.FONT_FAMILY, "B", 9.0)
    title_w = w - 2 * FRAME_PAD - 34.0
    pdf.set_xy(x + FRAME_PAD, y + 1.2)
    pdf.cell(title_w, 5.6, textstyle.clean_label(title, 70), align="L")
    pdf.set_font(textstyle.FONT_FAMILY, "", 6.5)
    pdf.set_xy(x + w - FRAME_PAD - 32.0, y + 1.2)
    pdf.cell(32.0, 5.6, badge, align="R")
    pdf.set_text_color(*MUTED)
    pdf.set_font(textstyle.FONT_FAMILY, "", 6.0)
    if footer_note:
        pdf.set_xy(x + FRAME_PAD, y + h - BRAND_H)
        pdf.cell(w - 2 * FRAME_PAD - 34.0, BRAND_H, footer_note, align="L")
    pdf.set_xy(x + w - FRAME_PAD - 34.0, y + h - BRAND_H)
    pdf.cell(34.0, BRAND_H, BRAND, align="R")
    return (x + FRAME_PAD, y + TITLE_H + 2.0, w - 2 * FRAME_PAD)


def draw_diagram(pdf: Any, spec: VisualSpec,
                 rect: tuple[float, float, float, float]) -> None:
    """Draw a full diagram (frame + template) for `spec` inside `rect`.

    Raises ValueError for map types (use mapdraw), unsupported types, or
    an undersized rect. Size rects via :func:`estimate_height_for_spec`.
    """
    from .engine import SUPPORTED_TYPES

    if spec.visual_type not in SUPPORTED_TYPES:
        raise ValueError("Unsupported visual type: %r" % spec.visual_type)
    if spec.visual_type in ("location_map", "regional_map",
                            "historical_map"):
        raise ValueError("Map visuals render via pdf_service.viz.mapdraw")
    needed = estimate_height_for_spec(spec, rect[2])
    if rect[3] + 1e-6 < needed:
        raise ValueError("Rect height %.1f below estimate %.1f"
                         % (rect[3], needed))
    footer = ""
    shown = spec.payload.get("shown_of")
    if shown:
        footer = "showing %d of %d" % (shown[0], shown[1])
    body = draw_frame(pdf, rect, spec.title,
                      textstyle.badge_label(spec.visual_type), footer)
    elements, _height = _layout_for_spec(spec, body[0], body[1], body[2])
    _resolve_texts(spec, elements)
    _draw_elements(pdf, elements)
