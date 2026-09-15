"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.

Canonical MCQ parser (Phase 1): one implementation for pasted text,
``.txt``/``.md``/``.markdown`` uploads, extracted PDF text and OCR output.

The parser never guesses an answer: every rejected block carries a
structured reason (see ``RE_*`` codes) so callers can report processed vs
skipped questions honestly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_LATEX_MAP_NOT_USED = None  # (LaTeX/markdown cleanup lives in shared.utils.text)

# ---------------------------------------------------------------------------
# Structured result / reject codes
# ---------------------------------------------------------------------------
RE_EMPTY = "empty"
RE_MISSING_QUESTION = "missing_question"
RE_INSUFFICIENT_OPTIONS = "insufficient_options"
RE_TOO_MANY_OPTIONS = "too_many_options"
RE_MISSING_ANSWER = "missing_answer"
RE_UNRECOGNIZED_ANSWER = "unrecognized_answer"
RE_ANSWER_OUT_OF_RANGE = "answer_out_of_range"
RE_CONFLICTING_ANSWERS = "conflicting_answers"
RE_MULTIPLE_CORRECT_UNSUPPORTED = "multiple_correct_unsupported"
RE_ANSWER_KEY_ASSOCIATION = "answer_key_association"
RE_MALFORMED_ANSWER_KEY = "malformed_answer_key"

MAX_OPTIONS = 10


@dataclass
class SkippedBlock:
    """A rejected document block with a machine-readable reason."""
    reason: str
    detail: str = ""
    ordinal: Optional[int] = None
    snippet: str = ""


@dataclass
class BlockResult:
    """Strict parse result for one candidate question block."""
    question: Optional[dict]
    reason: Optional[str] = None
    detail: Optional[str] = None
    bad_tokens: list = field(default_factory=list)


@dataclass
class DocumentParseResult:
    questions: list[dict]
    skipped: list[SkippedBlock]
    answer_key_detected: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.questions)


# ---------------------------------------------------------------------------
# Markdown / emoji helpers (legacy surface, preserved)
# ---------------------------------------------------------------------------
def _is_emoji(char: str) -> bool:
    """Return True if `char` is a single emoji (excluding the ✅ marker)."""
    if char == "✅":
        return False
    cp = ord(char)
    if 0x1F300 <= cp <= 0x1FAFF:
        return True
    if 0x2600 <= cp <= 0x26FF:
        return True
    if 0x2700 <= cp <= 0x27BF:
        return True
    if 0xFE00 <= cp <= 0xFE0F:
        return True
    if 0x1F1E0 <= cp <= 0x1F1FF:
        return True
    if 0x231A <= cp <= 0x231B:
        return True
    if 0x23E9 <= cp <= 0x23F3:
        return True
    if 0x25AA <= cp <= 0x25FE:
        return True
    if 0x2614 <= cp <= 0x2615:
        return True
    if 0x2648 <= cp <= 0x2653:
        return True
    return False


def _line_is_emoji_separator(line: str) -> bool:
    """True if the line consists only of emoji/emoji-modifier characters."""
    s = line.strip()
    if not s:
        return False
    for ch in s:
        if ch in ("️", "‍", "︎"):
            continue
        if not _is_emoji(ch):
            return False
    return True


def clean_markdown(text: str) -> str:
    """Strip common markdown emphasis/headings from pasted question text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return text


_TABLE_LINE_RE = re.compile(r"^\|.*\|$")


def _pad_table_blocks(text: str) -> str:
    """Ensure blank lines surround any markdown-table block so tables
    render distinctly from surrounding prose."""
    if "|" not in text:
        return text
    lines = text.split("\n")
    segments: list[tuple[str, bool]] = []
    i, n = 0, len(lines)
    while i < n:
        if _TABLE_LINE_RE.match(lines[i].strip()):
            block = []
            while i < n and _TABLE_LINE_RE.match(lines[i].strip()):
                block.append(lines[i])
                i += 1
            segments.append(("\n".join(block), True))
        else:
            segments.append((lines[i], False))
            i += 1
    if not segments:
        return text
    out = segments[0][0]
    for k in range(1, len(segments)):
        prev_is_table = segments[k - 1][1]
        cur_is_table = segments[k][1]
        sep = "\n\n" if (prev_is_table or cur_is_table) else "\n"
        out += sep + segments[k][0]
    if segments[-1][1]:
        out += "\n\n"
    return out


# ---------------------------------------------------------------------------
# Label / keyword machinery
# ---------------------------------------------------------------------------
_LETTERS = [chr(c) for c in range(ord("A"), ord("J") + 1)]  # A-J (10)
_DEV_LABELS = list("कखगघङचछजझञ")           # exactly 10 valid Devanagari labels
_DEV_OUT_OF_RANGE = set("टठडढणतथदधनपफबभमयरलळवशषसह")
_LATIN_HEAD_RE = re.compile(r"^([A-Ja-j])\s*[).:：\-–—]\s*(.*)$")
# A-Z beyond J and Devanagari beyond ञ are out of range
_LATIN_OOR_RE = re.compile(r"^[K-PR-Zk-pr-z]\s*[).:：\-–—]")  # excludes Q/q (Q.5 badges)
_DEV_HEAD_RE = re.compile(r"^([क-ञ])\s*[).:：\-–—]\s*(.*)$")
_DEV_OOR_RE = re.compile(r"^[ट-ह]\s*[).:：\-–—]")
_NUM_HEAD_RE = re.compile(r"^(\d{1,2})\s*[):：]\s*(.*)$")  # no dot: "1. Stem" is a question badge
_PAREN_NUM_STMT_RE = re.compile(r"^\(\s*\d{1,3}\s*[).]?")

# Leading non-word decoration: markdown bullets/numbers, emoji, dashes etc.
_DECO_PREFIX_RE = re.compile(r"^[\W_]+", re.UNICODE)


def _strip_decoration(line: str) -> str:
    raw = line.strip()
    s = _DECO_PREFIX_RE.sub("", raw)
    return s or raw


def _head_match(line: str):
    """Return (kind, index, remainder) for an option-head line or None.

    ``kind`` is 'lat', 'dev' or 'num'. Decoration (``-``, ``*``, leading
    emoji/whitespace) is stripped first so ``- A) x`` / ``**B) y**`` are
    recognised the same as bare labels.
    """
    raw = line.strip()
    # Parenthesised numbers -- "(1) Statement ..." -- enumerate statements
    # inside a stem; they are never option labels.
    if re.match(r"^\(\s*\d{1,3}\s*[).]?", raw):
        return None
    s = _strip_decoration(line)
    m = _LATIN_HEAD_RE.match(s)
    if m:
        lab = m.group(1).upper()
        return "lat", ord(lab) - ord("A"), m.group(2).strip()
    if _LATIN_OOR_RE.match(s):
        return "oor", -1, s
    m = _DEV_HEAD_RE.match(s)
    if m:
        return "dev", _DEV_LABELS.index(m.group(1)), m.group(2).strip()
    if _DEV_OOR_RE.match(s):
        return "oor", -1, s
    m = _NUM_HEAD_RE.match(s)
    if m:
        return "num", int(m.group(1)) - 1, m.group(2).strip()
    return None


def _line_is_option_head(line: str) -> bool:
    hm = _head_match(line)
    return hm is not None and hm[0] in ("lat", "dev", "num")


_CHECK_PREFIX_RE = re.compile(r"^[\W_]*✅")
_CHECK_ANY_RE = re.compile(r"✅")

_ANSWER_LINE_RE = re.compile(
    r"^\s*(?:correct\s+answers?|correct\s+options?|answers?|ans|उत्तर|सही\s+उत्तर)"
    r"\s*[:：]\s*(.+)$", re.I)
_EXPL_LINE_RE = re.compile(
    r"^\s*(?:explanation|solution|sol|ex|व्याख्या|समाधान|हल)"
    r"\s*[:：]\s*(.*)$", re.I)
_EXTRA_LINE_RE = re.compile(
    r"^\s*(?:extra\s+details?|अतिरिक्त\s+जानकारी)\s*[:：]\s*(.*)$", re.I)
_SOURCE_LINE_RE = re.compile(
    r"^(?:source|reference|स्रोत|संदर्भ|सन्दर्भ)\s*[:：]", re.I)
# Keywords that terminate option collection when encountered mid-region
_STOP_IN_OPT_RE = re.compile(
    r"^(?:correct\s+answers?|correct\s+options?|answers?|ans|उत्तर|सही\s+उत्तर|ex|explanation|solution|व्याख्या|"
    r"समाधान|हल|extra\s+details?|अतिरिक्त\s+जानकारी|source|reference|"
    r"स्रोत|संदर्भ|सन्दर्भ)\s*[:：]",
    re.I)
_TERM_IN_OPT_RE = re.compile(
    r"^(?:ex|explanation|solution|व्याख्या|समाधान|हल|extra\s+details?|"
    r"अतिरिक्त\s+जानकारी|source|reference|स्रोत|संदर्भ|सन्दर्भ)\s*[:：]",
    re.I)
_ANSWER_KEYWORD_RE = re.compile(
    r"(?:correct\s+answers?|correct\s+options?|\banswers?\b|\bans\b|उत्तर|सही\s+उत्तर)",
    re.I)

# Question-start badges
_Q_HEAD_RE = re.compile(
    r"^\s*Q\.?\s*0*(\d{1,3})(?!\d)(?:\s*[.)\-–—:：]\s*|\s+)(.*\S.*)$", re.I)
_NUM_QUESTION_RE = re.compile(r"^\s*0*(\d{1,3})\s*[.)]\s+(\S.*)$")
_NUM_NOSEP_RE = re.compile(r"^\s*\d")


def _strip_question_badge(line: str, ordinal: Optional[int] = None):
    """Remove a leading ``Q3.``/``3.`` badge from a question's first line.

    Numeric badges are only stripped for ``1.``/``1)`` (or the declared
    ordinal) so dates (``1947``), decimals (``2.5``) and ranges
    (``150-200``) are never touched. Returns ``(cleaned_line, badge_no)``.
    """
    m = _Q_HEAD_RE.match(line)
    if m:
        return (m.group(2).strip(), int(m.group(1)))
    m = _NUM_QUESTION_RE.match(line)
    if m:
        n = int(m.group(1))
        if n == 1 or (ordinal is not None and n == ordinal):
            return (m.group(2).strip(), n)
    return line.strip(), None


# ---------------------------------------------------------------------------
# Answer spec resolution
# ---------------------------------------------------------------------------
def _norm_text(s: str) -> str:
    """Collapse whitespace and casefold for exact-text answer matching."""
    return " ".join(str(s).split()).casefold()


def _resolve_answer_spec(spec: str, n_opts: int, option_texts: list[str] | None = None):
    """Map an Answer: line to option indices.

    Supports:
    - letter labels (A-J), Devanagari (क-ञ), ordinals (1..n)
    - exact option text (case-insensitive, whitespace-normalized)
    - option-level ✅ handled elsewhere

    Returns ``(indices, bad_range, bad_text)``. ``indices`` may contain
    several values (multi-answer). Tokens shaped like option labels or
    ordinals that fall beyond the available options go in ``bad_range``
    (out of range); anything else goes in ``bad_text`` (unrecognised).

    Ambiguous exact-text matches (same normalized text in multiple options)
    are NOT guessed — they become bad_text so the caller rejects with
    RE_UNRECOGNIZED_ANSWER rather than silently picking one.
    """
    indices: list[int] = []
    bad_range: list[str] = []
    bad_text: list[str] = []

    _strip_punct = "()[]{}." + chr(92)  # ()[]{}.\  - avoid raw string ending issue

    # Pre-normalize option texts for exact matching if provided
    norm_opts: list[str] = []
    if option_texts is not None:
        norm_opts = [_norm_text(o) for o in option_texts]

        # First, try whole spec as exact option text (common case:
        # "Answer: Delhi is the capital")
        whole_norm = _norm_text(spec.strip().strip(_strip_punct).strip())
        # Strip leading "option"/"choice" wrappers that sometimes surround text
        whole_tmp = re.sub(r"^(?:OPTION|CHOICE)\s*", "", whole_norm, flags=re.I)
        whole_tmp = re.sub(r"\s*(?:OPTION|CHOICE)$", "", whole_tmp, flags=re.I).strip()
        if whole_tmp:
            matches = [i for i, no in enumerate(norm_opts) if no == whole_tmp]
            if len(matches) == 1:
                return [matches[0]], [], []
            if len(matches) > 1:
                # Ambiguous exact text — do not guess
                return [], [], [spec.strip()]

    parts = re.split(r"\s*(?:,|/|;|\band\b|और|،)\s*", spec, flags=re.I)
    for raw in parts:
        token = raw.strip().strip(_strip_punct).rstrip(".").strip()
        if not token:
            continue
        token = re.sub(r"^(?:OPTION|CHOICE)\s*", "", token, flags=re.I)
        token = re.sub(r"\s*(?:OPTION|CHOICE)$", "", token, flags=re.I).strip()
        token = _CHECK_ANY_RE.sub("", token).strip().strip(_strip_punct).rstrip(".").strip()
        if not token:
            continue

        # Exact option-text match (tightened: no substring guessing)
        if norm_opts:
            tnorm = _norm_text(token)
            exact_matches = [i for i, no in enumerate(norm_opts) if no == tnorm]
            if len(exact_matches) == 1:
                indices.append(exact_matches[0])
                continue
            if len(exact_matches) > 1:
                # Ambiguous duplicate option texts — do not guess
                bad_text.append(token)
                continue

        m_num = re.match(r"^(\d{1,3})(?:st|nd|rd|th)?$", token, re.I)
        letter = None
        m_let = re.match(r"^([A-Za-z])\)?[.]?$", token)
        if m_let:
            letter = m_let.group(1).upper()
        m_dev = re.match(r"^([क-ह])[).]?$", token)
        if letter is not None:
            idx = ord(letter) - ord("A")
            if idx < n_opts:
                indices.append(idx)
            else:
                bad_range.append(token)
        elif m_dev:
            dev = m_dev.group(1)
            if dev in _DEV_LABELS:
                idx = _DEV_LABELS.index(dev)
                if idx < n_opts:
                    indices.append(idx)
                else:
                    bad_range.append(token)
            else:
                bad_range.append(token)
        elif m_num:
            n = int(m_num.group(1))
            if 1 <= n <= n_opts:
                indices.append(n - 1)
            else:
                bad_range.append(token)
        else:
            bad_text.append(token)
    return list(dict.fromkeys(indices)), bad_range, bad_text


# ---------------------------------------------------------------------------
# Strict block parser
# ---------------------------------------------------------------------------
def _core_parse(blk: str):
    """Core block parse returning (question_dict_or_None, reason, detail,
    bad_tokens). ``correct_option_id`` is a list when several options are
    marked; the strict wrapper converts that to a reject reason."""
    if not blk or not blk.strip():
        return None, RE_EMPTY, "empty block", []
    norm = _pad_table_blocks(clean_markdown(blk))
    lines = norm.splitlines()

    # Classify every line once.
    heads: list[tuple[int, str, int, str]] = []  # (line_idx, kind, idx, rest)
    answer_specs: list[tuple[int, str]] = []
    oor = False
    for i, raw in enumerate(lines):
        st = raw.strip()
        if not st:
            continue
        if _PAREN_NUM_STMT_RE.match(_strip_decoration(st)):
            # "(1) Statement ..." enumerates statements, never an option.
            pass
        hm = _head_match(st)
        if hm is not None:
            kind, idx, rest = hm
            if kind == "oor":
                oor = True
            else:
                heads.append((i, kind, idx, rest))
        deco = _strip_decoration(st)
        m = _ANSWER_LINE_RE.match(deco)
        if m:
            answer_specs.append((i, m.group(1).strip()))

    # Choose the label alphabet.
    has_lat = any(k == "lat" for _, k, _, _ in heads)
    has_dev = any(k == "dev" for _, k, _, _ in heads)
    has_num = any(k == "num" for _, k, _, _ in heads)
    kind_pref = "lat" if has_lat else ("dev" if has_dev else ("num" if has_num else None))

    typed_heads = [(i, k, idx, rest) for (i, k, idx, rest) in heads
                   if kind_pref and k == kind_pref]

    # Out-of-range label (K-Z, Devanagari beyond ञ) = too many options.
    if oor and kind_pref in ("lat", "dev"):
        return None, RE_TOO_MANY_OPTIONS, "more than 10 labelled options", []
    if typed_heads:
        over = [idx for _, _, idx, _ in typed_heads if idx >= MAX_OPTIONS]
        if over:
            return None, RE_TOO_MANY_OPTIONS, "more than 10 labelled options", []
        if kind_pref == "num" and (max(idx for _, _, idx, _ in typed_heads) >= MAX_OPTIONS):
            return None, RE_TOO_MANY_OPTIONS, "more than 10 labelled options", []

    region_start = None
    options: list[Optional[str]] = []
    option_checks: list[int] = []
    empty_labels = 0

    if typed_heads:
        # The region begins at the first head of the alphabet (index 0)
        # from which the labels run contiguously; back up over a run of
        # empty first labels (A) with no text, B) ... before real content).
        zero_heads = [h for h in typed_heads if h[2] == 0]
        for cand in zero_heads:
            run = sorted(h for h in typed_heads if h[0] >= cand[0])
            expected = 0
            ok = True
            ordered: list = []
            for h in run:
                if h[2] == expected:
                    ordered.append(h)
                    expected += 1
                elif h[2] < expected:
                    continue  # repeated label (legacy duplicate)
                else:
                    break
            if len(ordered) >= 2:
                region_start = cand[0]
                typed_heads = run
                break
        if region_start is None:
            region_start = typed_heads[0][0]

        head_by_line = {i: (k, idx, rest) for i, k, idx, rest in typed_heads
                        if i >= region_start}
        current = -1
        terminated = False
        for i, raw in enumerate(lines):
            if i < region_start:
                continue
            st = raw.strip()
            if not st:
                continue
            deco = _strip_decoration(st)
            am = _ANSWER_LINE_RE.match(deco)
            if am and not _head_match(deco):
                # Answer lines never terminate option collection (packed
                # columnar PDF extracts put them between option rows).
                continue
            if _TERM_IN_OPT_RE.match(deco):
                terminated = True
                break
            if i in head_by_line:
                _, idx, rest = head_by_line[i]
                leading = bool(_CHECK_PREFIX_RE.match(st))  # check on RAW line, before decoration strip
                text = rest
                checked = leading or "✅" in text
                text = _CHECK_ANY_RE.sub("", text).strip()
                if idx == current and not checked:
                    # Repeat of the same label: append as wrapped text.
                    if text and options:
                        options[-1] = ((options[-1] or "") + " " + text).strip()
                    continue
                if idx == current + 1:
                    options.append(text or None)
                    current = idx
                    if not text:
                        empty_labels += 1
                    if checked:
                        option_checks.append(idx)
                elif idx <= current:
                    # Duplicate label later in region: a check marks the
                    # earlier option of that label.
                    if checked and idx <= current:
                        option_checks.append(idx)
                # a gapped label (e.g. C with no B): stop collecting.
                else:
                    terminated = True
                    break
                continue
            # Non-head continuation line: append to the current option
            # unless it is itself a question-badge line.
            if current >= 0 and not _Q_HEAD_RE.match(deco):
                cont = _CHECK_ANY_RE.sub("", deco).strip()
                if cont and options:
                    options[-1] = ((options[-1] or "") + " " + cont).strip()

    # Stem = lines before the region (or all lines when no labels).
    stem_end = region_start if region_start is not None else len(lines)
    stem_lines = [l for l in lines[:stem_end]]
    while stem_lines and not stem_lines[0].strip():
        stem_lines.pop(0)
    while stem_lines and not stem_lines[-1].strip():
        stem_lines.pop()
    question = "\n".join(l.strip() for l in stem_lines).strip()
    # Strip a leading badge from the first stem line.
    if question:
        first_nl = question.find("\n")
        first = question if first_nl < 0 else question[:first_nl]
        cleaned, _badge = _strip_question_badge(first)
        if cleaned != first.strip():
            question = cleaned + (question[first_nl:] if first_nl >= 0 else "")
    question = question.strip()

    # Tail lines after a terminator (or everything keyword-ish at the
    # end) form the explanation.
    tail_start = len(lines)
    if typed_heads:
        term_idx = None
        for i, raw in enumerate(lines):
            if i < region_start:
                continue
            deco = _strip_decoration(raw.strip())
            if raw.strip() and _TERM_IN_OPT_RE.match(deco):
                term_idx = i
                break
        if term_idx is not None:
            tail_start = term_idx
    explanation = _collect_explanation(lines[tail_start:])

    # Structural validation.
    if oor:
        return None, RE_TOO_MANY_OPTIONS, "more than 10 labelled options", []
    n_heads = len(typed_heads)
    if n_heads and n_heads > MAX_OPTIONS:
        return None, RE_TOO_MANY_OPTIONS, "more than 10 labelled options", []
    if n_heads and empty_labels:
        return None, RE_INSUFFICIENT_OPTIONS, "a labelled option has no text", []
    if n_heads:
        real_opts = [o for o in options if o]
        if len(options) < 2 or len(real_opts) < 2:
            return None, RE_INSUFFICIENT_OPTIONS, \
                f"{len(real_opts)} option(s) found; need at least 2", []
    else:
        options = []

    if not question:
        if not options:
            return None, RE_EMPTY, "empty block", []
        return None, RE_MISSING_QUESTION, "no question text found", []
    if n_heads == 0:
        return None, RE_INSUFFICIENT_OPTIONS, "0 option(s) found; need at least 2", []

    option_checks = sorted(set(option_checks))
    n_opts = len(options)
    option_texts = [o or "" for o in options]

    # Resolve explicit Answer: lines, keeping per-line provenance so
    # "Answer: A, C" (multi on one line) is distinguished from
    # "Answer: A / Answer: C" (two lines disagreeing).
    per_line: list[tuple[int, list[int], list[str], list[str]]] = []
    spec_indices: list[int] = []
    bad_range_all: list[str] = []
    bad_text_all: list[str] = []
    for line_no, spec in answer_specs:
        ids, bad_range, bad_text = _resolve_answer_spec(spec, n_opts, option_texts)
        per_line.append((line_no, ids, bad_range, bad_text))
        bad_range_all.extend(bad_range)
        bad_text_all.extend(bad_text)
        for x in ids:
            if x not in spec_indices:
                spec_indices.append(x)
    spec_indices = sorted(set(spec_indices))

    correct: Optional[object] = None
    reason = None
    detail = None

    # Bad tokens: an out-of-range-shaped label/ordinal wins over free text.
    if bad_range_all or bad_text_all:
        bad0 = (bad_range_all or bad_text_all)[0]
        return None, (RE_ANSWER_OUT_OF_RANGE if bad_range_all
                      else RE_UNRECOGNIZED_ANSWER), \
            f"answer {bad0!r} could not be matched", bad_range_all + bad_text_all

    line_sets = [ids for _, ids, _, _ in per_line if ids]
    one_line_multi = any(len(ids) > 1 for ids in line_sets)
    distinct_line_sets = {tuple(sorted(s)) for s in line_sets}
    lines_disagree = len(distinct_line_sets) > 1

    checks_multi = len(option_checks) > 1
    if checks_multi or one_line_multi:
        correct = sorted(set(option_checks) | set(spec_indices))
        reason = RE_MULTIPLE_CORRECT_UNSUPPORTED
        detail = "multiple options marked correct (single-answer quizzes only)"
    elif lines_disagree:
        return None, RE_CONFLICTING_ANSWERS, \
            "Answer: lines name different options", []
    elif len(option_checks) == 1 and spec_indices:
        if option_checks[0] != spec_indices[0]:
            return ({"question": question, "options": option_texts,
                     "correct_option_id": option_checks[0],
                     "explanation": explanation},
                    RE_CONFLICTING_ANSWERS,
                    "✅ marker and Answer: line name different options", [])
        correct = option_checks[0]
    elif len(option_checks) == 1:
        correct = option_checks[0]
    elif len(spec_indices) == 1:
        correct = spec_indices[0]
    elif len(spec_indices) == 0 and answer_specs:
        return None, RE_UNRECOGNIZED_ANSWER, \
            "answer line did not name an option", []
    else:
        # Missing answer: still hand back the structural question dict so
        # the require_answer=False path (separate answer-key section) can
        # use it; the strict wrapper rejects it otherwise.
        qd = {"question": question, "options": option_texts,
              "correct_option_id": None, "explanation": explanation}
        return qd, RE_MISSING_ANSWER, "no ✅ marker or Answer: line", []

    qd = {"question": question, "options": option_texts,
          "correct_option_id": correct, "explanation": explanation}
    return qd, reason, detail, bad_range_all + bad_text_all


def _collect_explanation(lines: list[str]) -> Optional[str]:
    """Build explanation text from tail lines.

    Handles Solution/Ex/Explanation/व्याख्या/समाधान/हल, Extra details /
    अतिरिक्त जानकारी, nested bullets, and drops Source/Reference lines
    (including URLs). A later keyword block can re-open after a Source.
    """
    out: list[str] = []
    active = False
    for raw in lines:
        st = raw.strip()
        if not st:
            continue
        deco = _strip_decoration(st)
        m = _EXPL_LINE_RE.match(deco)
        if m:
            active = True
            rest = m.group(1).strip()
            if rest:
                out.append(rest)
            continue
        m = _EXTRA_LINE_RE.match(deco)
        if m:
            active = True
            rest = m.group(1).strip()
            if rest:
                out.append(rest)
            continue
        if _SOURCE_LINE_RE.match(deco):
            active = False
            continue
        am = _ANSWER_LINE_RE.match(deco)
        if am:
            continue
        if active:
            # Flatten nested bullets/indentation into plain lines.
            out.append(_strip_decoration(st))
    text = "\n".join(out).strip()
    return text or None


def parse_question_block_strict(blk: str, *, require_answer: bool = True) -> BlockResult:
    """Parse one block strictly. With ``require_answer`` the block is
    rejected when no answer can be determined; otherwise a question dict
    with ``correct_option_id=None`` is returned (used when a separate
    answer-key section will supply the answer)."""
    qd, reason, detail, bad = _core_parse(blk)
    if qd is not None and reason is None:
        if qd["correct_option_id"] is None and not require_answer:
            if bad:
                qd["_answer_warnings"] = list(dict.fromkeys(bad))
            return BlockResult(qd)
        if isinstance(qd["correct_option_id"], list):
            return BlockResult(
                None, RE_MULTIPLE_CORRECT_UNSUPPORTED,
                "multiple options marked correct (single-answer quizzes only)",
                bad)
        if bad:
            qd["_answer_warnings"] = list(dict.fromkeys(bad))
        return BlockResult(qd)
    if qd is not None and reason == RE_MULTIPLE_CORRECT_UNSUPPORTED:
        return BlockResult(None, reason, detail, bad)
    if qd is not None and not require_answer \
            and reason in (None, RE_MISSING_ANSWER):
        return BlockResult(qd)
    if qd is not None:
        return BlockResult(None, reason, detail, bad)
    # No question dict at all.
    if not require_answer and reason == RE_MISSING_ANSWER:
        # Rebuild without requiring an answer to still surface structural
        # problems (missing options etc. take precedence anyway).
        return BlockResult(None, reason, detail, bad)
    return BlockResult(None, reason, detail, bad)


def parse_question_block(blk: str) -> Optional[dict]:
    """Legacy tolerant contract. Returns a question dict (correct id may
    be a list for multi-marked blocks) or ``None``."""
    qd, reason, _detail, bad = _core_parse(blk)
    if qd is None:
        return None
    if qd.get("correct_option_id") is None:
        return None
    if reason == RE_MULTIPLE_CORRECT_UNSUPPORTED:
        qd["_answer_warnings"] = list(dict.fromkeys(bad))
        return qd  # correct_option_id is a list for legacy callers
    if isinstance(qd.get("correct_option_id"), list):
        return None
    if bad:
        qd["_answer_warnings"] = list(dict.fromkeys(bad))
    return qd


# ---------------------------------------------------------------------------
# Document segmentation
# ---------------------------------------------------------------------------
_KEY_HEADER_RE = re.compile(
    r"^\s*(?:ANSWER\s*KEY(?:\s*(?:&|AND)\s*SOLUTIONS?)?|ANSWERS?|"
    r"SOLUTIONS?|उत्तर\s*कुंजी)\s*[:：#-]?\s*$",
    re.I)
_KEY_HEADER_LOOSE_RE = re.compile(
    r"^\s*(?:ANSWER\s*KEY(?:\s*(?:&|AND)\s*SOLUTIONS?)?|ANSWERS?|"
    r"SOLUTIONS?)\b", re.I)


def _split_key_region(lines: list[str]):
    """Split a document into question lines and answer-key lines."""
    for i, ln in enumerate(lines):
        st = ln.strip()
        if _KEY_HEADER_RE.match(st):
            return lines[:i], lines[i + 1:], True
        # A loose header ("ANSWERS" / "SOLUTIONS" / "ANSWER KEY") only
        # counts when it carries no answer payload on the same line --
        # "Answer: J" is an inline answer, never a section start.
        if _KEY_HEADER_LOOSE_RE.match(st):
            tail = re.sub(
                r"^(?:ANSWER\s*KEY(?:\s*(?:&|AND)\s*SOLUTIONS?)?|ANSWERS?|SOLUTIONS?)",
                "", st, flags=re.I).strip(" :：#-–—")
            if not tail:
                return lines[:i], lines[i + 1:], True
    return lines, [], False


def _line_starts_question(st: str) -> Optional[int]:
    """If a line starts a new numbered/badged question, return its ordinal."""
    m = _Q_HEAD_RE.match(st)
    if m:
        return int(m.group(1))
    m = _NUM_QUESTION_RE.match(st)
    if m and not _head_match(st):
        return int(m.group(1))
    return None


def _looks_like_option_context(lines: list[str], start: int) -> bool:
    """True when option-head lines follow within the next ~8 lines."""
    for ln in lines[start + 1:start + 9]:
        hm = _head_match(ln.strip())
        if hm and hm[0] in ("lat", "dev", "num"):
            return True
        if _line_starts_question(ln.strip()):
            return False
    return False


def _split_packed(chunk_lines: list[str]) -> list[tuple[Optional[int], list[str]]]:
    """Split one blank-delimited chunk at packed question starts."""
    pieces: list[tuple[Optional[int], list[str]]] = []
    current: list[str] = []
    pending_ord: Optional[int] = None
    last_num: Optional[int] = None

    def head_ordinal(st):
        m = _Q_HEAD_RE.match(st)
        if m:
            return int(m.group(1)), "badge"
        m = _NUM_QUESTION_RE.match(st)
        if m and not _head_match(st):
            return int(m.group(1)), "num"
        return None

    have_content = False
    heads_seen = 0
    for li, ln in enumerate(chunk_lines):
        st = ln.strip()
        hm = _head_match(st)
        if hm and hm[0] in ("lat", "dev", "num") and st:
            heads_seen += 1
        ho = head_ordinal(st) if st else None
        do_split = False
        if ho:
            n, kind = ho
            if kind == "badge":
                do_split = heads_seen > 0
            else:
                expect = (last_num or 0) + 1 if last_num else 1
                if heads_seen > 0 and n == expect \
                        and _looks_like_option_context(chunk_lines, li):
                    do_split = True
            if do_split:
                pieces.append((pending_ord, current))
                current = []
                last_num = n
                heads_seen = 0
            if pending_ord is None or do_split:
                pending_ord = n
            if kind == "num":
                last_num = n
        current.append(ln)
        if st:
            have_content = True
    if any(x.strip() for x in current):
        pieces.append((pending_ord, current))
    return pieces


def _candidate_blocks(q_lines: list[str]):
    """Produce (ordinal, block_text) candidates from the question area."""
    # Blank-delimited raw chunks.
    raw_chunks: list[list[str]] = [[]]
    for ln in q_lines:
        if ln.strip():
            raw_chunks[-1].append(ln)
        else:
            if raw_chunks[-1]:
                raw_chunks.append([])
    if not raw_chunks[-1]:
        raw_chunks.pop()

    # First pass: packed splits inside chunks.
    split_chunks: list[tuple[Optional[int], list[str]]] = []
    for ch in raw_chunks:
        for o, piece in _split_packed(ch):
            split_chunks.append((o, piece))

    # Second pass: rejoin continuation blocks (bare option lists,
    # explanation text, wrapped prose) to their preceding question.
    merged: list[tuple[Optional[int], list[str]]] = []
    for o, piece in split_chunks:
        text = "\n".join(piece).strip()
        nonempty = [l.strip() for l in piece if l.strip()]
        starts_with_options = nonempty and _head_match(nonempty[0]) \
            and _head_match(nonempty[0])[0] in ("lat", "dev", "num")
        first_deco = _strip_decoration(nonempty[0]) if nonempty else ""
        starts_expl = bool(_EXPL_LINE_RE.match(first_deco)
                           or _EXTRA_LINE_RE.match(first_deco))
        has_any_head = any(
            (_h := _head_match(l)) and _h[0] in ("lat", "dev", "num")
            for l in nonempty)
        has_q_head = any(_line_starts_question(l) is not None for l in nonempty)
        has_q_mark = any("?" in l or "？" in l for l in nonempty)

        if merged:
            prev_o, prev = merged[-1]
            prev_res = parse_question_block_strict(
                "\n".join(prev), require_answer=False)
            prev_has_question = prev_res.question is not None
            if starts_with_options and not has_q_head and not has_q_mark:
                # "Bare options" chunk belongs to the previous stem.
                merged[-1] = (prev_o, prev + [""] + piece)
                continue
            if starts_expl and prev_has_question:
                merged[-1] = (prev_o, prev + [""] + piece)
                continue
            if (not has_any_head and not has_q_head and prev_has_question
                    and prev_res.question.get("explanation")
                    and not has_q_mark
                    and not _ANSWER_KEYWORD_RE.search(text)):
                # Plain prose continuing an already-started explanation.
                merged[-1] = (prev_o, prev + [""] + piece)
                continue
        merged.append((o, piece))
    return [(o, "\n".join(p).strip()) for o, p in merged
            if any(x.strip() for x in p)]


def _looks_like_title_noise(body: str) -> bool:
    """A document title / mock-test cover fragment (a few short lines
    with no question mark, option labels or answer markers) is
    decoration, not an attempted question."""
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if not lines or len(lines) > 8:
        return False
    for line in lines:
        if len(line) > 80:
            return False
        if _ANSWER_KEYWORD_RE.search(line) or "?" in line or "？" in line:
            return False
        hm = _head_match(line)
        if hm and hm[0] in ("lat", "dev", "num"):
            return False
    return True


# ---------------------------------------------------------------------------
# Answer-key / solutions section
# ---------------------------------------------------------------------------
_KEY_ROW_HEAD_RE = re.compile(
    r"^\s*(?:Q\.?\s*)?0*(\d{1,3})\s*[.)]\s*[\-–—:]?\s*"
    r"\(?([A-Ja-jक-ञ]|10|[1-9])\)?[.,;:]?\s*(.*)$")
_KEY_HYPHEN_RE = re.compile(
    r"^\s*Q?\s*0*(\d{1,3})\s*[-–—]\s*\(?([A-Ja-jक-ञ]|10|[1-9])\)?\s*[,;]?\s*(.*)$")
_KEY_INLINE_PAIR_RE = re.compile(
    r"Q?\s*(\d{1,3})\s*[-.)]\s*\(?([A-Ja-jक-ञ])\)?")
_SOLN_PREFIX_RE = re.compile(
    r"^\s*(?:solution|question|q|ans)\s*0*(\d{1,3})\s*[:.)\-]\s*(.*)$", re.I)
# A bare EXPLANATIONS/SOLUTIONS header INSIDE the key region switches the
# rest of the region to solutions-only: ordinal-headed lines there are
# explanations by number, never key rows ("1. Delhi is capital." must not
# read as answer D). Carries no payload by construction.
_SOLN_REGION_HEADER_RE = re.compile(
    r"^\s*(?:EXPLANATIONS?|SOLUTIONS?|व्याख्या|समाधान|हल)\s*[:：#-]?[ ]*$",
    re.I)
# Ordinal-headed line inside the solutions region: explanation by number.
_SOLN_ROW_RE = re.compile(
    r"^\s*(?:Q\.?\s*)?0*(\d{1,3})\s*[.)\-–—:]\s*(.*)$")
# Ordinal-headed but unreadable as a key row ("2. Z"): malformed, never a
# solution line for the previous question and never an answer.
_MALFORMED_ROW_RE = re.compile(
    r"^\s*(?:Q\.?\s*)?0*(\d{1,3})\s*[.)\-–—:]\s*\S")


def _letter_to_index(token: str) -> Optional[int]:
    t = token.strip().strip("().,;")
    if t == "10":
        return 9
    if len(t) != 1:
        return None
    if "A" <= t.upper() <= "J":
        return ord(t.upper()) - ord("A")
    if t in _DEV_LABELS:
        return _DEV_LABELS.index(t)
    if t.isdigit():
        n = int(t)
        return n - 1 if 1 <= n <= MAX_OPTIONS else None
    return None


def _parse_key_section(k_lines: list[str]):
    """Return (rows, rows_found, solutions, dup_conflict, malformed).

    ``rows`` maps ordinal -> (option index or None, inline rest);
    ``dup_conflict`` holds ordinals keyed twice with different answers
    (first row wins, the question is rejected, never guessed);
    ``malformed`` holds ordinals whose key row is unreadable (rejected,
    never leaked into a neighbour's solution).
    """
    rows: dict[int, tuple[Optional[int], str]] = {}
    soln: dict[int, list[str]] = {}
    dup_conflict: set[int] = set()
    malformed: set[int] = set()
    rows_found = 0
    current_ord: Optional[int] = None
    solutions_mode = False

    # Grid layout only scans the answer rows: a bare EXPLANATIONS header
    # starts solutions-only content where ordinals are never key rows.
    soln_at = next((i for i, ln in enumerate(k_lines)
                    if _SOLN_REGION_HEADER_RE.match(ln.strip())), len(k_lines))
    key_scan = k_lines[:soln_at]
    grid: dict[int, int] = {}
    int_re = re.compile(r"^\d{1,3}$")
    let_re = re.compile(r"^[A-Ja-jक-ञ]$")
    for i, ln in enumerate(key_scan):
        toks = ln.split()
        if (len(toks) >= 2 and all(int_re.match(t) for t in toks)
                and [int(t) for t in toks] == sorted(int(t) for t in toks)
                and i + 1 < len(key_scan)):
            ltoks = key_scan[i + 1].split()
            if len(ltoks) == len(toks) and all(let_re.match(t) for t in ltoks):
                for o, l in zip((int(t) for t in toks), ltoks):
                    idx = _letter_to_index(l)
                    if idx is not None:
                        grid[o] = idx
                break

    def attach_solution(ordn, text):
        text = text.strip()
        if not text:
            return
        m = _EXPL_LINE_RE.match(text)
        if m:
            text = m.group(1).strip() or text
        soln.setdefault(ordn, []).append(text)

    def record_row(n, idx, rest):
        nonlocal rows_found, current_ord
        rows_found += 1
        current_ord = n
        if n in rows:
            if rows[n][0] != idx:
                # Same question keyed twice with different answers:
                # keep the first row but flag the conflict.
                dup_conflict.add(n)
                return
        else:
            rows[n] = (idx, (rest or "").strip())
        if (rest or "").strip():
            attach_solution(n, rest)

    for raw in k_lines:
        st = raw.rstrip()
        stripped = st.strip()
        if not stripped:
            continue
        # Key rows must start at the left margin (continuation prose is
        # indented, e.g. "   2-C pairing below ...").
        indented = raw.startswith((" ", "\t", "  ")) and len(st) - len(st.lstrip()) >= 2
        if not indented and _SOLN_REGION_HEADER_RE.match(stripped):
            solutions_mode = True
            current_ord = None
            continue
        used = False
        if solutions_mode:
            ms = _SOLN_PREFIX_RE.match(st)
            if ms and not indented:
                current_ord = int(ms.group(1))
                if ms.group(2).strip():
                    attach_solution(current_ord, ms.group(2))
                used = True
            else:
                m = _SOLN_ROW_RE.match(st)
                if m and not indented:
                    current_ord = int(m.group(1))
                    if m.group(2).strip():
                        attach_solution(current_ord, m.group(2))
                    used = True
        else:
            # Lines carrying several pairs ("Q1-B, Q2-B") take priority over
            # the single-row patterns so the trailing pair is never swallowed
            # as solution prose.
            line_pairs = _KEY_INLINE_PAIR_RE.findall(st)
            if not indented and line_pairs and _KEY_INLINE_PAIR_RE.match(st):
                if len(line_pairs) >= 2 or not (_KEY_ROW_HEAD_RE.match(st)
                                                or _KEY_HYPHEN_RE.match(st)):
                    for os, ls in line_pairs:
                        record_row(int(os), _letter_to_index(ls), "")
                    continue
            m = _KEY_ROW_HEAD_RE.match(st)
            if m and not indented:
                record_row(int(m.group(1)), _letter_to_index(m.group(2)),
                           m.group(3))
                used = True
            else:
                m = _KEY_HYPHEN_RE.match(st)
                if m and not indented:
                    record_row(int(m.group(1)), _letter_to_index(m.group(2)),
                               m.group(3))
                    used = True
                else:
                    ms = _SOLN_PREFIX_RE.match(st)
                    if ms and not indented:
                        n = int(ms.group(1))
                        rest = ms.group(2).strip()
                        lm = re.match(
                            r"\(?([A-Ja-j\u0915-\u091e]|10|[1-9])\)?(?:[.:;)]+(?:\s|$)|\s+|$)(.*)$",
                            rest)
                        if lm:
                            record_row(n, _letter_to_index(lm.group(1)),
                                       lm.group(2))
                            used = True
                        else:
                            current_ord = n
                            used = True
                    else:
                        # Comma-separated inline pairs on one line.
                        pairs = _KEY_INLINE_PAIR_RE.findall(st)
                        if pairs and not indented and _KEY_INLINE_PAIR_RE.match(st):
                            for os, ls in pairs:
                                record_row(int(os), _letter_to_index(ls), "")
                            used = True
                        elif not indented:
                            mm = _MALFORMED_ROW_RE.match(st)
                            if mm:
                                # Ordinal-headed but unreadable as a key row
                                # ("2. Z"): never an answer, never leaked
                                # into the previous question's solution.
                                malformed.add(int(mm.group(1)))
                                current_ord = int(mm.group(1))
                                used = True
        if not used:
            if current_ord is None:
                continue
            if _KEY_HEADER_RE.match(stripped) \
                    or _SOLN_REGION_HEADER_RE.match(stripped):
                # A repeated bare section header is structure, never prose.
                continue
            deco = _strip_decoration(stripped)
            if _SOURCE_LINE_RE.match(deco):
                continue
            am = _ANSWER_LINE_RE.match(deco)
            if am:
                continue
            attach_solution(current_ord, deco)

    for o, idx in grid.items():
        rows.setdefault(o, (idx, ""))
        rows_found = max(rows_found, len(rows))
    solutions = {o: "\n".join(v).strip() for o, v in soln.items() if v}
    return rows, rows_found, solutions, dup_conflict, malformed


# ---------------------------------------------------------------------------
# Document entry point
# ---------------------------------------------------------------------------
def parse_question_document(text: str, *, allow_multiple_answer: bool = False
                            ) -> DocumentParseResult:
    warnings: list[str] = []
    if text is None or not str(text).strip():
        return DocumentParseResult(
            [], [SkippedBlock(RE_EMPTY, "empty document")], False, warnings)
    norm = _pad_table_blocks(clean_markdown(str(text)))
    lines = norm.splitlines()
    q_lines, k_lines, has_header = _split_key_region(lines)

    raw_candidates = _candidate_blocks(q_lines)
    # Drop title/cover-page fragments BEFORE positional alignment so they
    # never shift the ordinal <-> answer-key association.
    candidates = [(o, b) for (o, b) in raw_candidates
                  if not _looks_like_title_noise(b)]

    key_rows, key_rows_found, key_solutions, key_dup, key_malformed = (
        _parse_key_section(k_lines) if has_header else ({}, 0, {}, set(), set()))
    answer_key_detected = has_header and (key_rows_found > 0)

    questions: list[dict] = []
    skipped: list[SkippedBlock] = []

    for pos, (decl_ord, block) in enumerate(candidates, start=1):
        ordinal = decl_ord or pos
        snippet = " ".join(block.split())[:80]
        if _looks_like_title_noise(block):
            continue
        result = parse_question_block_strict(
            block, require_answer=not has_header)
        q = result.question
        key_row = key_rows.get(ordinal)
        key_idx = key_row[0] if key_row else None
        key_sol = key_solutions.get(ordinal)

        if q is None:
            # If an answer key exists and the ONLY problem was a missing
            # answer, retry without requiring one (structural problems
            # still win).
            if has_header and result.reason == RE_MISSING_ANSWER:
                result = parse_question_block_strict(block, require_answer=False)
                q = result.question
            if q is None:
                skipped.append(SkippedBlock(
                    result.reason or RE_INSUFFICIENT_OPTIONS,
                    result.detail or "", ordinal, snippet))
                continue

        inline = q.get("correct_option_id")
        n_opts = len(q["options"])

        if isinstance(inline, list):
            skipped.append(SkippedBlock(
                RE_MULTIPLE_CORRECT_UNSUPPORTED,
                "multiple options marked correct (single-answer quizzes only)",
                ordinal, snippet))
            continue

        if has_header and ordinal in key_dup:
            skipped.append(SkippedBlock(
                RE_CONFLICTING_ANSWERS,
                f"answer-key rows for question {ordinal} name different options",
                ordinal, snippet))
            continue

        if has_header and ordinal in key_malformed:
            skipped.append(SkippedBlock(
                RE_MALFORMED_ANSWER_KEY,
                f"answer-key row for question {ordinal} is malformed",
                ordinal, snippet))
            continue

        if has_header and key_idx is not None and key_idx >= n_opts:
            skipped.append(SkippedBlock(
                RE_ANSWER_OUT_OF_RANGE,
                f"answer key points outside {n_opts} options",
                ordinal, snippet))
            continue

        if inline is not None and has_header and key_idx is not None:
            if inline != key_idx:
                skipped.append(SkippedBlock(
                    RE_CONFLICTING_ANSWERS,
                    "inline marker and answer key name different options",
                    ordinal, snippet))
                continue
            correct = inline
        elif inline is not None:
            correct = inline
        elif has_header and key_idx is not None:
            correct = key_idx
        elif has_header:
            skipped.append(SkippedBlock(
                RE_ANSWER_KEY_ASSOCIATION,
                f"no answer-key row could be associated with question {ordinal}",
                ordinal, snippet))
            continue
        else:
            skipped.append(SkippedBlock(
                RE_MISSING_ANSWER, "no ✅ marker or Answer: line",
                ordinal, snippet))
            continue

        if not q.get("explanation") and key_sol:
            q["explanation"] = key_sol
        elif key_sol and q.get("explanation") and key_sol not in q["explanation"]:
            # Prefer the inline explanation; keep key solution too if it
            # adds text not already present.
            q["explanation"] = q["explanation"]
        q["correct_option_id"] = correct
        q.pop("_answer_warnings", None)
        questions.append(q)

    if has_header:
        known = {decl or pos for pos, (decl, _b) in enumerate(candidates, start=1)}
        for n in sorted(set(key_rows) | set(key_solutions)):
            if n not in known:
                warnings.append(
                    f"answer-key entry for unknown question {n} ignored")

    if has_header and key_rows_found == 0 and candidates:
        # Clearly labelled key section but nothing parseable.
        skipped.append(SkippedBlock(
            RE_MALFORMED_ANSWER_KEY,
            "answer-key section contained no readable ordinal/letter rows",
            None, " ".join(k_lines)[:80]))
        # Questions that were already answered inline survive; any that
        # were only "answered" by association are reclassified below.
        if not questions:
            skipped = [s for s in skipped
                       if s.reason != RE_ANSWER_KEY_ASSOCIATION] + [
                SkippedBlock(RE_MALFORMED_ANSWER_KEY,
                             "answer-key section contained no readable rows")]

    warnings = list(dict.fromkeys(warnings))
    return DocumentParseResult(questions, skipped, answer_key_detected, warnings)


# ---------------------------------------------------------------------------
# User-word filtering / noise (legacy surface, preserved verbatim)
# ---------------------------------------------------------------------------
def filter_words(text: Optional[str], remove_words: list[str]) -> Optional[str]:
    """Strip `[n/m]` progress markers and any user-configured remove-words
    from `text`, while preserving newlines exactly as typed/pasted.
    """
    if not text:
        return text
    text = re.sub(r"\[\s*\d+\s*/\s*\d+\s*\]", "", text)
    if remove_words:
        for w in remove_words:
            text = re.sub(rf"\b{re.escape(w)}\b", "", text, flags=re.IGNORECASE)
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" \t") for ln in text.split("\n")]
    return "\n".join(lines).strip("\n")


def strip_source_noise(text: Optional[str]) -> Optional[str]:
    """Remove leaked `[Q 3/10]`-style progress markers, raw URLs, t.me
    links, and @mentions that sometimes end up pasted in quiz text."""
    if not text:
        return text
    pattern = (
        r"(?:[\[\(]\s*Q\.?\s*\d+(?:\s*/\s*\d+)?\s*[\]\)]|"
        r"[\[\(]\s*\d+\s*/\s*\d+\s*[\]\)]|"
        r"\bQ\.?\s*\d+\s*/\s*\d+\)?|https?://[^\s]+|t\.me/[^\s]+|@\w+)"
    )
    return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()