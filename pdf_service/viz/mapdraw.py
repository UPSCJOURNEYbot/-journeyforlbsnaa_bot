"""Vector locator-map drawing on an fpdf2 document (or duck-typed fake).

Maps are drawn from the curated static data only: a base outline from
``geo_base.json`` plus markers for dataset places. No tiles, no network,
no new dependencies. Every map carries a "simplified outline" caption so
a coarse teaching sketch is never mistaken for a survey map.
"""

from __future__ import annotations

import math
from typing import Any

from ..render import BRAND
from . import textstyle
from .engine import load_base_maps

NAVY = (20, 40, 90)
INK = (30, 30, 30)
MUTED = (110, 110, 110)
GRID = (214, 218, 224)
LAND_FILL = (233, 238, 245)
MARKER = (178, 34, 34)

MIN_MAP_W = 45.0
MIN_MAP_H = 32.0
PAD = 2.0
CAPTION_H = 6.0

KIND_LABELS = {
    "city": "City",
    "river": "River",
    "mountain": "Peak",
    "lake": "Lake",
    "monument": "Monument",
    "battlefield": "Battlefield",
    "landmark": "Landmark",
}


def get_base(base_id: str) -> dict:
    """Base-map definition or ValueError for an unknown id."""
    bases = load_base_maps()["bases"]
    if base_id not in bases:
        raise ValueError(f"Unknown base map: {base_id!r}")
    return bases[base_id]


def suggest_height(base_id: str, width: float) -> float:
    """Recommended map height for a width (aspect-kept, clamped)."""
    bbox = get_base(base_id)["bbox"]
    aspect = (bbox[3] - bbox[1]) / (bbox[2] - bbox[0])
    return round(min(80.0, max(36.0, width * aspect * 0.92)), 1)


def _fit_scale(bbox: list, rect: tuple) -> float:
    _x, _y, w, h = rect
    return min(w / (bbox[2] - bbox[0]), h / (bbox[3] - bbox[1]))


def project(lon: float, lat: float, bbox: list,
            rect: tuple) -> tuple[float, float]:
    """Equirectangular projection of (lon, lat) into rect (aspect-fit)."""
    x, y, w, h = rect
    scale = _fit_scale(bbox, rect)
    xoff = x + (w - scale * (bbox[2] - bbox[0])) / 2.0
    yoff = y + (h - scale * (bbox[3] - bbox[1])) / 2.0
    return (xoff + (lon - bbox[0]) * scale,
            yoff + (bbox[3] - lat) * scale)


def caption_for(base: dict, places: list[dict]) -> str:
    """Honesty caption: place names + simplified-outline note + brand."""
    names = [p["name_en"] for p in places[:3]]
    head = ", ".join(names)
    if len(places) > 3:
        head += " +%d more" % (len(places) - 3)
    note = base.get("accuracy_note_en") or "Simplified outline"
    return f"{head}  •  {note}  •  {BRAND}" if head else \
        f"{note}  •  {BRAND}"


def _fitted_box(bbox: list, rect: tuple) -> tuple:
    """The letterboxed area actually covered by the bbox inside rect."""
    x0, y0 = project(bbox[0], bbox[3], bbox, rect)
    x1, y1 = project(bbox[2], bbox[1], bbox, rect)
    return (x0, y0, x1 - x0, y1 - y0)


def _draw_graticule(pdf: Any, base: dict, bbox: list,
                    box: tuple) -> None:
    step = float(base.get("graticule_step", 5) or 5)
    pdf.set_draw_color(*GRID)
    pdf.set_line_width(0.15)
    x0, y0, w, h = box
    lon = math.ceil(bbox[0] / step) * step
    while lon <= bbox[2] + 1e-9:
        px, _ = project(lon, bbox[3], bbox, box)
        pdf.line(px, y0, px, y0 + h)
        lon += step
    lat = math.ceil(bbox[1] / step) * step
    while lat <= bbox[3] + 1e-9:
        _, py = project(bbox[0], lat, bbox, box)
        pdf.line(x0, py, x0 + w, py)
        lat += step


def _draw_land(pdf: Any, base: dict, bbox: list, box: tuple) -> None:
    pdf.set_fill_color(*LAND_FILL)
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.4)
    scale = _fit_scale(bbox, box)
    for poly in base["polygons"]:
        pts = [project(lon, lat, bbox, box) for lon, lat in poly]
        pdf.polygon(pts, style="DF")
    for islet in base.get("islets", []):
        cx, cy = project(islet["lon"], islet["lat"], bbox, box)
        rx = islet["rx"] * scale
        ry = islet["ry"] * scale
        pdf.ellipse(cx - rx, cy - ry, rx * 2, ry * 2, style="DF")


def _label_size(pdf: Any, lines: list[str], sizes: list[float]) -> tuple:
    widths = []
    for text, size in zip(lines, sizes):
        pdf.set_font(textstyle.FONT_FAMILY, "", size)
        widths.append(pdf.get_string_width(text))
    height = sum(size * textstyle.LINE_FACTOR for size in sizes)
    return (max(widths) if widths else 0.0, height)


def _place_labels(pdf: Any, markers: list[tuple[float, float]],
                  labels: list[tuple[list[str], list[float]]],
                  box: tuple) -> list[tuple[float, float, float, float]]:
    """Deterministic label placement with overlap avoidance.

    Returns boxes [(x, y, w, h)] in marker order. Candidates are tried in
    a fixed offset order; the first box fully inside `box` (1mm margin)
    that overlaps no earlier box wins. Fallback: clamped first candidate.
    """
    x0, y0, w, h = box
    placed: list[tuple[float, float, float, float]] = []
    for (mx, my), (lines, sizes) in zip(markers, labels):
        lw, lh = _label_size(pdf, lines, sizes)
        candidates = [
            (mx + 2.5, my - lh - 1.0),
            (mx + 2.5, my + 2.0),
            (mx - lw - 2.5, my - lh - 1.0),
            (mx - lw - 2.5, my + 2.0),
            (mx + 2.5, my - lh - 6.0),
            (mx - lw - 2.5, my + 7.0),
        ]
        chosen = None
        for cx, cy in candidates:
            if not (x0 + 1 <= cx and cy >= y0 + 1
                    and cx + lw <= x0 + w - 1 and cy + lh <= y0 + h - 1):
                continue
            if any(not (cx + lw + 0.5 <= px or px + pw + 0.5 <= cx
                       or cy + lh + 0.5 <= py or py + ph + 0.5 <= cy)
                   for px, py, pw, ph in placed):
                continue
            chosen = (cx, cy)
            break
        if chosen is None:
            fx, fy = candidates[0]
            fx = min(max(fx, x0 + 1), x0 + w - 1 - lw)
            fy = min(max(fy, y0 + 1), y0 + h - 1 - lh)
            chosen = (fx, fy)
        placed.append((chosen[0], chosen[1], lw, lh))
    return placed


def _draw_markers(pdf: Any, places: list[dict], bbox: list,
                  box: tuple) -> None:
    markers = [project(p["lon"], p["lat"], bbox, box) for p in places]
    labels: list[tuple[list[str], list[float]]] = []
    for place in places:
        lines = [textstyle.clean_label(place["name_en"], 40)]
        sizes = [7.5]
        if place.get("name_hi"):
            lines.append(textstyle.clean_label(place["name_hi"], 40))
            sizes.append(6.5)
        labels.append((lines, sizes))
    boxes = _place_labels(pdf, markers, labels, box)
    for (mx, my), (lx, ly, lw, lh), (lines, sizes) in zip(markers, boxes,
                                                         labels):
        pdf.set_draw_color(*MUTED)
        pdf.set_line_width(0.2)
        pdf.line(mx, my, lx + lw / 2.0, ly + lh / 2.0)
        pdf.set_fill_color(*MARKER)
        pdf.ellipse(mx - 1.8, my - 1.8, 3.6, 3.6, style="F")
        pdf.set_fill_color(255, 255, 255)
        pdf.ellipse(mx - 0.6, my - 0.6, 1.2, 1.2, style="F")
        yy = ly
        for text, size in zip(lines, sizes):
            pdf.set_text_color(*INK)
            pdf.set_font(textstyle.FONT_FAMILY, "", size)
            pdf.set_xy(lx, yy)
            pdf.cell(lw, size * textstyle.LINE_FACTOR, text, align="C")
            yy += size * textstyle.LINE_FACTOR


def _draw_scale_bar(pdf: Any, bbox: list, box: tuple) -> None:
    x0, y0, w, h = box
    scale = _fit_scale(bbox, box)
    mid_lat = math.radians((bbox[1] + bbox[3]) / 2.0)
    km_per_deg = 111.32 * math.cos(mid_lat)
    nice = 25
    for candidate in (25, 50, 100, 200, 500, 1000, 2000, 5000):
        if candidate / km_per_deg * scale <= w * 0.35:
            nice = candidate
    bar_w = nice / km_per_deg * scale
    bx, by = x0 + 3.0, y0 + h - 6.5
    pdf.set_fill_color(40, 40, 40)
    pdf.rect(bx, by, bar_w / 2.0, 1.8, style="F")
    pdf.set_draw_color(40, 40, 40)
    pdf.set_line_width(0.25)
    pdf.rect(bx + bar_w / 2.0, by, bar_w / 2.0, 1.8, style="D")
    pdf.set_text_color(*INK)
    pdf.set_font(textstyle.FONT_FAMILY, "", 6.0)
    pdf.set_xy(bx, by + 2.0)
    pdf.cell(bar_w, 3.0, "%d km" % nice, align="C")


def _draw_north_arrow(pdf: Any, box: tuple) -> None:
    x0, y0, w, _h = box
    cx = x0 + w - 6.0
    cy = y0 + 8.0
    pdf.set_fill_color(*NAVY)
    pdf.polygon([(cx, cy - 4.5), (cx - 2.2, cy + 1.5),
                 (cx + 2.2, cy + 1.5)], style="F")
    pdf.set_text_color(*NAVY)
    pdf.set_font(textstyle.FONT_FAMILY, "B", 8.0)
    pdf.set_xy(cx - 4.0, cy + 1.8)
    pdf.cell(8.0, 4.0, "N", align="C")


def draw_map(pdf: Any, *, base_id: str, places: list[dict],
             rect: tuple[float, float, float, float],
             title: str = "") -> None:
    """Draw a bordered locator map inside `rect` = (x, y, w, h) in mm.

    Raises ValueError for an unknown base id or a rect smaller than the
    minimum (callers size via :func:`suggest_height`).
    """
    base = get_base(base_id)
    x, y, w, h = rect
    if w < MIN_MAP_W or h < MIN_MAP_H:
        raise ValueError("Map rect too small: %.1f x %.1f mm" % (w, h))
    bbox = base["bbox"]
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.5)
    pdf.rect(x, y, w, h, style="D")
    inner = (x + PAD, y + PAD, w - 2 * PAD, h - 2 * PAD - CAPTION_H)
    box = _fitted_box(bbox, inner)
    _draw_graticule(pdf, base, bbox, box)
    _draw_land(pdf, base, bbox, box)
    if places:
        _draw_markers(pdf, places, bbox, box)
    _draw_scale_bar(pdf, bbox, box)
    _draw_north_arrow(pdf, box)
    caption = caption_for(base, places)
    if title:
        caption = "%s  •  %s" % (textstyle.clean_label(title, 60), caption)
    pdf.set_text_color(*MUTED)
    size = textstyle.fit_size(pdf, caption, w - 2 * PAD - 2, 7.0, 6.0)
    pdf.set_font(textstyle.FONT_FAMILY, "", size)
    pdf.set_xy(x + PAD + 1, y + h - PAD - CAPTION_H + 1.0)
    pdf.cell(w - 2 * PAD - 2, 4.5, caption, align="C")
