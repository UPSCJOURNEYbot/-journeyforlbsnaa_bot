"""Phase H tests -- the bounded question bank and the two commands it powers.

``/pyq`` and ``/buildtest`` share one access-scoped bank reader
(:mod:`quizbot.analytics.question_bank`); these tests cover the pure helpers,
the service's honest empty states and both Telegram wizards. Everything runs
against the Phase E in-memory fake -- no network, no MongoDB.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from quizbot.analytics.question_bank import (
    BANK_QUESTION_CAP,
    DEFAULT_SIZE,
    MAX_SIZE,
    SCOPE_BANK,
    SCOPE_DUE,
    SCOPE_MISTAKES,
    SCOPE_UNSEEN,
    QuestionBankService,
    available_years,
    bank_row,
    dedupe_by_content,
    extract_pyq_year,
    filter_pool,
    question_identity,
    select_questions,
)
from quizbot.database import QuizRepository
from tests.test_phase_e_weak_practice import FakeCtx, FakeUpdate, new_db, question

USER = 555


def _run(coro):
    return asyncio.run(coro)


def tagged(text, year=None, **meta):
    """A playable stored question with an optional year tag."""
    if year is not None:
        meta["year"] = year
    return question(text, ["a", "b", "c", "d"], 1, **meta)


async def seed_bank(db, questions, *, qid="B1", creator=USER, quiz_type="free",
                    name="Bank quiz"):
    await QuizRepository(db).create(creator, name, questions, qid=qid,
                                    quiz_type=quiz_type, sections=[])
    return questions


async def seed_raw(db, questions, *, qid="RAW", creator=USER, quiz_type="free"):
    """Insert a quiz document directly.

    ``QuizRepository.create`` runs ``normalize_question`` (Phase B), which
    keeps only the whitelisted subject/topic/subtopic/difficulty block -- so a
    year that survived on an imported/legacy document can only be simulated at
    the document level. This is exactly the shape the bank reader must handle.
    """
    await db["quizzes"].insert_one({
        "qid": qid, "creator_id": creator, "quiz_name": qid,
        "questions": questions, "sections": [], "quiz_type": quiz_type,
        "created_at": "2026-09-01 10:00:00",
    })
    return questions


# ===========================================================================
# 1. Pure helpers
# ===========================================================================

class YearCases(unittest.TestCase):
    def test_01_metadata_wins(self):
        year, source = extract_pyq_year(
            {"question": "no marker here", "analytics": {"year": 2019}})
        self.assertEqual((year, source), (2019, "metadata"))
        year, source = extract_pyq_year({"question": "q", "year": "2015"})
        self.assertEqual((year, source), (2015, "metadata"))
        year, source = extract_pyq_year({"question": "q", "pyq_year": 2011})
        self.assertEqual((year, source), (2011, "metadata"))

    def test_02_text_markers_are_read(self):
        for text in ("Which of these? (2023)",
                     "UPSC 2021 — consider the following",
                     "Prelims 2019 question",
                     "PYQ 2018: match the following"):
            year, source = extract_pyq_year({"question": text})
            self.assertEqual(source, "text", text)
            self.assertIn(year, (2023, 2021, 2019, 2018))

    def test_03_a_bare_number_is_never_a_year(self):
        for text in ("In 2020 the GDP was 5000 crore",
                     "Article 2020 does not exist",
                     "عام 2020"):
            self.assertEqual(extract_pyq_year({"question": text}), (None, None), text)

    def test_04_implausible_years_rejected(self):
        for bad in (1978, 3050, "N/A", None, True, "20"):
            self.assertEqual(
                extract_pyq_year({"question": "q", "analytics": {"year": bad}}),
                (None, None), repr(bad))

    def test_05_no_question_no_year(self):
        self.assertEqual(extract_pyq_year(None), (None, None))
        self.assertEqual(extract_pyq_year({}), (None, None))


class IdentityCases(unittest.TestCase):
    def test_10_content_hash_is_the_identity(self):
        a = question_identity("B1", 0, question("Same?", ["a", "b"], 0))
        b = question_identity("B9", 7, question("Same?", ["a", "b"], 0))
        self.assertTrue(a.startswith("snap:"))
        self.assertEqual(a, b)          # same content => same identity

    def test_11_malformed_falls_back_to_qid(self):
        broken = {"question": "", "options": [], "correct_option_id": None}
        built = bank_row("B1", "quiz", 3, broken)
        self.assertIsNone(built)        # unplayable questions are skipped
        self.assertEqual(question_identity("B1", 3, {"question": "x"}),
                         "q:B1#3")

    def test_12_dedupe_keeps_first_occurrence(self):
        rows = [{"identity": "a"}, {"identity": "b"}, {"identity": "a"}]
        self.assertEqual([r["identity"] for r in dedupe_by_content(rows)],
                         ["a", "b"])


class SelectionCases(unittest.TestCase):
    def _rows(self, n, start=0):
        return [{"identity": f"snap:{i:03d}", "meta": {}} for i in range(start, start + n)]

    def test_20_same_seed_same_order(self):
        pool = self._rows(30)
        first = [r["identity"] for r in select_questions(pool, 5, seed="x")]
        second = [r["identity"] for r in select_questions(pool, 5, seed="x")]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 5)

    def test_21_unseen_questions_are_preferred(self):
        pool = self._rows(6)
        seen = {"snap:000", "snap:001", "snap:002", "snap:003"}
        only_two = {r["identity"] for r in select_questions(pool, 2, seed="s", seen=seen)}
        self.assertEqual(only_two, {"snap:004", "snap:005"})
        # A bigger request is filled with already-seen questions afterwards,
        # never padded with duplicates.
        ordered = [r["identity"] for r in select_questions(pool, 5, seed="s", seen=seen)]
        self.assertEqual(len(ordered), 5)
        self.assertEqual(set(ordered[:2]), {"snap:004", "snap:005"})

    def test_22_never_pads_and_never_repeats(self):
        pool = self._rows(3)
        chosen = select_questions(pool, 50, seed="s")
        self.assertEqual(len(chosen), 3)
        self.assertEqual(len({r["identity"] for r in chosen}), 3)

    def test_23_size_is_clamped(self):
        pool = self._rows(60)
        self.assertEqual(len(select_questions(pool, 999, seed="s")), MAX_SIZE)
        self.assertEqual(len(select_questions(pool, 0, seed="s")), DEFAULT_SIZE)
        self.assertEqual(len(select_questions(pool, None, seed="s")), DEFAULT_SIZE)

    def test_24_filter_pool_is_exact(self):
        rows = [
            {"identity": "a", "year": 2020, "meta": {"topic": "Polity", "difficulty": "hard"}},
            {"identity": "b", "year": 2021, "meta": {"topic": "Polity", "difficulty": "easy"}},
            {"identity": "c", "year": 2021, "meta": {"topic": "History", "difficulty": "hard"}},
        ]
        self.assertEqual([r["identity"] for r in filter_pool(rows, years=[2021])], ["b", "c"])
        self.assertEqual([r["identity"] for r in filter_pool(rows, topics=["Polity"])], ["a", "b"])
        self.assertEqual([r["identity"] for r in filter_pool(rows, difficulty="hard")], ["a", "c"])
        self.assertEqual([r["identity"] for r in filter_pool(rows, unseen_only=True, seen={"a"})],
                         ["b", "c"])
        self.assertEqual(filter_pool(rows, years=[1999]), [])
        self.assertEqual(available_years(rows), [2021, 2020])


# ===========================================================================
# 2. Service against the fake DB
# ===========================================================================

class BankServiceCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        self.service = QuestionBankService(self.db)

    async def _seed_default(self):
        await seed_bank(self.db, [
            tagged("Polity question (2023)", 2023, subject="Polity",
                   topic="Judiciary", difficulty="hard"),
            tagged("History question (2021)", None, subject="History",
                   topic="Modern", difficulty="moderate"),
            tagged("Untagged question", None, subject="Polity", topic="Judiciary"),
        ])
        return self.service

    async def test_30_index_counts_everything_honestly(self):
        await self._seed_default()
        await seed_raw(self.db, [
            tagged("Metadata-tagged question", 2018, subject="Polity",
                   topic="Judiciary"),
        ])
        index = await self.service.index(USER)
        self.assertEqual(index["questions"], 4)
        self.assertEqual(index["years"], [2023, 2021, 2018])
        self.assertEqual(index["year_counts"], {"2023": 1, "2021": 1, "2018": 1})
        self.assertEqual(index["untagged"], 1)
        self.assertEqual(index["unseen"], 4)
        self.assertEqual(index["topics"][0]["topic"], "Judiciary")
        self.assertEqual(index["topics"][0]["count"], 3)

    async def test_31_unknown_user_id_is_refused(self):
        for bad in ("x", None, True):
            with self.assertRaises(ValueError):
                await self.service.index(bad)

    async def test_32_build_pyq_for_one_year(self):
        await self._seed_default()
        await seed_raw(self.db, [tagged("Metadata 2021", 2021)])
        built = await self.service.build_pyq(USER, 2021, size=10)
        self.assertEqual(built["size"], 2)
        self.assertEqual({r["year"] for r in built["rows"]}, {2021})
        self.assertEqual({r["year_source"] for r in built["rows"]},
                         {"text", "metadata"})
        self.assertEqual(built["reason"], None)

    async def test_33_all_years_excludes_untagged(self):
        await self._seed_default()
        built = await self.service.build_pyq(USER, None, size=10)
        self.assertEqual(built["size"], 2)
        self.assertEqual({r["year"] for r in built["rows"]}, {2023, 2021})

    async def test_34_missing_year_is_an_honest_empty(self):
        await self._seed_default()
        built = await self.service.build_pyq(USER, 1990, size=10)
        self.assertEqual(built["size"], 0)
        self.assertEqual(built["reason"], "no_year")
        self.assertEqual(built["available_years"], [2023, 2021])
        self.assertEqual(built["rows"], [])

    async def test_35_untagged_bank_says_no_pyq(self):
        await seed_bank(self.db, [tagged("No year anywhere")])
        built = await self.service.build_pyq(USER, None, size=10)
        self.assertEqual(built["size"], 0)
        self.assertEqual(built["reason"], "no_pyq")
        selected = await self.service.build_pyq(USER, 2020, size=10)
        self.assertEqual(selected["reason"], "no_year")

    async def test_36_build_test_defaults_to_the_whole_bank(self):
        await self._seed_default()
        built = await self.service.build_test(USER, {"scope": SCOPE_BANK})
        self.assertEqual(built["size"], 3)
        self.assertEqual(len(built["origins_by_index"]), 0)   # bank has no provenance

    async def test_37_build_test_unseen_scope(self):
        await self._seed_default()
        # A mistake row alone is enough to mark a question as seen.
        await self.db["user_mistakes"].insert_one({
            "user_id": USER, "qid": "B1", "q_index": 0, "wrong_count": 1,
            "correct_count": 0, "status": "open", "topic": "Judiciary",
            "last_wrong_at": "2026-09-01 10:00:00",
        })
        built = await self.service.build_test(USER, {"scope": SCOPE_UNSEEN})
        self.assertEqual(built["size"], 2)

    async def test_38_build_test_size_and_bounds(self):
        await seed_bank(self.db, [tagged(f"Q{i}", 2020) for i in range(12)])
        built = await self.service.build_test(
            USER, {"scope": SCOPE_BANK, "size": 5})
        self.assertEqual(built["size"], 5)
        index = await self.service.index(USER)
        self.assertLessEqual(index["questions"], BANK_QUESTION_CAP)

    async def test_39_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            await self.service.build_test(USER, {"scope": "everything"})

    async def test_40_mistakes_and_due_scopes_are_honest_when_empty(self):
        await self._seed_default()
        mistakes = await self.service.build_test(USER, {"scope": SCOPE_MISTAKES})
        self.assertEqual(mistakes["size"], 0)
        self.assertEqual(mistakes["reason"], "no_mistakes")
        due = await self.service.build_test(USER, {"scope": SCOPE_DUE})
        self.assertEqual(due["size"], 0)
        self.assertEqual(due["reason"], "nothing_due")

    async def test_41_mistakes_scope_reuses_the_phase_d_engine(self):
        await seed_bank(self.db, [tagged("Missed one (2020)", 2020)])
        await self.db["user_mistakes"].insert_one({
            "user_id": USER, "qid": "B1", "q_index": 0, "wrong_count": 2,
            "correct_count": 0, "status": "open", "topic": "Polity",
            "last_wrong_at": "2026-09-01 10:00:00",
        })
        built = await self.service.build_test(USER, {"scope": SCOPE_MISTAKES})
        self.assertEqual(built["size"], 1)
        self.assertEqual(len(built["origins_by_index"]), 1)
        self.assertEqual(built["origins_by_index"][0][0]["qid"], "B1")

    async def test_42_paid_quiz_of_another_creator_is_not_in_the_bank(self):
        await seed_bank(self.db, [tagged("Paid (2020)", 2020)], qid="PAID",
                        creator=USER + 1, quiz_type="paid")
        index = await self.service.index(USER)
        self.assertEqual(index["questions"], 0)
        # ... but the creator sees their own quiz.
        own = await self.service.index(USER + 1)
        self.assertEqual(own["questions"], 1)

    async def test_43_public_free_quizzes_are_readable(self):
        await seed_bank(self.db, [tagged("Public (2022)", 2022)], qid="PUB",
                        creator=USER + 9, quiz_type="free")
        index = await self.service.index(USER)
        self.assertEqual(index["questions"], 1)

    async def test_44_duplicate_content_counts_once(self):
        shared = "Repeated question (2020)"
        await QuizRepository(self.db).create(
            USER, "A", [tagged(shared, 2020)], qid="A", quiz_type="free", sections=[])
        await QuizRepository(self.db).create(
            USER, "B", [tagged(shared, 2020)], qid="B", quiz_type="free", sections=[])
        index = await self.service.index(USER)
        self.assertEqual(index["questions"], 1)

    async def test_45_playable_shape_is_canonical(self):
        await self._seed_default()
        built = await self.service.build_pyq(USER, 2023, size=5)
        played = built["questions"][0]
        self.assertEqual(played["analytics"]["topic"], "Judiciary")
        self.assertEqual(played["analytics"]["year"], 2023)
        self.assertEqual(played["correct_option_id"], 1)
        self.assertEqual(len(played["options"]), 4)


# ===========================================================================
# 3. /pyq handler
# ===========================================================================

class _Launcher:
    """Records practice.launch calls instead of starting a real session."""

    def __init__(self):
        self.calls = []

    async def __call__(self, ctx, user_id, questions, **kw):
        self.calls.append((user_id, questions, kw))
        return True, ""


class PyqHandlerCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from quizbot.runner_bot.handlers import pyq as mod
        self.mod = mod
        self.db = new_db()
        self._orig = mod.get_db
        mod.get_db = lambda: self.db
        self.addCleanup(lambda: setattr(mod, "get_db", self._orig))
        self.launcher = _Launcher()
        patcher = patch("quizbot.runner_bot.practice.launch", self.launcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _seed_tagged(self):
        await seed_bank(self.db, [
            tagged("Polity (2023)", 2023, subject="Polity", topic="Judiciary"),
            tagged("History (2021)", 2021, subject="History", topic="Modern"),
        ])

    async def test_50_empty_bank_is_explained(self):
        ctx = FakeCtx()
        await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        self.assertIn("couldn't find any playable question", ctx.bot.sent[-1][1])

    async def test_51_untagged_bank_says_how_to_tag(self):
        await seed_bank(self.db, [tagged("No year")])
        ctx = FakeCtx()
        await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        self.assertIn("none of them carries a year", ctx.bot.sent[-1][1])

    async def test_52_menu_lists_years_and_counts(self):
        await self._seed_tagged()
        ctx = FakeCtx()
        await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        text, kw = ctx.bot.sent[-1][1], ctx.bot.sent[-1][2]
        self.assertIn("Tagged with a year: <b>2</b>", text)
        labels = [b.text for row in kw["reply_markup"].inline_keyboard for b in row]
        self.assertTrue(any("2023" in label for label in labels), labels)
        self.assertTrue(any("2021" in label for label in labels), labels)
        callbacks = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
        self.assertTrue(all(cb.startswith("pyq:") for cb in callbacks))

    async def test_53_text_argument_launches_that_year(self):
        await self._seed_tagged()
        ctx = FakeCtx()
        ctx.args = ["2023"]
        await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        self.assertEqual(len(self.launcher.calls), 1)
        user_id, questions, kw = self.launcher.calls[0]
        self.assertEqual(user_id, USER)
        self.assertEqual(len(questions), 1)
        self.assertEqual(kw["marker"], "_pyq_session")
        self.assertTrue(kw["show_explanation"])
        self.assertIn("PYQ 2023", ctx.bot.sent[-1][1])

    async def test_54_unknown_year_reports_available_years(self):
        await self._seed_tagged()
        ctx = FakeCtx()
        ctx.args = ["1990"]
        await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        self.assertEqual(self.launcher.calls, [])
        self.assertIn("Available years", ctx.bot.sent[-1][1])

    async def test_55_all_argument_uses_every_tagged_year(self):
        await self._seed_tagged()
        ctx = FakeCtx()
        ctx.args = ["all"]
        await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        self.assertEqual(len(self.launcher.calls[0][1]), 2)

    async def test_56_group_chat_is_refused(self):
        update = FakeUpdate(uid=USER, chat_type="group")
        await self.mod.pyq_command(update, FakeCtx())
        self.assertIn("personal", update.message.replies[-1])
        self.assertEqual(self.launcher.calls, [])

    async def test_57_callback_foreign_user_is_rejected(self):
        await self._seed_tagged()
        update = FakeUpdate(uid=USER, data="pyq:year:999999:2023:0")
        await self.mod.pyq_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])
        self.assertEqual(self.launcher.calls, [])

    async def test_58_callback_year_launches_and_answers(self):
        await self._seed_tagged()
        update = FakeUpdate(uid=USER, data=f"pyq:year:{USER}:2021:0")
        ctx = FakeCtx()
        await self.mod.pyq_callback(update, ctx)
        self.assertEqual(len(self.launcher.calls), 1)
        self.assertIn("PYQ 2021", ctx.bot.sent[-1][1])

    async def test_59_callback_year_not_in_the_bank_is_rejected(self):
        await self._seed_tagged()
        update = FakeUpdate(uid=USER, data=f"pyq:year:{USER}:1800:0")
        await self.mod.pyq_callback(update, FakeCtx())
        self.assertIn("no longer available", update.callback_query.answers[-1][0])

    async def test_60_unseen_toggle_re_renders(self):
        await self._seed_tagged()
        update = FakeUpdate(uid=USER, data=f"pyq:unseen:{USER}:1")
        await self.mod.pyq_callback(update, FakeCtx())
        self.assertIn("Unseen-only filter is ON", update.message.edited[-1])

    async def test_61_launch_honours_unseen_only(self):
        await self._seed_tagged()
        update = FakeUpdate(uid=USER, data=f"pyq:year:{USER}:2023:1")
        ctx = FakeCtx()
        await self.mod.pyq_callback(update, ctx)
        self.assertEqual(len(self.launcher.calls), 1)

    async def test_62_invalid_callback_payload_is_refused(self):
        update = FakeUpdate(uid=USER, data="pyq:bogus")
        await self.mod.pyq_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])

    async def test_63_busy_user_is_told_to_stop_first(self):
        await self._seed_tagged()
        ctx = FakeCtx()
        ctx.args = ["2023"]
        with patch("quizbot.runner_bot.practice.busy", lambda uid: True):
            await self.mod.pyq_command(FakeUpdate(uid=USER), ctx)
        self.assertEqual(self.launcher.calls, [])
        self.assertIn("/stop", ctx.bot.sent[-1][1])

    async def test_64_registration(self):
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        self.mod.register(app)
        commands, callbacks = set(), []
        for handlers in app.handlers.values():
            for handler in handlers:
                if isinstance(handler, CommandHandler):
                    commands.update(getattr(handler, "commands", set()))
                if isinstance(handler, CallbackQueryHandler):
                    callbacks.append(handler)
        self.assertEqual(commands, {"pyq"})
        self.assertEqual(len(callbacks), 1)
        self.assertTrue(callbacks[0].pattern.match("pyq:menu:1"))


# ===========================================================================
# 4. /buildtest handler
# ===========================================================================

class BuildtestStateCases(unittest.TestCase):
    def test_70_valid_payloads_round_trip(self):
        from quizbot.runner_bot.handlers.buildtest import parse_state
        state = parse_state("bt:go:7:30:mistakes:hard:1,3:-")
        self.assertEqual(state["action"], "go")
        self.assertEqual(state["user_id"], 7)
        self.assertEqual(state["size"], 30)
        self.assertEqual(state["scope"], SCOPE_MISTAKES)
        self.assertEqual(state["difficulty"], "hard")
        self.assertEqual(state["selection"], [1, 3])

    def test_71_sizes_and_allow_lists_are_re_validated(self):
        from quizbot.runner_bot.handlers.buildtest import parse_state
        self.assertEqual(parse_state("bt:s:7:777:bank:any:-")["size"], DEFAULT_SIZE)
        self.assertEqual(parse_state("bt:s:7:20:nonsense:any:-")["scope"], SCOPE_BANK)
        self.assertEqual(parse_state("bt:s:7:20:bank:impossible:-")["difficulty"], "any")

    def test_72_forged_actions_and_shapes_are_refused(self):
        from quizbot.runner_bot.handlers.buildtest import parse_state
        for bad in (None, "", "mst:menu:1", "bt:delete:1:20:bank:any:-",
                    "bt:go:notanint:20:bank:any:-", "bt"):
            self.assertIsNone(parse_state(bad), repr(bad))

    def test_73_selection_is_bounded_and_deduplicated(self):
        from quizbot.runner_bot.handlers.buildtest import parse_state
        state = parse_state("bt:menu:7:20:bank:any:3,3,9,1,2,4,5,6,7,8,9,10,11")
        self.assertEqual(state["selection"], sorted(set(state["selection"])))
        self.assertLessEqual(len(state["selection"]), 8)

    def test_74_toggle_index_is_parsed(self):
        from quizbot.runner_bot.handlers.buildtest import parse_state
        self.assertEqual(parse_state("bt:t:7:20:weak:any:1,2:1")["toggle"], 1)
        self.assertEqual(parse_state("bt:t:7:20:weak:any:-")["toggle"], -1)


class BuildtestHandlerCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from quizbot.runner_bot.handlers import buildtest as mod
        self.mod = mod
        self.db = new_db()
        self._orig = mod.get_db
        mod.get_db = lambda: self.db
        self.addCleanup(lambda: setattr(mod, "get_db", self._orig))
        self.launcher = _Launcher()
        patcher = patch("quizbot.runner_bot.practice.launch", self.launcher)
        patcher.start()
        self.addCleanup(patcher.stop)
        await seed_bank(self.db, [
            tagged("Polity (2023)", 2023, subject="Polity", topic="Judiciary",
                   difficulty="hard"),
            tagged("History (2021)", 2021, subject="History", topic="Modern",
                   difficulty="easy"),
            tagged("Polity two", None, subject="Polity", topic="Parliament",
                   difficulty="hard"),
        ])

    async def test_80_command_shows_the_builder(self):
        ctx = FakeCtx()
        await self.mod.buildtest_command(FakeUpdate(uid=USER), ctx)
        text = ctx.bot.sent[-1][1]
        self.assertIn("Build a test", text)
        self.assertIn(f"<b>{DEFAULT_SIZE}</b>", text)
        self.assertIn("3", text)          # bank size

    async def test_81_text_args_set_size_and_scope(self):
        ctx = FakeCtx()
        ctx.args = ["30", "weak"]
        await self.mod.buildtest_command(FakeUpdate(uid=USER), ctx)
        text = ctx.bot.sent[-1][1]
        self.assertIn("<b>30</b>", text)
        self.assertIn("Weak topics", text)
        ctx2 = FakeCtx()
        ctx2.args = ["weak"]
        await self.mod.buildtest_command(FakeUpdate(uid=USER), ctx2)
        self.assertIn("Weak topics", ctx2.bot.sent[-1][1])

    async def test_82_invalid_text_args_are_ignored(self):
        ctx = FakeCtx()
        ctx.args = ["999", "everything"]
        await self.mod.buildtest_command(FakeUpdate(uid=USER), ctx)
        self.assertIn(f"<b>{DEFAULT_SIZE}</b>", ctx.bot.sent[-1][1])

    async def test_83_group_chat_is_refused(self):
        update = FakeUpdate(uid=USER, chat_type="supergroup")
        await self.mod.buildtest_command(update, FakeCtx())
        self.assertIn("personal", update.message.replies[-1])

    async def test_84_scope_button_re_renders(self):
        update = FakeUpdate(uid=USER, data=f"bt:sc:{USER}:20:weak:any:-")
        await self.mod.buildtest_callback(update, FakeCtx())
        self.assertIn("Weak topics", update.message.edited[-1])

    async def test_85_foreign_user_is_rejected(self):
        update = FakeUpdate(uid=USER, data="bt:menu:999999:20:bank:any:-")
        await self.mod.buildtest_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])

    async def test_86_topic_screen_toggles_selection(self):
        update = FakeUpdate(uid=USER, data=f"bt:t:{USER}:20:bank:any:-:0")
        await self.mod.buildtest_callback(update, FakeCtx())
        self.assertIn("✅", update.message.edited[-1])
        update2 = FakeUpdate(uid=USER, data=f"bt:t:{USER}:20:bank:any:0:0")
        await self.mod.buildtest_callback(update2, FakeCtx())
        self.assertNotIn("✅ Executive", update2.message.edited[-1])

    async def test_87_stale_topic_index_is_rejected(self):
        update = FakeUpdate(uid=USER, data=f"bt:t:{USER}:20:bank:any:-:9")
        await self.mod.buildtest_callback(update, FakeCtx())
        self.assertIn("no longer available", update.callback_query.answers[-1][0])

    async def test_88_go_builds_and_starts_without_explanations(self):
        update = FakeUpdate(uid=USER, data=f"bt:go:{USER}:20:bank:any:-")
        ctx = FakeCtx()
        await self.mod.buildtest_callback(update, ctx)
        self.assertEqual(len(self.launcher.calls), 1)
        _user, questions, kw = self.launcher.calls[0]
        self.assertEqual(len(questions), 3)
        self.assertEqual(kw["marker"], "_buildtest_session")
        self.assertFalse(kw["show_explanation"])
        self.assertIn("Explanations are hidden", ctx.bot.sent[-1][1])

    async def test_89_go_on_an_empty_source_is_honest(self):
        update = FakeUpdate(uid=USER, data=f"bt:go:{USER}:20:mistakes:any:-")
        ctx = FakeCtx()
        await self.mod.buildtest_callback(update, ctx)
        self.assertEqual(self.launcher.calls, [])
        self.assertIn("no saved mistakes", ctx.bot.sent[-1][1])

    async def test_90_size_10_is_honoured_and_never_padded(self):
        update = FakeUpdate(uid=USER, data=f"bt:go:{USER}:10:bank:any:-")
        ctx = FakeCtx()
        await self.mod.buildtest_callback(update, ctx)
        self.assertEqual(len(self.launcher.calls[0][1]), 3)
        self.assertIn("shorter", ctx.bot.sent[-1][1])

    async def test_91_topic_filter_reaches_the_builder(self):
        index = await QuestionBankService(self.db).index(USER)
        target = next(i for i, t in enumerate(index["topics"])
                      if t["topic"] == "Modern")
        update = FakeUpdate(uid=USER, data=f"bt:go:{USER}:20:bank:any:{target}")
        ctx = FakeCtx()
        await self.mod.buildtest_callback(update, ctx)
        questions = self.launcher.calls[0][1]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["analytics"]["topic"], "Modern")

    async def test_92_busy_user_gets_one_clear_line(self):
        update = FakeUpdate(uid=USER, data=f"bt:go:{USER}:20:bank:any:-")
        ctx = FakeCtx()
        with patch("quizbot.runner_bot.practice.busy", lambda uid: True):
            await self.mod.buildtest_callback(update, ctx)
        self.assertEqual(self.launcher.calls, [])
        self.assertIn("/stop", ctx.bot.sent[-1][1])

    async def test_93_invalid_callback_is_refused(self):
        update = FakeUpdate(uid=USER, data="bt:frobnicate:1")
        await self.mod.buildtest_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])

    async def test_94_registration(self):
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        self.mod.register(app)
        commands, callbacks = set(), []
        for handlers in app.handlers.values():
            for handler in handlers:
                if isinstance(handler, CommandHandler):
                    commands.update(getattr(handler, "commands", set()))
                if isinstance(handler, CallbackQueryHandler):
                    callbacks.append(handler)
        self.assertEqual(commands, {"buildtest"})
        self.assertEqual(len(callbacks), 1)
        self.assertTrue(callbacks[0].pattern.match("bt:go:1"))
