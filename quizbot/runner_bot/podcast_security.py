"""Per-user Gemini API key protection for the podcast system (Phase 6).

Users' Gemini API keys are secrets: they are encrypted at rest with a
server-side master secret (Fernet, from the `cryptography` package that is
already a project dependency) and decrypted only in-process at the moment a
Gemini API call is made. Plaintext keys are never logged, never echoed to
Telegram, and never leave the server.

The master secret lives ONLY in the PODCAST_KEY_SECRET environment
variable (see .env.example) -- never in source, never in git. It may be a
Fernet key itself or any sufficiently long random string (SHA-256 derived
into a Fernet key). If unset, an ephemeral process key is used and a
warning is logged: keys then do NOT survive a bot restart.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENV_VAR = "PODCAST_KEY_SECRET"
_MASK = "••••"

# Cache Fernet instances by the secret value they were built from, so tests
# (and operators) can rotate PODCAST_KEY_SECRET without a restart.
_fernet_cache: dict[str, Fernet] = {}
_ephemeral_warned = False


def clear_cache() -> None:
    """Drop cached Fernet instances (used by tests)."""
    _fernet_cache.clear()


def _master_secret() -> str | None:
    value = (os.environ.get(ENV_VAR) or "").strip()
    return value or None


def _fernet() -> Fernet:
    """Build (or reuse) the Fernet cipher for the current master secret."""
    global _ephemeral_warned
    secret = _master_secret()
    cache_key = secret if secret is not None else "<ephemeral>"
    hit = _fernet_cache.get(cache_key)
    if hit is not None:
        return hit
    if secret is None:
        cipher = Fernet(Fernet.generate_key())
        if not _ephemeral_warned:
            _ephemeral_warned = True
            logger.warning(
                "%s is not set: podcast API keys are encrypted with an "
                "ephemeral process key and will NOT survive a restart. "
                "Set %s to a long random string in production.",
                ENV_VAR, ENV_VAR,
            )
    else:
        try:
            # Accept a ready-made Fernet key first.
            cipher = Fernet(secret.encode("utf-8"))
        except Exception:
            # Otherwise derive one deterministically from any long string.
            digest = hashlib.sha256(secret.encode("utf-8")).digest()
            cipher = Fernet(base64.urlsafe_b64encode(digest))
    _fernet_cache[cache_key] = cipher
    return cipher


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt a user API key for storage. Never logs the key."""
    if not plaintext or not plaintext.strip():
        raise ValueError("Cannot encrypt an empty API key.")
    return _fernet().encrypt(plaintext.strip().encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str) -> str:
    """Decrypt a stored key back to plaintext for server-side API use.

    Raises ValueError when the blob is missing/corrupt or was encrypted
    with a different master secret (operator must ask the user to re-set
    the key). The exception message never contains key material.
    """
    if not token:
        raise ValueError("No stored API key to decrypt.")
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "Stored API key cannot be decrypted with the current master "
            "secret (it may have been rotated)."
        ) from exc
    except Exception as exc:
        raise ValueError("Stored API key blob is invalid.") from exc


def mask_api_key(key: str) -> str:
    """Safe-to-display fingerprint, e.g. ``AIza••••9f3k`` (never the key)."""
    text = (key or "").strip()
    if len(text) <= 4:
        return _MASK
    if len(text) <= 8:
        return f"{_MASK}{text[-4:]}"
    return f"{text[:4]}{_MASK}{text[-4:]}"


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove any secret substrings from text destined for users/logs."""
    if not text:
        return ""
    out = str(text)
    for secret in secrets or []:
        if secret and len(secret) >= 6 and secret in out:
            out = out.replace(secret, "[redacted]")
    return out
