"""PART 2 -- forwarded quiz-poll import in the /create wizard + command audit.

Offline: no Telegram network, no MongoDB (repositories mocked).

The bug being pinned down: ``register()`` already accepted poll messages
(``filters.text | filters.poll``) and ``_poll_text()`` already existed to
unwrap Kurigram's ``FormattedText``, but *nothing ever read ``m.poll``*. A
forwarded quiz poll therefore fell through ``handle_creation_message`` to
``if not m.text: return`` and vanished without a trace -- while a poll sent
during a typed-input step (quiz name, timer, section name, ...) hit
``m.text.strip()`` and raised AttributeError, which Pyrogram swallowed,
leaving the wizard silently stuck.

Covers:
  * ``_poll_to_question`` -- conversion, both API shapes, every rejection.
  * ``_import_poll`` -- session append, remove-words, DB failure tolerance.
  * ``handle_creation_message`` -- poll dispatch, typed-step guard, no
    regression for plain-text paste.
  * command audit -- reserved commands still reachable, poll filter wired.
"""

from __future__ import annotations

import asyncio
import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.enums import PollType
from pyrogram.types import Message as _RealMessage

from quizbot.creator_bot import state as creator_state
from quizbot.creator_bot.handlers import quiz_creation as qc
from quizbot.creator_bot.handlers.quiz_creation import (
    _RESERVED_COMMANDS,
    _TYPED_INPUT_STEPS,
    _poll_correct_ids,
    _poll_text,
    _poll_to_question,
    handle_creation_message,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


_UIDS = itertools.count(740000)


def _uid() -> int:
    return next(_UIDS)


# ─────────────────────────── fakes ───────────────────────────────────────────


def ft(text):
    """Mimic Kurigram's ``FormattedText`` wrapper (has ``.text``)."""
    return SimpleNamespace(text=text, entities=None)


def opt(text, formatted: bool = True):
    """Mimic ``PollOption`` -- its ``.text`` is a FormattedText on Kurigram."""
    return SimpleNamespace(text=ft(text) if formatted else text, voter_count=0)


def make_poll(
    question="Which Article deals with the Uniform Civil Code?",
    options=("Article 42", "Article 44", "Article 51", "Article 32"),
    correct=(1,),
    explanation=None,
    poll_type=PollType.QUIZ,
    formatted: bool = True,
    legacy_single: bool = False,
):
    """Build a stand-in for ``pyrogram.types.Poll``.

    ``formatted=True`` reproduces the newer Kurigram shape (question/options
    are ``FormattedText``); ``formatted=False`` the older plain-``str`` shape.
    ``legacy_single=True`` exposes ``correct_option_id`` (int) instead of
    ``correct_option_ids`` (list).
    """
    poll = SimpleNamespace(
        id="p1",
        question=ft(question) if formatted else question,
        options=[opt(o, formatted) for o in options],
        type=poll_type,
        explanation=ft(explanation) if formatted else explanation,
        allows_multiple_answers=len(correct or ()) > 1,
        is_closed=True,
    )
    if legacy_single:
        poll.correct_option_id = (correct or (None,))[0]
    else:
        poll.correct_option_ids = list(correct) if correct else None
    return poll


def make_msg(poll=None, text=None, uid=None):
    uid = _uid() if uid is None else uid
    return SimpleNamespace(
        id=1,
        from_user=SimpleNamespace(id=uid),
        poll=poll,
        text=text,
        reply_to_message=None,
        reply=AsyncMock(),
    )


def _session(uid, **extra):
    ud = {"questions": [], "quiz_name": None, "timer": None, "awaiting_name": False}
    ud.update(extra)
    creator_state.quiz_creation[uid] = ud
    return ud


def _repo_ctx(remove_words=None, boom: bool = False):
    """Patch ``UserRepository``/``get_db`` inside the quiz_creation module."""
    repo = MagicMock()
    if boom:
        repo.return_value.get_or_create = AsyncMock(side_effect=RuntimeError("mongo down"))
    else:
        repo.return_value.get_or_create = AsyncMock(
            return_value={"remove_words": remove_words or []}
        )
    return (
        patch.object(qc, "UserRepository", repo),
        patch.object(qc, "get_db", lambda: None),
    )


# ─────────────────────── _poll_text / _poll_correct_ids ──────────────────────


class PollPrimitiveCases(unittest.TestCase):
    def test_formatted_text_is_unwrapped_to_str(self):
        self.assertEqual(_poll_text(ft("hello")), "hello")
        self.assertIsInstance(_poll_text(ft("hello")), str)

    def test_plain_str_and_none_pass_through(self):
        self.assertEqual(_poll_text("hello"), "hello")
        self.assertIsNone(_poll_text(None))

    def test_formatted_text_with_none_text_is_none_not_a_repr(self):
        """Regression: this used to fall through to ``str(value)`` and leak
        ``namespace(text=None, entities=None)`` into the question text."""
        self.assertIsNone(_poll_text(ft(None)))

    def test_correct_ids_from_list_shape(self):
        self.assertEqual(_poll_correct_ids(make_poll(correct=(2,))), [2])

    def test_correct_ids_from_legacy_int_shape(self):
        self.assertEqual(
            _poll_correct_ids(make_poll(correct=(2,), legacy_single=True)), [2]
        )

    def test_correct_ids_none_when_unmarked(self):
        self.assertIsNone(_poll_correct_ids(make_poll(correct=())))

    def test_correct_ids_none_on_garbage(self):
        self.assertIsNone(_poll_correct_ids(SimpleNamespace(correct_option_ids=["x"])))


# ────────────────────────── _poll_to_question: happy path ────────────────────


class PollConversionHappyCases(unittest.TestCase):
    def test_quiz_poll_becomes_question_dict(self):
        q, err = _poll_to_question(make_poll(correct=(1,)))
        self.assertIsNone(err)
        self.assertEqual(
            q,
            {
                "question": "Which Article deals with the Uniform Civil Code?",
                "options": ["Article 42", "Article 44", "Article 51", "Article 32"],
                "correct_option_id": 1,
                "explanation": None,
            },
        )

    def test_shape_matches_text_parser_output(self):
        """The imported dict must be interchangeable with parse_question_block's."""
        from quizbot.creator_bot.parsing import parse_question_block

        parsed = parse_question_block(
            "Q?\nA) one\nB) two ✅\nEx: because."
        )
        poll_q, _ = _poll_to_question(
            make_poll(question="Q?", options=("one", "two"), correct=(1,),
                      explanation="because.")
        )
        self.assertEqual(sorted(parsed), sorted(poll_q))
        self.assertEqual(parsed["correct_option_id"], poll_q["correct_option_id"])

    def test_explanation_is_captured(self):
        q, err = _poll_to_question(make_poll(explanation="Article 44 directs the State."))
        self.assertIsNone(err)
        self.assertEqual(q["explanation"], "Article 44 directs the State.")

    def test_plain_str_option_shape_still_imports(self):
        """Older Pyrogram builds hand back bare strings, not FormattedText."""
        q, err = _poll_to_question(make_poll(formatted=False, correct=(0,)))
        self.assertIsNone(err)
        self.assertEqual(q["correct_option_id"], 0)
        self.assertEqual(q["options"][0], "Article 42")

    def test_legacy_single_correct_option_id_supported(self):
        q, err = _poll_to_question(make_poll(correct=(3,), legacy_single=True))
        self.assertIsNone(err)
        self.assertEqual(q["correct_option_id"], 3)

    def test_answer_index_refers_to_original_positions(self):
        """The last option is correct -- off-by-one here would be silent."""
        opts = ("A one", "B two", "C three", "D four")
        q, err = _poll_to_question(make_poll(options=opts, correct=(3,)))
        self.assertIsNone(err)
        self.assertEqual(q["options"][q["correct_option_id"]], "D four")

    def test_two_option_poll_is_accepted(self):
        q, err = _poll_to_question(make_poll(options=("Yes", "No"), correct=(0,)))
        self.assertIsNone(err)
        self.assertEqual(len(q["options"]), 2)


# ─────────────────────────── _poll_to_question: rejections ───────────────────


class PollConversionRejectionCases(unittest.TestCase):
    def _rejected(self, poll, remove_words=None):
        q, err = _poll_to_question(poll, remove_words)
        self.assertIsNone(q)
        self.assertIsInstance(err, str)
        self.assertTrue(err.strip())
        return err

    def test_none_poll(self):
        self.assertIn("no poll", self._rejected(None))

    def test_regular_poll_rejected(self):
        err = self._rejected(make_poll(poll_type=PollType.REGULAR))
        self.assertIn("regular poll", err)

    def test_multi_answer_poll_rejected(self):
        err = self._rejected(make_poll(correct=(0, 2)))
        self.assertIn("Multiple-answer", err)

    def test_unmarked_answer_rejected(self):
        err = self._rejected(make_poll(correct=()))
        self.assertIn("no marked correct answer", err)

    def test_out_of_range_answer_rejected(self):
        err = self._rejected(make_poll(correct=(9,)))
        self.assertIn("outside its options", err)

    def test_negative_answer_rejected(self):
        self.assertIn("outside its options", self._rejected(make_poll(correct=(-1,))))

    def test_single_option_rejected(self):
        self.assertIn("at least two options", self._rejected(make_poll(options=("Only",))))

    def test_no_options_rejected(self):
        self.assertIn("at least two options", self._rejected(make_poll(options=())))

    def test_empty_option_rejected(self):
        self.assertIn(
            "empty option",
            self._rejected(make_poll(options=("Article 44", "   "))),
        )

    def test_missing_question_text_rejected(self):
        self.assertIn("no question text", self._rejected(make_poll(question="")))

    def test_none_question_rejected(self):
        self.assertIn("no question text", self._rejected(make_poll(question=None)))

    def test_poll_type_none_is_tolerated(self):
        """Some builds omit ``type``; a marked answer is proof enough."""
        poll = make_poll(correct=(1,))
        poll.type = None
        q, err = _poll_to_question(poll)
        self.assertIsNone(err)
        self.assertEqual(q["correct_option_id"], 1)


# ─────────────────────────── cleanup: words + noise ──────────────────────────


class PollCleanupCases(unittest.TestCase):
    def test_remove_words_applied_to_question_and_options(self):
        poll = make_poll(
            question="Solve: what is JOIN the capital?",
            options=("JOIN Delhi", "JOIN Mumbai"),
            correct=(0,),
            explanation="JOIN Delhi is correct.",
        )
        q, err = _poll_to_question(poll, ["JOIN"])
        self.assertIsNone(err)
        self.assertEqual(q["question"], "Solve: what is the capital?")
        self.assertEqual(q["options"], ["Delhi", "Mumbai"])
        self.assertEqual(q["explanation"], "Delhi is correct.")

    def test_source_noise_stripped(self):
        poll = make_poll(
            question="[Q 3/10] Capital of India? https://t.me/somechannel",
            options=("Delhi", "Mumbai"),
            correct=(0,),
        )
        q, err = _poll_to_question(poll)
        self.assertIsNone(err)
        self.assertNotIn("t.me", q["question"])
        self.assertNotIn("[Q 3/10]", q["question"])

    def test_poll_reduced_to_nothing_is_rejected(self):
        poll = make_poll(
            question="t.me/onlylink",
            options=("https://x.test/a", "https://x.test/b"),
            correct=(0,),
        )
        q, err = _poll_to_question(poll)
        self.assertIsNone(q)
        self.assertIn("Nothing usable", err)

    def test_no_remove_words_is_a_noop(self):
        a, _ = _poll_to_question(make_poll(explanation="Ex here."))
        b, _ = _poll_to_question(make_poll(explanation="Ex here."), [])
        self.assertEqual(a, b)


# ─────────────────────────────── _import_poll ────────────────────────────────


class ImportPollCases(unittest.TestCase):
    def test_appends_question_and_reports_running_total(self):
        uid = _uid()
        ud = _session(uid, quiz_name="Polity", awaiting_name=False)
        ud["questions"].append({"question": "pre-existing", "options": ["a", "b"],
                                "correct_option_id": 0, "explanation": None})
        m = make_msg(poll=make_poll(correct=(1,)), uid=uid)
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(qc._import_poll(m, uid, ud))

        self.assertEqual(len(ud["questions"]), 2)
        imported = ud["questions"][1]
        self.assertEqual(imported["correct_option_id"], 1)
        self.assertIsNone(imported["file_id"])
        self.assertIsNone(imported["reply_text"])
        self.assertIn("2 questions saved", m.reply.await_args.args[0])

    def test_remove_words_flow_through_from_user_profile(self):
        uid = _uid()
        ud = _session(uid)
        poll = make_poll(question="JOIN capital?", options=("JOIN Delhi", "Mumbai"), correct=(0,))
        m = make_msg(poll=poll, uid=uid)
        p1, p2 = _repo_ctx(remove_words=["JOIN"])
        with p1, p2:
            _run(qc._import_poll(m, uid, ud))
        self.assertEqual(ud["questions"][0]["question"], "capital?")
        self.assertEqual(ud["questions"][0]["options"], ["Delhi", "Mumbai"])

    def test_db_failure_does_not_block_import(self):
        uid = _uid()
        ud = _session(uid)
        m = make_msg(poll=make_poll(correct=(0,)), uid=uid)
        p1, p2 = _repo_ctx(boom=True)
        with p1, p2:
            _run(qc._import_poll(m, uid, ud))
        self.assertEqual(len(ud["questions"]), 1)

    def test_invalid_poll_replies_and_appends_nothing(self):
        uid = _uid()
        ud = _session(uid)
        m = make_msg(poll=make_poll(poll_type=PollType.REGULAR), uid=uid)
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(qc._import_poll(m, uid, ud))
        self.assertEqual(ud["questions"], [])
        self.assertIn("regular poll", m.reply.await_args.args[0])


# ─────────────────────────── handle_creation_message ─────────────────────────


class HandleCreationMessagePollCases(unittest.TestCase):
    def test_poll_during_question_entry_is_imported(self):
        uid = _uid()
        ud = _session(uid, quiz_name="Polity")
        m = make_msg(poll=make_poll(correct=(2,)), uid=uid)
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(handle_creation_message(MagicMock(), m))
        self.assertEqual(len(ud["questions"]), 1)
        self.assertEqual(ud["questions"][0]["correct_option_id"], 2)

    def test_poll_before_naming_is_refused_not_crash(self):
        """Regression: awaiting_name did ``m.text.strip()`` on a poll -> AttributeError."""
        uid = _uid()
        ud = _session(uid, awaiting_name=True)
        m = make_msg(poll=make_poll(correct=(0,)), uid=uid)
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(handle_creation_message(MagicMock(), m))  # must not raise
        self.assertEqual(ud["questions"], [])
        self.assertIsNone(ud["quiz_name"])
        self.assertIn("Reply with text", m.reply.await_args.args[0])

    def test_poll_during_every_typed_step_is_refused(self):
        for step in _TYPED_INPUT_STEPS:
            with self.subTest(step=step):
                uid = _uid()
                ud = _session(uid, **{step: True})
                m = make_msg(poll=make_poll(correct=(0,)), uid=uid)
                p1, p2 = _repo_ctx()
                with p1, p2:
                    _run(handle_creation_message(MagicMock(), m))
                self.assertEqual(ud["questions"], [], f"{step} must not import")
                self.assertIn("Reply with text", m.reply.await_args.args[0])

    def test_poll_from_stranger_is_ignored(self):
        m = make_msg(poll=make_poll(correct=(0,)))
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(handle_creation_message(MagicMock(), m))
        m.reply.assert_not_awaited()

    def test_plain_text_question_still_parses(self):
        """No regression for the existing paste-a-question path."""
        uid = _uid()
        ud = _session(uid, quiz_name="Polity")
        m = make_msg(text="Capital of India?\nA) Delhi ✅\nB) Mumbai", uid=uid)
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(handle_creation_message(MagicMock(), m))
        self.assertEqual(len(ud["questions"]), 1)
        self.assertEqual(ud["questions"][0]["correct_option_id"], 0)

    def test_mixed_poll_and_text_import_both(self):
        uid = _uid()
        ud = _session(uid, quiz_name="Polity")
        p1, p2 = _repo_ctx()
        with p1, p2:
            _run(handle_creation_message(
                MagicMock(), make_msg(text="Q1?\nA) a ✅\nB) b", uid=uid)))
            _run(handle_creation_message(
                MagicMock(), make_msg(poll=make_poll(correct=(1,)), uid=uid)))
        self.assertEqual(len(ud["questions"]), 2)


# ───────────────────────────────── command audit ─────────────────────────────


class ReservedCommandAuditCases(unittest.TestCase):
    def test_wizard_exit_commands_are_reserved(self):
        for cmd in ("done", "cancel", "create", "start", "help"):
            self.assertIn(cmd, _RESERVED_COMMANDS, f"/{cmd} must stay reachable")

    def test_no_duplicate_reserved_commands(self):
        self.assertEqual(len(_RESERVED_COMMANDS), len(set(_RESERVED_COMMANDS)))

    def test_typed_input_steps_are_a_subset_of_the_wizards_awaiting_flags(self):
        """Guards against a new awaiting_* step forgetting the poll guard."""
        src = open(qc.__file__, encoding="utf-8").read()
        for step in _TYPED_INPUT_STEPS:
            self.assertIn(f'ud.get("{step}")', src, f"{step} is no longer checked")


class _RecordingClient:
    """Captures what ``register()`` wires up, without a real Pyrogram Client."""

    def __init__(self):
        self.message_handlers = []
        self.callback_handlers = []

    def on_message(self, flt):
        self.message_handlers.append(flt)

        def _decorate(func):
            return func

        return _decorate

    def on_callback_query(self, flt):
        self.callback_handlers.append(flt)

        def _decorate(func):
            return func

        return _decorate


class _FakeMessage(_RealMessage):
    """A real ``pyrogram.types.Message`` instance (so Pyrogram's
    ``isinstance``-based filters accept it) that skips the heavy ``__init__``.
    """

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def _fake_tg_msg(uid, *, text=None, poll=None):
    """A Message-shaped stand-in good enough for Pyrogram's filter callables."""
    from pyrogram.enums import ChatType

    return _FakeMessage(
        id=1,
        text=text,
        poll=poll,
        caption=None,
        chat=SimpleNamespace(type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=uid),
        via_bot=None,
        outgoing=False,
        service=False,
        reply_to_message=None,
        video_note=None,
        command=None,
        entities=None,
    )


class RegistrationAuditCases(unittest.TestCase):
    """Audit what ``register()`` actually wires up, using the real filters."""

    def setUp(self):
        self.app = _RecordingClient()
        qc.register(self.app)
        # /create, /done, /cancel, document, photo, text|poll catch-all
        self.assertEqual(len(self.app.message_handlers), 6)
        self.assertEqual(len(self.app.callback_handlers), 1)
        self.catchall = self.app.message_handlers[-1]

    def test_catchall_lets_a_poll_through(self):
        """The original bug: polls were accepted by the filter but nothing ever
        read ``m.poll``, so they vanished. This pins the routing half."""
        uid = _uid()
        _session(uid, quiz_name="Polity")
        ok = _run(self.catchall(MagicMock(), _fake_tg_msg(uid, poll=make_poll())))
        self.assertTrue(ok)
        creator_state.quiz_creation.pop(uid, None)

    def test_catchall_does_not_swallow_reserved_commands(self):
        for cmd in ("done", "cancel", "start", "help", "myquizzes"):
            with self.subTest(cmd=cmd):
                uid = _uid()
                _session(uid, quiz_name="Polity")
                ok = _run(self.catchall(MagicMock(), _fake_tg_msg(uid, text=f"/{cmd}")))
                self.assertFalse(ok, f"/{cmd} must not be caught by the wizard")
                creator_state.quiz_creation.pop(uid, None)

    def test_catchall_ignores_users_with_no_wizard_open(self):
        uid = _uid()
        creator_state.quiz_creation.pop(uid, None)
        ok = _run(self.catchall(MagicMock(), _fake_tg_msg(uid, poll=make_poll())))
        self.assertFalse(ok)

    def test_reserved_command_handlers_are_registered(self):
        uid = _uid()
        _session(uid, quiz_name="Polity")
        for idx, cmd in ((0, "create"), (1, "done"), (2, "cancel")):
            with self.subTest(cmd=cmd):
                flt = self.app.message_handlers[idx]
                self.assertTrue(
                    _run(flt(MagicMock(), _fake_tg_msg(uid, text=f"/{cmd}")))
                )
        creator_state.quiz_creation.pop(uid, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
