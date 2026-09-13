"""Phase B analytics foundation.

Canonical, user-scoped analytics layer that attaches to the existing quiz
execution/result lifecycle (it never replaces it).

Package layout:

* ``metadata``    -- pure question-metadata / section-topic / snapshot /
                     difficulty-normalisation helpers (no DB, no Telegram).
* ``runtime``     -- pure helpers that turn the live quiz scorer's already
                     resolved answers into canonical, shuffle-safe question
                     results at the result boundary.
* ``aggregation`` -- pure statistical helpers with explicit denominators.
* ``repository``  -- ``QuestionEventRepository`` (canonical question_events).
* ``service``     -- ``AnalyticsService``: the ONE write/read entry point the
                     bot and Mini App share, and the single canonical source
                     future features (/coach, /weakquiz, /mistakes,
                     /dashboard, /report, /xp, /userstats, /adminstats) will
                     read from.

The database layer (``quizbot.database``) only imports the pure ``metadata``
sub-module at import time; ``repository``/``service`` import the database
layer, so there is no import cycle.
"""

from __future__ import annotations

from . import aggregation, metadata, runtime  # noqa: F401

__all__ = ["aggregation", "metadata", "runtime"]
