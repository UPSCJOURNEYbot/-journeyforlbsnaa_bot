"""Pure question-metadata helpers for the Phase B analytics foundation.

This module deliberately has **no database and no Telegram imports** so it
can be unit-tested in isolation and reused from both the quiz runner, the
Mini App and the database layer.

Design rules (see Phase B brief):

* Metadata is always **optional**. A question without metadata stays
  byte-compatible; nothing is ever fabricated.
* No UPSC taxonomy is hardcoded. Subjects/topics come from explicit question
  metadata or from the quiz's own sections (clearly marked as a weaker
  ``topic_source="section"`` provenance).
* Synonyms like "FR" vs "Fundamental Rights" are **never** merged -- labels
  are only whitespace/entity normalised, never semantically combined.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Outcome vocabulary (canonical values stored on question_events)
# ---------------------------------------------------------------------------

OUTCOME_CORRECT = "correct"
OUTCOME_INCORRECT = "incorrect"
OUTCOME_SKIPPED = "skipped"
OUTCOMES = (OUTCOME_CORRECT, OUTCOME_INCORRECT, OUTCOME_SKIPPED)

# Where a topic label came from. ``question`` is explicit metadata on the
# question; ``section`` is inferred from the containing quiz section and is
# explicitly weaker evidence (sections are free text, not a taxonomy).
TOPIC_SOURCE_QUESTION = "question"
TOPIC_SOURCE_SECTION = "section"

# ---------------------------------------------------------------------------
# Difficulty vocabulary
# ---------------------------------------------------------------------------
#
# Canonical values ARE the project's existing AI-quiz wizard vocabulary
# (see ``ai_providers.AIQUIZ_DIFFICULTY`` and ``ai_quiz.DIFF_LABELS``):
#   moderate / hard / extreme
# Only near-synonyms of those three are folded in. Labels with no project
# equivalent (e.g. "easy"/"beginner") are DROPPED (-> None), never remapped to
# a value the project does not use, and historical records without difficulty
# stay explicitly unknown (None).
_DIFFICULTY_ALIASES = {
    "moderate": "moderate",
    "medium": "moderate",
    "normal": "moderate",
    "standard": "moderate",
    "intermediate": "moderate",
    "avg": "moderate",
    "average": "moderate",
    "hard": "hard",
    "difficult": "hard",
    "advanced": "hard",
    "tough": "hard",
    "extreme": "extreme",
    "veryhard": "extreme",
    "very hard": "extreme",
    "expert": "extreme",
    "hardest": "extreme",
}

_DIFF_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Metadata keys understood inside a question's optional ``analytics`` block.
_METADATA_KEYS = ("subject", "topic", "subtopic", "difficulty")
_LABEL_KEYS = ("subject", "topic", "subtopic")

# Defensive size caps for snapshots (identity preservation, not bulk export).
# Real questions are far below these; they only guard against pathological
# documents bloating an event record.
SNAPSHOT_QUESTION_LIMIT = 4000
SNAPSHOT_OPTION_LIMIT = 1000


def normalize_difficulty(value: Any) -> Optional[str]:
    """Map a raw difficulty value to the canonical vocabulary, or None.

    Accepts e.g. "Hard", "MODERATE", "very-hard"; unknown/empty values
    return None so analytics can distinguish *unknown* from a real value.
    """
    if value is None:
        return None
    key = _DIFF_STRIP_RE.sub(" ", str(value).lower()).strip()
    if not key:
        return None
    return _DIFFICULTY_ALIASES.get(key)


def normalize_label(value: Any) -> Optional[str]:
    """Light, deterministic label cleanup -- NOT semantic normalisation.

    Unescapes HTML entities (quiz text stores e.g. ``Fundamental Rights
    &amp; Duties``), collapses whitespace and trims. Case and wording are
    preserved, so "FR" and "Fundamental Rights" stay distinct topics.
    """
    if value is None:
        return None
    text = _html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def as_int_list(value: Any) -> list[int]:
    """Coerce an option id / list of option ids into a sorted list of ints."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = [int(v) for v in value if isinstance(v, (int, float)) and not isinstance(v, bool)]
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out = [int(value)]
    else:
        return []
    return sorted(set(out))


def extract_question_analytics(question: Optional[dict]) -> dict:
    """Return the sanitised optional ``analytics`` block for one question.

    Reads a nested ``analytics`` dict first. For maximum forward/backward
    compatibility it also tolerates flat top-level ``subject`` / ``topic`` /
    ``subtopic`` / ``difficulty`` keys (e.g. supplied by future importers),
    but never invents values. Returns ``{}`` when nothing is known so callers
    can distinguish *absent* from *present*.
    """
    if not isinstance(question, dict):
        return {}

    src = question.get("analytics")
    if not isinstance(src, dict):
        src = {k: question.get(k) for k in _METADATA_KEYS if question.get(k) is not None}

    out: dict[str, Any] = {}
    for key in _LABEL_KEYS:
        label = normalize_label(src.get(key))
        if label:
            out[key] = label
    difficulty = normalize_difficulty(src.get("difficulty"))
    if difficulty:
        out["difficulty"] = difficulty
    return out


def normalize_question(question: Any) -> Any:
    """Return a copy of a stored question carrying its optional ``analytics``
    block -- and nothing else changed.

    Wording, options, correct answer(s), explanations and media are never
    modified. Questions with no metadata are returned structurally
    unchanged (no empty ``analytics`` dict is injected).
    """
    if not isinstance(question, dict):
        return question
    q = dict(question)
    analytics = extract_question_analytics(q)
    if analytics:
        q["analytics"] = analytics
    return q


def attach_question_analytics(
    questions: list[dict],
    *,
    topic: Optional[str] = None,
    subject: Optional[str] = None,
    subtopic: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> list[dict]:
    """Attach genuinely-known metadata to in-memory question objects
    (used for ad-hoc AI/PDF quizzes where the wizard collected it).

    Explicit per-question metadata always wins; this only fills blanks, and
    only with data the user actually supplied (e.g. the /aiquiz topic and
    chosen difficulty). Never called with guessed data.
    """
    fill = {
        "subject": normalize_label(subject),
        "topic": normalize_label(topic),
        "subtopic": normalize_label(subtopic),
        "difficulty": normalize_difficulty(difficulty),
    }
    fill = {k: v for k, v in fill.items() if v}
    if not fill:
        return questions
    for q in questions:
        if not isinstance(q, dict):
            continue
        merged = extract_question_analytics(q)
        for key, value in fill.items():
            merged.setdefault(key, value)
        q["analytics"] = merged
    return questions


def resolve_section(sections: Optional[list[dict]], q_index: int) -> Optional[dict]:
    """Return the section dict that owns 0-based question index ``q_index``.

    Mirrors ``runner_bot.quiz_utils.get_section_for_question`` but lives here
    (pure, no runner import) so the database/analytics layer stays
    dependency-light. Tolerates malformed sections by ignoring them.
    """
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        rng = sec.get("question_range")
        if not rng or len(rng) != 2:
            continue
        try:
            start, end = int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            continue
        if start - 1 <= q_index < end:
            return sec
    return None


def resolve_metadata(
    question: Optional[dict], sections: Optional[list[dict]], q_index: int
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Resolve canonical metadata for a question at play time.

    Returns ``(subject, topic, subtopic, difficulty, topic_source)``.

    A section name is used as ``topic`` only when the question carries no
    explicit topic, and ``topic_source`` is then ``"section"`` so downstream
    analytics never mistakes a free-text section name for a formal taxonomy
    node.
    """
    analytics = extract_question_analytics(question)
    subject = analytics.get("subject")
    topic = analytics.get("topic")
    subtopic = analytics.get("subtopic")
    difficulty = analytics.get("difficulty")
    topic_source = TOPIC_SOURCE_QUESTION if topic else None

    if not topic:
        section = resolve_section(sections, q_index)
        if section is not None:
            section_name = normalize_label(section.get("name"))
            if section_name:
                topic = section_name
                topic_source = TOPIC_SOURCE_SECTION

    return subject, topic, subtopic, difficulty, topic_source


def build_snapshot(question: Optional[dict]) -> Optional[dict]:
    """Minimal identity snapshot for a question.

    Stores just enough to guarantee that a mistake recorded months ago keeps
    pointing at the *same* question even if the quiz is later edited in
    place: question text, the option list (verbatim) and the correct answer.
    Explanations/media are deliberately excluded to avoid duplicating large
    blobs. Returns None when the input is unusable.
    """
    if not isinstance(question, dict):
        return None
    raw_options = question.get("options")
    if not isinstance(raw_options, list):
        return None
    options = [str(o)[:SNAPSHOT_OPTION_LIMIT] for o in raw_options]
    correct = question.get("correct_option_id")
    if isinstance(correct, list):
        correct_ids = as_int_list(correct)
    elif isinstance(correct, (int, float)) and not isinstance(correct, bool):
        correct_ids = [int(correct)]
    else:
        correct_ids = []
    return {
        "question": str(question.get("question", "") or "")[:SNAPSHOT_QUESTION_LIMIT],
        "options": options,
        "correct_option_id": correct_ids if len(correct_ids) != 1 else correct_ids[0],
    }
