"""Phase 6 finalization — focused regression tests for /podcast robustness.

Covers the missing finalization behavior only (no network, no Mongo, no
ffmpeg):

* Correct runtime classification of real ``google.genai`` errors: 429
  (quota) is never "invalid key", 4xx invalid key, 5xx transient, and
  timeouts get a distinct timeout message rather than "Network issue".
* The SDK client gets a bounded HTTP timeout (never hangs indefinitely).
* The branded "Journey for लबासना" opening is actually handed to the TTS
  stage (so it ends up in the final audio), not just in text.
* TTS/audio failures and Telegram delivery failures are reported with
  their own messages, separate from Gemini errors, and never leak the key.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import quizbot.runner_bot.handlers.podcast as pod
import quizbot.runner_bot.podcast_security as sec

TEST_SECRET = "finalization-test-master-secret-0123456789"
KEY_A = "AIzaSyTESTKEYAAAAAAA11111111111111111111"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Minimal fakes (same shapes as the Phase 6 suite)
# ---------------------------------------------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    def __init__(self, chat_id=100, text=""):
        self.chat_id = chat_id
        self.chat = FakeChat(chat_id)
        self.text = text
        self.edits = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)
        self.text = text
        return self

    async def delete(self):
        return True


class FakeBot:
    def __init__(self):
        self.sent = []
        self.audios = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return FakeMessage(chat_id=chat_id, text=text)

    async def send_audio(self, **kw):
        data = kw.get("audio")
        if hasattr(data, "read"):
            kw = dict(kw)
            kw["audio_bytes"] = data.read()
        self.audios.append(kw)
        return FakeMessage(chat_id=kw.get("chat_id", 0))


class FakeCtx:
    def __init__(self, bot):
        self.bot = bot


class FinalizationBase(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get(sec.ENV_VAR)
        os.environ[sec.ENV_VAR] = TEST_SECRET
        sec.clear_cache()
        pod.PODCAST_SESSIONS.clear()
        pod._PROMO_ROTATION.clear()
        pod._OUTRO_ROTATION.clear()
        self.addCleanup(pod.PODCAST_SESSIONS.clear)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(sec.ENV_VAR, None)
        else:
            os.environ[sec.ENV_VAR] = self._old_env
        sec.clear_cache()

    def _status_captured_bot(self):
        """Return (bot, status_list) where status_list[0] reflects edits."""
        bot = FakeBot()
        status_msgs = []
        orig = bot.send_message

        async def _send(chat_id, text, **kw):
            m = await orig(chat_id, text, **kw)
            status_msgs.append(m)
            return m

        bot.send_message = _send
        return bot, status_msgs


# ---------------------------------------------------------------------------
# Runtime error classification (real google.genai error objects)
# ---------------------------------------------------------------------------

class ErrorClassificationCases(FinalizationBase):
    def test_sdk_429_quota_is_never_invalid_key(self):
        from google.genai import errors
        exc = errors.ClientError(429, {"error": {
            "code": 429,
            "message": ("You exceeded your current quota, please check your "
                        "plan and billing details."),
            "status": "RESOURCE_EXHAUSTED",
        }})
        err = pod._classify_gemini_error(exc)
        self.assertEqual(err.kind, "quota")
        self.assertIn("quota", err.user_msg)
        self.assertNotIn("key", err.user_msg)

    def test_sdk_400_invalid_key(self):
        from google.genai import errors
        exc = errors.ClientError(400, {"error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
        }})
        self.assertEqual(pod._classify_gemini_error(exc).kind, "invalid_key")

    def test_sdk_401_unauth_is_invalid_key(self):
        from google.genai import errors
        exc = errors.ClientError(401, {"error": {
            "code": 401, "message": "Request had invalid authentication credentials.",
            "status": "UNAUTHENTICATED",
        }})
        self.assertEqual(pod._classify_gemini_error(exc).kind, "invalid_key")

    def test_sdk_503_is_transient_busy(self):
        from google.genai import errors
        exc = errors.ServerError(503, {"error": {
            "code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE",
        }})
        err = pod._classify_gemini_error(exc)
        self.assertEqual(err.kind, "transient")
        self.assertIn("busy", err.user_msg)

    def test_timeout_gets_timeout_message_not_network(self):
        err = pod._classify_gemini_error(TimeoutError())
        self.assertEqual(err.kind, "transient")
        self.assertEqual(err.user_msg, pod._TIMEOUT_MSG)
        self.assertNotIn("Network issue", err.user_msg)

    def test_httpx_timeout_gets_timeout_message(self):
        import httpx
        err = pod._classify_gemini_error(httpx.ReadTimeout("timed out"))
        self.assertEqual(err.kind, "transient")
        self.assertEqual(err.user_msg, pod._TIMEOUT_MSG)

    def test_sdk_504_deadline_exceeded_is_timeout(self):
        from google.genai import errors
        exc = errors.ServerError(504, {"error": {
            "code": 504, "message": "The request was cancelled.",
            "status": "DEADLINE_EXCEEDED",
        }})
        err = pod._classify_gemini_error(exc)
        self.assertEqual(err.kind, "transient")
        self.assertEqual(err.user_msg, pod._TIMEOUT_MSG)

    def test_429_text_with_rate_limit_is_quota(self):
        # "rate limit"/"too many requests" used to fall into the transient
        # bucket; a 429 must stay quota.
        err = pod._classify_gemini_error(Exception("429 too many requests rate limit"))
        self.assertEqual(err.kind, "quota")

    def test_connection_error_is_network_transient(self):
        err = pod._classify_gemini_error(
            Exception("HTTPSConnectionPool connection refused"))
        self.assertEqual(err.kind, "transient")
        self.assertIn("Network issue", err.user_msg)

    def test_unknown_error_is_other(self):
        err = pod._classify_gemini_error(Exception("something unexpected"))
        self.assertEqual(err.kind, "other")

    def test_with_retries_timeout_surfaces_timeout_message(self):
        def boom():
            raise TimeoutError()

        with patch.object(pod, "_retry_delay", AsyncMock()):
            with self.assertRaises(pod.GeminiRequestError) as cm:
                run(pod._with_gemini_retries(boom, retries=2))
        self.assertEqual(cm.exception.kind, "transient")
        self.assertEqual(cm.exception.user_msg, pod._TIMEOUT_MSG)


# ---------------------------------------------------------------------------
# Bounded SDK timeout (never hangs indefinitely)
# ---------------------------------------------------------------------------

class BoundedTimeoutCases(FinalizationBase):
    def test_new_client_gets_http_timeout(self):
        from google import genai
        with patch.object(genai, "Client") as mock_client:
            pod._new_genai_client(KEY_A)
        kwargs = mock_client.call_args.kwargs
        self.assertIn("http_options", kwargs)
        self.assertEqual(kwargs["http_options"].timeout, pod.GEMINI_HTTP_TIMEOUT_MS)

    def test_http_timeout_is_less_than_asyncio_bound(self):
        self.assertLess(
            pod.GEMINI_HTTP_TIMEOUT_MS / 1000.0, pod.GEMINI_CALL_TIMEOUT
        )


# ---------------------------------------------------------------------------
# Branded opening reaches the TTS stage + stage-separated error reporting
# ---------------------------------------------------------------------------

class EndToEndFinalizationCases(FinalizationBase):
    def test_branded_opening_is_handed_to_tts(self):
        uid = 1
        bot, status_msgs = self._status_captured_bot()
        ctx = FakeCtx(bot)
        captured = {}

        async def fake_tts(lines, out, api_key, progress_cb=None):
            captured["lines"] = lines
            Path(out).write_bytes(b"mp3")

        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] body concept\n[MALE] example")), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "topic",
                                      "content", "lbl"))
        lines = captured["lines"]
        # The ~30s branded opening must be the first spoken lines.
        self.assertEqual(lines[0], pod.PROMO_TEMPLATES[0][0])
        self.assertEqual(lines[1], pod.PROMO_TEMPLATES[0][1])
        joined_open = " ".join(t for _, t in lines[:2])
        self.assertIn("Journey for लबासना", joined_open)
        # Body is preserved in the middle; outro closes with the brand.
        texts = [t for _, t in lines]
        self.assertIn("body concept", texts)
        joined_close = " ".join(t for _, t in lines[-2:])
        self.assertIn("Journey for लबासना", joined_close)
        # One audio delivered.
        self.assertEqual(len(bot.audios), 1)

    def test_tts_failure_reported_separately_from_gemini(self):
        uid = 2
        bot, status_msgs = self._status_captured_bot()
        ctx = FakeCtx(bot)
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge",
                          AsyncMock(side_effect=RuntimeError(
                              "Gemini TTS returned no audio data."))):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "t", "content", "l"))
        self.assertEqual(bot.audios, [])
        self.assertIn("Audio taiyaar karne me problem", status_msgs[0].text)
        self.assertNotIn("Network issue", status_msgs[0].text)

    def test_delivery_failure_reported_separately(self):
        uid = 3
        bot, status_msgs = self._status_captured_bot()
        ctx = FakeCtx(bot)
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        part = str(Path(tmp) / "a.mp3")
        Path(part).write_bytes(b"1")

        async def fake_tts(lines, out, api_key, progress_cb=None):
            Path(out).write_bytes(b"x")

        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge", fake_tts), \
             patch.object(pod, "_split_audio_if_needed",
                          AsyncMock(return_value=[part])), \
             patch.object(bot, "send_audio",
                          AsyncMock(side_effect=Exception("telegram send failed"))):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "t", "content", "l"))
        self.assertIn("Telegram par bhejne me problem", status_msgs[0].text)
        self.assertNotIn("Network issue", status_msgs[0].text)
        # Temp part file must be cleaned up after the failure.
        self.assertFalse(os.path.exists(part))

    def test_gemini_quota_during_tts_keeps_quota_message(self):
        uid = 4
        bot, status_msgs = self._status_captured_bot()
        ctx = FakeCtx(bot)
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge",
                          AsyncMock(side_effect=pod.GeminiRequestError(
                              "quota", pod._QUOTA_MSG))):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "t", "content", "l"))
        self.assertIn("quota abhi available nahi", status_msgs[0].text)
        self.assertNotIn("Audio taiyaar karne me problem", status_msgs[0].text)

    def test_no_key_leak_in_tts_and_delivery_errors(self):
        uid = 5
        bot, status_msgs = self._status_captured_bot()
        ctx = FakeCtx(bot)
        with patch.object(pod, "_gemini_generate",
                          AsyncMock(return_value="[FEMALE] s\n[MALE] t")), \
             patch.object(pod, "_gemini_tts_and_merge",
                          AsyncMock(side_effect=RuntimeError(
                              f"audio stage blew up with {KEY_A}"))):
            run(pod._generate_podcast(uid, 100, ctx, KEY_A, "t", "content", "l"))
        self.assertNotIn(KEY_A, status_msgs[0].text)


if __name__ == "__main__":
    unittest.main()
