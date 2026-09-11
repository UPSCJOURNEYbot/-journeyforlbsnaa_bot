"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import re
import unicodedata as _ud
from typing import Optional

_LATEX_MAP_NOT_USED = None  # (LaTeX/markdown cleanup lives in shared.utils.text)


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


_ABCD_RE = re.compile(r"^[A-Da-d]\)")


def parse_question_block(blk: str) -> Optional[dict]:
    """Parse a human-friendly MCQ block.

    Accepted answers:
      * an option already marked with ``✅``
      * ``Answer: b`` / ``Answer: B`` / ``उत्तर: b``
      * ``Answer: 2`` / ``Answer: (b)`` / ``Answer: B, D``

    Accepted option labels include A-D, a-d, 1-4 and unlabeled options.
    ``Solution:``, ``Explanation:`` and ``Extra details:`` are ignored after
    the answer/options section.  This deliberately keeps the importer
    tolerant of AI-generated and exam-style question files.
    """
    blk = clean_markdown(blk)
    lines = blk.splitlines()
    if not any(x.strip() for x in lines):
        return None

    answer_spec: Optional[str] = None
    exp_lines: list[str] = []
    in_explanation = False
    content: list[str] = []
    for ln in lines:
        st = ln.strip()
        if not st:
            if in_explanation and exp_lines and exp_lines[-1] != "":
                exp_lines.append("")
            else:
                content.append(ln)
            continue
        # Tolerate leading decoration (💡, ✅, •, …) before label lines such
        # as "💡 Explanation:" as produced by quiz-report layouts.
        st = re.sub(r"^\W+", "", st) or st

        m = re.match(r'^(?:correct\s+answer|correct\s+option|answer|ans|उत्तर|सही\s+उत्तर)\s*[:：]\s*(.+)$', st, re.I)
        if m:
            answer_spec = m.group(1).strip()
            in_explanation = False
            continue

        m = re.match(r'^(?:ex|explanation|solution|व्याख्या|समाधान)\s*[:：]\s*(.*)$', st, re.I)
        if m:
            first = m.group(1).strip()
            if first:
                exp_lines.append(first)
            in_explanation = True
            continue

        if re.match(r'^(?:extra\s+details?|अतिरिक्त\s+जानकारी)\s*[:：]', st, re.I):
            label, _, rest = st.partition(":")
            if rest.strip():
                exp_lines.append(rest.strip())
            in_explanation = True
            continue

        if re.match(r'^(?:source|reference)\s*[:：]', st, re.I):
            in_explanation = False
            continue

        if in_explanation:
            exp_lines.append(st)
        else:
            content.append(ln)

    exp = "\n".join(exp_lines).strip() or None

    all_lines = content
    non_blank_idx = [i for i, ln in enumerate(all_lines) if ln.strip()]
    if not non_blank_idx:
        return None

    # Find first option-looking line. This works for both A) and A. styles.
    option_re = re.compile(r'^[A-Da-d]\s*[\).:-]\s*\S')
    first_option_idx = next((i for i in non_blank_idx if option_re.match(all_lines[i].strip())), None)
    sep_line_idx = next((i for i in non_blank_idx if _line_is_emoji_separator(all_lines[i])), None)

    if sep_line_idx is not None:
        q_lines = all_lines[:sep_line_idx]
        opt_lines = all_lines[sep_line_idx + 1:]
    elif first_option_idx is not None and first_option_idx > non_blank_idx[0]:
        q_lines = all_lines[:first_option_idx]
        opt_lines = all_lines[first_option_idx:]
    else:
        # Standard four-space-indented options: first line is the question.
        q_lines = all_lines[:non_blank_idx[0] + 1]
        opt_lines = all_lines[non_blank_idx[0] + 1:]

    while q_lines and not q_lines[0].strip():
        q_lines.pop(0)
    while q_lines and not q_lines[-1].strip():
        q_lines.pop()
    question = _pad_table_blocks("\n".join(q_lines).strip())

    opts: list[str] = []
    coids: list[int] = []
    label_to_index: dict[str, int] = {}
    for ln in opt_lines:
        st = ln.strip()
        if not st:
            continue
        st = re.sub(r"^\W+", "", st) or st
        if re.match(r'^(?:answer|ans|उत्तर|ex|explanation|solution|व्याख्या|समाधान|extra\s+details?|अतिरिक्त\s+जानकारी|source|reference)\s*[:：]', st, re.I):
            break
        label_match = re.match(r'^([A-Da-d]|[1-9][0-9]?)\s*[\).:-]\s*(.*)$', st)
        if label_match:
            label = label_match.group(1).upper()
            text = label_match.group(2).strip()
            # Some generators put a clean option list first and repeat the
            # same option with a trailing ✅ later. Treat that as an answer
            # marker rather than creating a fifth option.
            if label in label_to_index:
                existing = label_to_index[label]
                if '✅' in text:
                    coids.append(existing)
                continue
            label_to_index[label] = len(opts)
        else:
            text = st
        if not text:
            continue
        if '✅' in text:
            coids.append(len(opts))
            text = text.replace('✅', '').strip()
        opts.append(text)

    # Resolve an explicit Answer: line only when no/insufficient checkmarks.
    if answer_spec:
        for token in re.split(r'\s*(?:,|/|\band\b|और)\s*', answer_spec, flags=re.I):
            token = token.strip().strip('()[]{}').rstrip('.').strip()
            if not token:
                continue
            normalized = re.sub(r'^(?:OPTION|CHOICE)\s*', '', token, flags=re.I)
            normalized = re.sub(r'\s*(?:OPTION|CHOICE)$', '', normalized, flags=re.I).strip()
            ordinal = re.match(r'^(\d+)(?:st|nd|rd|th)?\s*(?:OPTION|CHOICE)?$', normalized, re.I)
            key = normalized.upper()
            if key in label_to_index:
                coids.append(label_to_index[key])
                continue
            if ordinal:
                n = int(ordinal.group(1))
                if 1 <= n <= len(opts):
                    coids.append(n - 1)

    # De-duplicate while preserving order; reject invalid answer markers.
    coids = list(dict.fromkeys(i for i in coids if 0 <= i < len(opts)))
    if not question or len(opts) < 2 or not coids:
        return None

    coid = coids[0] if len(coids) == 1 else coids
    return {"question": question, "options": opts, "correct_option_id": coid, "explanation": exp}


def filter_words(text: Optional[str], remove_words: list[str]) -> Optional[str]:
    """Strip `[n/m]` progress markers and any user-configured remove-words
    from `text`, while preserving newlines exactly as typed/pasted.

    Only horizontal whitespace (spaces/tabs) is collapsed -- line breaks
    are never touched, so multi-line questions/options/explanations keep
    their original line structure end-to-end.
    """
    if not text:
        return text
    text = re.sub(r"\[\s*\d+\s*/\s*\d+\s*\]", "", text)
    if remove_words:
        for w in remove_words:
            text = re.sub(rf"\b{re.escape(w)}\b", "", text, flags=re.IGNORECASE)
    # Collapse runs of spaces/tabs only (never \n) and trim horizontal
    # whitespace at the start/end of each line, without merging lines.
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" \t") for ln in text.split("\n")]
    return "\n".join(lines).strip("\n")


def strip_source_noise(text: Optional[str]) -> Optional[str]:
    """Remove leaked `[Q 3/10]`-style progress markers, raw URLs, t.me
    links, and @mentions that sometimes end up pasted into quiz text.

    The bracket/paren marker only matches when it actually looks like a
    progress tag -- either a `Q` prefix (`[Q3]`, `(Q.5)`) or a `n/m` pair
    (`[11/100]`, `(3/10)`). A bare lone number in brackets/parens, like
    `(1)` or `[2]`, is legitimate question content (e.g. an enumerated
    list: "(1) Anaphase (2) Metaphase") and must never be stripped.
    """
    if not text:
        return text
    pattern = (
        r"(?:[\[\(]\s*Q\.?\s*\d+(?:\s*/\s*\d+)?\s*[\]\)]|"
        r"[\[\(]\s*\d+\s*/\s*\d+\s*[\]\)]|"
        r"\bQ\.?\s*\d+\s*/\s*\d+\)?|https?://[^\s]+|t\.me/[^\s]+|@\w+)"
    )
    return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
