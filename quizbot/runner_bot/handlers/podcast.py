"""Podcast generation for Journey for लबासना (Phase 6).

Sources: Test Series quizzes (custom question ranges), topic/text, or PDF.
PDFs are automatically classified as MCQ/question material or study/content
material. A single PDF generation is limited to 10 questions or 10 pages;
custom selection accepts comma-separated numbers and ranges such as
``1,3,7-10`` (maximum 10 items).

Every user generates podcasts with their OWN Gemini API key, saved once via
/podcast and stored encrypted (see podcast_security). A shared
GEMINI_API_KEY env var is still honored as a *compatibility fallback* when
the caller has no per-user key (so VPS deployments that still carry a
legacy env key keep working), but per-user keys always take precedence
and the env fallback is never logged or exposed.
"""
from __future__ import annotations

import asyncio
import base64
import wave
import json
import logging
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from quizbot.database import AIKeyRepository, PodcastKeyRepository, QuizRepository, get_db
from quizbot.runner_bot.podcast_security import (
    decrypt_api_key,
    encrypt_api_key,
    mask_api_key,
    redact as redact_secrets,
)
from quizbot.shared import config
from quizbot.runner_bot.telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

PODCAST_SESSIONS: dict[int, dict[str, Any]] = {}
MAX_SELECTION = 10
# Test Series ranges are intentionally NOT capped at 10 (only PDF input is);
# the guard below just matches the platform's maximum test-series size.
TESTSERIES_MAX_QUESTIONS = 300
FEMALE_VOICE = "hi-IN-SwaraNeural"
MALE_VOICE = "hi-IN-MadhurNeural"

BRAND = "Journey for लबासना"

# ---------------------------------------------------------------------------
# Branded advertisement templates (~30 seconds each, two hosts).
#
# Rotated per user so consecutive podcasts never repeat the same promo.
# Every template promotes only Journey for लबासना with honest, generic
# wording -- no users/ranks/results/statistics are ever invented.
# ---------------------------------------------------------------------------
PROMO_TEMPLATES: tuple[tuple[tuple[str, str], ...], ...] = (
    (("FEMALE", "Welcome to Journey for लबासना platform mein aapka hardik swagat hai! Ye podcast aapki UPSC taiyaari ko simple, practical aur memorable banane ke liye hai."),
     ("MALE", "Bilkul! Yahan har topic ko aasaan भाषा, real-life examples aur exam-oriented insights ke saath samjhaya jata hai. Suniye, samjhiye aur revise kijiye — chaliye, aaj ka episode shuru karte hain!")),
    (("FEMALE", "Journey for लबासना par aapka swagat hai — jahan UPSC preparation banti hai systematic, conceptual aur stress-free."),
     ("MALE", "Har episode mein concepts, smart elimination techniques aur revision takeaways — taaki Prelims aur Mains dono strong hon. Toh chaliye, aaj ki discussion shuru karte hain!")),
    (("FEMALE", "Swagat hai aapka Journey for लबासना ke podcast mein — test series ko sirf attempt nahi, deeply samajhne ka smart tareeka."),
     ("MALE", "Yahan hum questions ke peeche ke concepts kholte hain, har option analyse karte hain aur exam temperament banate hain. Chaliye, aaj ke questions ki journey shuru karte hain!")),
    (("FEMALE", "Welcome to Journey for लबासना! Daily practice aur smart revision se selection ka safar आसान बनता है।"),
     ("MALE", "Is podcast mein hum practice questions ko lively discussion mein badalte hain — taaki concept bhi clear ho aur yaad bhi rahe. Toh shuru karte hain aaj ka episode!")),
    (("FEMALE", "Journey for लबासना ek complete learning ecosystem hai — quizzes, test series, aur ab ye podcast, sab ek hi jagah."),
     ("MALE", "Concept clarity, regular practice aur focused revision — apni taiyaari ko ek nayi direction dijiye. Chaliye, aaj kuch naya seekhte hain!")),
    (("FEMALE", "Sapne bade hain aur manzil door nahi — swagat hai aapka Journey for लबासना par!"),
     ("MALE", "Roz thoda-thoda consistent effort hi topper banata hai. Aaiye, aaj ke episode se apni preparation ko aur strong banayein. Shuru karte hain!")),
    (("FEMALE", "UPSC ki journey mein sahi guidance sabse zaroori hai — aur isi liye hai Journey for लबासना."),
     ("MALE", "Har episode mein naye concepts, naye perspectives aur exam-ready understanding. Toh chaliye, aaj ka safar shuru karte hain!")),
)

# Legacy single-promo alias (first template, flattened). New code uses the
# rotating PROMO_TEMPLATES above; kept so any external reference keeps working.
PROMO = " ".join(text for _speaker, text in PROMO_TEMPLATES[0])

# Per-user rotation counters (in-memory; consecutive podcasts always differ).
_PROMO_ROTATION: dict[int, int] = {}
_OUTRO_ROTATION: dict[int, int] = {}

# Soft branded outros -- short, warm, professional, varied per episode.
OUTRO_TEMPLATES: tuple[tuple[tuple[str, str], ...], ...] = (
    (("MALE", "Toh isi ke saath aaj ke episode mein itna hi. Umeed hai aaj ki discussion aapki preparation mein useful rahi hogi."),
     ("FEMALE", "Jude rahiye Journey for लबासना ke saath, aur milte hain agle episode mein. All the best!")),
    (("MALE", "Aaj humne kaafi important concepts cover kiye — inhe revise karna na bhoolein."),
     ("FEMALE", "Journey for लबासना ke saath apni taiyaari jaari rakhiye. Agle episode mein phir milenge!")),
    (("MALE", "Revision hi selection ki chaabi hai — aaj ke points ko ek baar phir zaroor dohrayein."),
     ("FEMALE", "Journey for लबासना aapke saath hai har kadam par. Milte hain agle episode mein!")),
    (("MALE", "Umeed hai aaj ka episode aapko pasand aaya hoga aur saare concepts clear hue honge."),
     ("FEMALE", "Seekhte rahiye, badhte rahiye — Journey for लबासना ke saath. Bye-bye, take care!")),
    (("MALE", "Consistency se hi sapne poore hote hain — kal phir ek naye topic ke saath milenge."),
     ("FEMALE", "Tab tak ke liye, Journey for लबासना ki taraf se shubhkaamnaayein. Keep preparing!")),
)

TAKEAWAY_BRIDGE = ("FEMALE", "Toh doston, aaj ke episode ke important points ko short notes mein likh lijiye — revision mein bahut kaam aayenge.")

SCRIPT_RULES = """
Create an educational two-host podcast in natural Hindi/Hinglish.
Hosts: [FEMALE] and [MALE]. Keep it conversational, clear, engaging and accurate.
The supplied source is authoritative for source-derived facts. Do not invent source claims.
Explain difficult terms immediately in simple language and add a clearly labelled real-life
example/analogy wherever it improves understanding. Connect relevant points to UPSC Prelims
and Mains without turning the podcast into a dry lecture.
Do NOT include any brand intro, welcome or advertisement -- the episode already opens with
one. Begin directly with why the selected material/topic matters for UPSC and practical
understanding, then explain it in depth.
Always write the brand exactly as "Journey for लबासना" (Hindi script), never any
Latin-script variant of the brand name.
End with a concise recap and revision takeaways.
Return ONLY dialogue lines, one per line, prefixed [FEMALE] or [MALE].
"""

QUESTION_RULES = """
For EACH question, use this explanation sequence:
1) Read/restate what the question is testing.
2) State the correct answer (only when supported by the supplied material; otherwise reason it out transparently).
3) Explain the concept in depth but simple language.
4) Explain why EACH other option is wrong, not just the correct option.
5) Give the UPSC approach and elimination technique.
6) Add a memorable memory trick when useful.
7) Give a short revision note.
8) Give source/static-current-affairs linkage only when supported or clearly labelled as context.
Use real-life examples/analogies to make difficult concepts intuitive.
Always write the brand exactly as "Journey for लबासना" (Hindi script).
"""


def _enforce_brand_text(text: str) -> str:
    """Force the Hindi-script brand spelling (reliable TTS pronunciation)."""
    return re.sub(r"(?i)lbsnaa", "लबासना", text or "")


def _finalize_lines(lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Brand-guard every dialogue line just before voice generation."""
    return [(speaker, _enforce_brand_text(text)) for speaker, text in lines]


def _build_ad_lines(uid: int) -> list[tuple[str, str]]:
    """Next promo template for this user (rotation => consecutive differ)."""
    idx = _PROMO_ROTATION.get(uid, 0) % len(PROMO_TEMPLATES)
    _PROMO_ROTATION[uid] = _PROMO_ROTATION.get(uid, 0) + 1
    return [(speaker, text) for speaker, text in PROMO_TEMPLATES[idx]]


def _build_outro_lines(uid: int) -> list[tuple[str, str]]:
    """Next outro template for this user (rotation => consecutive differ)."""
    idx = _OUTRO_ROTATION.get(uid, 0) % len(OUTRO_TEMPLATES)
    _OUTRO_ROTATION[uid] = _OUTRO_ROTATION.get(uid, 0) + 1
    return [(speaker, text) for speaker, text in OUTRO_TEMPLATES[idx]]


def _assemble_episode(uid: int, body: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[ad] + [body] + [takeaway bridge] + [outro], brand-guarded."""
    lines = _build_ad_lines(uid) + list(body) + [TAKEAWAY_BRIDGE] + _build_outro_lines(uid)
    return _finalize_lines(lines)


def _selection_kb(uid: int, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Default 10", callback_data=f"pod_default_{uid}_{kind}"),
         InlineKeyboardButton("✏️ Custom", callback_data=f"pod_custom_{uid}_{kind}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"pod_cancel_{uid}")],
    ])


def _menu_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Test Series", callback_data=f"podmenu_{uid}_ts"),
         InlineKeyboardButton("📄 PDF", callback_data=f"podmenu_{uid}_pdf")],
        [InlineKeyboardButton("📝 Topic/Text", callback_data=f"podmenu_{uid}_topic")],
        [InlineKeyboardButton("🔄 Change Gemini API Key", callback_data=f"podkey_{uid}_change"),
         InlineKeyboardButton("🗑️ Remove Gemini API Key", callback_data=f"podkey_{uid}_remove")],
    ])


def _key_prompt_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Set Gemini API Key", callback_data=f"podkey_{uid}_set")],
    ])


def _cancel_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"pod_cancel_{uid}")],
    ])


def _tokenize_numbers(text: str) -> list[int]:
    """Parse ``1-10``, ``5``, ``1,3,7-9`` into ordered unique integers.

    Pure helper shared by PDF custom selection and Test Series ranges.
    Raises ValueError with a user-facing message on any bad input.
    """
    cleaned = re.sub(r"\s*-\s*", "-", (text or "").strip())
    parts = [p for p in re.split(r"[,\s]+", cleaned) if p]
    if not parts:
        raise ValueError("No selection supplied")
    vals: list[int] = []
    for part in parts:
        if re.fullmatch(r"\d+", part):
            nums = [int(part)]
        else:
            m = re.fullmatch(r"(\d+)-(\d+)", part)
            if not m:
                raise ValueError("Use numbers/ranges like 1-10 ya 1,3,5")
            start, end = int(m.group(1)), int(m.group(2))
            if start < 1 or end < start:
                raise ValueError(f"Invalid range '{part}' (use e.g. 1-10)")
            if end - start + 1 > 1000:
                raise ValueError("Range too large -- split it into smaller ranges")
            nums = list(range(start, end + 1))
        for n in nums:
            if n not in vals:
                vals.append(n)
    return vals


def _parse_selection(text: str, maximum: int) -> list[int]:
    text = text.strip().lower()
    if text in {"default", "all"}:
        return list(range(1, min(MAX_SELECTION, maximum) + 1))
    vals = _tokenize_numbers(text)
    for n in vals:
        if n < 1 or n > maximum:
            raise ValueError(f"Number {n} is outside 1-{maximum}")
    if len(vals) > MAX_SELECTION:
        raise ValueError("Maximum 10 questions/pages per podcast")
    return vals


def _extract_pdf_pages(path: str) -> list[str]:
    import fitz
    doc = fitz.open(path)
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


# Numbered-question prefix: Q.1. / Q1. / Q 1. / Question 1. / plain 1.
_QNUM_PREFIX = r"(?:Q(?:uestion)?\s*\.?\s*)?"


def _looks_like_questions(text: str) -> bool:
    # Strong signals: numbered question stems plus option labels.
    qnums = len(re.findall(rf"(?m)^\s*{_QNUM_PREFIX}\d+\s*[.)\-:]\s+", text, re.I))
    opts = len(re.findall(r"(?m)^\s*[A-Da-d]\s*[.)\-:]\s+", text))
    answer = len(re.findall(r"(?im)^\s*(?:answer|correct answer|उत्तर|सही उत्तर)\s*[:：]", text))
    return (qnums >= 2 and opts >= 4) or (qnums >= 2 and answer >= 1)


def _extract_question_blocks(text: str) -> list[tuple[int, str]]:
    """Return (source question number, full question block)."""
    starts = list(re.finditer(rf"(?m)^\s*{_QNUM_PREFIX}(\d+)\s*[.)\-: ]\s+", text, re.I))
    if len(starts) < 2:
        # Fallback: preserve sequential numbering when a PDF has no clear numbering.
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
        return [(i, b) for i, b in enumerate(blocks, 1)]
    blocks: list[tuple[int, str]] = []
    seen: set[int] = set()
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[m.start():end].strip()
        if not block:
            continue
        num = int(m.group(1))
        # Duplicate numbering can occur in headers/footers; keep the first real block.
        if num in seen:
            continue
        seen.add(num)
        blocks.append((num, block))
    return blocks


GEMINI_TEXT_MODEL = "gemini-2.5-flash"
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
TTS_VOICE_FEMALE = "Kore"
TTS_VOICE_MALE = "Puck"

# TTS input budget per request (Gemini ~8,192 tokens incl. prompt overhead).
TTS_CHUNK_CHARS = 18000
# Telegram Bot API file ceiling is 50 MB; split above 45 MB to stay safe.
PODCAST_MAX_FILE_BYTES = 45 * 1024 * 1024
# Limited retries for TRANSIENT Gemini failures only (quota/invalid-key
# failures never retry, to avoid burning the user's quota pointlessly).
GEMINI_MAX_RETRIES = 2
_RETRY_BACKOFF = (1.0, 3.0)
# Bounded per-call timeout (seconds): a stuck Gemini request must never hang
# podcast generation indefinitely. Applies to validation, script and TTS calls.
GEMINI_CALL_TIMEOUT = 180.0


def _genai_types():
    """Lazily import ``google.genai.types``; None when the SDK is absent.

    The SDK is a production dependency (requirements.txt), but tests replace
    the client through ``_new_genai_client`` without it. Callers must fall
    back to a plain ``None`` config for the (possibly patched) client call.
    """
    try:
        from google.genai import types  # type: ignore
        return types
    except Exception:
        return None


class GeminiRequestError(RuntimeError):
    """A classified Gemini failure.

    kind: "invalid_key" | "quota" | "transient" | "other".
    user_msg is always a safe Hinglish message (never contains key material).
    """

    def __init__(self, kind: str, user_msg: str):
        super().__init__(user_msg)
        self.kind = kind
        self.user_msg = user_msg


_INVALID_KEY_MSG = (
    "❌ Aapki Gemini API key kaam nahi kar rahi. Nayi valid key set karein."
)
_QUOTA_MSG = (
    "Gemini API quota abhi available nahi hai. "
    "Apni Gemini API billing/quota settings check karein."
)
_BUSY_MSG = "Gemini API abhi busy hai. Kuch der baad dobara try karein."
_NET_MSG = "Network issue lag raha hai. Thodi der baad dobara try karein."


def _classify_gemini_error(exc: Exception) -> GeminiRequestError:
    """Map a raw Gemini exception to a kind + safe user message. Pure."""
    text = str(exc or "").lower()
    invalid_markers = (
        "api_key_invalid", "api key not valid", "invalid api key",
        "incorrect api key", "key is invalid", "unauthenticated",
        "leaked", "permission_denied",
    )
    if any(m in text for m in invalid_markers):
        return GeminiRequestError("invalid_key", _INVALID_KEY_MSG)
    quota_markers = (
        # Gemini 429 == RESOURCE_EXHAUSTED (quota/billing), never a key
        # problem and not worth blind retries.
        "429", "quota", "resource_exhausted", "resource exhausted",
        "billing", "limit: 0", "free tier",
    )
    if any(m in text for m in quota_markers):
        return GeminiRequestError("quota", _QUOTA_MSG)
    transient_markers = (
        "500", "502", "503", "504", "unavailable", "overloaded",
        "timeout", "timed out", "temporarily", "connection", "network",
        "econnreset", "broken pipe", "rate limit", "too many requests",
        "deadline exceeded", "try again",
    )
    if any(m in text for m in transient_markers):
        kinds = "timeout" if "timeout" in text or "timed out" in text else "busy"
        return GeminiRequestError(
            "transient", _NET_MSG if kinds == "timeout" else _BUSY_MSG
        )
    return GeminiRequestError(
        "other", "Gemini se jawab nahi mil paya. Dobara try karein."
    )


async def _retry_delay(secs: float) -> None:
    await asyncio.sleep(secs)


async def _with_gemini_retries(sync_fn: Callable[[], Any], retries: int) -> Any:
    """Run a blocking Gemini call with limited transient-only retries."""
    last: Optional[GeminiRequestError] = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(sync_fn), timeout=GEMINI_CALL_TIMEOUT
            )
        except GeminiRequestError as err:
            raise err
        except Exception as exc:
            err = (
                GeminiRequestError("transient", _NET_MSG)
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                else _classify_gemini_error(exc)
            )
            last = err
            if err.kind == "transient" and attempt < retries:
                await _retry_delay(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
                continue
            raise err from exc
    assert last is not None
    raise last


def _new_genai_client(api_key: str):
    """Seam for constructing the google-genai client (patched in tests)."""
    from google import genai
    return genai.Client(api_key=api_key)


def _generate_once(prompt: str, api_key: str, max_tokens: int) -> str:
    types = _genai_types()
    client = _new_genai_client(api_key)
    response = client.models.generate_content(
        model=GEMINI_TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.7,
        ) if types is not None else None,
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text


async def _gemini_generate(prompt: str, api_key: str, max_tokens: int = 4096,
                           retries: int = GEMINI_MAX_RETRIES) -> str:
    """Generate podcast script text with the USER's Gemini API key."""
    return await _with_gemini_retries(
        lambda: _generate_once(prompt, api_key, max_tokens), retries
    )


async def _validate_gemini_key(api_key: str) -> tuple[bool, str]:
    """Minimal safe validation call. Returns (ok, user_message).

    Never raises, never echoes the key, never surfaces raw API payloads.
    """

    def call() -> str:
        types = _genai_types()
        client = _new_genai_client(api_key)
        response = client.models.generate_content(
            model=GEMINI_TEXT_MODEL,
            contents="Reply with the single word OK.",
            config=types.GenerateContentConfig(
                max_output_tokens=8,
                temperature=0,
            ) if types is not None else None,
        )
        return (response.text or "").strip()

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(call), timeout=GEMINI_CALL_TIMEOUT
        )
    except Exception as exc:
        err = _classify_gemini_error(exc)
        if err.kind == "invalid_key":
            return False, "❌ Ye Gemini API key valid nahi lagi. Key check karke dobara bhejein."
        if err.kind == "quota":
            return False, f"❌ {_QUOTA_MSG}"
        return False, "❌ Key validate nahi ho payi (network/API issue). Dobara try karein."
    if not text:
        return False, "❌ Key se jawab nahi mila. Key check karke dobara bhejein."
    return True, ""


def _pcm_from_response(response) -> bytes:
    """Extract PCM bytes from Gemini TTS response across SDK response shapes."""
    try:
        parts = response.candidates[0].content.parts
    except Exception:
        parts = getattr(response, "parts", []) or []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline is None:
            continue
        data = getattr(inline, "data", None)
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return base64.b64decode(data)
    raise RuntimeError("Gemini TTS returned no audio data.")


def _tts_once(lines: list[tuple[str, str]], api_key: str) -> bytes:
    transcript = "\n".join(
        f"{'Female Host' if speaker == 'FEMALE' else 'Male Host'}: {text}"
        for speaker, text in lines
    )
    prompt = (
        "Perform this as a natural Hindi/Hinglish educational podcast conversation. "
        "Use exactly two speakers named Female Host and Male Host. Female Host should sound "
        "warm and clear; Male Host should sound friendly and conversational. Do not add, remove, "
        "or paraphrase words. Preserve the transcript exactly as written.\n\n"
        + transcript
    )
    types = _genai_types()
    config = (
        types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                language_code="hi-IN",
                multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                    speaker_voice_configs=[
                        types.SpeakerVoiceConfig(
                            speaker="Female Host",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE_FEMALE)
                            ),
                        ),
                        types.SpeakerVoiceConfig(
                            speaker="Male Host",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE_MALE)
                            ),
                        ),
                    ]
                ),
            ),
        )
        if types is not None
        else None
    )
    client = _new_genai_client(api_key)
    response = client.models.generate_content(
        model=GEMINI_TTS_MODEL,
        contents=prompt,
        config=config,
    )
    return _pcm_from_response(response)


async def _gemini_tts_chunk(lines: list[tuple[str, str]], wav_path: str, api_key: str) -> None:
    pcm = await _with_gemini_retries(lambda: _tts_once(lines, api_key), retries=1)
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)


def _chunk_lines_for_tts(lines: list[tuple[str, str]],
                         max_chars: int = TTS_CHUNK_CHARS) -> list[list[tuple[str, str]]]:
    """Split dialogue into TTS requests, preserving order with no loss/dup."""
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    chars = 0
    for line in lines:
        line_chars = len(line[1]) + 30
        if current and chars + line_chars > max_chars:
            chunks.append(current)
            current = []
            chars = 0
        current.append(line)
        chars += line_chars
    if current:
        chunks.append(current)
    return chunks


async def _run_cmd(*args: str, capture_stdout: bool = False) -> tuple[int, str]:
    """Run ffmpeg/ffprobe without blocking the bot. Seam for tests."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    tail = (out if capture_stdout else err).decode(errors="ignore")
    return proc.returncode or 0, tail[-2000:]


async def _merge_wavs_to_mp3(wavs: list[Path], out_path: str, work: Path) -> None:
    concat = work / "concat.txt"
    concat.write_text(
        "".join(f"file '{p.as_posix().replace(chr(39), chr(39)+chr(92)+chr(39))}'\n" for p in wavs),
        encoding="utf-8",
    )
    rc, err = await _run_cmd(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
        "-c:a", "libmp3lame", "-b:a", "128k", out_path,
    )
    if rc != 0:
        raise RuntimeError(f"Gemini audio merge failed: {err[-500:]}")


async def _probe_duration(path: str) -> Optional[float]:
    rc, out = await _run_cmd(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path, capture_stdout=True,
    )
    if rc != 0:
        return None
    try:
        return float(out.strip().split()[0])
    except (ValueError, IndexError):
        return None


async def _split_audio_if_needed(out_path: str,
                                 max_bytes: int = PODCAST_MAX_FILE_BYTES) -> list[str]:
    """Split an oversized MP3 into sequential Part files; else [out_path].

    Never raises for split problems -- worst case the single file is sent.
    """
    try:
        size = os.path.getsize(out_path)
    except OSError:
        return [out_path]
    if size <= max_bytes:
        return [out_path]
    duration = await _probe_duration(out_path)
    if not duration or duration <= 0:
        logger.warning("podcast split skipped: no duration for %s", out_path)
        return [out_path]
    parts = max(2, math.ceil(size / max_bytes))
    seg = duration / parts
    tag = f"{os.getpid()}_{abs(hash(out_path)) % 10**8}"
    pattern = str(config.TEMP_DIR / f"podcast_{tag}_part%03d.mp3")
    try:
        rc, err = await _run_cmd(
            "ffmpeg", "-y", "-i", out_path, "-f", "segment",
            "-segment_time", f"{seg:.2f}", "-c", "copy", pattern,
        )
        found = sorted(config.TEMP_DIR.glob(f"podcast_{tag}_part*.mp3"))
        if rc != 0 or not found:
            logger.error("podcast split failed: %s", err[-300:])
            return [out_path]
        return [str(p) for p in found]
    except Exception:
        logger.exception("podcast split failed")
        return [out_path]


async def _gemini_tts_and_merge(lines: list[tuple[str, str]], out_path: str,
                                api_key: str,
                                progress_cb: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
    """Use Gemini 2.5 Flash TTS for two-host audio, chunked for its input limit."""
    work = Path(tempfile.mkdtemp(prefix="journey_gemini_tts_", dir=str(config.TEMP_DIR)))
    try:
        # Keep each request comfortably below the 8,192-token TTS input limit.
        chunks = _chunk_lines_for_tts(lines)
        wavs: list[Path] = []
        for i, chunk in enumerate(chunks):
            wav = work / f"{i:04d}.wav"
            await _gemini_tts_chunk(chunk, str(wav), api_key)
            wavs.append(wav)
        if progress_cb is not None:
            await progress_cb("audio")
        await _merge_wavs_to_mp3(wavs, out_path, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _edit_status(status: Any, text: str) -> None:
    if status is None:
        return
    try:
        await status.edit_text(text)
    except Exception:
        pass


def _user_facing_error(exc: Exception, secrets: Optional[list[str]] = None) -> str:
    """Safe Hinglish failure message. Never leaks keys or stack traces."""
    if isinstance(exc, GeminiRequestError):
        return f"❌ {exc.user_msg}"
    text = redact_secrets(str(exc or ""), secrets)[:300]
    low = text.lower()
    if "merge failed" in low or "ffmpeg" in low or "libmp3lame" in low:
        return "❌ Audio taiyaar karne me problem aayi. Thodi der baad dobara try karein."
    if "valid two-host dialogue" in low or "valid dialogue" in low:
        return "❌ AI se sahi script nahi ban paya. Dobara try karein."
    if text:
        return f"❌ Podcast generation failed: {text}"
    return "❌ Podcast generation failed. Dobara try karein."


async def _generate_question_script(uid: int, questions: list[tuple[int, str]],
                                    api_key: str,
                                    progress_cb: Optional[Callable[[int, int], Awaitable[None]]] = None,
                                    ) -> list[tuple[str, str]]:
    """Generate each question as a separately numbered segment so numbering is never skipped."""
    all_lines: list[tuple[str, str]] = []
    total = len(questions)
    for i, (qnum, question) in enumerate(questions, 1):
        prompt = f"""
You are creating ONE segment of an educational UPSC podcast.
This segment is strictly for Question Number {qnum}.

MANDATORY AUDIO FLOW:
- First spoken line MUST clearly announce: "Ab hum Question Number {qnum} par aate hain."
- Then read/briefly restate the question so the listener knows exactly which question is being discussed.
- State the correct answer and explain it in depth.
- Explain why EACH other option is wrong.
- Give UPSC approach and elimination technique.
- Add a useful memory trick and a short revision note.
- Use a simple real-life example/analogy where helpful.
- Before ending this segment, clearly say that the discussion of Question Number {qnum} is complete.
- Do NOT discuss any other question.
- Always write the brand exactly as "Journey for लबासना" (Hindi script).

{QUESTION_RULES}

Return ONLY dialogue lines prefixed [FEMALE] or [MALE].
Use natural Hindi/Hinglish, conversational Q&A between a female and male host.

SOURCE — QUESTION {qnum}:
{question}
"""
        raw = await _gemini_generate(prompt, api_key, max_tokens=min(5000, max(2200, len(question) // 2)))
        lines = _parse_dialogue(raw)
        if not lines:
            raise RuntimeError(f"AI did not return valid dialogue for Question {qnum}.")
        # Hard guarantee of an audible question-number marker even if the model omits it.
        lines.insert(0, ("FEMALE", f"Ab hum Question Number {qnum} par aate hain."))
        lines.append(("MALE", f"Question Number {qnum} ki discussion yahin complete hoti hai."))
        all_lines.extend(lines)
        if progress_cb is not None:
            await progress_cb(i, total)
    return all_lines

def _extract_tagged_questions(source: str) -> list[tuple[int, str]]:
    """Extract the exact question numbers selected by the PDF picker."""
    matches = list(re.finditer(r"(?ms)^\s*\[QUESTION\s+(\d+)\]\s*\n(.*?)(?=^\s*\[QUESTION\s+\d+\]\s*$|\Z)", source, re.I))
    if matches:
        return [(int(m.group(1)), m.group(2).strip()) for m in matches if m.group(2).strip()]
    return _extract_question_blocks(source)


def _script_prompt(source: str, mode: str, label: str) -> str:
    if mode == "question":
        task = (
            f"This is question material ({label}). Explain every selected question using the exact rule below.\n"
            f"{QUESTION_RULES}"
        )
    else:
        task = (
            f"This is study/content material ({label}). Explain all selected material in a coherent sequence, "
            "with context, mechanisms, consequences, examples and UPSC relevance."
        )
    return f"{SCRIPT_RULES}\n{task}\n\nSOURCE MATERIAL:\n{source}\n"


def _parse_dialogue(raw: str) -> list[tuple[str, str]]:
    out = []
    for line in raw.splitlines():
        m = re.match(r"\s*\[(FEMALE|MALE)\]\s*[:\-]?\s*(.+)", line, re.I)
        if m and m.group(2).strip():
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


# ---------------------------------------------------------------------------
# Per-user Gemini API keys (encrypted at rest, server-side only)
# ---------------------------------------------------------------------------

def _key_repo() -> PodcastKeyRepository:
    """Seam returning the key repository (patched in tests)."""
    return PodcastKeyRepository(get_db())


def _quiz_repo() -> QuizRepository:
    """Seam returning the quiz repository (patched in tests)."""
    return QuizRepository(get_db())


async def _load_user_key(uid: int) -> Optional[str]:
    """Return the user's decrypted Gemini key, or None when unusable.

    Missing rows, DB blips and undecryptable blobs (e.g. after a master
    secret rotation) all map to None -- the caller then asks for a key.
    Plaintext keys are never logged here.

    Compatibility (Phase 6 fix): if the per-user podcast key is missing or
    undecryptable, we also check the generic AI-key store (AIKeyRepository)
    for a ``gemini`` provider key (users who used ``/setkey gemini`` before
    the podcast feature) and finally the legacy ``GEMINI_API_KEY`` env var
    (shared fallback for VPS deployments that still carry it). Per-user
    podcast keys always win; fallbacks are never logged and never stored
    into the podcast collection.
    """
    # 1) Primary: per-user encrypted podcast key
    try:
        enc = await _key_repo().get_encrypted(uid)
    except Exception:
        logger.exception("podcast key lookup failed for user %s", uid)
        return None
    if enc:
        try:
            return decrypt_api_key(enc)
        except ValueError:
            logger.warning("podcast key undecryptable for user %s (re-set needed)", uid)
            # fall through to compatibility checks rather than returning None
        except Exception:
            logger.warning("podcast key undecryptable for user %s (re-set needed)", uid)
            # fall through

    # 2) Compatibility: generic AI keys (``/setkey gemini``)
    try:
        akeys = await AIKeyRepository(get_db()).list_for_provider(uid, "gemini")
        if akeys:
            # AIKeyRepository stores api_key as plaintext; take the first
            # (round-robin ordering) and treat it as the user's Gemini key.
            candidate = (akeys[0].get("api_key") or "").strip()
            if candidate and len(candidate) >= 8:
                return candidate
    except Exception:
        logger.debug("podcast AIKey fallback lookup failed for user %s", uid, exc_info=True)

    # 3) Compatibility: legacy shared env key (server-wide fallback)
    try:
        env_key = (getattr(config, "GEMINI_API_KEY", None) or "").strip()
        if env_key and len(env_key) >= 8:
            return env_key
    except Exception:
        pass

    # No key available from any source
    if enc:
        # We had an unreadable per-user blob and no viable fallback
        return None
    return None


async def _send_key_prompt(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                           uid: int, prefix: str = "") -> None:
    await safe_send_message(
        ctx, chat_id,
        f"{prefix}🎙️ Podcast बनाने के लिए पहले अपनी Gemini API Key सेट करें।\n\n"
        "🔑 Aapki key sirf aapke podcasts ke liye use hogi aur securely save rahegi.\n"
        "🆓 Free key: Google AI Studio (aistudio.google.com) se le sakte hain.",
        reply_markup=_key_prompt_kb(uid),
    )


async def _send_main_menu(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                          uid: int, api_key: str) -> None:
    await safe_send_message(
        ctx, chat_id,
        "🎙️ <b>Journey for लबासना Podcast</b>\n\n"
        f"🔑 Key set: <code>{mask_api_key(api_key)}</code>\n\n"
        "📚 Source chunein:\n"
        "• <b>Test Series</b> — apne quiz se question range ka podcast\n"
        "• <b>PDF</b> — Question/Content PDF (max 10)\n"
        "• <b>Topic/Text</b> — kisi bhi topic par podcast",
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_kb(uid),
    )


async def _generate_podcast(uid: int, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE,
                            api_key: str, source: str, mode: str, label: str) -> None:
    status = await safe_send_message(ctx, chat_id, "🎙️ Podcast script तैयार किया जा रहा है...")
    out = str(config.TEMP_DIR / f"podcast_{uid}_{os.getpid()}_{abs(hash(label)) % 10**8}.mp3")
    parts: list[str] = []
    try:
        async def on_script(i: int, n: int) -> None:
            if n <= 10 or i == 1 or i == n or i % 10 == 0:
                await _edit_status(status, f"🎙️ Podcast script तैयार किया जा रहा है...\nQuestion {i}/{n}")

        if mode == "question":
            raw_questions = _extract_tagged_questions(source)
            questions = raw_questions
            if not questions:
                raise RuntimeError("No questions found in the selected source.")
            lines = await _generate_question_script(uid, questions, api_key, progress_cb=on_script)
        else:
            prompt = _script_prompt(source, mode, label)
            raw = await _gemini_generate(prompt, api_key, max_tokens=min(12000, max(5000, len(source) // 2)))
            lines = _parse_dialogue(raw)
            if not lines:
                raise RuntimeError("AI did not return a valid two-host dialogue.")
        # Brand promo is spoken first, then the educational script, then the outro.
        lines = _assemble_episode(uid, lines)

        await _edit_status(status, "🎧 Voices generate की जा रही हैं...")

        async def on_audio(stage: str) -> None:
            if stage == "audio":
                await _edit_status(status, "🔊 Audio तैयार किया जा रहा है...")

        await _gemini_tts_and_merge(lines, out, api_key, progress_cb=on_audio)
        parts = await _split_audio_if_needed(out)
        await _edit_status(status, "✅ Podcast तैयार है.")
        total = len(parts)
        for i, part in enumerate(parts, 1):
            caption = f"🎧 Part {i}/{total} — {label}"[:200] if total > 1 else None
            with open(part, "rb") as fh:
                await ctx.bot.send_audio(
                    chat_id=chat_id, audio=fh,
                    title="Journey for लबासना — Podcast",
                    performer="Journey for लबासना",
                    caption=caption,
                )
    except GeminiRequestError as exc:
        logger.warning("podcast gemini failure kind=%s", exc.kind)
        if exc.kind == "invalid_key":
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass
            await _send_key_prompt(
                ctx, chat_id, uid,
                prefix="❌ Aapki saved Gemini API key kaam nahi kar rahi. Nayi key set karein:\n\n",
            )
        else:
            await _edit_status(status, f"❌ {exc.user_msg}")
    except Exception as exc:
        # Redacted on purpose: tracebacks must never carry the caller's key.
        logger.error("Podcast generation failed: %s",
                     redact_secrets(str(exc), [api_key]))
        await _edit_status(status, _user_facing_error(exc, secrets=[api_key]))
    finally:
        for path in {out, *parts}:
            try:
                os.remove(path)
            except OSError:
                pass


async def _start_pdf_processing(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, doc) -> None:
    """Download, classify and present selection controls for a PDF source."""
    chat_id = update.effective_chat.id
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if not name.endswith(".pdf") and mime != "application/pdf":
        await safe_send_message(
            ctx, chat_id,
            "❌ Podcast source ke liye PDF upload करें, या text/topic भेजें."
        )
        return

    status = await safe_send_message(
        ctx, chat_id,
        "📥 PDF पढ़ रहा हूँ और automatically questions/content detect कर रहा हूँ..."
    )
    path = config.TEMP_DIR / f"pod_source_{uid}_{os.getpid()}.pdf"
    try:
        f = await ctx.bot.get_file(doc.file_id)
        data = await f.download_as_bytearray()
        path.write_bytes(bytes(data))

        pages = await asyncio.to_thread(_extract_pdf_pages, str(path))
        text = "\n\n".join(f"[PAGE {i+1}]\n{page}" for i, page in enumerate(pages))
        mode = "question" if _looks_like_questions(text) else "content"
        questions = _extract_question_blocks(text) if mode == "question" else []
        note = ""
        if mode == "question" and not questions:
            # Clearly report the detection problem instead of misfiring.
            mode = "content"
            note = "\n⚠️ Saaf questions detect nahi hue, isliye <b>content mode</b> me le raha hoon."

        # Keep the extracted pages in the session so custom selection can be
        # non-contiguous (e.g. 1,3,7,8,10) without losing source order.
        PODCAST_SESSIONS[uid] = {
            "step": "select",
            "mode": mode,
            "pages": pages,
            "questions": questions,
            "path": str(path),
            "chat_id": chat_id,
        }
        kind = "question" if mode == "question" else "content"
        if mode == "question":
            detail = (
                f"❓ Detected questions: <b>{len(questions)}</b>\n"
                "🎯 Default: first 10 questions"
            )
        else:
            detail = "📖 Default: first 10 pages"

        if status:
            await status.edit_text(
                f"🔎 <b>Detected:</b> {'Question PDF' if mode == 'question' else 'Study/Content PDF'}\n\n"
                f"📄 Total pages: <b>{len(pages)}</b>\n{detail}{note}\n\n"
                "✏️ Custom के लिए <code>1,3,7,8,9,10,13</code> जैसे अधिकतम 10 numbers दें.",
                parse_mode=ParseMode.HTML,
                reply_markup=_selection_kb(uid, kind),
            )
    except Exception as exc:
        logger.exception("Podcast PDF processing failed")
        await safe_send_message(ctx, chat_id, f"❌ PDF read failed: {redact_secrets(str(exc))[:300]}")
        path.unlink(missing_ok=True)
        PODCAST_SESSIONS.pop(uid, None)


async def podcast_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start podcast generation.

    Supports all of these user-friendly forms:
      /podcast
      /podcast <topic or text>
      /podcast as a reply to a text message
      /podcast as a reply to a PDF document

    A per-user Gemini API key is required for every form; when missing the
    user is asked to set it once (then it is reused automatically).
    """
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    PODCAST_SESSIONS.pop(uid, None)

    # If /podcast is sent as a reply, use the replied source immediately.
    replied = update.message.reply_to_message if update.message else None
    if replied:
        if replied.document:
            api_key = await _load_user_key(uid)
            if not api_key:
                await _send_key_prompt(ctx, chat_id, uid)
                return
            await _start_pdf_processing(update, ctx, uid, replied.document)
            return
        if replied.text and replied.text.strip():
            api_key = await _load_user_key(uid)
            if not api_key:
                await _send_key_prompt(ctx, chat_id, uid)
                return
            await _generate_podcast(
                uid, chat_id, ctx, api_key, replied.text.strip(), "content", "replied text"
            )
            return

    # If text is supplied in the same /podcast command, do NOT show the
    # instructions again — start generation immediately.
    # Parse the raw command text as a fallback to ctx.args. This is
    # deliberately robust for mobile Telegram clients and /podcast@botname.
    raw_command = (update.message.text or "").strip() if update.message else ""
    command_text = " ".join(ctx.args).strip()
    if not command_text and raw_command:
        command_text = re.sub(r"^/podcast(?:@[A-Za-z0-9_]+)?\s*", "", raw_command, count=1, flags=re.I).strip()
    if command_text:
        api_key = await _load_user_key(uid)
        if not api_key:
            await _send_key_prompt(ctx, chat_id, uid)
            return
        await _generate_podcast(
            uid, chat_id, ctx, api_key, command_text, "content", "topic/text"
        )
        return

    # Bare /podcast: key gate, then the source menu.
    api_key = await _load_user_key(uid)
    if not api_key:
        await _send_key_prompt(ctx, chat_id, uid)
        return
    await _send_main_menu(ctx, chat_id, uid, api_key)


async def podcast_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a PDF sent after bare /podcast."""
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "source" or not update.message or not update.message.document:
        return
    if not await _load_user_key(uid):
        PODCAST_SESSIONS.pop(uid, None)
        await _send_key_prompt(ctx, update.effective_chat.id, uid)
        return
    await _start_pdf_processing(update, ctx, uid, update.message.document)


async def podcast_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle topic/text sent after bare /podcast."""
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "source" or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text:
        return
    api_key = await _load_user_key(uid)
    PODCAST_SESSIONS.pop(uid, None)
    if not api_key:
        await _send_key_prompt(ctx, update.effective_chat.id, uid)
        return
    await _generate_podcast(uid, update.effective_chat.id, ctx, api_key, text, "content", "topic/text")


async def podcast_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    uid = int(parts[2])
    if q.from_user.id != uid:
        await q.answer("❌ Not your session", show_alert=True)
        return
    sess = PODCAST_SESSIONS.get(uid)
    if not sess:
        await q.message.edit_text("❌ Session expired. /podcast again")
        return
    action = parts[1]
    if action == "cancel":
        path = sess.get("path")
        PODCAST_SESSIONS.pop(uid, None)
        if path:
            Path(path).unlink(missing_ok=True)
        await q.message.edit_text("❌ Podcast cancelled.")
        return

    mode = sess.get("mode", "content")
    if action == "default":
        api_key = await _load_user_key(uid)
        if not api_key:
            path = sess.get("path")
            PODCAST_SESSIONS.pop(uid, None)
            if path:
                Path(path).unlink(missing_ok=True)
            await _send_key_prompt(ctx, q.message.chat_id, uid)
            return
        if mode == "question":
            items = sess.get("questions") or _extract_question_blocks("\n\n".join(sess.get("pages", [])))
            chosen = items[:MAX_SELECTION]
            if not chosen:
                await q.message.edit_text("❌ कोई question detect नहीं हुआ. कृपया दूसरा PDF दें.")
                PODCAST_SESSIONS.pop(uid, None)
                return
            source = "\n\n".join(f"[QUESTION {num}]\n{block}" for num, block in chosen)
            label = "questions " + ",".join(str(num) for num, _ in chosen)
            count = len(chosen)
            shortfall = f"\nℹ️ PDF me kul {len(items)} questions mile — sab use kar raha hoon." if len(items) <= MAX_SELECTION else ""
        else:
            pages = sess.get("pages", [])
            selected = list(range(1, min(MAX_SELECTION, len(pages)) + 1))
            if not selected:
                await q.message.edit_text("❌ PDF में readable text नहीं मिला.")
                PODCAST_SESSIONS.pop(uid, None)
                return
            source = "\n\n".join(f"[PAGE {i}]\n{pages[i-1]}" for i in selected)
            label = f"pages {selected[0]}-{selected[-1]}"
            count = len(selected)
            shortfall = f"\nℹ️ PDF me kul {len(pages)} pages — sab use kar raha hoon." if len(pages) <= MAX_SELECTION else ""
        path = sess.get("path")
        PODCAST_SESSIONS.pop(uid, None)
        if path:
            Path(path).unlink(missing_ok=True)
        await q.message.edit_text(f"✅ Selected default {count} {'questions' if mode == 'question' else 'pages'}.{shortfall}\n🎙️ Generating...")
        await _generate_podcast(uid, q.message.chat_id, ctx, api_key, source, mode, label)
        return

    if action == "custom":
        sess["step"] = "custom"
        await q.message.edit_text(
            "✏️ Custom selection भेजें. उदाहरण: <code>1,3,7,8,9,10,13</code>\n"
            "अधिकतम 10 questions/pages.",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_kb(uid),
        )


async def podcast_custom_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "custom" or not update.message or not update.message.text:
        return
    api_key = await _load_user_key(uid)
    if not api_key:
        path = sess.get("path")
        PODCAST_SESSIONS.pop(uid, None)
        if path:
            Path(path).unlink(missing_ok=True)
        await _send_key_prompt(ctx, update.effective_chat.id, uid)
        return
    try:
        mode = sess.get("mode", "content")
        if mode == "question":
            items = sess.get("questions") or _extract_question_blocks("\n\n".join(sess.get("pages", [])))
            available = {num: block for num, block in items}
            vals = _tokenize_numbers(update.message.text)
            if len(vals) > MAX_SELECTION:
                raise ValueError("Maximum 10 questions per podcast")
            missing = [n for n in vals if n not in available]
            if missing:
                raise ValueError(f"Question number(s) {', '.join(map(str, missing))} PDF में नहीं मिले")
            chosen_pairs = [(n, available[n]) for n in vals]
            source = "\n\n".join(f"[QUESTION {num}]\n{block}" for num, block in chosen_pairs)
            label = "questions " + ",".join(map(str, [n for n, _ in chosen_pairs]))
        else:
            pages = sess.get("pages", [])
            selected = _parse_selection(update.message.text, len(pages))
            source = "\n\n".join(f"[PAGE {i}]\n{pages[i-1]}" for i in selected)
            label = "pages " + ",".join(map(str, selected))
    except ValueError as exc:
        await safe_send_message(ctx, update.effective_chat.id, f"❌ {exc}")
        return
    path = sess.get("path")
    PODCAST_SESSIONS.pop(uid, None)
    if path:
        Path(path).unlink(missing_ok=True)
    await _generate_podcast(uid, update.effective_chat.id, ctx, api_key, source, mode, label)


# ---------------------------------------------------------------------------
# Key management callbacks + input
# ---------------------------------------------------------------------------

async def podcast_key_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    uid = int(parts[1])
    if q.from_user.id != uid:
        await q.answer("❌ Not your session", show_alert=True)
        return
    # Join the tail: "remove_yes" itself contains an underscore.
    action = "_".join(parts[2:])
    chat_id = q.message.chat_id

    if action in ("set", "change"):
        PODCAST_SESSIONS[uid] = {"step": "await_key", "chat_id": chat_id}
        await q.message.edit_text(
            "🔑 Apni Gemini API Key bhejein.\n\n"
            "✅ Validate hote hi securely save ho jayegi — dobara nahi maangi jayegi.\n"
            "🔒 Poori key kabhi display ya log nahi hogi.\n"
            "🗑️ Aapka key wala message turant delete kar diya jayega.\n\n"
            "🆓 Free key: Google AI Studio (aistudio.google.com).",
            reply_markup=_cancel_kb(uid),
        )
        return

    if action == "remove":
        await q.message.edit_text(
            "🗑️ Aapki saved Gemini API Key permanently delete ho jayegi. Pakka?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, remove", callback_data=f"podkey_{uid}_remove_yes"),
                 InlineKeyboardButton("◀️ Back", callback_data=f"podkey_{uid}_back")],
            ]),
        )
        return

    if action == "back":
        api_key = await _load_user_key(uid)
        PODCAST_SESSIONS.pop(uid, None)
        if api_key:
            await q.message.edit_text(
                "🎙️ <b>Journey for लबासना Podcast</b>\n\n"
                f"🔑 Key set: <code>{mask_api_key(api_key)}</code>\n\n📚 Source chunein:",
                parse_mode=ParseMode.HTML,
                reply_markup=_menu_kb(uid),
            )
        else:
            await q.message.edit_text(
                "🎙️ Podcast बनाने के लिए पहले अपनी Gemini API Key सेट करें।",
                reply_markup=_key_prompt_kb(uid),
            )
        return

    if action == "remove_yes":
        try:
            existed = await _key_repo().delete(uid)
        except Exception:
            logger.exception("podcast key delete failed for user %s", uid)
            await q.message.edit_text("❌ Key remove nahi ho payi. Dobara try karein.")
            return
        PODCAST_SESSIONS.pop(uid, None)
        logger.info("podcast key removed for user %s (existed=%s)", uid, existed)
        await q.message.edit_text(
            "🗑️ Aapki Gemini API Key remove kar di gayi.\n\n"
            "🎙️ Podcast बनाने के लिए पहले अपनी Gemini API Key सेट करें।",
            reply_markup=_key_prompt_kb(uid),
        )
        return


async def podcast_key_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a Gemini API key sent after Set/Change."""
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "await_key" or not update.message or not update.message.text:
        return
    api_key = update.message.text.strip()
    # Hygiene: delete the message carrying the secret (best effort).
    try:
        await update.message.delete()
    except Exception:
        pass
    if not api_key or len(api_key) < 8:
        await safe_send_message(
            ctx, update.effective_chat.id,
            "❌ Ye key bahut chhoti lag rahi hai. Poori Gemini API key bhejein.",
            reply_markup=_cancel_kb(uid),
        )
        return
    status = await safe_send_message(ctx, update.effective_chat.id, "🔑 Key validate ho rahi hai...")
    ok, message = await _validate_gemini_key(api_key)
    if not ok:
        await _edit_status(status, f"{message}\n\nSahi key bhejein ya ❌ Cancel karein.")
        if status is not None:
            try:
                await status.edit_reply_markup(reply_markup=_cancel_kb(uid))
            except Exception:
                pass
        return
    try:
        await _key_repo().save(uid, encrypt_api_key(api_key))
    except Exception:
        logger.exception("podcast key save failed for user %s", uid)
        await _edit_status(status, "❌ Key save nahi ho payi. Dobara try karein.")
        return
    PODCAST_SESSIONS.pop(uid, None)
    logger.info("podcast key saved for user %s", uid)
    await _edit_status(status, "✅ Gemini API Key save ho gayi!")
    await _send_main_menu(ctx, update.effective_chat.id, uid, api_key)


# ---------------------------------------------------------------------------
# Source menu + Test Series flow
# ---------------------------------------------------------------------------

async def podcast_menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    uid = int(parts[1])
    if q.from_user.id != uid:
        await q.answer("❌ Not your session", show_alert=True)
        return
    action = parts[2]
    chat_id = q.message.chat_id
    api_key = await _load_user_key(uid)
    if not api_key:
        PODCAST_SESSIONS.pop(uid, None)
        await q.message.edit_text(
            "🎙️ Podcast बनाने के लिए पहले अपनी Gemini API Key सेट करें।",
            reply_markup=_key_prompt_kb(uid),
        )
        return

    if action == "pdf":
        PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": chat_id}
        await q.message.edit_text(
            "📄 Apna <b>PDF upload karein</b>.\n\n"
            "🤖 Bot automatically pahchan lega: <b>Question PDF</b> ya <b>Study/Content PDF</b>.\n"
            "📌 Ek podcast me max <b>10 pages/questions</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_kb(uid),
        )
        return

    if action == "topic":
        PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": chat_id}
        await q.message.edit_text(
            "📝 Apna <b>topic ya text bhejein</b> — usi par detailed podcast banega.",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_kb(uid),
        )
        return

    if action == "ts":
        try:
            quizzes = await _quiz_repo().list_by_creator(uid)
        except Exception:
            logger.exception("podcast quiz list failed for user %s", uid)
            await q.message.edit_text("❌ Quiz list nahi mil payi. Dobara try karein.")
            return
        if not quizzes:
            await q.message.edit_text(
                "📚 Aapne abhi koi quiz nahi banaya.\n\n"
                "Pehle /create se quiz banayein, phir uska podcast banayein.",
                reply_markup=_cancel_kb(uid),
            )
            return
        shown = quizzes[:10]
        rows = [
            [InlineKeyboardButton(
                f"📝 {(quiz.get('quiz_name') or quiz.get('qid'))[:32]}",
                callback_data=f"podts_{uid}_{quiz.get('qid')}",
            )]
            for quiz in shown
        ]
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"pod_cancel_{uid}")])
        PODCAST_SESSIONS[uid] = {
            "step": "ts_pick",
            "chat_id": chat_id,
            "qids": [quiz.get("qid") for quiz in shown],
        }
        extra = f"\n(Showing latest 10 of {len(quizzes)}.)" if len(quizzes) > 10 else ""
        await q.message.edit_text(
            f"📚 Apna quiz chunein:{extra}",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return


def _quiz_question_block(pos: int, question: dict) -> str:
    stem = str(question.get("question", "") or "").strip()
    opts = [str(o) for o in question.get("options", []) if o]
    opt_lines = "\n".join(f"{chr(65 + i)}) {o}" for i, o in enumerate(opts))
    corr = question.get("correct_option_id", question.get("correct_option"))
    ids = list(corr) if isinstance(corr, (list, tuple)) else [corr]
    letters = sorted({
        chr(65 + int(i)) for i in ids
        if isinstance(i, (int, float)) and not isinstance(i, bool)
        and 0 <= int(i) < len(opts)
    })
    correct = ", ".join(letters) if letters else "Not specified"
    expl = str(question.get("explanation", "") or "").strip()
    return (
        f"Question: {stem or '—'}\n"
        f"Options:\n{opt_lines or '—'}\n"
        f"Correct Answer: {correct}\n"
        f"Explanation: {expl or 'Not provided'}"
    )


async def podcast_testseries_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    uid = int(parts[1])
    if q.from_user.id != uid:
        await q.answer("❌ Not your session", show_alert=True)
        return
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "ts_pick":
        await q.message.edit_text("❌ Session expired. /podcast again")
        return
    qid = parts[2]
    if qid not in (sess.get("qids") or []):
        await q.message.edit_text("❌ Ye quiz is session ka nahi hai. /podcast again")
        return
    try:
        quiz = await _quiz_repo().get(qid)
    except Exception:
        logger.exception("podcast quiz fetch failed for %s", qid)
        await q.message.edit_text("❌ Quiz load nahi ho paya. Dobara try karein.")
        return
    if not quiz or not quiz.get("questions"):
        await q.message.edit_text("❌ Is quiz me koi question nahi mila.")
        PODCAST_SESSIONS.pop(uid, None)
        return
    total = len(quiz["questions"])
    PODCAST_SESSIONS[uid] = {
        "step": "await_range",
        "chat_id": q.message.chat_id,
        "qid": qid,
        "qname": str(quiz.get("quiz_name") or qid),
        "count": total,
    }
    await q.message.edit_text(
        f"📚 <b>{quiz.get('quiz_name') or qid}</b> — {total} questions.\n\n"
        f"Question range bhejein, jaise <code>1-10</code> ya <code>5-15</code> "
        f"(max {TESTSERIES_MAX_QUESTIONS}).",
        parse_mode=ParseMode.HTML,
        reply_markup=_cancel_kb(uid),
    )


async def podcast_range_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a Test Series question range like 1-10 or 5-15."""
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "await_range" or not update.message or not update.message.text:
        return
    api_key = await _load_user_key(uid)
    if not api_key:
        PODCAST_SESSIONS.pop(uid, None)
        await _send_key_prompt(ctx, update.effective_chat.id, uid)
        return
    count = int(sess.get("count") or 0)
    try:
        vals = _tokenize_numbers(update.message.text)
    except ValueError as exc:
        await safe_send_message(ctx, update.effective_chat.id, f"❌ {exc}")
        return
    if len(vals) > TESTSERIES_MAX_QUESTIONS:
        await safe_send_message(
            ctx, update.effective_chat.id,
            f"❌ Ek podcast me max {TESTSERIES_MAX_QUESTIONS} questions. Chhoti range bhejein.",
        )
        return
    missing = [n for n in vals if n < 1 or n > count]
    if missing:
        await safe_send_message(
            ctx, update.effective_chat.id,
            f"❌ Question number(s) {', '.join(map(str, missing))} is quiz me nahi hain (1-{count}).",
        )
        return
    try:
        quiz = await _quiz_repo().get(sess["qid"])
    except Exception:
        logger.exception("podcast quiz fetch failed")
        await safe_send_message(ctx, update.effective_chat.id, "❌ Quiz load nahi ho paya. Dobara try karein.")
        return
    if not quiz or not quiz.get("questions"):
        await safe_send_message(ctx, update.effective_chat.id, "❌ Quiz me questions nahi mile.")
        PODCAST_SESSIONS.pop(uid, None)
        return
    questions = quiz["questions"]
    pairs = [(n, _quiz_question_block(n, questions[n - 1])) for n in vals]
    source = "\n\n".join(f"[QUESTION {num}]\n{block}" for num, block in pairs)
    label = f"{sess.get('qname')} Q" + ",".join(map(str, vals))
    PODCAST_SESSIONS.pop(uid, None)
    await safe_send_message(
        ctx, update.effective_chat.id,
        f"✅ {len(pairs)} questions selected ({', '.join(map(str, vals))}).\n🎙️ Generating...",
    )
    await _generate_podcast(uid, update.effective_chat.id, ctx, api_key, source, "question", label)


def register(application) -> None:
    application.add_handler(CommandHandler("podcast", podcast_command))
    application.add_handler(CallbackQueryHandler(podcast_callback, pattern=r"^pod_(?:default|custom|cancel)_"))
    application.add_handler(CallbackQueryHandler(podcast_key_callback, pattern=r"^podkey_"))
    application.add_handler(CallbackQueryHandler(podcast_menu_callback, pattern=r"^podmenu_"))
    application.add_handler(CallbackQueryHandler(podcast_testseries_callback, pattern=r"^podts_"))
    application.add_handler(MessageHandler(filters.Document.ALL, podcast_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, podcast_custom_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, podcast_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, podcast_key_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, podcast_range_text))
