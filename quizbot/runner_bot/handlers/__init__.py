"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

from telegram.ext import Application

from . import admin, ai_quiz, buildtest, mix, mistakes, pdf_quiz, poll_quiz, podcast, pyq, quiz_play, reminders, reports, revise, scheduling, setup_wizard, stats, translation, weakquiz

_MODULES = (
    quiz_play,     # /start, /pause, /resume, /stop, /leaderboard, /slow, /fast, /normal, poll answers
    setup_wizard,  # qs_* quiz-setup wizard callbacks
    mistakes,      # /mistakes mistake-revision (Phase D)
    revise,        # /revise spaced-repetition queue (Phase H)
    weakquiz,      # /weakquiz weak-topic targeted practice (Phase E)
    pyq,           # /pyq previous-year-question practice (Phase H)
    buildtest,     # /buildtest custom test builder (Phase H)
    reminders,     # /remind daily study reminders (Phase H)
    stats,         # /stats, /xp -- personal XP/level/streak card (Phase C UI)
    poll_quiz,     # /pollquiz, /pollstop
    mix,           # /mix
    ai_quiz,       # /aiquiz
    pdf_quiz,      # /pdfquiz
    podcast,       # /podcast
    reports,       # /html, /pdf, compare_ callback
    scheduling,    # /schedule, /viewschedule, /cancelschedule
    translation,   # /trans
    admin,         # /help, channel command routing (registered last so it doesn't
                   # shadow the more specific per-feature MessageHandlers above)
)


def register(application: Application) -> None:
    """Register every handler module's commands/callbacks on `application`."""
    for module in _MODULES:
        module.register(application)
