"""Gemini key compatibility fix — legacy plaintext + env/AIKey fallback.

Phase 6 fix: podcast keys are Fernet-encrypted per-user, but older deployments
may have stored raw AIza keys (or users set gemini via /setkey). This suite
verifies:

* decrypt_api_key accepts legacy plaintext AIza keys (whitespace tolerant)
  without requiring re-encryption, while still enforcing Fernet for random
  blobs and for wrong-master-secret cases.
* _load_user_key falls back to AIKeyRepository gemini keys and to the
  GEMINI_API_KEY env var when no per-user podcast key exists, with correct
  precedence and without leaking secrets.

All 13 tests are offline (no Mongo, no network, no ffmpeg).
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import quizbot.runner_bot.handlers.podcast as pod
import quizbot.runner_bot.podcast_security as sec

TEST_SECRET = "compat-test-master-secret-abcdef0123456789"
KEY_A = "AIzaSyTESTKEYAAAAAAA11111111111111111111"
KEY_B = "AIzaSyTESTKEYBBBBBBB22222222222222222222"
FERNET_TOKEN_EXAMPLE = "gAAAAABh"  # prefix of any Fernet token


def run(coro):
    return asyncio.run(coro)


class FakeKeyRepo:
    def __init__(self, store=None):
        self.store = store or {}

    async def get_encrypted(self, uid):
        return self.store.get(uid)

    async def save(self, uid, enc):
        self.store[uid] = enc

    async def delete(self, uid):
        return self.store.pop(uid, None) is not None


class FakeAIKeys:
    def __init__(self, by_user=None):
        # by_user: {uid: [ {api_key: ...}, ... ]}
        self.by_user = by_user or {}

    async def list_for_provider(self, uid, provider):
        if provider != "gemini":
            return []
        return self.by_user.get(uid, [])


class GeminiCompatTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.get("PODCAST_KEY_SECRET")
        self._orig_gemini = os.environ.get("GEMINI_API_KEY")
        self._orig_google = os.environ.get("GOOGLE_API_KEY")
        os.environ["PODCAST_KEY_SECRET"] = TEST_SECRET
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        sec.clear_cache()
        # also clear GEMINI_API_KEY in config (re-read env)
        import quizbot.shared.config as cfg
        cfg.GEMINI_API_KEY = None

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop("PODCAST_KEY_SECRET", None)
        else:
            os.environ["PODCAST_KEY_SECRET"] = self._orig_env
        if self._orig_gemini is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._orig_gemini
        if self._orig_google is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = self._orig_google
        sec.clear_cache()
        import quizbot.shared.config as cfg
        from quizbot.shared.config import _env
        # restore config from env (best-effort)
        cfg.GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY") or None

    # ------------------------------------------------------------------ #
    # podcast_security — legacy plaintext handling
    # ------------------------------------------------------------------ #

    def test_decrypt_legacy_plaintext_aiZa(self):
        """A raw AIza key stored before encryption is returned as-is."""
        self.assertEqual(sec.decrypt_api_key(KEY_A), KEY_A)

    def test_decrypt_legacy_plaintext_with_whitespace(self):
        """Plaintext with surrounding whitespace/newlines is stripped and returned."""
        self.assertEqual(sec.decrypt_api_key(f"  {KEY_A} \n"), KEY_A)
        self.assertEqual(sec.decrypt_api_key(f"\n\t{KEY_B}\t\n"), KEY_B)

    def test_decrypt_encrypted_roundtrip_still_works(self):
        """Fernet-encrypted keys still decrypt correctly."""
        token = sec.encrypt_api_key(KEY_A)
        self.assertNotEqual(token, KEY_A)
        self.assertTrue(token.startswith(FERNET_TOKEN_EXAMPLE) or len(token) > 50)
        self.assertEqual(sec.decrypt_api_key(token), KEY_A)

    def test_decrypt_invalid_blob_still_raises(self):
        """Random garbage that is not AIza and not Fernet must raise ValueError."""
        for bad in ["", "   ", "not-a-key", "gAAAAABad", "short", "AIza", "AIza123"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sec.decrypt_api_key(bad)

    def test_decrypt_wrong_secret_raises_not_plaintext_fallback(self):
        """Fernet token encrypted with one secret must not be treated as plaintext with another."""
        token = sec.encrypt_api_key(KEY_A)
        # Switch master secret
        os.environ["PODCAST_KEY_SECRET"] = "different-secret-9999"
        sec.clear_cache()
        with self.assertRaises(ValueError) as cm:
            sec.decrypt_api_key(token)
        self.assertIn("master secret", str(cm.exception).lower())
        # Ensure error does not leak key material
        self.assertNotIn(KEY_A, str(cm.exception))
        self.assertNotIn(token[:10], str(cm.exception))

    def test_is_plain_gemini_key_true_cases(self):
        """Heuristic accepts plausible AIza keys of various lengths."""
        self.assertTrue(sec._is_plain_gemini_key(KEY_A))
        self.assertTrue(sec._is_plain_gemini_key(KEY_B))
        self.assertTrue(sec._is_plain_gemini_key("AIzaSy" + "A" * 30))
        self.assertTrue(sec._is_plain_gemini_key(f"  {KEY_A}  "))

    def test_is_plain_gemini_key_false_cases(self):
        """Heuristic rejects short, wrong-prefix, invalid chars, and Fernet tokens."""
        self.assertFalse(sec._is_plain_gemini_key(""))
        self.assertFalse(sec._is_plain_gemini_key("   "))
        self.assertFalse(sec._is_plain_gemini_key("AIza"))  # too short
        self.assertFalse(sec._is_plain_gemini_key("AIza" + "!" * 30))  # invalid char
        self.assertFalse(sec._is_plain_gemini_key("gAAAAABhFakeFernetToken1234567890"))
        self.assertFalse(sec._is_plain_gemini_key("notAIzaAtAllButLongEnough1234567890"))
        self.assertFalse(sec._is_plain_gemini_key("Bearer AIzaSy..."))

    # ------------------------------------------------------------------ #
    # _load_user_key — fallback chain
    # ------------------------------------------------------------------ #

    def test_load_user_key_prefers_podcast_over_aikey(self):
        """Per-user podcast key wins over AIKeyRepository and env fallback."""
        pod_key = sec.encrypt_api_key(KEY_A)
        aikey = [{"api_key": KEY_B, "provider": "gemini"}]

        async def _run():
            with patch.object(pod, "_key_repo", return_value=FakeKeyRepo({42: pod_key})):
                with patch.object(pod, "get_db", return_value=AsyncMock()):
                    with patch("quizbot.runner_bot.handlers.podcast.AIKeyRepository") as MockAI:
                        MockAI.return_value.list_for_provider = AsyncMock(return_value=aikey)
                        with patch.object(pod.config, "GEMINI_API_KEY", "AIzaEnvFallbackKey123456789012345"):
                            val = await pod._load_user_key(42)
                            self.assertEqual(val, KEY_A)
                            return val

        run(_run())

    def test_load_user_key_falls_back_to_aikey_when_no_podcast(self):
        """When no podcast key, AIKeyRepository gemini key is returned."""
        aikey = [{"api_key": KEY_B, "provider": "gemini"}]

        async def _run():
            with patch.object(pod, "_key_repo", return_value=FakeKeyRepo({})):
                with patch.object(pod, "get_db", return_value=AsyncMock()):
                    with patch("quizbot.runner_bot.handlers.podcast.AIKeyRepository") as MockAI:
                        MockAI.return_value.list_for_provider = AsyncMock(return_value=aikey)
                        with patch.object(pod.config, "GEMINI_API_KEY", None):
                            val = await pod._load_user_key(99)
                            self.assertEqual(val, KEY_B)

        run(_run())

    def test_load_user_key_falls_back_to_env_when_no_podcast_nor_aikey(self):
        """When neither store has a key, GEMINI_API_KEY env fallback is used."""
        async def _run():
            with patch.object(pod, "_key_repo", return_value=FakeKeyRepo({})):
                with patch.object(pod, "get_db", return_value=AsyncMock()):
                    with patch("quizbot.runner_bot.handlers.podcast.AIKeyRepository") as MockAI:
                        MockAI.return_value.list_for_provider = AsyncMock(return_value=[])
                        with patch.object(pod.config, "GEMINI_API_KEY", KEY_A):
                            val = await pod._load_user_key(77)
                            self.assertEqual(val, KEY_A)

        run(_run())

    def test_load_user_key_returns_none_when_nothing(self):
        """All fallbacks empty => None (caller will prompt for key)."""
        async def _run():
            with patch.object(pod, "_key_repo", return_value=FakeKeyRepo({})):
                with patch.object(pod, "get_db", return_value=AsyncMock()):
                    with patch("quizbot.runner_bot.handlers.podcast.AIKeyRepository") as MockAI:
                        MockAI.return_value.list_for_provider = AsyncMock(return_value=[])
                        with patch.object(pod.config, "GEMINI_API_KEY", None):
                            val = await pod._load_user_key(123)
                            self.assertIsNone(val)

        run(_run())

    def test_load_user_key_legacy_plaintext_decrypt(self):
        """Legacy plaintext stored in podcast_keys is returned via decrypt fallback."""
        async def _run():
            # Store raw AIza, not encrypted — legacy deployment
            with patch.object(pod, "_key_repo", return_value=FakeKeyRepo({55: KEY_A})):
                with patch.object(pod, "get_db", return_value=AsyncMock()):
                    with patch("quizbot.runner_bot.handlers.podcast.AIKeyRepository") as MockAI:
                        MockAI.return_value.list_for_provider = AsyncMock(return_value=[])
                        with patch.object(pod.config, "GEMINI_API_KEY", None):
                            val = await pod._load_user_key(55)
                            self.assertEqual(val, KEY_A)

        run(_run())

    def test_config_gemini_api_key_reads_env(self):
        """quizbot.shared.config.GEMINI_API_KEY reads GEMINI_API_KEY and GOOGLE_API_KEY."""
        import importlib
        import quizbot.shared.config as cfg

        # Test GEMINI_API_KEY
        os.environ["GEMINI_API_KEY"] = KEY_A
        os.environ.pop("GOOGLE_API_KEY", None)
        importlib.reload(cfg)
        self.assertEqual(cfg.GEMINI_API_KEY, KEY_A)

        # Test GOOGLE_API_KEY fallback
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ["GOOGLE_API_KEY"] = KEY_B
        importlib.reload(cfg)
        self.assertEqual(cfg.GEMINI_API_KEY, KEY_B)

        # Cleanup
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        importlib.reload(cfg)
        # After reload without env, should be None
        self.assertIsNone(cfg.GEMINI_API_KEY)
        # Restore test secret for other tests
        os.environ["PODCAST_KEY_SECRET"] = TEST_SECRET
        sec.clear_cache()
