"""Bot-side tests for /testseries (no Telegram network, no MongoDB).

Covers: argument parsing, /api/generate payload building, hidden-command
behaviour (nothing registered in any Telegram menu), private-chat wiring of
/testseries in the single-bot bridge, single-polling architecture, and proof
that /whtml, /pdf toggles, FREE_BOT and ownership checks are untouched.
"""

from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path

# Pyrogram (imported transitively by creator handler modules) calls
# asyncio.get_event_loop() at import time, which raises on 3.11+ without a
# live loop set. Production is unaffected: there the imports happen inside
# the running loop. Tests refresh the loop right before such imports.
def _ensure_loop() -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed loop")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

ROOT = Path(__file__).resolve().parent.parent


class ParseArgsCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_loop()
        from quizbot.creator_bot.handlers.reports import \
            _parse_testseries_args
        cls.parse = staticmethod(_parse_testseries_args)

    def test_single_quiz_defaults(self):
        mode, title, ids = self.parse("GGN123")
        self.assertEqual((mode, title, ids), ("keyonly", "Mock Test",
                                              ["GGN123"]))

    def test_multi_quiz_and_modes(self):
        self.assertEqual(self.parse("A B mode=inline")[0], "inline")
        self.assertEqual(self.parse("A B mode=keyonly")[0], "keyonly")
        self.assertEqual(self.parse("A B MODE=INLINE")[0], "inline")
        # unknown mode values are ignored (default preserved)
        self.assertEqual(self.parse("A mode=bogus")[0], "keyonly")
        self.assertEqual(self.parse("A B")[2], ["A", "B"])

    def test_title_parsing(self):
        mode, title, ids = self.parse("A B title=SSC_Mock_2026 mode=inline")
        self.assertEqual(title, "SSC Mock 2026")
        self.assertEqual(mode, "inline")
        self.assertEqual(ids, ["A", "B"])
        self.assertEqual(self.parse("title=Only")[2], [])

    def test_empty_and_whitespace(self):
        self.assertEqual(self.parse(""), ("keyonly", "Mock Test", []))
        self.assertEqual(self.parse("   "), ("keyonly", "Mock Test", []))


class PayloadCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_loop()
        from quizbot.creator_bot.handlers.reports import \
            _build_testseries_payload
        cls.build = staticmethod(_build_testseries_payload)

    def _quiz(self, qid="Q1", name="Quiz One", questions=None):
        return {"qid": qid, "quiz_name": name,
                "questions": questions if questions is not None else []}

    def test_contract_shape_and_mode_mapping(self):
        quizzes = [self._quiz(questions=[
            {"question": " 2+2? ", "options": ["3", "4"],
             "correct_option_id": 1, "explanation": " math "},
            {"question": "skip me", "options": [],
             "correct_option_id": 0, "explanation": ""},
            {"question": "multi", "options": ["a", "b", "c"],
             "correct_option_id": [0, 2], "explanation": None},
        ])]
        for mode, display in (("inline", "inline"), ("keyonly", "end")):
            payload = self.build(quizzes, mode, "T")
            self.assertEqual(payload["solution_display"], display)
            self.assertEqual(payload["exam_title"], "T")
            self.assertEqual(payload["tagline"], "Test Series")
            self.assertEqual(payload["institute_name"], "Quiz Creator")
            self.assertTrue(payload["async"])
            self.assertEqual(payload["quiz_names"], ["Quiz One"])
            got = payload["questions_json"]
            self.assertEqual(len(got), 2, "option-less Q must be skipped")
            self.assertEqual(got[0]["question"], "2+2?")
            self.assertEqual(got[0]["explanation"], "math")
            self.assertEqual(got[0]["correct_option_id"], 1)
            self.assertEqual(got[1]["correct_option_id"], [0, 2])
            self.assertEqual(got[1]["explanation"], "")

    def test_quiz_name_falls_back_to_qid(self):
        payload = self.build(
            [self._quiz(qid="QX", name="", questions=[
                {"question": "q", "options": ["a", "b"],
                 "correct_option_id": 0}])], "keyonly", "T")
        self.assertEqual(payload["quiz_names"], ["QX"])

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            self.build([self._quiz()], "keyonly", "T")
        with self.assertRaises(RuntimeError):
            self.build([self._quiz(questions=[
                {"question": "x", "options": []}])], "inline", "T")


class HiddenCommandCases(unittest.TestCase):
    """`/testseries` must stay manually callable yet absent from any menu."""

    def test_no_menu_registration_in_code(self):
        hits = []
        for path in list((ROOT / "quizbot").rglob("*.py")) + [ROOT / "run.py"]:
            text = path.read_text()
            if "set_my_commands" in text or "BotCommand(" in text:
                hits.append(str(path))
        self.assertEqual(hits, [], "menu registration must not exist")

    def test_botfather_list_has_no_testseries(self):
        text = (ROOT / "BOTFATHER_COMMANDS.txt").read_text().lower()
        for alias in ("testseries", "mocktest"):
            self.assertNotIn(alias, text)
        # command lines look like "name - description"; "tsr" must not be one
        commands = {line.split("-", 1)[0].strip()
                    for line in text.splitlines() if "-" in line}
        self.assertNotIn("tsr", commands)

    def test_bridge_keeps_testseries_wired_and_private(self):
        # Real wiring test on an offline PTB Application (no network).
        _ensure_loop()
        from telegram.ext import Application, CommandHandler
        from quizbot.runner_bot.creator_bridge import register_creator_bridge

        app = (Application.builder()
               .token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build())
        register_creator_bridge(app)
        by_command: dict[str, list] = {}
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    for cmd in h.commands:
                        by_command.setdefault(cmd, []).append(h)
        for alias in ("testseries", "tsr", "mocktest"):
            self.assertIn(alias, by_command,
                          f"/{alias} must stay callable")
            self.assertEqual(len(by_command[alias]), 1,
                             f"/{alias} registered twice?")
            handler = by_command[alias][0]
            self.assertIsNotNone(handler.filters,
                                 f"/{alias} lost its chat filter")
            self.assertIn("PRIVATE", repr(handler.filters),
                          f"/{alias} must be private-chat only")
        # Regression anchors: neighbours untouched.
        self.assertIn("whtml", by_command)
        # PTB gives every CommandHandler a default UpdateType.MESSAGES
        # filter; /whtml must keep exactly that (no private restriction).
        self.assertNotIn("PRIVATE", repr(by_command["whtml"][0].filters),
                         "/whtml wiring must not change")
        self.assertNotIn("start", by_command,
                         "/start belongs to the Runner, not the bridge")


class ArchitectureCases(unittest.TestCase):
    def test_single_polling_client(self):
        run_py = (ROOT / "run.py").read_text()
        self.assertIn("ONE Telegram bot / ONE polling client", run_py)
        self.assertNotIn("run_creator_bot", run_py)
        bot_py = (ROOT / "quizbot" / "runner_bot" / "bot.py").read_text()
        self.assertEqual(bot_py.count(".start_polling("), 1)
        self.assertIn("register_creator_bridge(application)", bot_py)

    def test_testseries_security_checks_present(self):
        src = (ROOT / "quizbot" / "creator_bot" / "handlers"
               / "reports.py").read_text()
        self.assertIn('filters.command(["testseries", "tsr", "mocktest"])',
                      src)
        self.assertIn("filters.private", src)
        self.assertIn("creator_id", src)          # ownership check
        self.assertIn("is_premium_user", src)     # user validation
        self.assertIn("_TSR_LOCK", src)           # single-job guard
        self.assertIn("PDF_API_BASE", src)

    def test_whtml_and_pdf_toggle_intact(self):
        bridge = (ROOT / "quizbot" / "runner_bot"
                  / "creator_bridge.py").read_text()
        self.assertIn('"whtml"', bridge)
        runner_reports = (ROOT / "quizbot" / "runner_bot" / "handlers"
                          / "reports.py").read_text()
        self.assertIn('CommandHandler("html"', runner_reports)
        self.assertIn('CommandHandler("pdf"', runner_reports)
        play = (ROOT / "quizbot" / "runner_bot" / "handlers"
                / "quiz_play.py").read_text()
        self.assertIn("html_report", play)
        self.assertIn("pdf_report", play)

    def test_free_bot_behavior_preserved(self):
        config_src = (ROOT / "quizbot" / "shared" / "config.py").read_text()
        self.assertIn('FREE_BOT: bool = _env_bool("FREE_BOT", True)',
                      config_src)
        from quizbot.shared.utils import is_premium_user
        self.assertTrue(asyncio.run(is_premium_user(424242)))

    def test_everything_compiles(self):
        import py_compile
        targets = [ROOT / "run.py",
                   ROOT / "pdf_service" / "app.py",
                   ROOT / "pdf_service" / "render.py",
                   ROOT / "pdf_service" / "server.py",
                   *sorted((ROOT / "quizbot").rglob("*.py")),
                   *sorted((ROOT / "tests").rglob("*.py"))]
        self.assertGreater(len(targets), 60)
        for path in targets:
            py_compile.compile(str(path), doraise=True)


class ServiceFailureCases(unittest.TestCase):
    """Bot must survive PDF-service outages with clean errors (no network)."""

    def _quizzes(self):
        return [{"qid": "Q1", "quiz_name": "Quiz",
                 "questions": [{"question": "q", "options": ["a", "b"],
                                "correct_option_id": 0,
                                "explanation": ""}]}]

    def _run_generate(self, fake_request, **kwargs):
        _ensure_loop()
        import quizbot.creator_bot.handlers.reports as rep
        import quizbot.shared.config as config
        from unittest.mock import AsyncMock, patch
        with patch.object(config, "PDF_API_BASE", "http://127.0.0.1:9"), \
             patch.object(rep, "request_json", AsyncMock(
                 side_effect=fake_request)):
            return asyncio.run(rep._generate_pdf_via_api(
                self._quizzes(), "keyonly", "T", **kwargs))

    def test_service_down_raises_cleanly(self):
        def boom(*a, **k):
            raise RuntimeError("connection refused")
        with self.assertRaises(RuntimeError) as ctx:
            self._run_generate(boom)
        self.assertIn("connection refused", str(ctx.exception))

    def test_lost_job_fails_fast(self):
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return (200, {"progress_url": "/api/progress/dead",
                              "download_url": "/api/download/dead"})
            return (404, {"detail": "Unknown or expired job."})

        import time
        started = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            self._run_generate(fake)  # default 180s budget...
        self.assertIn("PDF job lost", str(ctx.exception))
        self.assertLess(time.time() - started, 10,
                        "must fail fast, not hang until timeout")

    def test_error_status_propagates(self):
        def fake(*a, **k):
            if a[0] == "GET":
                return (200, {"status": "error", "error": "boom-detail"})
            return (200, {"progress_url": "/p", "download_url": "/d"})
        with self.assertRaises(RuntimeError) as ctx:
            self._run_generate(fake)
        self.assertIn("boom-detail", str(ctx.exception))

    def test_timeout_message_when_never_done(self):
        def fake(*a, **k):
            if a[0] == "GET":
                return (200, {"status": "processing", "progress": 3})
            return (200, {"progress_url": "/p", "download_url": "/d"})
        with self.assertRaises(RuntimeError) as ctx:
            self._run_generate(fake, poll_timeout=0)
        self.assertIn("timed out", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
