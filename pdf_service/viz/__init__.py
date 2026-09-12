"""Phase 3: Intelligent Solution Visual Engine (milestone 1: core + data).

Accuracy-first, deterministic, dependency-free (stdlib + fpdf2 + the
vendored Hind fonts already used by :mod:`pdf_service.render`).

Milestone 1 ships the decision engine, the drawing modules and the
local/static data layer. It is deliberately NOT wired into the live PDF
generation flow yet -- see :func:`engine.decide_visual`.

Accuracy contract (enforced by design, pinned by tests):

* Never fabricate geography, locations, historical boundaries,
  scientific facts or any other factual visual content.
* A location visual is produced only for places present in the curated
  ``geo_places.json`` dataset AND inside a region that has a base map in
  ``geo_base.json``. Otherwise the engine returns ``None`` (no visual).
* Diagram templates only re-structure text already present in the
  question/explanation -- they never assert new facts.
* The engine is optional: :func:`engine.safe_decide_visual` never raises
  and the (future) PDF wiring must treat ``None`` as "render without a
  visual", so visuals can never prevent PDF generation.
"""

from __future__ import annotations

from .engine import (
    SUPPORTED_TYPES,
    VisualSpec,
    VisualType,
    decide_visual,
    detect_subject,
    find_places,
    load_base_maps,
    load_places,
    load_subjects,
    reload_data,
    safe_decide_visual,
    usable_places,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "SUPPORTED_TYPES",
    "VisualSpec",
    "VisualType",
    "decide_visual",
    "detect_subject",
    "find_places",
    "load_base_maps",
    "load_places",
    "load_subjects",
    "reload_data",
    "safe_decide_visual",
    "usable_places",
]
