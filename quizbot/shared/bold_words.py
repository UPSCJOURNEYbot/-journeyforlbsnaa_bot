"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

"""Automatic important-word bold formatting for quiz text.

Given a *plain-text* question stem, statement or explanation, this module
selects a balanced handful of genuinely important terms and wraps them in
``<b>...</b>`` so the existing Telegram quiz presentation highlights the
words that matter.

Design rules (fixed "balanced" profile):

* **Content is never altered.**  Unescaping + de-tagging the output yields
  exactly the input text; only ``<b>`` spans are added around whole words.
* **Balanced density.**  At most ``MAX_BOLD_SPANS`` bold spans per message
  (approximately 3-6 on typical questions) and at most
  ``MAX_BOLD_RATIO`` (35%) of the characters wrapped, so the text never
  looks like a highlighter explosion.
* **Multilingual.**  English, Hindi (Devanagari) and Hinglish (romanised
  Hindi) are supported via per-script stop-word lists, a shared UPSC-ish
  keyword booster and script-aware length scoring.
* **HTML-safe.**  All non-bold text passes through ``html.escape`` first,
  so ``&``, ``<`` and ``>`` can never form accidental markup.  Text that
  already contains markup is returned *unchanged* (never escaped, never
  re-bolded) so existing formatting is preserved exactly.
* **Framework-agnostic.**  No telegram/pyrogram imports; callers decide
  which parse mode to advertise based on the ``formatted`` flag.

The Gemini/AI question-generation prompts are NOT touched by this module --
bolding is applied purely at presentation time.
"""

import html
import re
from typing import Optional

# --- Balanced profile -------------------------------------------------------

#: Never emit more than this many bold spans per message.
MAX_BOLD_SPANS = 6

#: Never wrap more than this fraction of the message's characters.
MAX_BOLD_RATIO = 0.35

#: Messages shorter than this are left un-bolded (nothing worth stressing).
MIN_TEXT_LEN = 25

#: Candidates must score at least this to be eligible.
MIN_SCORE = 4

# --- Tokenisation -----------------------------------------------------------

# Word tokens: Latin letters (incl. Latin-1 supplement/extended), digits and
# the Devanagari block (U+0900-U+097F, which includes vowel signs / marks),
# allowing internal apostrophes and hyphens ("India's", "co-operative").
# Plain ``\w`` cannot be used: Python's ``re`` does not treat Devanagari
# combining vowel signs as word characters and would shred Hindi words.
_WORD_RE = re.compile(
    r"[0-9A-Za-z\u00C0-\u024F\u0900-\u097F]+"
    r"(?:['\u2019\-][0-9A-Za-z\u00C0-\u024F\u0900-\u097F]+)*"
)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

# Any markup that is already present (HTML tags or HTML entities).  Text
# containing these is treated as authored rich text and passed through
# untouched.
_MARKUP_RE = re.compile(r"<[^<>]*>|&[A-Za-z#0-9]+;")

_SENTENCE_END = ".!?\n\r"

# --- Stop-words -------------------------------------------------------------

_EN_STOP = """
a an the is are was were be been being am do does did doing have has had having
of in on at to for from by with about against between into through during
before after above below up down out off over under again further then once
here there when where why how all any both each few more most other some such
no nor not only own same so than too very can will just should now could would
shall may might must i you he she it we they them his her its our their this
that these those which who whom whose what and or but if because as until
while also known called given follow following consider considered choose
correct incorrect true false statement statements question questions option
options answer answers example examples type types pair pairs match listed
below above among amongst regarding respect reference relation terms
regardless whether either neither both always never often sometimes
""".split()

_HI_STOP = """
के का की को में से पर और या एक दो तीन नहीं है हैं था थे थी क्या कौन कहाँ कब
क्यों कैसे लिए साथ द्वारा यह वह ये वे इस उस इन उन अपने अपनी सब कुछ भी ही तो न
हाँ जैसे जब तब होता होती होते करने करना किया गया गई गए हुआ हुई हुए रहे रहा रही
सकते सकता सकती चाहिए जाता जाती जाते माना करते करता करती दिया लिया होगा होगी
होंगे वाले वाली वाला कहा बताया प्रदान अनुसार संबंधित विषय निम्न उपरोक्त सही
गलत कथन विकल्प उत्तर प्रश्न उदाहरण प्रकार जोड़ी चुनें
""".split()

# Romanised (Hinglish) function words -- never important.
_HING_STOP = """
hai hain ho hoga hogi honge hota hoti hote karte karna karne karo kare kiya
kiye gaya gayi gaye hua hui hue rahe raha rahi sakte sakra chahiye chaiye
ka ki ke ko kaa mein me in se par aur athva ya ek do teen nahi nahin kya kyu
kyun kaun kahan kab kaise liye lie saath sath dwara dvara wala vala wali vali
ye woh vo yah is us un yeh unka unki unke mera meri hamare apna apni apne sab
kuch bhi hi to tho na ha haan jaise waise jahan vahan jab tab mana jata jati
diya liya hone kaa waghera raha chahe
""".split()

_STOPWORDS = frozenset(w.casefold() for w in (_EN_STOP + _HI_STOP + _HING_STOP))

# --- High-precision keyword booster (small, UPSC-oriented) -------------------

_KEYWORDS = frozenset(w.casefold() for w in """
constitution constitutional fundamental preamble republic secular sovereign
socialist parliament legislature judiciary executive federal amendment
ratification writ mandamus certiorari prohibition habeas quorum ordinance
panchayat municipality monarchy colonial imperialism nationalism
satyagraha swadeshi khilafat noncooperation civil disobedience dandi
mauryan gupta mughal vedic harappan indus chola pallava buddhist jain
monsoon cyclone jet ozone tectonic earthquake glacier peninsula plateau
delta estuary biodiversity ecosystem lithosphere atmosphere ionosphere
photosynthesis respiration chromosome mitochondria enzyme antigen antibody
vaccine antibiotic germination pollination transpiration catalyst
oxidation reduction sublimation distillation hydrocarbon polymer isotope
neutron proton electron quantum inflation deflation fiscal deficit revenue
tariff subsidy forex gdp gnp repo liquidity demographic census migration
संविधान मौलिक कर्तव्य नीति संसद संसदीय न्यायपालिका कार्यपालिका संशोधन
अध्यादेश पंचायत राजतंत्र उपनिवेश राष्ट्रवाद सत्याग्रह स्वदेशी स्वतंत्रता
मानसून चक्रवात ओजोन भूकंप हिमालय प्रायद्वीप पठार डेल्टा ज्वालामुखी
पारिस्थितिकी जैव विविधता प्रकाश संश्लेषण श्वसन गुणसूत्र माइटोकॉन्ड्रिया
एंजाइम प्रतिजन प्रतिपिंड टीका प्रतिजैविक ऑक्सीकरण उत्प्रेरक बहुलक समस्थानिक
मुद्रास्फीति राजकोषीय घाटा राजस्व प्रशुल्क सहायता जनगणना प्रवास
samvidhan maulik kartavya niti sansad sansadiya nyaypalika karyapalika
sanshodhan adhyadesh panchayat rajtantra rashtravad satyagrah swadeshi
swatantrata mausam chakravat ozone bhukamp himalay prayadweep pathar
arthvyavastha mudrasphiti rajkoshiy ghata rajasya janganna pravasan
""".split())

# --- Scoring -----------------------------------------------------------------


def _is_acronym(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,6}", tok))


def _sentence_start(text: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return True
    return text[i] in _SENTENCE_END


def _score_token(
    tok: str,
    *,
    sent_start: bool,
    prev_cap: bool,
    next_cap: bool,
) -> int:
    low = tok.casefold()
    dev = bool(_DEVANAGARI_RE.search(tok))
    n = len(tok)
    score = 0

    if _is_acronym(tok):
        score += 5  # UPSC, RBI, NITI, GDP ...
    elif any(ch.isdigit() for ch in tok):
        if tok.isdigit():
            # Years and other notable figures; tiny bare numbers are noise.
            score += 4 if 1000 <= int(tok) <= 2100 else 0
        else:
            score += 4  # mixed alphanumerics like 5G, Article370
    if dev:
        if n >= 6:
            score += 4
        elif n >= 4:
            score += 2
    else:
        if n >= 10:
            score += 4
        elif n >= 8:
            score += 3
        elif n >= 6:
            score += 2
        elif n == 5:
            score += 1

    if not dev and tok[0].isupper():
        # A capital mid-sentence usually signals a proper noun; at the start
        # of a sentence it may just be the sentence's first word.
        score += 1 if sent_start else 3
        if prev_cap or next_cap:
            score += 1  # part of a capitalised chain ("Supreme Court")

    if low in _KEYWORDS:
        score += 3

    return score


def _candidates(text: str) -> list[tuple[int, int, int]]:
    """Best (score, start, end) per distinct word, ignoring stop-words.

    A word that appears several times is only ever bolded once -- at its
    highest-scoring occurrence (ties keep the earliest).
    """
    matches = list(_WORD_RE.finditer(text))
    caps = [bool(m.group(0)) and m.group(0)[0].isupper() for m in matches]
    best: dict[str, tuple[int, int, int]] = {}
    for i, m in enumerate(matches):
        tok = m.group(0)
        low = tok.casefold()
        if low in _STOPWORDS:
            continue
        score = _score_token(
            tok,
            sent_start=_sentence_start(text, m.start()),
            prev_cap=i > 0 and caps[i - 1],
            next_cap=i + 1 < len(matches) and caps[i + 1],
        )
        if score < 1:
            continue
        start, end = m.start(), m.end()
        prev = best.get(low)
        if prev is None or score > prev[0]:
            best[low] = (score, start, end)
    return list(best.values())


def _pick_spans(text: str, cands: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    budget = MAX_BOLD_RATIO * len(text)
    picked: list[tuple[int, int, int]] = []  # (score, start, end)
    used = 0
    for score, start, end in sorted(cands, key=lambda c: (-c[0], c[1])):
        if len(picked) >= MAX_BOLD_SPANS:
            break
        if used + (end - start) > budget:
            continue
        picked.append((score, start, end))
        used += end - start

    # Merge directly adjacent picks ("Article" + "370") into one span; the
    # joining space then counts towards the budget too.
    merged: list[tuple[int, int, int]] = []
    for score, start, end in sorted(picked, key=lambda p: p[1]):
        if merged and start == merged[-1][2] + 1 and text[merged[-1][2]] == " ":
            merged[-1] = (merged[-1][0], merged[-1][1], end)
        else:
            merged.append((score, start, end))

    # Enforce the ratio strictly, even after merges: drop the weakest spans
    # until the wrapped character count fits.
    while merged and sum(e - s for _, s, e in merged) > budget:
        merged.remove(min(merged, key=lambda p: (p[0], p[2])))

    return [(s, e) for _, s, e in merged]


# --- Public API ---------------------------------------------------------------


def bold_important_words(text: str) -> str:
    """Return *text* HTML-escaped with balanced ``<b>`` spans around the
    genuinely important words.  Plain text in -> HTML out.

    Text that already contains markup (tags or entities) is returned
    **unchanged** so existing formatting is preserved exactly and never
    double-escaped.
    """
    if not text:
        return text
    if _MARKUP_RE.search(text):
        return text  # authored rich text -- leave exactly as-is

    stripped = text.strip()
    if len(stripped) < MIN_TEXT_LEN:
        return html.escape(text, quote=False)

    spans = _pick_spans(text, _candidates(text))
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(html.escape(text[last:start], quote=False))
        out.append("<b>")
        out.append(html.escape(text[start:end], quote=False))
        out.append("</b>")
        last = end
    out.append(html.escape(text[last:], quote=False))
    return "".join(out)


def format_bold_html(text: str) -> tuple[str, bool]:
    """Convenience wrapper: ``(html_text, formatted)``.

    ``formatted`` is False only when *text* already contained markup and was
    therefore passed through untouched -- callers must not advertise an HTML
    parse mode in that case (the original text may not be valid HTML).
    """
    if not text:
        return text, False
    if _MARKUP_RE.search(text):
        return text, False
    return bold_important_words(text), True


def apply_bold_to_poll_fields(
    poll_question: str,
    poll_explanation: Optional[str],
    *,
    skip_question: bool = False,
) -> tuple[str, Optional[str], dict[str, str]]:
    """Apply important-word bolding to the two poll fields that support a
    parse mode (question stem and quiz explanation).

    Returns ``(question, explanation, parse_mode_kwargs)`` where
    ``parse_mode_kwargs`` contains ``question_parse_mode`` /
    ``explanation_parse_mode`` **only** for fields this module actually
    re-formatted (never for pass-through text, so pre-existing markup keeps
    its current behaviour).  Options are intentionally never touched.
    """
    extra: dict[str, str] = {}
    if not skip_question:
        q, ok = format_bold_html(poll_question)
        if ok:
            poll_question, extra["question_parse_mode"] = q, "HTML"
    if poll_explanation:
        e, ok = format_bold_html(poll_explanation)
        if ok:
            poll_explanation, extra["explanation_parse_mode"] = e, "HTML"
    return poll_question, poll_explanation, extra
