"""PART 3 -- /aiquiz hardening + /schedule end-to-end audit.

Offline: no Telegram network, no MongoDB, no AI provider. A real
``AsyncIOScheduler`` is used so the "does the job actually fire" tests exercise
APScheduler rather than a stub.

Bugs pinned down here (all reproduced against the pre-fix code first):

/aiquiz
  * the topic typed after ``/aiquiz`` and the model's own question/option text
    were interpolated raw into ``parse_mode=HTML`` messages. A single ``<`` made
    Telegram reject the message: the opening prompt never arrived at all
    (``safe_send_message`` -> BadRequest -> None) and every wizard step silently
    failed to re-render (``_edit`` swallowed the error), freezing the wizard;
  * callback_data values were trusted, so a forged callback could inject markup
    into a group message and reach ``int()``/``float()`` and die in the
    catch-all handler;
  * nothing prevented a second concurrent generation for one user -- Telegram
    keeps an inline keyboard attached to an edited message unless an empty one
    is passed, so the exam-style buttons stayed live for the whole 30-120s
    generation and a second tap spawned a second provider run (double spend,
    two tasks racing on one message and one session);
  * an empty result or an exception dropped the whole wizard session, so a
    retry meant re-typing /aiquiz and re-picking five settings.

/schedule
  * ``_run`` hardcoded ``correct_mark: 1.0`` and omitted ``show_explanation``,
    so a scheduled quiz was rescored at +1 and lost its explanations even when
    the quiz document said +4 / show them (``/start`` uses the document);
  * ``_run`` passed ``protect: False``, disabling forward/save protection on
    every scheduled group quiz while every manual path enables it;
  * ``_run`` did not check for a live quiz, so a schedule firing mid-quiz
    replaced ``session_mgr``'s entry -- participants, answers and poll ids of
    the running quiz were lost and two loops posted into one group;
  * ``except Exception: return`` around ``get_chat_member`` made a failed
    permission check completely silent;
  * ``/cancelschedule`` removed only the first matching job and still reported
    success, leaving the other run of the same quiz to fire anyway;
  * ``restore()`` only re-armed future rows, so schedules missed while the bot
    was down were never reported and their rows stayed in MongoDB forever.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.error import BadRequest

from quizbot.runner_bot import ai_providers, state
from quizbot.runner_bot.handlers import ai_quiz, quiz_play, scheduling, setup_wizard
from quizbot.runner_bot.telegram_utils import esc

# ───────────────────────────────── helpers ──────────────────────────────────

_UIDS = itertools.count(880000)
_CHATS = itertools.count(-100900)


def _uid() -> int:
    return next(_UIDS)


def _chat() -> int:
    return next(_CHATS)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


# ─────────────────── Telegram HTML parse-mode emulation ─────────────────────

TG_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span",
    "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji", "tg-quote",
}
_ENTITY_RE = re.compile(r"&#?(\w{1,12});")
_VALID_ENTITIES = {"amp", "lt", "gt", "quot", "apos", "nbsp", "#39"}


def tg_html_check(text: str) -> None:
    """Raise BadRequest the way the Bot API does for ``parse_mode=HTML``.

    Unknown/unbalanced tags, invalid entities and stray ``<`` are rejected; a
    lone ``&`` is tolerated (Telegram tolerates it too).
    """
    stack: list[str] = []
    for m in re.finditer(r"<(/?)\s*([a-zA-Z][a-zA-Z0-9\-]*)([^>]*)>", text):
        closing, tag = m.group(1) == "/", m.group(2).lower()
        if tag not in TG_ALLOWED_TAGS:
            raise BadRequest(f"Can't parse entities: unsupported start tag \"{m.group(2)}\"")
        if closing:
            if not stack or stack[-1] != tag:
                raise BadRequest(f"Can't parse entities: unexpected end tag \"{tag}\"")
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        raise BadRequest(f"Can't parse entities: can't find end tag \"{stack[-1]}\"")
    for i, ch in enumerate(text):
        if ch == "<" and not re.match(r"</?\s*[a-zA-Z][a-zA-Z0-9\-]*[^>]*>", text[i:]):
            raise BadRequest(f"Can't parse entities: unexpected '<' at byte offset {i}")
    for m in _ENTITY_RE.finditer(text):
        if m.group(1) not in _VALID_ENTITIES:
            raise BadRequest(f"Can't parse entities: unsupported entity \"{m.group(0)}\"")


class Sent:
    """A message the bot "sent"."""

    def __init__(self, chat_id, text, kwargs):
        self.chat_id = chat_id
        self.text = text
        self.kwargs = dict(kwargs)
        self.message_id = 4242
        self.poll = None

    @property
    def parse_mode(self):
        return self.kwargs.get("parse_mode")

    async def edit_text(self, text, **kw):
        if kw.get("parse_mode") == "HTML":
            tg_html_check(text)
        self.text = text
        return self


class FakeBot:
    """Records outgoing calls and validates HTML exactly like Telegram."""

    def __init__(self, admin_status="administrator", member_raises=None, send_raises=None):
        self.sent: list[Sent] = []
        self.admin_status = admin_status
        self.member_raises = member_raises
        self.send_raises = send_raises
        self.member_calls = 0

    async def send_message(self, chat_id=None, text=None, **kw):
        if self.send_raises:
            raise self.send_raises
        if kw.get("parse_mode") == "HTML":
            tg_html_check(text)
        s = Sent(chat_id, text, kw)
        self.sent.append(s)
        return s

    async def get_chat_member(self, chat_id, user_id):
        self.member_calls += 1
        if self.member_raises:
            raise self.member_raises
        return SimpleNamespace(status=self.admin_status, user=SimpleNamespace(id=user_id))

    async def get_chat(self, chat_id):
        return SimpleNamespace(title="Test Group", first_name=None, username=None, id=chat_id)

    def texts(self):
        return [s.text for s in self.sent]


class FakeCtx:
    def __init__(self, bot=None):
        self.bot = bot or FakeBot()
        self.args: list[str] = []


class FakeMessage:
    def __init__(self, chat_id, user_id, chat_type="supergroup", text="/x", thread_id=None):
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = SimpleNamespace(id=user_id, first_name="Admin", is_bot=False)
        self.text = text
        self.message_id = 1
        self.message_thread_id = thread_id


class FakeUpdate:
    def __init__(self, message=None, callback_query=None):
        self.message = message
        self.callback_query = callback_query
        self.effective_message = message
        self.effective_chat = message.chat if message is not None else None


class WizardMsg:
    """The single message a /aiquiz wizard keeps editing."""

    def __init__(self):
        self.edits: list[tuple] = []
        self.rejected = 0
        self.text = None
        self.last_markup = "unset"

    async def edit_text(self, text, **kw):
        if kw.get("parse_mode") == "HTML":
            try:
                tg_html_check(text)
            except BadRequest:
                self.rejected += 1
                raise
        self.edits.append((text, kw))
        self.text = text
        self.last_markup = kw.get("reply_markup", "omitted")
        return Sent(1, text, kw)


class FakeQuery:
    def __init__(self, data, uid, message=None):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = message
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))

    @property
    def alerts(self):
        return [a for a in self.answers if a[0]]


# ───────────────────────── in-memory MongoDB stand-in ───────────────────────


class FakeCol:
    def __init__(self, name="c"):
        self.name = name
        self.docs: dict[str, dict] = {}
        self.fail_writes = False
        self.events: list[str] = []
        self._n = 0

    def _match(self, doc, filt) -> bool:
        for k, v in filt.items():
            if isinstance(v, dict):
                if "$gt" in v and not (doc.get(k) is not None and doc[k] > v["$gt"]):
                    return False
                if "$lte" in v and not (doc.get(k) is not None and doc[k] <= v["$lte"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find(self, filt=None):
        col = self
        rows = [dict(d) for d in self.docs.values() if self._match(d, filt or {})]

        class _Cur:
            def __init__(self, rows):
                self._it = iter(rows)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    row = next(self._it)
                except StopIteration:
                    raise StopAsyncIteration
                col.events.append("scan")
                return row

        return _Cur(rows)

    async def update_one(self, filt, update, upsert=False):
        if self.fail_writes:
            raise RuntimeError("MongoDB write failed")
        for d in self.docs.values():
            if self._match(d, filt):
                d.update(update.get("$set", {}))
                return
        if upsert:
            self._n += 1
            doc = dict(filt)
            doc.update(update.get("$set", {}))
            self.docs[str(self._n)] = doc

    async def delete_one(self, filt):
        self.events.append("delete")
        for key in list(self.docs):
            if self._match(self.docs[key], filt):
                del self.docs[key]
                return

    def job_ids(self):
        return sorted(d["job_id"] for d in self.docs.values() if "job_id" in d)


class FakeDb:
    def __init__(self):
        self.cols: dict[str, FakeCol] = {}

    def collection(self, name):
        return self.cols.setdefault(name, FakeCol(name))

    def __getitem__(self, name):
        return self.collection(name)


class FakeQuizRepo:
    quizzes: dict[str, dict] = {}

    def __init__(self, db=None):
        pass

    async def get(self, qid):
        q = self.quizzes.get(qid)
        return dict(q) if q else None


def frozen_clock(when: datetime):
    """Patch `scheduling.datetime` so `datetime.now(IST)` returns a fixed instant.

    /schedule's day-boundary rule (a time that has already passed today rolls
    to tomorrow) is only testable deterministically with a known clock: with a
    real one, "90 minutes from now" crosses midnight in the late evening and
    the "stays today" expectation flips.
    """
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz is not None else when

    return patch.object(scheduling, "datetime", _FrozenDatetime)


def make_quiz(qid="ABC123", **over) -> dict:
    q = {
        "qid": qid, "quiz_name": "Polity <Mock> & Current", "creator_id": 999,
        "timer": 45, "negative_marks": 0.25, "correct_marks": 4,
        "shuffle_questions": False, "shuffle_options": True, "show_explanation": True,
        "fixed_settings": True, "sections": [], "promo_message": None, "quiz_type": "free",
        "questions": [
            {"question": "Q1 <tag> & more", "options": ["A & B", "C <D>", "E", "F"],
             "correct_option_id": 0, "explanation": "Because <reasons>", "file_id": None,
             "reply_text": None},
            {"question": "Q2", "options": ["A", "B", "C", "D"], "correct_option_id": 1,
             "explanation": None, "file_id": None, "reply_text": None},
        ],
    }
    q.update(over)
    return q


_UNSET = object()

# Captured before any test patches the module attribute away, so a test can
# still drive the REAL launcher with the settings a scheduled run produced.
_REAL_LAUNCH = setup_wizard._launch_quiz_from_settings

AI_QUESTIONS = [
    {"question": "Which article <312> deals with All India Services & why?",
     "options": ["Art 312 & 313", "Art <311>", "Art 314", "Art 315"],
     "correct_option_id": [0, 1], "explanation": "Because <reasons> & facts",
     "file_id": None, "reply_text": None},
    {"question": "Q2 plain", "options": ["A", "B", "C", "D"], "correct_option_id": 2,
     "explanation": None, "file_id": None, "reply_text": None},
    {"question": "Q3 plain", "options": ["A", "B", "C", "D"], "correct_option_id": 3,
     "explanation": None, "file_id": None, "reply_text": None},
]

HOSTILE_TOPIC = "Polity <Art 312> & Amendments"


# ══════════════════════════════ /aiquiz ═════════════════════════════════════


class AiQuizBase(unittest.TestCase):
    def setUp(self):
        state.AI_QUIZ_SESSIONS.clear()
        state.AI_QUIZ_INFLIGHT.clear()
        state.last_working_ai.clear()
        state.session_mgr.sessions.clear()
        self.bot = FakeBot()
        self.ctx = FakeCtx(self.bot)
        self.msg = WizardMsg()
        self.uid = _uid()
        self.chat = _chat()
        self.gen_calls: list[dict] = []
        self._patches = [
            patch.object(ai_quiz, "get_provider_keys", AsyncMock(return_value=[])),
            patch.object(ai_quiz, "generate_in_chunks", self._fake_generate),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    async def _fake_generate(self, uid, topic, count, lang, diff, exam, status_msg,
                             bilingual_lang2=None, chunk_size=25):
        self.gen_calls.append({"uid": uid, "topic": topic, "count": count, "lang": lang,
                               "diff": diff, "exam": exam})
        await asyncio.sleep(0.01)
        return [dict(q) for q in AI_QUESTIONS[:count]]

    def start_session(self, **over) -> dict:
        sess = {"topic": HOSTILE_TOPIC, "chat_id": self.chat, "chat_type": "supergroup",
                "user_id": self.uid}
        sess.update(over)
        state.AI_QUIZ_SESSIONS[self.uid] = sess
        return sess

    def tap(self, step, value=None, uid=None, msg=None):
        data = f"aiq_{step}_{self.uid if uid is None else uid}"
        if value is not None:
            data += f"_{value}"
        q = FakeQuery(data, self.uid if uid is None else uid, msg or self.msg)
        return q, FakeUpdate(callback_query=q)

    async def walk_to_exam(self, topic=HOSTILE_TOPIC):
        """Drive the real wizard from /aiquiz up to (not including) generation."""
        sess = self.start_session(topic=topic, count=10, lang="en", bilingual=False,
                                 lang2=None, diff="hard")
        for step, value in [("count", "10"), ("lang", "en"), ("bilingual", "no"),
                            ("diff", "hard")]:
            q, upd = self.tap(step, value)
            await ai_quiz.aiquiz_callback(upd, self.ctx)
        return sess


class AiQuizHtmlEscapingCases(AiQuizBase):
    def test_command_escapes_the_topic(self):
        self.ctx.args = HOSTILE_TOPIC.split()
        upd = FakeUpdate(FakeMessage(self.chat, self.uid, text="/aiquiz ..."))
        _run(ai_quiz.aiquiz_command(upd, self.ctx))
        self.assertEqual(len(self.bot.sent), 1, "the opening prompt must reach the user")
        text = self.bot.sent[0].text
        self.assertIn("&lt;Art 312&gt;", text)
        self.assertIn("&amp; Amendments", text)
        self.assertNotIn("<Art 312>", text)
        self.assertEqual(self.bot.sent[0].parse_mode, "HTML")

    def test_command_keeps_a_plain_topic_readable(self):
        self.ctx.args = ["Indian", "History"]
        _run(ai_quiz.aiquiz_command(FakeUpdate(FakeMessage(self.chat, self.uid)), self.ctx))
        self.assertIn("AI Quiz: Indian History", self.bot.sent[0].text)

    def test_command_drops_the_session_when_the_prompt_never_arrives(self):
        self.bot.send_raises = BadRequest("Chat not found")
        self.ctx.args = ["History"]
        _run(ai_quiz.aiquiz_command(FakeUpdate(FakeMessage(self.chat, self.uid)), self.ctx))
        self.assertNotIn(self.uid, state.AI_QUIZ_SESSIONS)

    def test_command_without_args_shows_usage(self):
        self.ctx.args = []
        _run(ai_quiz.aiquiz_command(FakeUpdate(FakeMessage(self.chat, self.uid)), self.ctx))
        self.assertIn("/aiquiz &lt;topic&gt;", self.bot.sent[0].text)
        self.assertNotIn(self.uid, state.AI_QUIZ_SESSIONS)

    def test_every_wizard_step_renders_with_a_hostile_topic(self):
        async def go():
            self.ctx.args = HOSTILE_TOPIC.split()
            await ai_quiz.aiquiz_command(FakeUpdate(FakeMessage(self.chat, self.uid)), self.ctx)
            for step, value in [("count", "10"), ("lang", "en"), ("bilingual", "yes"),
                                ("lang2", "hi"), ("diff", "extreme")]:
                q, upd = self.tap(step, value)
                await ai_quiz.aiquiz_callback(upd, self.ctx)
        _run(go())
        self.assertEqual(self.msg.rejected, 0, "no wizard step may fail to re-render")
        self.assertEqual(len(self.msg.edits), 5)
        for text, _kw in self.msg.edits:
            self.assertNotIn("<Art 312>", text)

    def test_preview_escapes_model_output(self):
        async def go():
            await self.walk_to_exam()
            q, upd = self.tap("exam", "civil")
            await ai_quiz.aiquiz_callback(upd, self.ctx)
            await asyncio.sleep(0.1)
        _run(go())
        self.assertEqual(self.msg.rejected, 0)
        self.assertIn("questions generated", self.msg.text)
        self.assertIn("&lt;312&gt;", self.msg.text)
        self.assertIn("Art 312 &amp; 313", self.msg.text)
        self.assertNotIn("<312>", self.msg.text)
        self.assertNotIn("A & B", self.msg.text.replace("&amp;", "@"))

    def test_generation_error_escapes_the_exception_text(self):
        async def boom(*a, **k):
            await asyncio.sleep(0)
            raise RuntimeError("Gemini 429: quota <exhausted> & retry later")

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", boom):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.1)
        _run(go())
        self.assertEqual(self.msg.rejected, 0)
        self.assertIn("Generation error", self.msg.text)
        self.assertIn("&lt;exhausted&gt;", self.msg.text)

    def test_ready_card_escapes_the_topic(self):
        async def go():
            sess = await self.walk_to_exam()
            q, upd = self.tap("exam", "civil")
            await ai_quiz.aiquiz_callback(upd, self.ctx)
            await asyncio.sleep(0.1)
            sess.update(timer=20, neg=0.25, cm=1, shuffle_q=False, shuffle_o=False,
                        shuffle_o_count=0, questions=AI_QUESTIONS)
            q, upd = self.tap("ex", "no")
            with patch.object(ai_quiz, "_launch_ai_quiz", AsyncMock()) as launch:
                await ai_quiz.aiquiz_callback(upd, self.ctx)
            return launch
        launch = _run(go())
        self.assertIn("&lt;Art 312&gt;", self.msg.text)
        self.assertEqual(launch.await_count, 1)

    def test_edit_falls_back_to_plain_text_when_html_is_rejected(self):
        """Defence in depth: even if escaping were missed, the step still shows."""
        async def go():
            await self.msg.edit_text("<b>fine</b>", parse_mode="HTML")
            # a hand-built message that Telegram would reject
            broken = "boom <not-a-tag> here"
            await ai_quiz._edit(self.msg, broken)
            return self.msg.text
        text = _run(go())
        self.assertEqual(text, "boom <not-a-tag> here")

    def test_progress_message_inside_generate_in_chunks_escapes_the_topic(self):
        """Real generate_in_chunks (only the provider call is stubbed)."""
        prompts: list[str] = []
        status = WizardMsg()

        async def fake_ai(user_id, prompt, max_tokens=4096):
            prompts.append(prompt)
            await asyncio.sleep(0)
            import json
            return json.dumps([{"q": f"Question <{i}>", "o": ["A", "B", "C", "D"],
                                "c": [0], "e": None} for i in range(25)])

        async def go():
            with patch.object(ai_providers, "ai_generate", fake_ai):
                return await ai_providers.generate_in_chunks(
                    self.uid, HOSTILE_TOPIC, 30, "en", "hard", "civil", status)
        qs = _run(go())
        self.assertEqual(len(qs), 30)
        self.assertEqual(status.rejected, 0, "the progress edit must not be rejected")
        self.assertIn("&lt;Art 312&gt; &amp; Amendments", status.text)
        # ... while the prompt handed to the model still carries the raw topic.
        self.assertIn(f'"{HOSTILE_TOPIC}"', prompts[0])


class AiQuizPromptUnchangedCases(unittest.TestCase):
    """The Gemini question-generation prompt must be byte-for-byte untouched."""

    EXPECTED_FORMAT = (
        '[TASK]Generate {count} MCQ on: "{topic}"\n'
        "[STYLE]{diff}|{exam}\n"
        "[LANG]{lang}\n"
        "[RULES]JSON array only. No prose. No markdown. Start with [ end with ]\n"
        'Each item: {{"q":"<270ch","o":["A","B","C","D"],"c":[0],"e":"<150ch or null"}}\n'
        "c = 0-based correct index array. Multi-correct allowed.\n"
        "[OUTPUT]"
    )
    EXPECTED_BILINGUAL = (
        '[TASK]Generate {count} MCQ on: "{topic}"\n'
        "[STYLE]{diff}|{exam}\n"
        "[LANG]Bilingual: {lang1} / {lang2}\n"
        "[RULES]JSON array only. No prose. No markdown. Start with [ end with ]\n"
        'Each item: {{"q":"<question in {lang1_short}> / <question in {lang2_short}>",'
        '"o":["<A in {lang1_short}> / <A in {lang2_short}>","<B in {lang1_short}> / '
        '<B in {lang2_short}>","<C in {lang1_short}> / <C in {lang2_short}>",'
        '"<D in {lang1_short}> / <D in {lang2_short}>"],"c":[0],'
        '"e":"<exp in {lang1_short}> / <exp in {lang2_short}> or null"}}\n'
        "Use / as separator between the two languages in every text field.\n"
        "Keep total q length ≤270 chars, each option ≤90 chars.\n"
        "[OUTPUT]"
    )
    EXPECTED_GEMINI_SYSTEM = (
        "You are an expert quiz question generator. Output ONLY the formatted "
        "questions, nothing else — no intro, no numbering outside the format, no extra text."
    )

    def test_question_format_constant(self):
        self.assertEqual(ai_providers.QUESTION_FORMAT, self.EXPECTED_FORMAT)

    def test_bilingual_question_format_constant(self):
        self.assertEqual(ai_providers.QUESTION_FORMAT_BILINGUAL, self.EXPECTED_BILINGUAL)

    def test_style_and_language_tables(self):
        self.assertEqual(
            ai_providers.AIQUIZ_DIFFICULTY["extreme"],
            "Extreme Hard — UPSC Mains/IAS depth, statement-based",
        )
        self.assertEqual(
            ai_providers.AIQUIZ_EXAM["civil"],
            "UPSC Civil Services — analytical, multi-statement, multi-correct possible",
        )
        self.assertEqual(ai_providers.AIQUIZ_LANG["en"], "English")

    def test_gemini_request_payload_is_unchanged(self):
        """Pin the exact wire payload: system instruction, contents, config."""
        captured = {}

        async def fake_request(method, url, json_body=None, headers=None, **kw):
            captured.update(method=method, url=url, body=json_body, headers=headers)
            return 200, {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}

        async def go():
            with patch.object(ai_providers, "request_json", fake_request):
                return await ai_providers._call_gemini("AIza-key", "PROMPT-BODY", max_tokens=2048)
        _run(go())
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], ai_providers.config.GEMINI_URL)
        self.assertEqual(captured["headers"]["x-goog-api-key"], "AIza-key")
        body = captured["body"]
        self.assertEqual(body["system_instruction"]["parts"][0]["text"], self.EXPECTED_GEMINI_SYSTEM)
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": "PROMPT-BODY"}]}])
        self.assertEqual(body["generationConfig"], {"maxOutputTokens": 2048, "temperature": 0.7})

    def test_prompt_rendering_is_unchanged(self):
        rendered = ai_providers.QUESTION_FORMAT.format(
            count=25, topic=HOSTILE_TOPIC, lang="English",
            diff=ai_providers.AIQUIZ_DIFFICULTY["hard"],
            exam=ai_providers.AIQUIZ_EXAM["civil"],
        )
        self.assertTrue(rendered.startswith(f'[TASK]Generate 25 MCQ on: "{HOSTILE_TOPIC}"'))
        self.assertIn("[STYLE]Hard — advanced level, tricky options|UPSC Civil Services", rendered)
        self.assertIn("[LANG]English", rendered)
        # The topic reaches the model raw -- escaping is a Telegram concern only.
        self.assertIn("<Art 312> & Amendments", rendered)


class AiQuizCallbackValidationCases(AiQuizBase):
    def test_every_button_value_is_on_the_allow_list(self):
        """The allow-list and the keyboards must never drift apart."""
        for step in ("shufflecount", "count", "lang", "bilingual", "lang2", "diff",
                     "exam", "timer", "neg", "cm", "shuffle"):
            kb = ai_quiz._kb(step, self.uid)
            for row in kb.inline_keyboard:
                for btn in row:
                    parts = btn.callback_data.split("_", 3)
                    self.assertEqual(parts[0], "aiq")
                    self.assertEqual(parts[1], step, f"{btn.text} -> {btn.callback_data}")
                    value = parts[3] if len(parts) > 3 else None
                    allowed = ai_quiz._STEP_VALUES.get(step)
                    if step == "count":
                        self.assertTrue(1 <= int(value) <= 100)
                    else:
                        self.assertIn(value, allowed, f"{step}: {value!r} not in {sorted(allowed)}")

    def test_forged_values_are_rejected_and_never_stored(self):
        cases = [("lang", "<script>alert(1)</script>"), ("lang2", "notalang"),
                 ("diff", "<b>x"), ("exam", "&amp;"), ("timer", "99999"),
                 ("neg", "-5"), ("cm", "abc"), ("shufflecount", "99"),
                 ("bilingual", "maybe"), ("shuffle", "everything"), ("ex", "perhaps")]

        async def go():
            self.start_session(count=10, lang="en", diff="hard", exam="civil",
                               timer=20, neg=0.0, cm=1)
            for step, value in cases:
                q, upd = self.tap(step, value)
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                self.assertTrue(q.alerts, f"{step}={value!r} must be rejected with an alert")
        _run(go())
        sess = state.AI_QUIZ_SESSIONS[self.uid]
        self.assertEqual(sess["lang"], "en")
        self.assertEqual(sess["diff"], "hard")
        self.assertEqual(sess["exam"], "civil")
        self.assertEqual(sess["timer"], 20)
        self.assertEqual(sess["neg"], 0.0)
        self.assertEqual(sess["cm"], 1)
        self.assertEqual(self.msg.rejected, 0)
        self.assertEqual(len(self.gen_calls), 0)

    def test_forged_count_is_rejected_with_an_alert(self):
        async def go():
            self.start_session()
            for value in ("abc", "", "0", "101", "-5"):
                q, upd = self.tap("count", value or "x")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                self.assertTrue(q.alerts, f"count={value!r} must be rejected")
            self.assertNotIn("count", state.AI_QUIZ_SESSIONS[self.uid])
        _run(go())

    def test_another_users_callback_is_rejected(self):
        async def go():
            self.start_session()
            other = _uid()
            q = FakeQuery(f"aiq_count_{other}_50", self.uid, self.msg)
            await ai_quiz.aiquiz_callback(FakeUpdate(callback_query=q), self.ctx)
            self.assertTrue(any("Not your session" in a[0] for a in q.alerts))
            self.assertNotIn("count", state.AI_QUIZ_SESSIONS[self.uid])
        _run(go())

    def test_expired_session_is_reported(self):
        async def go():
            q, upd = self.tap("count", "10")
            await ai_quiz.aiquiz_callback(upd, self.ctx)
            self.assertIn("Session expired", self.msg.text)
        _run(go())

    def test_malformed_callback_data_is_ignored(self):
        async def go():
            self.start_session()
            q = FakeQuery("aiq_count", self.uid, self.msg)
            await ai_quiz.aiquiz_callback(FakeUpdate(callback_query=q), self.ctx)
            q = FakeQuery(f"aiq_nonsense_{self.uid}", self.uid, self.msg)
            await ai_quiz.aiquiz_callback(FakeUpdate(callback_query=q), self.ctx)
            self.assertEqual(len(self.gen_calls), 0)
        _run(go())


class AiQuizConcurrencyCases(AiQuizBase):
    def test_claim_is_atomic_and_one_shot(self):
        self.assertTrue(ai_quiz._claim_generation(self.uid))
        self.assertFalse(ai_quiz._claim_generation(self.uid))
        self.assertIn(self.uid, state.AI_QUIZ_INFLIGHT)
        ai_quiz._release_generation(self.uid)
        self.assertNotIn(self.uid, state.AI_QUIZ_INFLIGHT)
        self.assertTrue(ai_quiz._claim_generation(self.uid))

    def test_stale_inflight_entry_is_aged_out(self):
        state.AI_QUIZ_INFLIGHT[self.uid] = (
            __import__("time").monotonic() - ai_quiz.INFLIGHT_TTL - 1
        )
        self.assertTrue(ai_quiz._claim_generation(self.uid))

    def test_double_tap_starts_exactly_one_generation(self):
        async def go():
            await self.walk_to_exam()
            q1, u1 = self.tap("exam", "civil")
            q2, u2 = self.tap("exam", "ssc")
            await asyncio.gather(
                ai_quiz.aiquiz_callback(u1, self.ctx),
                ai_quiz.aiquiz_callback(u2, self.ctx),
            )
            await asyncio.sleep(0.1)
            return q2
        q2 = _run(go())
        self.assertEqual(len(self.gen_calls), 1, "one generation per user, never two")
        self.assertTrue(q2.alerts, "the second tap must explain itself")
        self.assertIn("still being generated", q2.alerts[0][0])
        self.assertEqual(self.gen_calls[0]["exam"], "civil")

    def test_keyboard_is_cleared_while_generating(self):
        """Removing the buttons is the UI-level half of the duplicate guard."""
        async def go():
            await self.walk_to_exam()
            q, upd = self.tap("exam", "civil")
            await ai_quiz.aiquiz_callback(upd, self.ctx)
            await asyncio.sleep(0.1)
        _run(go())
        status_edits = [(text, kw) for text, kw in self.msg.edits if "Generating questions" in text]
        self.assertEqual(len(status_edits), 1)
        markup = status_edits[0][1].get("reply_markup")
        self.assertIsNotNone(markup, "the buttons must be removed while generating")
        self.assertFalse(markup.inline_keyboard)

    def test_slot_released_and_retry_possible_after_success(self):
        self._retry_outcome("success", returns=[dict(q) for q in AI_QUESTIONS])

    def test_slot_released_and_retry_possible_after_empty_result(self):
        self._retry_outcome("empty", returns=[])

    def test_slot_released_and_retry_possible_after_exception(self):
        self._retry_outcome("exception", raises=RuntimeError("Gemini 429"))

    def _retry_outcome(self, label, returns=None, raises=None):
        async def fake(uid, topic, count, lang, diff, exam, status_msg, bilingual_lang2=None,
                       chunk_size=25):
            self.gen_calls.append({"attempt": len(self.gen_calls) + 1})
            await asyncio.sleep(0.01)
            if raises:
                raise raises
            return returns

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", fake):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.1)
                self.assertNotIn(self.uid, state.AI_QUIZ_INFLIGHT, f"{label}: slot must be freed")
                first = len(self.gen_calls)
                # one tap on Retry must be enough -- no /aiquiz, no re-picking
                q2, u2 = self.tap("retry")
                await ai_quiz.aiquiz_callback(u2, self.ctx)
                await asyncio.sleep(0.1)
                return first
        first = _run(go())
        self.assertEqual(first, 1, f"{label}: first attempt ran")
        self.assertEqual(len(self.gen_calls), 2, f"{label}: retry must run a second generation")

    def test_retry_keeps_every_wizard_choice(self):
        async def fake_empty(*a, **k):
            self.gen_calls.append({"count": a[2], "lang": a[3], "diff": a[4], "exam": a[5]})
            await asyncio.sleep(0.01)
            return []

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", fake_empty):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.1)
                q, upd = self.tap("retry")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.1)
        _run(go())
        self.assertEqual(len(self.gen_calls), 2)
        self.assertEqual(self.gen_calls[0], self.gen_calls[1], "retry must reuse the same settings")
        self.assertEqual(self.gen_calls[1]["exam"], "civil")
        self.assertEqual(self.gen_calls[1]["count"], 10)

    def test_failure_offers_retry_and_cancel_buttons(self):
        async def fake_empty(*a, **k):
            await asyncio.sleep(0.01)
            return []

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", fake_empty):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.1)
        _run(go())
        data = [b.callback_data for row in self.msg.last_markup.inline_keyboard for b in row]
        self.assertEqual(data, [f"aiq_retry_{self.uid}", f"aiq_cancel_{self.uid}"])
        self.assertIn(self.uid, state.AI_QUIZ_SESSIONS, "choices must survive a failure")

    def test_empty_result_clears_the_cached_provider(self):
        async def fake_empty(*a, **k):
            await asyncio.sleep(0.01)
            return []

        async def go():
            state.last_working_ai[self.uid] = {"provider": "gemini", "key_id": 1, "api_key": "k"}
            with patch.object(ai_quiz, "generate_in_chunks", fake_empty):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.1)
        _run(go())
        self.assertNotIn(self.uid, state.last_working_ai)

    def test_cancel_drops_the_session_and_discards_the_running_result(self):
        async def slow(*a, **k):
            await asyncio.sleep(0.05)
            return [dict(q) for q in AI_QUESTIONS]

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", slow):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.01)
                self.assertIn(self.uid, state.AI_QUIZ_INFLIGHT)
                q2, u2 = self.tap("cancel")
                await ai_quiz.aiquiz_callback(u2, self.ctx)
                self.assertNotIn(self.uid, state.AI_QUIZ_SESSIONS)
                self.assertNotIn(self.uid, state.AI_QUIZ_INFLIGHT)
                await asyncio.sleep(0.15)
        _run(go())
        self.assertNotIn(self.uid, state.AI_QUIZ_SESSIONS,
                         "a finished generation must not resurrect a cancelled session")
        self.assertIn("cancelled", self.msg.text.lower())

    def test_new_aiquiz_during_generation_is_not_clobbered(self):
        async def slow(*a, **k):
            await asyncio.sleep(0.05)
            return [dict(q) for q in AI_QUESTIONS]

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", slow):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.01)
                # the user gives up and starts a different topic
                self.ctx.args = ["Modern", "History"]
                await ai_quiz.aiquiz_command(FakeUpdate(FakeMessage(self.chat, self.uid)), self.ctx)
                new_sess = state.AI_QUIZ_SESSIONS[self.uid]
                self.assertEqual(new_sess["topic"], "Modern History")
                await asyncio.sleep(0.15)
                return new_sess
        new_sess = _run(go())
        self.assertIs(state.AI_QUIZ_SESSIONS[self.uid], new_sess)
        self.assertEqual(new_sess["topic"], "Modern History")
        self.assertNotIn("questions", new_sess)

    def test_second_generation_blocked_until_the_first_finishes(self):
        async def slow(*a, **k):
            self.gen_calls.append({"topic": a[1]})
            await asyncio.sleep(0.08)
            return [dict(q) for q in AI_QUESTIONS]

        async def go():
            with patch.object(ai_quiz, "generate_in_chunks", slow):
                await self.walk_to_exam()
                q, upd = self.tap("exam", "civil")
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                # a brand new /aiquiz while the first is still running
                self.ctx.args = ["Geography"]
                await ai_quiz.aiquiz_command(FakeUpdate(FakeMessage(self.chat, self.uid)), self.ctx)
                state.AI_QUIZ_SESSIONS[self.uid].update(count=5, lang="en", bilingual=False,
                                                        lang2=None, diff="hard", exam="ssc")
                q2, u2 = self.tap("exam", "ssc")
                await ai_quiz.aiquiz_callback(u2, self.ctx)
                self.assertTrue(q2.alerts, "must be told to wait")
                self.assertEqual(len(self.gen_calls), 0)  # slow() has not returned yet
                await asyncio.sleep(0.2)
                q3, u3 = self.tap("exam", "ssc")
                await ai_quiz.aiquiz_callback(u3, self.ctx)
                await asyncio.sleep(0.2)
        _run(go())
        self.assertEqual(len(self.gen_calls), 2, "allowed again once the first run finished")


class AiQuizLaunchCases(AiQuizBase):
    def test_ex_step_launches_into_the_originating_group(self):
        async def go():
            sess = await self.walk_to_exam()
            q, upd = self.tap("exam", "civil")
            await ai_quiz.aiquiz_callback(upd, self.ctx)
            await asyncio.sleep(0.1)
            for step, value in [("timer", "20"), ("neg", "0.25"), ("cm", "2"),
                                ("shuffle", "b"), ("shufflecount", "2"), ("ex", "yes")]:
                q, upd = self.tap(step, value)
                await ai_quiz.aiquiz_callback(upd, self.ctx)
                await asyncio.sleep(0.01)
            return sess
        _run(go())
        self.assertNotIn(self.uid, state.AI_QUIZ_SESSIONS)
        self.assertIn("AI Quiz Ready", self.msg.text)
        self.assertFalse(self.msg.last_markup.inline_keyboard,
                         "the wizard keyboard must be cleared before the quiz starts")

    def test_launch_refuses_when_the_chat_is_busy(self):
        async def go():
            await state.session_mgr.create(self.chat, {"quiz_id": "LIVE"})
            with patch.object(quiz_play, "start_private_quiz", AsyncMock()):
                await ai_quiz._launch_ai_quiz(
                    self.uid, self.ctx, AI_QUESTIONS, "Topic", 20, 0.25, 1,
                    chat_id=self.chat, chat_type="supergroup")
        _run(go())
        self.assertTrue(any("already running" in t for t in self.bot.texts()))

    def test_group_launch_creates_a_session(self):
        async def go():
            spawned = []
            with patch.object(quiz_play, "run_group_quiz", AsyncMock()) as rgq, \
                 patch.object(state.tasks, "spawn", lambda coro, name="": spawned.append((coro, name))):
                await ai_quiz._launch_ai_quiz(
                    self.uid, self.ctx, AI_QUESTIONS, "Topic <x> & y", 20, 0.25, 1,
                    chat_id=self.chat, chat_type="supergroup")
            for coro, _n in spawned:
                coro.close()
            return rgq
        _run(go())
        sess = state.session_mgr.get(self.chat)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["current_index"], 0)
        self.assertEqual(sess["quiz_data"]["quiz_name"], "AI: Topic <x> & y")


class AiQuizRegistrationCases(unittest.TestCase):
    def test_register_adds_exactly_the_aiquiz_command_and_callback(self):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler

        app = Application.builder().token("123456789:AAExampleTokenExampleTokenExampleTokenExa").build()
        ai_quiz.register(app)
        handlers = app.handlers[0]
        self.assertEqual(len(handlers), 2)
        cmd = [h for h in handlers if isinstance(h, CommandHandler)]
        cb = [h for h in handlers if isinstance(h, CallbackQueryHandler)]
        self.assertEqual(len(cmd), 1)
        self.assertEqual(cmd[0].commands, frozenset({"aiquiz"}))
        self.assertIs(cmd[0].callback, ai_quiz.aiquiz_command)
        self.assertEqual(cb[0].pattern.pattern, "^aiq_")
        self.assertIs(cb[0].callback, ai_quiz.aiquiz_callback)


# ══════════════════════════════ /schedule ════════════════════════════════════


class ScheduleBase(unittest.TestCase):
    def setUp(self):
        state.session_mgr.sessions.clear()
        self.db = FakeDb()
        self.col = self.db.collection("scheduled_quizzes")
        self.quiz = make_quiz()
        FakeQuizRepo.quizzes = {self.quiz["qid"]: self.quiz}
        self.bot = FakeBot()
        self.ctx = FakeCtx(self.bot)
        self.chat = _chat()
        self.admin = _uid()
        self.launched: list[dict] = []
        self.ist = scheduling.IST
        self._patches = [
            patch.object(scheduling, "get_db", lambda: self.db),
            patch.object(scheduling, "QuizRepository", FakeQuizRepo),
            patch.object(setup_wizard, "_launch_quiz_from_settings", self._record_launch),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        self.addCleanup(self._shutdown)
        self.scheduler = None

    def _shutdown(self):
        if self.scheduler is not None and self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass  # its loop is already gone -- nothing left to stop
        scheduling.schedule_mgr = None

    async def _record_launch(self, chat_id, ctx, ps):
        self.launched.append({"chat_id": chat_id, "ps": ps, "ctx": ctx})

    def new_manager(self, bot=_UNSET) -> scheduling.ScheduledQuizManager:
        """APScheduler must be started inside a running loop, so tests call
        `self.start_scheduler()` from their coroutine instead of here."""
        self.scheduler = AsyncIOScheduler()
        mgr = scheduling.init_schedule_manager(self.scheduler, self.bot if bot is _UNSET else bot)
        mgr.col = self.col
        return mgr

    def start_scheduler(self):
        if not self.scheduler.running:
            self.scheduler.start()

    def _run(self, coro):
        """Run `coro`, stopping the scheduler before its event loop closes.

        APScheduler's shutdown() pokes the loop it was started on, so tearing
        it down in tearDown (after the loop is gone) raises.
        """
        async def go():
            try:
                return await coro
            finally:
                if self.scheduler is not None and self.scheduler.running:
                    self.scheduler.shutdown(wait=False)
        return _run(go())

    def message(self, chat_type="supergroup", args=None, chat_id=None, user_id=None):
        self.ctx.args = list(args or [])
        return FakeUpdate(FakeMessage(chat_id or self.chat, user_id or self.admin, chat_type))


class ScheduleCommandCases(ScheduleBase):
    def test_private_chat_is_refused(self):
        for fn in (scheduling.schedule_command, scheduling.viewschedule_command,
                   scheduling.cancelschedule_command):
            bot = FakeBot()
            self.ctx.bot = bot
            mgr = self.new_manager()
            self._run(fn(self.message(chat_type="private", args=["ABC123", "12:00"]), self.ctx))
            self.assertIn("Groups only.", bot.texts()[0])
            self.assertEqual(len(mgr.jobs), 0)

    def test_only_admins_and_creators_may_schedule(self):
        for status, allowed in [("administrator", True), ("creator", True), ("member", False),
                                ("restricted", False), ("left", False), ("kicked", False)]:
            with self.subTest(status=status):
                bot = FakeBot(admin_status=status)
                self.ctx.bot = bot
                mgr = self.new_manager()
                self._run(scheduling.schedule_command(
                    self.message(args=["ABC123", "23:59"]), self.ctx))
                if allowed:
                    self.assertEqual(len(mgr.jobs), 1)
                else:
                    self.assertEqual(len(mgr.jobs), 0)
                    self.assertIn("\U0001F6AB Admin only.", bot.texts())

    def test_permission_check_failure_is_not_silent(self):
        bot = FakeBot(member_raises=BadRequest("Chat not found"))
        self.ctx.bot = bot
        mgr = self.new_manager()
        self._run(scheduling.schedule_command(self.message(args=["ABC123", "23:59"]), self.ctx))
        self.assertEqual(len(bot.sent), 1, "the admin must get an answer, not silence")
        self.assertIn("Could not verify your admin rights", bot.sent[0].text)
        self.assertEqual(len(mgr.jobs), 0)

    def test_cancelschedule_permission_failure_is_not_silent(self):
        bot = FakeBot(member_raises=BadRequest("Flood control exceeded"))
        self.ctx.bot = bot
        self.new_manager()
        self._run(scheduling.cancelschedule_command(self.message(args=["ABC123"]), self.ctx))
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("Could not verify your admin rights", bot.sent[0].text)

    def test_usage_message_when_args_are_missing(self):
        mgr = self.new_manager()
        self._run(scheduling.schedule_command(self.message(args=["ABC123"]), self.ctx))
        self.assertIn("/schedule QUIZ_ID HH:MM", self.bot.texts()[0])
        self.assertEqual(len(mgr.jobs), 0)

    def test_invalid_times_are_rejected_safely(self):
        for bad in ("25:99", "abc", "12:30:45", "", "-1:00", "12", ":", "99:00", "12:60",
                    "1.5:00", "12:30 PM", "None", "١٢:٩٩"):
            with self.subTest(bad=bad):
                bot = FakeBot()
                self.ctx.bot = bot
                mgr = self.new_manager()
                self._run(scheduling.schedule_command(self.message(args=["ABC123", bad]), self.ctx))
                self.assertEqual(len(mgr.jobs), 0, f"{bad!r} must not schedule anything")
                self.assertEqual(len(bot.sent), 1, f"{bad!r} must be answered")
                self.assertIn("Invalid time", bot.sent[0].text)

    def test_valid_times_are_accepted(self):
        # int() also accepts non-ASCII digits, so an admin typing Eastern
        # Arabic numerals gets exactly the time they asked for rather than a
        # confusing rejection.
        for good in ("00:00", "09:05", "23:59", "14:30", "١٢:٣٠"):
            with self.subTest(good=good):
                self.ctx.bot = FakeBot()
                mgr = self.new_manager()
                self._run(scheduling.schedule_command(self.message(args=["ABC123", good]), self.ctx))
                self.assertEqual(len(mgr.jobs), 1)

    def test_unknown_quiz_is_rejected(self):
        mgr = self.new_manager()
        self._run(scheduling.schedule_command(self.message(args=["NOPE", "23:59"]), self.ctx))
        self.assertIn("Quiz NOPE not found.", self.bot.texts()[0])
        self.assertEqual(len(mgr.jobs), 0)

    def test_quiz_with_no_questions_is_rejected_up_front(self):
        FakeQuizRepo.quizzes["EMPTY"] = make_quiz("EMPTY", questions=[])
        mgr = self.new_manager()
        self._run(scheduling.schedule_command(self.message(args=["EMPTY", "23:59"]), self.ctx))
        self.assertIn("no questions", self.bot.texts()[0])
        self.assertEqual(len(mgr.jobs), 0)

    def test_premium_gate_still_applies(self):
        mgr = self.new_manager()
        with patch.object(scheduling, "is_premium_user", AsyncMock(return_value=False)):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "23:59"]), self.ctx))
        self.assertIn("Premium required", self.bot.texts()[0])
        self.assertEqual(len(mgr.jobs), 0)

    def test_scheduler_not_ready_is_reported(self):
        scheduling.schedule_mgr = None
        self._run(scheduling.schedule_command(self.message(args=["ABC123", "23:59"]), self.ctx))
        self.assertIn("Scheduler is not ready", self.bot.texts()[0])

    def test_past_time_rolls_to_tomorrow_and_says_so(self):
        mgr = self.new_manager()
        now = self.ist.localize(datetime(2026, 3, 15, 20, 0))
        with frozen_clock(now):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "09:30"]), self.ctx))
        self.assertEqual(len(mgr.jobs), 1)
        when = list(mgr.jobs.values())[0]["scheduled_time"]
        self.assertEqual(when.strftime("%H:%M"), "09:30")
        self.assertEqual(when.date(), (now + timedelta(days=1)).date())
        self.assertGreater(when, now)
        self.assertIn("already passed today", self.bot.texts()[0])
        self.assertIn("tomorrow", self.bot.texts()[0])

    def test_future_time_is_kept_for_today(self):
        mgr = self.new_manager()
        now = self.ist.localize(datetime(2026, 3, 15, 10, 0))
        with frozen_clock(now):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "14:30"]), self.ctx))
        self.assertEqual(len(mgr.jobs), 1)
        when = list(mgr.jobs.values())[0]["scheduled_time"]
        self.assertEqual(when.strftime("%H:%M"), "14:30")
        self.assertEqual(when.date(), now.date(), "a time still ahead today must not roll over")
        self.assertNotIn("already passed today", self.bot.texts()[0])

    def test_the_exact_current_minute_rolls_to_tomorrow(self):
        """`sched_time <= now` -- the boundary itself is already too late."""
        mgr = self.new_manager()
        now = self.ist.localize(datetime(2026, 3, 15, 10, 0))
        with frozen_clock(now):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "10:00"]), self.ctx))
        self.assertEqual(len(mgr.jobs), 1)
        when = list(mgr.jobs.values())[0]["scheduled_time"]
        self.assertEqual(when.date(), (now + timedelta(days=1)).date())
        self.assertIn("already passed today", self.bot.texts()[0])

    def test_late_evening_target_does_not_silently_land_yesterday(self):
        """The real-world shape of the flake this clock freezing removed."""
        mgr = self.new_manager()
        now = self.ist.localize(datetime(2026, 3, 15, 22, 41))
        with frozen_clock(now):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "00:11"]), self.ctx))
        when = list(mgr.jobs.values())[0]["scheduled_time"]
        self.assertGreater(when, now, "a schedule must never be armed in the past")
        self.assertEqual(when.date(), (now + timedelta(days=1)).date())

    def test_timezone_is_ist_and_matches_the_requested_hhmm(self):
        mgr = self.new_manager()
        now = self.ist.localize(datetime(2026, 3, 15, 10, 0))
        with frozen_clock(now):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "14:30"]), self.ctx))
        when = list(mgr.jobs.values())[0]["scheduled_time"]
        self.assertEqual(str(when.tzinfo), "Asia/Kolkata")
        self.assertEqual(when.strftime("%H:%M"), "14:30")
        self.assertEqual((when.second, when.microsecond), (0, 0))
        self.assertEqual(when.utcoffset(), timedelta(hours=5, minutes=30))

    def test_confirmation_escapes_the_quiz_name(self):
        self.new_manager()
        ahead = (datetime.now(self.ist) + timedelta(minutes=90)).strftime("%H:%M")
        self._run(scheduling.schedule_command(self.message(args=["ABC123", ahead]), self.ctx))
        text = self.bot.texts()[0]
        self.assertIn("&lt;Mock&gt; &amp; Current", text)
        self.assertNotIn("<Mock>", text)
        self.assertIn("Scheduled!", text)

    def test_state_is_persisted_to_mongodb(self):
        mgr = self.new_manager()
        ahead = (datetime.now(self.ist) + timedelta(minutes=90)).strftime("%H:%M")
        self._run(scheduling.schedule_command(self.message(args=["ABC123", ahead]), self.ctx))
        self.assertEqual(len(self.col.docs), 1)
        row = list(self.col.docs.values())[0]
        self.assertEqual(row["chat_id"], self.chat)
        self.assertEqual(row["quiz_id"], "ABC123")
        self.assertEqual(row["created_by"], self.admin)
        self.assertEqual(row["job_id"], list(mgr.jobs)[0])
        # round-trips back into a datetime
        parsed = datetime.fromisoformat(row["scheduled_time"])
        self.assertEqual(parsed.strftime("%H:%M"), ahead)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=5, minutes=30))

    def test_storage_failure_is_reported_not_swallowed(self):
        self.new_manager()
        self.col.fail_writes = True
        ahead = (datetime.now(self.ist) + timedelta(minutes=90)).strftime("%H:%M")
        self._run(scheduling.schedule_command(self.message(args=["ABC123", ahead]), self.ctx))
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("Could not save the schedule", self.bot.sent[0].text)
        self.assertNotIn("Scheduled!", " ".join(self.bot.texts()))

    def test_unexpected_failure_is_reported_not_swallowed(self):
        self.new_manager()
        with patch.object(scheduling, "QuizRepository", side_effect=RuntimeError("db down")):
            self._run(scheduling.schedule_command(self.message(args=["ABC123", "23:59"]), self.ctx))
        self.assertIn("Could not schedule that quiz", self.bot.texts()[0])

    def test_same_quiz_same_time_is_deduplicated(self):
        mgr = self.new_manager()
        ahead = (datetime.now(self.ist) + timedelta(minutes=90)).strftime("%H:%M")
        self._run(scheduling.schedule_command(self.message(args=["ABC123", ahead]), self.ctx))
        self._run(scheduling.schedule_command(self.message(args=["ABC123", ahead]), self.ctx))
        self.assertEqual(len(mgr.jobs), 1)
        self.assertEqual(len(self.col.docs), 1)

    def test_same_quiz_second_time_is_accepted_and_announced(self):
        mgr = self.new_manager()
        t1 = (datetime.now(self.ist) + timedelta(minutes=90)).strftime("%H:%M")
        t2 = (datetime.now(self.ist) + timedelta(minutes=150)).strftime("%H:%M")
        self._run(scheduling.schedule_command(self.message(args=["ABC123", t1]), self.ctx))
        self.bot.sent.clear()
        self._run(scheduling.schedule_command(self.message(args=["ABC123", t2]), self.ctx))
        self.assertEqual(len(mgr.jobs), 2)
        self.assertIn("also scheduled for", self.bot.texts()[0])


class ScheduleFiringCases(ScheduleBase):
    def _schedule_and_fire(self, mgr, delay=0.4, chat_id=None):
        chat_id = chat_id or self.chat
        when = datetime.now(self.ist) + timedelta(seconds=delay)

        async def go():
            self.start_scheduler()
            job_id = await mgr.add(chat_id, "ABC123", when, self.admin, self.ctx)
            await asyncio.sleep(delay + 1.2)
            return job_id
        return self._run(go())

    def test_job_actually_fires_at_the_requested_time(self):
        mgr = self.new_manager()
        started = datetime.now(self.ist)
        self._schedule_and_fire(mgr, delay=0.5)
        self.assertEqual(len(self.launched), 1, "the scheduled quiz must launch")
        self.assertAlmostEqual(
            (datetime.now(self.ist) - started).total_seconds(), 1.7, delta=1.2)

    def test_quiz_is_posted_into_the_correct_group(self):
        mgr = self.new_manager()
        other = _chat()
        self._schedule_and_fire(mgr, chat_id=other)
        self.assertEqual(self.launched[0]["chat_id"], other)
        self.assertTrue(any(s.chat_id == other and "Scheduled Quiz Starting" in s.text
                            for s in self.bot.sent))

    def test_launch_uses_the_quiz_documents_own_settings(self):
        mgr = self.new_manager()
        self._schedule_and_fire(mgr)
        ps = self.launched[0]["ps"]
        self.assertEqual(ps["correct_mark"], 4.0)
        self.assertEqual(ps["neg_mark"], 0.25)
        self.assertTrue(ps["show_explanation"])
        self.assertTrue(ps["shuffle_o"])
        self.assertFalse(ps["shuffle_q"])
        self.assertIsNone(ps["timer_override"])
        self.assertEqual(ps["quiz"]["timer"], 45)
        self.assertEqual(len(ps["quiz"]["questions"]), 2)
        self.assertEqual(ps["initiator_id"], self.admin)
        self.assertEqual(ps["chat_type"], "group")

    def test_launch_matches_the_fixed_settings_start_path(self):
        """Same quiz, same values /start would apply -- no rescored run."""
        mgr = self.new_manager()
        self._schedule_and_fire(mgr)
        ps = self.launched[0]["ps"]
        quiz = self.quiz
        self.assertEqual(ps["correct_mark"], float(quiz["correct_marks"]))
        self.assertEqual(ps["neg_mark"], float(quiz["negative_marks"]))
        self.assertEqual(ps["show_explanation"], bool(quiz["show_explanation"]))
        self.assertEqual(ps["shuffle_q"], bool(quiz["shuffle_questions"]))
        self.assertEqual(ps["shuffle_o"], bool(quiz["shuffle_options"]))

    def test_defaults_when_the_quiz_document_is_sparse(self):
        FakeQuizRepo.quizzes["SPARSE"] = {"qid": "SPARSE", "quiz_name": "x", "questions": [{}]}
        mgr = self.new_manager()

        async def go():
            self.start_scheduler()
            await mgr.add(self.chat, "SPARSE", datetime.now(self.ist) + timedelta(seconds=0.4),
                          self.admin, self.ctx)
            await asyncio.sleep(1.6)
        self._run(go())
        ps = self.launched[0]["ps"]
        self.assertEqual(ps["correct_mark"], 1.0)
        self.assertEqual(ps["neg_mark"], 0.0)
        self.assertFalse(ps["show_explanation"])

    def test_bad_stored_numbers_do_not_break_the_launch(self):
        FakeQuizRepo.quizzes["BAD"] = make_quiz("BAD", correct_marks=None, negative_marks="x")
        mgr = self.new_manager()

        async def go():
            self.start_scheduler()
            await mgr.add(self.chat, "BAD", datetime.now(self.ist) + timedelta(seconds=0.4),
                          self.admin, self.ctx)
            await asyncio.sleep(1.6)
        self._run(go())
        ps = self.launched[0]["ps"]
        self.assertEqual(ps["correct_mark"], 1.0)
        self.assertEqual(ps["neg_mark"], 0.0)

    def test_content_protection_is_on_for_a_group(self):
        mgr = self.new_manager()
        self._schedule_and_fire(mgr)
        self.assertTrue(self.launched[0]["ps"]["protect"])

    def test_content_protection_off_only_for_the_creators_own_chat(self):
        mgr = self.new_manager()
        self._schedule_and_fire(mgr, chat_id=self.quiz["creator_id"])
        self.assertFalse(self.launched[0]["ps"]["protect"])

    def test_refuses_to_clobber_a_running_quiz(self):
        mgr = self.new_manager()

        async def go():
            self.start_scheduler()
            await state.session_mgr.create(self.chat, {
                "quiz_id": "LIVE", "quiz_data": {"creator_id": 1}, "polls": {"p1": {}},
                "participants": {"7": {"name": "Ashok", "answers": {}}},
            })
            await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(seconds=0.4),
                          self.admin, self.ctx)
            await asyncio.sleep(1.6)
        self._run(go())
        self.assertEqual(len(self.launched), 0, "must not launch over a live quiz")
        sess = state.session_mgr.get(self.chat)
        self.assertEqual(sess["quiz_id"], "LIVE", "the live session must survive")
        self.assertIn("7", sess["participants"])
        self.assertIn("p1", sess["polls"])
        self.assertTrue(any("did not start" in t and "another quiz is still running" in t
                            for t in self.bot.texts()), self.bot.texts())
        self.assertTrue(any("/start ABC123" in t for t in self.bot.texts()))

    def test_missing_quiz_at_fire_time_is_reported(self):
        mgr = self.new_manager()

        async def go():
            self.start_scheduler()
            await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(seconds=0.4),
                          self.admin, self.ctx)
            FakeQuizRepo.quizzes.clear()
            await asyncio.sleep(1.6)
        self._run(go())
        self.assertEqual(len(self.launched), 0)
        self.assertTrue(any("not found" in t for t in self.bot.texts()))
        FakeQuizRepo.quizzes["ABC123"] = self.quiz

    def test_launch_error_is_surfaced_and_nothing_is_left_behind(self):
        mgr = self.new_manager()

        async def boom(chat_id, ctx, ps):
            raise RuntimeError("Telegram said no")

        async def go():
            self.start_scheduler()
            with patch.object(setup_wizard, "_launch_quiz_from_settings", boom):
                await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(seconds=0.4),
                              self.admin, self.ctx)
                await asyncio.sleep(1.6)
        self._run(go())
        self.assertTrue(any("Error starting scheduled quiz" in t for t in self.bot.texts()))
        self.assertEqual(len(mgr.jobs), 0)
        self.assertEqual(len(self.col.docs), 0)

    def test_job_and_mongo_row_are_purged_after_firing(self):
        mgr = self.new_manager()
        job_id = self._schedule_and_fire(mgr)
        self.assertEqual(len(self.col.docs), 0)
        self.assertNotIn(job_id, mgr.jobs)
        self.assertIsNone(self.scheduler.get_job(job_id))

    def test_job_carries_a_bot_context_not_the_command_context(self):
        mgr = self.new_manager()
        self._schedule_and_fire(mgr)
        ctx = self.launched[0]["ctx"]
        self.assertIsInstance(ctx, scheduling._BotContext)
        self.assertIs(ctx.bot, self.bot)

    def test_real_launch_path_runs_end_to_end(self):
        """Feed the settings a scheduled run produces into the REAL launcher.

        Proves the fake update/context built by `_run` are good enough for
        `_launch_quiz_from_settings` to post a start card, create the session
        poll answers land in, and spawn the group quiz loop.
        """
        mgr = self.new_manager()
        self._schedule_and_fire(mgr)
        ps = self.launched[0]["ps"]
        ctx = self.launched[0]["ctx"]

        async def real_launch():
            with patch.object(setup_wizard, "_save_quiz_prefs", AsyncMock()), \
                 patch.object(quiz_play, "run_group_quiz", AsyncMock()) as rgq, \
                 patch.object(setup_wizard, "_send_start_card", AsyncMock()) as card:
                await _REAL_LAUNCH(self.chat, ctx, ps)
                return rgq, card
        rgq, card = self._run(real_launch())

        self.assertEqual(card.await_count, 1, "the start card must be posted")
        self.assertEqual(rgq.await_count, 1, "the group quiz loop must be spawned")
        sess = state.session_mgr.get(self.chat)
        self.assertIsNotNone(sess, "a session must exist for poll answers to land in")
        self.assertEqual(sess["quiz_id"], "ABC123")
        self.assertFalse(sess["is_private"])
        self.assertEqual(sess["participants"], {})
        self.assertEqual(sess["quiz_data"]["correct_mark"], 4)
        self.assertTrue(sess["quiz_data"]["show_explanation"])
        self.assertTrue(sess["quiz_data"]["shuffle_options"])
        # the fake update must expose everything the launcher reads from it
        self.assertIsNone(ps["update"].message.message_thread_id)
        self.assertEqual(sess["message_thread_id"], None)


class ScheduleViewCancelCases(ScheduleBase):
    def test_viewschedule_lists_pending_jobs(self):
        mgr = self.new_manager()

        async def go():
            await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(minutes=10),
                          self.admin, self.ctx)
            await scheduling.viewschedule_command(self.message(args=[]), self.ctx)
        self._run(go())
        text = self.bot.texts()[-1]
        self.assertIn("Scheduled Quizzes", text)
        self.assertIn("<code>ABC123</code>", text)
        self.assertRegex(text, r"in 0h (9|10)m")

    def test_viewschedule_escapes_the_quiz_id(self):
        mgr = self.new_manager()

        async def go():
            await mgr.add(self.chat, "ZZ<9>&x", datetime.now(self.ist) + timedelta(minutes=10),
                          self.admin, self.ctx)
            await scheduling.viewschedule_command(self.message(args=[]), self.ctx)
        self._run(go())
        text = self.bot.texts()[-1]
        self.assertIn("ZZ&lt;9&gt;&amp;x", text)
        self.assertNotIn("ZZ<9>", text)

    def test_viewschedule_when_empty(self):
        self.new_manager()
        self._run(scheduling.viewschedule_command(self.message(args=[]), self.ctx))
        self.assertIn("No scheduled quizzes.", self.bot.texts()[0])

    def test_viewschedule_only_shows_this_chat(self):
        mgr = self.new_manager()

        async def go():
            await mgr.add(_chat(), "ABC123", datetime.now(self.ist) + timedelta(minutes=10),
                          self.admin, self.ctx)
            await scheduling.viewschedule_command(self.message(args=[]), self.ctx)
        self._run(go())
        self.assertIn("No scheduled quizzes.", self.bot.texts()[0])

    def test_cancelschedule_removes_the_job_and_the_row(self):
        mgr = self.new_manager()

        async def go():
            await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(minutes=10),
                          self.admin, self.ctx)
            job_id = list(mgr.jobs)[0]
            await scheduling.cancelschedule_command(self.message(args=["ABC123"]), self.ctx)
            return job_id
        job_id = self._run(go())
        self.assertEqual(len(mgr.jobs), 0)
        self.assertEqual(len(self.col.docs), 0)
        self.assertIsNone(self.scheduler.get_job(job_id))
        self.assertIn("Schedule cancelled for ABC123.", self.bot.texts()[-1])

    def test_cancelschedule_removes_every_run_of_that_quiz(self):
        mgr = self.new_manager()

        async def go():
            t = (datetime.now(self.ist) + timedelta(minutes=30)).replace(second=0, microsecond=0)
            await mgr.add(self.chat, "ABC123", t, self.admin, self.ctx)
            await mgr.add(self.chat, "ABC123", t + timedelta(minutes=5), self.admin, self.ctx)
            await mgr.add(self.chat, "OTHER", t + timedelta(minutes=9), self.admin, self.ctx)
            self.assertEqual(len(mgr.jobs), 3)
            self.bot.sent.clear()
            await scheduling.cancelschedule_command(self.message(args=["ABC123"]), self.ctx)
        self._run(go())
        remaining = [j["quiz_id"] for j in mgr.jobs.values()]
        self.assertEqual(remaining, ["OTHER"], "both runs of ABC123 must be gone")
        self.assertEqual(self.col.job_ids(), [j for j in self.col.job_ids() if "OTHER" in j])
        self.assertIn("Cancelled all 2 schedules for ABC123.", self.bot.texts()[-1])

    def test_cancelschedule_unknown_quiz(self):
        self.new_manager()
        self._run(scheduling.cancelschedule_command(self.message(args=["NOPE"]), self.ctx))
        self.assertIn("No schedule for NOPE.", self.bot.texts()[-1])

    def test_cancelschedule_without_args(self):
        self.new_manager()
        self._run(scheduling.cancelschedule_command(self.message(args=[]), self.ctx))
        self.assertIn("/cancelschedule QUIZ_ID", self.bot.texts()[-1])

    def test_cancelschedule_does_not_touch_other_groups(self):
        mgr = self.new_manager()
        other = _chat()

        async def go():
            await mgr.add(other, "ABC123", datetime.now(self.ist) + timedelta(minutes=10),
                          self.admin, self.ctx)
            await scheduling.cancelschedule_command(self.message(args=["ABC123"]), self.ctx)
        self._run(go())
        self.assertEqual(len(mgr.jobs), 1, "another group's schedule must survive")

    def test_cancelled_job_never_fires(self):
        mgr = self.new_manager()

        async def go():
            self.start_scheduler()
            await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(seconds=0.6),
                          self.admin, self.ctx)
            await scheduling.cancelschedule_command(self.message(args=["ABC123"]), self.ctx)
            await asyncio.sleep(1.8)
        self._run(go())
        self.assertEqual(len(self.launched), 0)


class ScheduleRestartRecoveryCases(ScheduleBase):
    def _row(self, job_id, when, chat_id=None, qid="ABC123"):
        return {"job_id": job_id, "chat_id": chat_id or self.chat, "quiz_id": qid,
                "scheduled_time": when.isoformat(), "created_by": self.admin,
                "created_at": when.isoformat()}

    def test_future_rows_are_rearmed(self):
        mgr = self.new_manager(bot=self.bot)
        future = datetime.now(self.ist) + timedelta(minutes=30)
        self.col.docs["1"] = self._row("job_future", future)

        async def go():
            await mgr.restore()
            return self.scheduler.get_job("job_future")
        job = self._run(go())
        self.assertIsNotNone(job)
        self.assertEqual(len(mgr.jobs), 1)
        self.assertEqual(mgr.jobs["job_future"]["quiz_id"], "ABC123")
        self.assertEqual(mgr.jobs["job_future"]["created_by"], self.admin)
        self.assertEqual(job.args[0], self.chat)
        self.assertIsInstance(job.args[2], scheduling._BotContext)

    def test_rearmed_job_actually_fires_after_a_restart(self):
        mgr = self.new_manager(bot=self.bot)
        self.col.docs["1"] = self._row("job_future", datetime.now(self.ist) + timedelta(seconds=0.5))

        async def go():
            self.start_scheduler()
            await mgr.restore()
            await asyncio.sleep(1.8)
        self._run(go())
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0]["chat_id"], self.chat)

    def test_schedules_missed_while_down_are_reported_and_purged(self):
        mgr = self.new_manager(bot=self.bot)
        missed = datetime.now(self.ist) - timedelta(hours=3)
        self.col.docs["1"] = self._row("job_missed", missed)

        async def go():
            await mgr.restore()
        self._run(go())
        self.assertEqual(len(mgr.jobs), 0)
        self.assertEqual(len(self.col.docs), 0, "a row that can never fire must not linger")
        self.assertIsNone(self.scheduler.get_job("job_missed"))
        self.assertEqual(len(self.bot.sent), 1, "the group must be told, not left guessing")
        text = self.bot.sent[0].text
        self.assertIn("did not run", text)
        self.assertIn("ABC123", text)
        self.assertIn("/schedule ABC123 HH:MM", text)

    def test_missed_and_future_rows_are_handled_together(self):
        mgr = self.new_manager(bot=self.bot)
        now = datetime.now(self.ist)
        self.col.docs["1"] = self._row("old", now - timedelta(hours=1))
        self.col.docs["2"] = self._row("new", now + timedelta(hours=1))
        self.col.docs["3"] = self._row("old2", now - timedelta(days=2), chat_id=_chat())

        self._run(mgr.restore())
        self.assertEqual(sorted(mgr.jobs), ["new"])
        self.assertEqual(self.col.job_ids(), ["new"])
        self.assertEqual(len(self.bot.sent), 2)

    def test_notifications_are_capped_but_rows_are_still_purged(self):
        mgr = self.new_manager(bot=self.bot)
        now = datetime.now(self.ist)
        chat = _chat()
        for i in range(scheduling.MAX_MISSED_NOTICES + 5):
            self.col.docs[str(i)] = self._row(f"old{i}", now - timedelta(minutes=i + 1), chat_id=chat)
        self._run(mgr.restore())
        self.assertEqual(len(self.col.docs), 0, "every dead row must be purged")
        self.assertEqual(len(self.bot.sent), scheduling.MAX_MISSED_NOTICES)

    def test_nothing_is_deleted_while_the_scan_cursor_is_open(self):
        """Mutating a collection mid-scan can make MongoDB skip documents."""
        mgr = self.new_manager(bot=self.bot)
        now = datetime.now(self.ist)
        for i in range(6):
            self.col.docs[str(i)] = self._row(f"missed{i}", now - timedelta(minutes=i + 1))
        self.col.docs["live"] = self._row("still_pending", now + timedelta(hours=1))
        self.col.events.clear()

        self._run(mgr.restore())

        self.assertIn("scan", self.col.events)
        self.assertIn("delete", self.col.events)
        self.assertGreater(self.col.events.index("delete"),
                           max(i for i, e in enumerate(self.col.events) if e == "scan"),
                           f"every delete must come after the scan: {self.col.events}")
        self.assertEqual(len(self.col.docs), 1, "the pending row must survive")
        self.assertEqual(sorted(mgr.jobs), ["still_pending"])
        self.assertEqual(self.col.events.count("delete"), 6, "one delete per dead row")

    def test_unparsable_row_is_dropped(self):
        mgr = self.new_manager(bot=self.bot)
        self.col.docs["1"] = {"job_id": "junk", "chat_id": self.chat, "quiz_id": "ABC123",
                              "scheduled_time": "not-a-timestamp", "created_by": self.admin}
        self._run(mgr.restore())
        self.assertEqual(len(self.col.docs), 0)
        self.assertEqual(len(mgr.jobs), 0)

    def test_restore_without_a_bot_does_not_crash(self):
        mgr = self.new_manager(bot=None)
        self.col.docs["1"] = self._row("job_future", datetime.now(self.ist) + timedelta(hours=1))
        self._run(mgr.restore())
        self.assertEqual(len(mgr.jobs), 0)

    def test_restore_on_an_empty_collection_is_a_noop(self):
        mgr = self.new_manager(bot=self.bot)
        self._run(mgr.restore())
        self.assertEqual(len(mgr.jobs), 0)
        self.assertEqual(len(self.bot.sent), 0)

    def test_utc_stored_timestamps_are_read_as_ist(self):
        """A row written with a UTC offset must still fire at the right instant."""
        mgr = self.new_manager(bot=self.bot)
        when = (datetime.now(self.ist) + timedelta(seconds=0.5)).astimezone(
            __import__("pytz").utc)
        self.col.docs["1"] = self._row("job_utc", when)

        async def go():
            self.start_scheduler()
            await mgr.restore()
            await asyncio.sleep(1.8)
        self._run(go())
        self.assertEqual(len(self.launched), 1)


class ScheduleManagerUnitCases(ScheduleBase):
    def test_add_is_idempotent_for_the_same_instant(self):
        mgr = self.new_manager()
        when = (datetime.now(self.ist) + timedelta(minutes=30)).replace(second=0, microsecond=0)

        async def go():
            j1 = await mgr.add(self.chat, "ABC123", when, self.admin, self.ctx)
            j2 = await mgr.add(self.chat, "ABC123", when, self.admin, self.ctx)
            return j1, j2
        j1, j2 = self._run(go())
        self.assertEqual(j1, j2)
        self.assertEqual(len(mgr.jobs), 1)
        self.assertEqual(len(self.col.docs), 1)

    def test_remove_of_an_unknown_job_is_false(self):
        mgr = self.new_manager()
        self.assertFalse(self._run(mgr.remove("nope")))

    def test_get_for_chat_returns_the_job_id(self):
        mgr = self.new_manager()

        async def go():
            await mgr.add(self.chat, "ABC123", datetime.now(self.ist) + timedelta(minutes=5),
                          self.admin, self.ctx)
            return await mgr.get_for_chat(self.chat)
        rows = self._run(go())
        self.assertEqual(len(rows), 1)
        self.assertIn("job_id", rows[0])
        self.assertEqual(rows[0]["quiz_id"], "ABC123")


class ScheduleRegistrationCases(unittest.TestCase):
    TOKEN = "123456789:AAExampleTokenExampleTokenExampleTokenExa"

    def test_register_adds_exactly_the_three_schedule_commands(self):
        from telegram.ext import Application, CommandHandler

        app = Application.builder().token(self.TOKEN).build()
        scheduling.register(app)
        cmds = [h for h in app.handlers[0] if isinstance(h, CommandHandler)]
        self.assertEqual(len(cmds), 3)
        got = {next(iter(h.commands)): h.callback for h in cmds}
        self.assertEqual(
            set(got), {"schedule", "viewschedule", "cancelschedule"})
        self.assertIs(got["schedule"], scheduling.schedule_command)
        self.assertIs(got["viewschedule"], scheduling.viewschedule_command)
        self.assertIs(got["cancelschedule"], scheduling.cancelschedule_command)

    def test_part3_commands_are_unique_across_the_whole_single_bot(self):
        from telegram.ext import Application, CommandHandler

        from quizbot.runner_bot import handlers
        from quizbot.runner_bot.creator_bridge import register_creator_bridge

        app = Application.builder().token(self.TOKEN).build()
        handlers.register(app)
        register_creator_bridge(app)
        seen: dict[str, list[int]] = {}
        for group, hs in app.handlers.items():
            for h in hs:
                if isinstance(h, CommandHandler):
                    for c in h.commands:
                        seen.setdefault(c, []).append(group)
        for cmd in ("schedule", "viewschedule", "cancelschedule", "aiquiz"):
            self.assertIn(cmd, seen, f"/{cmd} must be registered")
            self.assertEqual(len(seen[cmd]), 1, f"/{cmd} must be registered exactly once")
        # nothing else in the app may claim the aiq_ callback namespace
        aiq = [h for group in app.handlers.values() for h in group
               if type(h).__name__ == "CallbackQueryHandler"
               and getattr(h, "pattern", None) and h.pattern.match("aiq_count_1_5")]
        self.assertEqual(len(aiq), 1)

    def test_commands_are_dispatched_by_the_real_ptb_filters(self):
        """Use PTB's own check_update so the audit covers real dispatch."""
        from telegram.ext import Application, CommandHandler

        from quizbot.runner_bot import handlers

        app = Application.builder().token(self.TOKEN).build()
        handlers.register(app)
        by_cmd = {next(iter(h.commands)): h for h in app.handlers[0]
                  if isinstance(h, CommandHandler)}

        for cmd in ("schedule", "viewschedule", "cancelschedule", "aiquiz"):
            h = by_cmd[cmd]
            self.assertTrue(h.check_update(command_update(f"/{cmd} ABC 14:30", "supergroup")),
                            f"/{cmd} in a supergroup must be dispatched")
            self.assertTrue(h.check_update(command_update(f"/{cmd}", "group")),
                            f"/{cmd} in a basic group must be dispatched")
            # channel posts carry no user, so they must never reach these
            self.assertFalse(h.check_update(command_update(f"/{cmd} ABC", "channel")),
                             f"/{cmd} must not fire on a channel post")
            self.assertFalse(h.check_update(command_update(f"/not{cmd}", "supergroup")))

    def test_args_reach_the_handler(self):
        from telegram.ext import Application, CommandHandler

        from quizbot.runner_bot import handlers

        app = Application.builder().token(self.TOKEN).build()
        handlers.register(app)
        h = [x for x in app.handlers[0]
             if isinstance(x, CommandHandler) and "schedule" in x.commands][0]
        checked = h.check_update(command_update("/schedule ABC123 14:30", "supergroup"))
        self.assertTrue(checked)
        self.assertEqual(checked[0], ["ABC123", "14:30"], "ctx.args must be QUIZ_ID HH:MM")
        self.assertTrue(h.check_update(command_update("/schedule@journeybot ABC 14:30", "supergroup")))
        self.assertFalse(h.check_update(command_update("/schedule@otherbot ABC 14:30", "supergroup")))


def command_update(text, chat_type="supergroup"):
    """Build a real telegram.Update carrying a bot-command message.

    PTB's CommandHandler.check_update reads ``message.get_bot().username`` to
    resolve ``/cmd@thisbot``, so the message needs *a* bot object -- a
    SimpleNamespace is enough and keeps the test offline (a real Bot refuses
    to expose .username before initialize()).
    """
    from telegram import Chat, Message, MessageEntity, Update, User
    from telegram.constants import MessageEntityType

    fake_bot = SimpleNamespace(username="journeybot")

    if chat_type == "channel":
        chat = Chat(id=-1001234567, type="channel")
        msg = Message(message_id=1, date=datetime.now(), chat=chat, text=text)
        msg.set_bot(fake_bot)
        return Update(update_id=1, channel_post=msg)

    user = User(id=1, first_name="A", is_bot=False)
    chat = Chat(id=1 if chat_type == "private" else -1001234567, type=chat_type)
    first = text.split()[0]
    msg = Message(
        message_id=1, date=datetime.now(), chat=chat, from_user=user, text=text,
        entities=(MessageEntity(type=MessageEntityType.BOT_COMMAND, offset=0, length=len(first)),),
    )
    msg.set_bot(fake_bot)
    return Update(update_id=1, message=msg)


class EscapingHelperCases(unittest.TestCase):
    def test_esc_handles_the_shapes_used_by_part3(self):
        self.assertEqual(esc("A <B> & C"), "A &lt;B&gt; &amp; C")
        self.assertEqual(esc(None), "")
        self.assertEqual(esc(42), "42")
        self.assertEqual(esc("quotes \"' stay"), "quotes \"' stay")

    def test_escaped_text_passes_telegram_html_validation(self):
        for raw in ("A <B> & C", "<script>", "&&&", "a<b", "</b>", "&amp;", "plain"):
            with self.subTest(raw=raw):
                tg_html_check(f"<b>{esc(raw)}</b>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
