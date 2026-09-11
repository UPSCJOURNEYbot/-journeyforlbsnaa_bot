"""Podcast generation for Journey for लबासना.

Accepts a topic/text or a PDF. PDFs are automatically classified as either
MCQ/question material or study/content material. A single generation is
limited to 10 questions or 10 pages; custom selection accepts a comma-separated
list such as ``1,3,7,8,9,10,13`` (maximum 10 items).
"""
from __future__ import annotations

import asyncio
import base64
import wave
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from quizbot.shared import config
from quizbot.runner_bot.telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

PODCAST_SESSIONS: dict[int, dict[str, Any]] = {}
MAX_SELECTION = 10
FEMALE_VOICE = "hi-IN-SwaraNeural"
MALE_VOICE = "hi-IN-MadhurNeural"

PROMO = (
    'Welcome to "Journey for लबासना" — aapka swagat hai! Ye sirf podcast nahi, '
    'UPSC preparation ko simple, practical aur memorable banane ki ek learning journey hai. '
    'Har topic ko context, real-life examples aur exam-oriented insights ke saath samjhiye. '
    'Chaliye, seekhte hain, samajhte hain aur लबासना ki journey ki taraf ek kadam aur badhate hain.'
)

SCRIPT_RULES = """
Create an educational two-host podcast in natural Hindi/Hinglish.
Hosts: [FEMALE] and [MALE]. Keep it conversational, clear, engaging and accurate.
The supplied source is authoritative for source-derived facts. Do not invent source claims.
Explain difficult terms immediately in simple language and add a clearly labelled real-life
example/analogy wherever it improves understanding. Connect relevant points to UPSC Prelims
and Mains without turning the podcast into a dry lecture.
Start with a natural 20–30 second brand intro in substance: welcome to "Journey for लबासना"; briefly say why this material/topic is important for UPSC and practical understanding.
Then explain why the selected material/topic matters for UPSC and practical understanding.
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
"""


def _selection_kb(uid: int, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Default 10", callback_data=f"pod_default_{uid}_{kind}"),
         InlineKeyboardButton("✏️ Custom", callback_data=f"pod_custom_{uid}_{kind}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"pod_cancel_{uid}")],
    ])


def _parse_selection(text: str, maximum: int) -> list[int]:
    text = text.strip().lower()
    if text in {"default", "all"}:
        return list(range(1, min(MAX_SELECTION, maximum) + 1))
    vals: list[int] = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if not part.isdigit():
            raise ValueError("Use numbers separated by commas, e.g. 1,3,7,8,9,10,13")
        n = int(part)
        if n < 1 or n > maximum:
            raise ValueError(f"Number {n} is outside 1-{maximum}")
        if n not in vals:
            vals.append(n)
    if not vals:
        raise ValueError("No selection supplied")
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


def _looks_like_questions(text: str) -> bool:
    # Strong signals: numbered question stems plus option labels.
    qnums = len(re.findall(r"(?m)^\s*(?:Q(?:uestion)?\s*)?\d+\s*[.)\-:]\s+", text, re.I))
    opts = len(re.findall(r"(?m)^\s*[A-Da-d]\s*[.)\-:]\s+", text))
    answer = len(re.findall(r"(?im)^\s*(?:answer|correct answer|उत्तर|सही उत्तर)\s*[:：]", text))
    return (qnums >= 2 and opts >= 4) or (qnums >= 2 and answer >= 1)


def _extract_question_blocks(text: str) -> list[tuple[int, str]]:
    """Return (source question number, full question block)."""
    starts = list(re.finditer(r"(?m)^\s*(?:Q(?:uestion)?\s*)?(\d+)\s*[.)\-: ]\s+", text, re.I))
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


def _gemini_client():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing in .env")
    from google import genai
    return genai.Client(api_key=key)


async def _gemini_generate(prompt: str, max_tokens: int = 4096) -> str:
    """Generate podcast script text directly through the Gemini API key in .env."""
    def call() -> str:
        from google.genai import types
        client = _gemini_client()
        response = client.models.generate_content(
            model=GEMINI_TEXT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.7,
            ),
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty response.")
        return text
    return await asyncio.to_thread(call)


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


async def _gemini_tts_chunk(lines: list[tuple[str, str]], wav_path: str) -> None:
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

    def call() -> bytes:
        from google.genai import types
        client = _gemini_client()
        response = client.models.generate_content(
            model=GEMINI_TTS_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    language_code="hi-IN",
                    multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                        speaker_voice_configs=[
                            types.SpeakerVoiceConfig(
                                speaker="Female Host",
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                                ),
                            ),
                            types.SpeakerVoiceConfig(
                                speaker="Male Host",
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
                                ),
                            ),
                        ]
                    ),
                ),
            ),
        )
        return _pcm_from_response(response)

    pcm = await asyncio.to_thread(call)
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)


async def _gemini_tts_and_merge(lines: list[tuple[str, str]], out_path: str) -> None:
    """Use Gemini 2.5 Flash TTS for two-host audio, chunked for its input limit."""
    work = Path(tempfile.mkdtemp(prefix="journey_gemini_tts_", dir=str(config.TEMP_DIR)))
    try:
        # Keep each request comfortably below the 8,192-token TTS input limit.
        chunks: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        chars = 0
        for line in lines:
            line_chars = len(line[1]) + 30
            if current and chars + line_chars > 18000:
                chunks.append(current)
                current = []
                chars = 0
            current.append(line)
            chars += line_chars
        if current:
            chunks.append(current)

        wavs: list[Path] = []
        for i, chunk in enumerate(chunks):
            wav = work / f"{i:04d}.wav"
            await _gemini_tts_chunk(chunk, str(wav))
            wavs.append(wav)

        concat = work / "concat.txt"
        concat.write_text(
            "".join(f"file '{p.as_posix().replace(chr(39), chr(39)+chr(92)+chr(39))}'\n" for p in wavs),
            encoding="utf-8",
        )
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c:a", "libmp3lame", "-b:a", "128k", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Gemini audio merge failed: {err.decode(errors='ignore')[-500:]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _generate_question_script(uid: int, questions: list[tuple[int, str]]) -> list[tuple[str, str]]:
    """Generate each question as a separately numbered segment so numbering is never skipped."""
    all_lines: list[tuple[str, str]] = []
    for qnum, question in questions:
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

{QUESTION_RULES}

Return ONLY dialogue lines prefixed [FEMALE] or [MALE].
Use natural Hindi/Hinglish, conversational Q&A between a female and male host.

SOURCE — QUESTION {qnum}:
{question}
"""
        raw = await _gemini_generate(prompt, max_tokens=min(5000, max(2200, len(question) // 2)))
        lines = _parse_dialogue(raw)
        if not lines:
            raise RuntimeError(f"AI did not return valid dialogue for Question {qnum}.")
        # Hard guarantee of an audible question-number marker even if the model omits it.
        lines.insert(0, ("FEMALE", f"Ab hum Question Number {qnum} par aate hain."))
        lines.append(("MALE", f"Question Number {qnum} ki discussion yahin complete hoti hai."))
        all_lines.extend(lines)
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


async def _generate_podcast(uid: int, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, source: str, mode: str, label: str) -> None:
    status = await safe_send_message(ctx, chat_id, "🎙️ <b>Podcast script तैयार हो रहा है...</b>\nAI source ko explain + real-life examples ke saath convert kar raha hoon.", parse_mode=ParseMode.HTML)
    try:
        if mode == "question":
            raw_questions = _extract_tagged_questions(source)
            questions = raw_questions
            lines = await _generate_question_script(uid, questions)
        else:
            prompt = _script_prompt(source, mode, label)
            raw = await _gemini_generate(prompt, max_tokens=min(12000, max(5000, len(source) // 2)))
            lines = _parse_dialogue(raw)
            if not lines:
                raise RuntimeError("AI did not return a valid two-host dialogue.")
        # Brand promo is spoken first, then the generated educational script.
        lines = [("FEMALE", PROMO)] + lines
        out = str(config.TEMP_DIR / f"podcast_{uid}_{os.getpid()}_{abs(hash(label)) % 10**8}.mp3")
        await _gemini_tts_and_merge(lines, out)
        if status:
            try:
                await status.edit_text("🎧 <b>Podcast तैयार है!</b>\n\n📚 Source explained with examples + UPSC-oriented understanding.", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        with open(out, "rb") as fh:
            await ctx.bot.send_audio(chat_id=chat_id, audio=fh, title="Journey for लबासना — Podcast", performer="Journey for लबासना")
        os.remove(out)
    except Exception as exc:
        logger.exception("Podcast generation failed")
        if status:
            try:
                await status.edit_text(f"❌ Podcast generation failed: {str(exc)[:500]}")
            except Exception:
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
                f"📄 Total pages: <b>{len(pages)}</b>\n{detail}\n\n"
                "✏️ Custom के लिए <code>1,3,7,8,9,10,13</code> जैसे अधिकतम 10 numbers दें.",
                parse_mode=ParseMode.HTML,
                reply_markup=_selection_kb(uid, kind),
            )
    except Exception as exc:
        logger.exception("Podcast PDF processing failed")
        await safe_send_message(ctx, chat_id, f"❌ PDF read failed: {str(exc)[:300]}")
        path.unlink(missing_ok=True)
        PODCAST_SESSIONS.pop(uid, None)


async def podcast_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start podcast generation.

    Supports all of these user-friendly forms:
      /podcast
      /podcast <topic or text>
      /podcast as a reply to a text message
      /podcast as a reply to a PDF document
    """
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    PODCAST_SESSIONS.pop(uid, None)

    # If /podcast is sent as a reply, use the replied source immediately.
    replied = update.message.reply_to_message if update.message else None
    if replied:
        if replied.document:
            await _start_pdf_processing(update, ctx, uid, replied.document)
            return
        if replied.text and replied.text.strip():
            await _generate_podcast(
                uid, chat_id, ctx, replied.text.strip(), "content", "replied text"
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
        await _generate_podcast(
            uid, chat_id, ctx, command_text, "content", "topic/text"
        )
        return

    # Bare /podcast: enter source-selection state and wait for the next text/PDF.
    PODCAST_SESSIONS[uid] = {"step": "source", "chat_id": chat_id}
    await safe_send_message(
        ctx, chat_id,
        "🎙️ <b>Journey for लबासना Podcast</b>\n\n"
        "अब <b>Topic/text भेजें</b> या <b>PDF upload करें</b>.\n\n"
        "📚 PDF को bot automatically पहचान लेगा कि वह <b>Question PDF</b> है या <b>Study/Content PDF</b>.\n"
        "📌 एक podcast में अधिकतम <b>10 pages</b> या <b>10 questions</b> होंगे.\n"
        "✏️ Custom selection: <code>1,3,7,8,9,10,13</code> (अधिकतम 10).\n\n"
        "📣 हर podcast की शुरुआत 20–30 sec के <b>Journey for लबासना</b> intro से होगी.",
        parse_mode=ParseMode.HTML,
    )


async def podcast_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a PDF sent after bare /podcast."""
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "source" or not update.message or not update.message.document:
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
    PODCAST_SESSIONS.pop(uid, None)
    await _generate_podcast(uid, update.effective_chat.id, ctx, text, "content", "topic/text")


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
        path = sess.get("path")
        PODCAST_SESSIONS.pop(uid, None)
        if path:
            Path(path).unlink(missing_ok=True)
        await q.message.edit_text(f"✅ Selected default {count} {'questions' if mode == 'question' else 'pages'}.\n🎙️ Generating...")
        await _generate_podcast(uid, q.message.chat_id, ctx, source, mode, label)
        return

    if action == "custom":
        sess["step"] = "custom"
        await q.message.edit_text(
            "✏️ Custom selection भेजें. उदाहरण: <code>1,3,7,8,9,10,13</code>\n"
            "अधिकतम 10 questions/pages.", parse_mode=ParseMode.HTML
        )


async def podcast_custom_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    sess = PODCAST_SESSIONS.get(uid)
    if not sess or sess.get("step") != "custom" or not update.message or not update.message.text:
        return
    try:
        mode = sess.get("mode", "content")
        if mode == "question":
            items = sess.get("questions") or _extract_question_blocks("\n\n".join(sess.get("pages", [])))
            available = {num: block for num, block in items}
            raw = update.message.text.strip()
            vals = []
            for part in re.split(r"[,\s]+", raw):
                if not part or not part.isdigit():
                    raise ValueError("Question numbers comma se दें, जैसे 1,3,7,8,10")
                n = int(part)
                if n < 1:
                    raise ValueError("Question number 1 या उससे बड़ा होना चाहिए")
                if n not in vals:
                    vals.append(n)
            if not vals:
                raise ValueError("कोई question number नहीं दिया गया")
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
    await _generate_podcast(uid, update.effective_chat.id, ctx, source, mode, label)


def register(application) -> None:
    application.add_handler(CommandHandler("podcast", podcast_command))
    application.add_handler(CallbackQueryHandler(podcast_callback, pattern=r"^pod_(?:default|custom|cancel)_"))
    application.add_handler(MessageHandler(filters.Document.ALL, podcast_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, podcast_custom_text))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, podcast_text))
