"""Phase H tests -- SRS engine + ``/revise``.

Pure box-ladder/replay tests run without any database; service and handler
tests reuse the Phase E in-memory Motor-like fake (no network, no MongoDB).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from quizbot.analytics import srs
from quizbot.database import MistakeRepository, QuizRepository
from tests.test_phase_e_weak_practice import (
    FakeCtx,
    FakeUpdate,
    new_db,
    question,
)

USER = 4242


def _run(coro):
    import asyncio
    return asyncio.run(coro)


async def seed_quiz(db, qid="SRS1", creator=999, n=5, qtype="free"):
    questions = [
        question(f"SRS question {i}", [f"a{i}", f"b{i}"], 0,
                 subject="Polity", topic="Judiciary", difficulty="hard")
        for i in range(n)
    ]
    await QuizRepository(db).create(creator, "SRS quiz", questions, qid=qid,
                                    quiz_type=qtype, sections=[])
    return questions


async def seed_mistake(db, *, qid="SRS1", q_index=0, history=None, topic="Judiciary",
                       wrong=1, correct=0, status="open",
                       last_wrong_at="2026-09-01 10:00:00",
                       last_correct_at=None, snapshot_id=None, user=USER):
    doc = {
        "user_id": user, "qid": qid, "q_index": q_index,
        "wrong_count": wrong, "correct_count": correct, "status": status,
        "topic": topic, "subject": "Polity", "difficulty": "hard",
        "last_wrong_at": last_wrong_at, "last_correct_at": last_correct_at,
        "first_wrong_at": last_wrong_at,
        "revision_history": list(history or []),
    }
    if snapshot_id:
        doc["snapshot_id"] = snapshot_id
    await db["user_mistakes"].insert_one(doc)
    return doc


# ===========================================================================
# 1. Pure box ladder
# ===========================================================================

class LadderCases(unittest.TestCase):
    def test_01_ladder_shape(self):
        self.assertEqual(srs.SRS_INTERVALS, (0, 1, 3, 7, 16, 35))
        self.assertEqual(srs.MAX_BOX, 5)
        self.assertEqual(srs.interval_days(0), 0)
        self.assertEqual(srs.interval_days(3), 7)
        self.assertEqual(srs.interval_days(5), 35)

    def test_02_correct_promotes_and_caps(self):
        self.assertEqual(srs.next_box(0, "correct"), 1)
        self.assertEqual(srs.next_box(4, "correct"), 5)
        self.assertEqual(srs.next_box(5, "correct"), 5)  # capped

    def test_03_incorrect_resets(self):
        for box in (1, 3, 5):
            self.assertEqual(srs.next_box(box, "incorrect"), 0)

    def test_04_skipped_and_unknown_change_nothing(self):
        self.assertEqual(srs.next_box(4, "skipped"), 4)
        self.assertEqual(srs.next_box(4, None), 4)
        self.assertEqual(srs.next_box(4, "bogus"), 4)

    def test_05_total_for_garbage_input(self):
        self.assertEqual(srs.next_box("x", "correct"), 1)
        self.assertEqual(srs.interval_days("x"), 0)
        self.assertEqual(srs.interval_days(-3), 0)
        self.assertEqual(srs.interval_days(99), 35)

    def test_06_day_arithmetic(self):
        self.assertEqual(srs.add_days("2026-09-17", 3), "2026-09-20")
        self.assertEqual(srs.day_diff("2026-09-20", "2026-09-17"), 3)
        self.assertEqual(srs.day_diff("2026-09-17", "2026-09-20"), -3)


# ===========================================================================
# 2. Pure replay of one mistake's timeline
# ===========================================================================

class ReplayCases(unittest.TestCase):
    def test_10_empty_history_is_due_now(self):
        card = srs.card_from_history([], today="2026-09-17")
        self.assertEqual(card["box"], 0)
        self.assertEqual(card["due_day"], "2026-09-17")
        self.assertTrue(card["due_today"])
        self.assertEqual(card["reviews"], 0)
        self.assertIsNone(card["last_review_day"])

    def test_11_one_correct_moves_one_interval(self):
        card = srs.card_from_history(
            [{"at": "2026-09-01 10:00:00", "outcome": "correct"}],
            today="2026-09-17")
        self.assertEqual(card["box"], 1)
        self.assertEqual(card["due_day"], "2026-09-02")
        self.assertEqual(card["overdue_days"], 15)
        self.assertTrue(card["due_today"])
        self.assertEqual(card["reps"], 1)

    def test_12_two_corrects_use_the_third_day(self):
        card = srs.card_from_history([
            {"at": "2026-09-01 10:00:00", "outcome": "correct"},
            {"at": "2026-09-05 09:00:00", "outcome": "correct"},
        ], today="2026-09-17")
        self.assertEqual(card["box"], 2)
        self.assertEqual(card["due_day"], "2026-09-08")
        self.assertEqual(card["reps"], 2)

    def test_13_wrong_resets_and_counts_lapse(self):
        card = srs.card_from_history([
            {"at": "2026-09-01 10:00:00", "outcome": "correct"},
            {"at": "2026-09-05 09:00:00", "outcome": "correct"},
            {"at": "2026-09-10 09:00:00", "outcome": "incorrect"},
        ], today="2026-09-17")
        self.assertEqual(card["box"], 0)
        self.assertEqual(card["lapses"], 1)
        self.assertEqual(card["reps"], 2)
        self.assertEqual(card["due_day"], "2026-09-10")  # box 0 == same day
        self.assertTrue(card["due_today"])

    def test_14_skip_keeps_the_schedule(self):
        history = [
            {"at": "2026-09-01 10:00:00", "outcome": "correct"},
            {"at": "2026-09-02 10:00:00", "outcome": "skipped"},
        ]
        card = srs.card_from_history(history, today="2026-09-17")
        self.assertEqual(card["box"], 1)
        self.assertEqual(card["due_day"], "2026-09-02")
        # A skip is not a review: it must not extend the interval.
        self.assertEqual(card["reviews"], 2)
        self.assertEqual(card["reps"], 1)

    def test_15_out_of_order_history_is_sorted(self):
        card = srs.card_from_history([
            {"at": "2026-09-10 10:00:00", "outcome": "incorrect"},
            {"at": "2026-09-01 10:00:00", "outcome": "correct"},
        ], today="2026-09-17")
        self.assertEqual(card["box"], 0)      # correct then wrong -> box 0
        self.assertEqual(card["last_review_day"], "2026-09-10")

    def test_16_malformed_entries_are_ignored(self):
        card = srs.card_from_history([
            None, "nope", {"outcome": "???"},
            {"at": "2026-09-02 10:00:00", "outcome": "correct"},
        ], today="2026-09-17")
        self.assertEqual(card["box"], 1)
        self.assertEqual(card["reviews"], 1)

    def test_17_legacy_resolved_row_synthesises_correct(self):
        card = srs.card_from_history(
            [], today="2026-09-17", correct_count=2, wrong_count=1,
            status=MistakeRepository.STATUS_RESOLVED,
            last_correct_at="2026-09-12 10:00:00")
        self.assertEqual(card["box"], 1)
        self.assertEqual(card["due_day"], "2026-09-13")
        self.assertEqual(card["overdue_days"], 4)

    def test_18_legacy_open_row_stays_learning(self):
        card = srs.card_from_history(
            [], today="2026-09-17", wrong_count=3, last_wrong_at="2026-09-01 10:00:00")
        self.assertEqual(card["box"], 0)
        self.assertEqual(card["lapses"], 1)
        self.assertTrue(card["due_today"])

    def test_19_future_due_is_not_overdue(self):
        card = srs.card_from_history(
            [{"at": "2026-09-16 10:00:00", "outcome": "correct"}],
            today="2026-09-17")
        self.assertEqual(card["due_day"], "2026-09-17")
        self.assertEqual(card["overdue_days"], 0)
        self.assertTrue(card["due_today"])


# ===========================================================================
# 3. Pure queue helpers
# ===========================================================================

def _group(key, box, due_day, wrong=1, topic="T", today="2026-09-17"):
    return {
        "key": ("snap", key), "topic": topic, "wrong_max": wrong,
        "srs": {"box": box, "due_day": due_day, "overdue_days": 0,
                "due_today": due_day <= today},
    }


class QueueCases(unittest.TestCase):
    def test_20_rank_overdue_first(self):
        due = [_group("a", 0, "2026-09-10"), _group("b", 2, "2026-09-01")]
        due[0]["srs"]["overdue_days"] = 7
        due[1]["srs"]["overdue_days"] = 16
        ordered = sorted(due, key=lambda g: srs.srs_rank_key(g, "2026-09-17"))
        self.assertEqual(ordered[0]["key"][1], "b")

    def test_21_split_due_and_histogram(self):
        due, later = srs.split_due([
            _group("a", 0, "2026-09-17"), _group("b", 3, "2026-10-01"),
        ], "2026-09-17")
        self.assertEqual([g["key"][1] for g in due], ["a"])
        self.assertEqual([g["key"][1] for g in later], ["b"])
        self.assertEqual(srs.box_histogram(
            due + later), {0: 1, 3: 1})
        self.assertEqual(srs.next_due_day(due + later), "2026-09-17")

    def test_22_annotate_groups_uses_merged_history(self):
        groups = [{
            "key": ("snap", "x"), "origins": [], "wrong_total": 2,
            "wrong_max": 2, "correct_total": 1, "open": True, "relapse": True,
            "first_wrong_at": None, "last_wrong_at": None,
            "last_correct_at": None,
            "history": [
                {"at": "2026-09-01 10:00:00", "outcome": "correct"},
                {"at": "2026-09-08 10:00:00", "outcome": "correct"},
            ],
        }]
        annotated = srs.annotate_groups(groups, "2026-09-17")
        self.assertEqual(annotated[0]["srs"]["box"], 2)
        self.assertEqual(annotated[0]["srs"]["due_day"], "2026-09-11")


# ===========================================================================
# 4. Service (fake Mongo)
# ===========================================================================

class ServiceCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = new_db()
        self.service = srs.SrsService(self.db)

    async def test_30_cards_are_a_projection_of_mistakes(self):
        await seed_mistake(self.db, q_index=0, history=[
            {"at": "2026-09-10 10:00:00", "outcome": "correct"}])
        await seed_mistake(self.db, q_index=1, history=[])
        data = await self.service.cards(USER, now="2026-09-17 12:00:00")
        self.assertEqual(len(data["groups"]), 2)
        self.assertEqual(data["today"], "2026-09-17")
        boxes = sorted(g["srs"]["box"] for g in data["groups"])
        self.assertEqual(boxes, [0, 1])
        self.assertIsNone(data["next_due_day"])  # both are due (or overdue)

    async def test_31_cards_write_nothing(self):
        await seed_mistake(self.db)
        before = sorted(self.db._cols)
        await self.service.cards(USER, now="2026-09-17 12:00:00")
        self.assertEqual(sorted(self.db._cols), before)
        self.assertEqual(len(self.db["user_mistakes"].docs), 1)

    async def test_32_overview_counts_and_topics(self):
        for i in range(3):
            await seed_mistake(self.db, q_index=i, history=[])
        await seed_mistake(self.db, q_index=4, history=[
            {"at": "2026-09-16 10:00:00", "outcome": "correct"}])
        ov = await self.service.overview(USER, now="2026-09-17 12:00:00")
        self.assertEqual(ov["total_cards"], 4)
        # The promoted card's interval is exactly 1 day, so it is due again
        # today as well: 3 learning cards + 1 due-today card.
        self.assertEqual(ov["due_count"], 4)
        self.assertEqual(ov["learning_count"], 3)
        self.assertEqual(ov["due_topics"][0]["topic"], "Judiciary")
        self.assertEqual(ov["due_topics"][0]["count"], 4)

    async def test_33_build_revision_returns_playable_questions(self):
        questions = await seed_quiz(self.db, n=4)
        for i in range(3):
            await seed_mistake(self.db, q_index=i, history=[])
        built = await self.service.build_revision(
            USER, now="2026-09-17 12:00:00", size=2)
        self.assertEqual(built["size"], 2)
        self.assertEqual(built["due_count"], 3)
        self.assertEqual(len(built["origins_by_index"]), 2)
        for q in built["questions"]:
            self.assertIn(q["question"], [x["question"] for x in questions])
            self.assertTrue(q["options"])

    async def test_34_build_revision_bounded_and_cards_meta(self):
        await seed_quiz(self.db, n=12)
        for i in range(12):
            await seed_mistake(self.db, q_index=i, history=[])
        built = await self.service.build_revision(
            USER, now="2026-09-17 12:00:00", size=5)
        self.assertEqual(built["size"], 5)
        self.assertEqual(len(built["cards"]), 5)
        self.assertIn("box", built["cards"][0])
        self.assertIn("due_day", built["cards"][0])

    async def test_35_include_ahead_uses_future_cards(self):
        await seed_quiz(self.db, n=3)
        # Card A: one correct yesterday -> box 1 -> due TODAY.
        await seed_mistake(self.db, q_index=0, history=[
            {"at": "2026-09-16 10:00:00", "outcome": "correct"}])
        # Card B: six corrects ending yesterday -> box 5 -> due in 35 days.
        await seed_mistake(self.db, q_index=1, history=[
            {"at": f"2026-09-{day:02d} 10:00:00", "outcome": "correct"}
            for day in (1, 3, 6, 9, 12, 16)
        ])
        strict = await self.service.build_revision(
            USER, now="2026-09-17 12:00:00", size=5)
        ahead = await self.service.build_revision(
            USER, now="2026-09-17 12:00:00", size=5, include_ahead=True)
        self.assertEqual(strict["size"], 1)          # only card A is due
        self.assertEqual(ahead["size"], 2)           # card B practisable ahead
        self.assertEqual(ahead["include_ahead"], True)
        self.assertEqual(ahead["next_due_day"], "2026-10-21")

    async def test_36_unresolvable_cards_are_excluded_not_faked(self):
        # A snapshot_id that no stored quiz matches: the live question is a
        # different question, and no snapshot exists -> nothing is shown.
        await seed_quiz(self.db, n=2)
        await seed_mistake(self.db, q_index=0, snapshot_id="deadbeef")
        built = await self.service.build_revision(
            USER, now="2026-09-17 12:00:00", size=5)
        self.assertEqual(built["size"], 0)
        self.assertEqual(built["excluded"], 1)


# ===========================================================================
# 5. /revise handler
# ===========================================================================

class ReviseHandlerCases(unittest.IsolatedAsyncioTestCase):
    def _patch_db(self):
        from quizbot.runner_bot.handlers import revise as mod
        self.mod = mod
        self._orig = mod.get_db
        mod.get_db = lambda: self.db
        self.addCleanup(lambda: setattr(mod, "get_db", self._orig))

    async def asyncSetUp(self):
        self.db = new_db()
        self._patch_db()

    async def test_40_private_card_lists_the_queue(self):
        await seed_quiz(self.db, n=3)
        for i in range(3):
            await seed_mistake(self.db, q_index=i, history=[])
        ctx = FakeCtx()
        await self.mod.revise_command(FakeUpdate(uid=USER), ctx)
        text = ctx.bot.sent[-1][1]
        self.assertIn("Spaced revision", text)
        self.assertIn("Due today: <b>3</b>", text)

    async def test_41_group_chat_is_refused(self):
        update = FakeUpdate(uid=USER, chat_type="group")
        await self.mod.revise_command(update, FakeCtx())
        self.assertIn("personal", update.message.replies[-1])

    async def test_42_zero_state_is_honest(self):
        ctx = FakeCtx()
        await self.mod.revise_command(FakeUpdate(uid=USER), ctx)
        self.assertIn("no saved mistakes", ctx.bot.sent[-1][1])

    async def test_43_text_argument_launches(self):
        await seed_quiz(self.db, n=3)
        for i in range(3):
            await seed_mistake(self.db, q_index=i, history=[])
        calls = []

        async def fake_launch(ctx, user_id, questions, **kw):
            calls.append((user_id, questions, kw))
            return True, ""

        class Ctx(FakeCtx):
            args = ["now"]

        ctx = Ctx()
        with patch.object(self.mod.practice, "launch", fake_launch):
            await self.mod.revise_command(FakeUpdate(uid=USER), ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], USER)
        origins = calls[0][2]["origins_by_index"]
        self.assertEqual(origins[0][0]["qid"], "SRS1")
        self.assertEqual(origins[0][0]["q_index"], 0)

    async def test_44_callback_owner_check(self):
        await seed_mistake(self.db)
        update = FakeUpdate(uid=USER, data="rev:menu:999999")
        await self.mod.revise_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])  # alert=True

    async def test_45_callback_menu_and_schedule(self):
        await seed_mistake(self.db)
        update = FakeUpdate(uid=USER, data=f"rev:info:{USER}")
        await self.mod.revise_callback(update, FakeCtx())
        self.assertIn("How your schedule works", update.callback_query.message.edited[-1])

    async def test_46_callback_bad_action_rejected(self):
        update = FakeUpdate(uid=USER, data=f"rev:wat:{USER}")
        await self.mod.revise_callback(update, FakeCtx())
        self.assertTrue(update.callback_query.answers[-1][1])

    async def test_47_busy_session_reports_instead_of_clobbering(self):
        await seed_mistake(self.db)
        with patch("quizbot.runner_bot.practice.session_mgr") as mgr:
            mgr.get.return_value = {"quiz_id": "x"}
            status = await self.mod._launch(FakeCtx(), USER, False)
        self.assertIn("already active", status)

    async def test_48_registration(self):
        app = Application.builder().token("123456:FAKE-TOKEN-FOR-UNIT-TESTS").build()
        self.mod.register(app)
        cmds = set()
        callbacks = 0
        for handlers in app.handlers.values():
            for h in handlers:
                if isinstance(h, CommandHandler):
                    cmds.update(getattr(h, "commands", set()))
                if isinstance(h, CallbackQueryHandler):
                    callbacks += 1
        self.assertEqual(cmds, {"revise"})
        self.assertEqual(callbacks, 1)
