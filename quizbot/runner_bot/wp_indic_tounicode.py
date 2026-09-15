"""Advance Quiz Bot — WeasyPrint Indic text-layer audit helpers.

The Quiz Result PDF must show Hindi/Devanagari correctly in TWO places:

1. visually (correct shaping/glyphs — Pango + HarfBuzz + a real Indic
   font, bundled at ``pdf_service/fonts`` and referenced via ``@font-face``
   in :mod:`quizbot.runner_bot.pdf_reports`); and
2. in the PDF text layer — copy/paste, screen readers and PDF search all
   read the embedded text mappings, not the rendered glyphs.

Text-layer fix in this repository
----------------------------------
WeasyPrint 62.x builds each font subset's ToUnicode CMap purely from glyph
ids; Devanagari pre-base matra glyphs (ि, drawn LEFT of its consonant while
its codepoint follows it) share shaping clusters with their consonant and
the same consonant glyph id is reused standalone, so plain ToUnicode yields
``विवरण`` -> ``विविरण`` in extraction even though the page looks correct.
``wp_indic_compat`` fixes this at render time by wrapping every Pango run in
a standards-compliant ``/Span << /ActualText <logical text> >> BDC … EMC``
marker — the same mechanism Chromium/Acrobat use for complex scripts.

Extractor note
--------------
Among the local extractors, PyMuPDF/MuPDF honours ``/ActualText`` (as do
Acrobat, PDFium/Chrome, pdf.js and Apple Preview); pdfminer.six (through
20260107) and pypdf ignore it entirely and therefore still read the
imperfect ToUnicode fallback. This module therefore extracts with PyMuPDF,
which also supplies page geometry and words. When a PDF without
ActualText markers is audited, PyMuPDF can surface stray Greek glyphs for
empty ToUnicode mappings — the audit still runs deterministically and
reports failures honestly instead of passing them.

Everything here is read-only and side-effect free.
"""

from __future__ import annotations

import re
import logging
import unicodedata
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Glyphs that mean "this codepoint had no glyph / no text mapping".
_TOFU_CHARS = ("\ufffd",)
# PyMuPDF surfaces unmapped glyphs as the literal name .notdef.
_TOFU_TOKENS = (".notdef",)


_INDIC_SCRIPT_RANGES = (
    (0x0900, 0x097F),  # Devanagari
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi
    (0x0A80, 0x0AFF),  # Gujarati
    (0x0B00, 0x0B7F),  # Oriya
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
)


def _is_indic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _INDIC_SCRIPT_RANGES)


def _normalize(text: str) -> str:
    """NFC-fold for comparison only (never mutates stored content).

    Zero-width joiners/non-joiners, variation selectors and zero-width
    spaces are visually significant but routinely normalised away by PDF
    text extractors, so comparisons ignore them; every other codepoint
    counts.
    """
    text = unicodedata.normalize("NFC", text)
    return "".join(ch for ch in text
                   if ch not in ("\u200c", "\u200d", "\ufe0f", "\u200b"))


def _indic_key(text: str) -> str:
    """Collapse a phrase to its Indic script codepoints only.

    CSS multi-column layouts are read line-by-line by spatial extractors;
    ActualText-marked PDFs restore logical order, but defensive matching on
    the uninterrupted Indic codepoint sequence keeps the gate robust for
    mixed Latin/Hindi/emoji content.
    """
    return "".join(ch for ch in _normalize(text) if _is_indic(ch))


def _indic_present(term: str, haystack_norm: str) -> bool:
    key = _indic_key(term)
    if not key:
        return True
    hay = "".join(ch for ch in haystack_norm if _is_indic(ch))
    return key in hay


def _open_doc(pdf_bytes_or_doc):
    import fitz
    if isinstance(pdf_bytes_or_doc, fitz.Document):
        return pdf_bytes_or_doc, False
    return fitz.open(stream=pdf_bytes_or_doc, filetype="pdf"), True


def page_texts(pdf_bytes: bytes) -> list[str]:
    """One text string per page (PyMuPDF, /ActualText-aware)."""
    doc, owned = _open_doc(pdf_bytes)
    try:
        return [page.get_text("text") for page in doc]
    finally:
        if owned:
            doc.close()


def page_count(pdf_bytes) -> int:
    doc, owned = _open_doc(pdf_bytes)
    try:
        return doc.page_count
    finally:
        if owned:
            doc.close()


def page_words(pdf_bytes, page_index: int = 0) -> list[dict]:
    """Word boxes for a page: dicts with text/bbox/block/line/word_no."""
    doc, owned = _open_doc(pdf_bytes)
    try:
        page = doc[page_index]
        return page.get_text("words")
    finally:
        if owned:
            doc.close()


def full_text(pdf_bytes: bytes) -> str:
    return "\n".join(page_texts(pdf_bytes))


def find_tofu(pdf_bytes_or_text) -> list[str]:
    """Return replacement/.notdef markers found in rendered text."""
    text = (pdf_bytes_or_text if isinstance(pdf_bytes_or_text, str)
            else full_text(pdf_bytes_or_text))
    hits = [ch for ch in _TOFU_CHARS if ch in text]
    hits.extend(tok for tok in _TOFU_TOKENS if tok in text)
    return hits


def verify_indic_roundtrip(
    pdf_bytes: bytes, expected_terms: Iterable[str]
) -> tuple[bool, dict[str, str]]:
    """Prove that ``expected_terms`` (Devanagari phrases, answers,
    explanations) survive into the PDF's text layer, modulo Unicode
    composition / invisible formatting characters.

    Returns ``(ok, missing)``; a missing term is a failed gate, never
    silently passed.
    """
    extracted = _normalize(full_text(pdf_bytes))
    missing: dict[str, str] = {}
    for term in expected_terms:
        term = (term or "").strip()
        if not term:
            continue
        if _is_indic(term[0]):
            present = _indic_present(term, extracted)
        else:
            present = _normalize(term) in extracted
        if not present:
            missing[term] = "not present in extracted PDF text layer"
    return (not missing), missing


def audit_report_pdf(
    pdf_bytes: bytes,
    *,
    expected_terms: Optional[Iterable[str]] = None,
    must_contain: Optional[Iterable[str]] = None,
) -> dict:
    """One deterministic audit record for a rendered Quiz Result PDF.

    ``expected_terms`` – Devanagari phrases that must round-trip;
    ``must_contain``   – Latin/marker strings that must be present
                         (titles, "Answer:", leaderboard names, ...).
    """
    pages = page_texts(pdf_bytes)
    extracted = "\n".join(pages)
    norm = _normalize(extracted)

    missing_indic: dict[str, str] = {}
    for term in expected_terms or ():
        term = (term or "").strip()
        if not term:
            continue
        if _is_indic(term[0]):
            present = _indic_present(term, norm)
        else:
            present = _normalize(term) in norm
        if not present:
            missing_indic[term] = "not present in extracted PDF text layer"

    def _latin_present(term: str) -> bool:
        # Span-boundary tolerant: columns/emoji insert non-alphanumerics
        # between words, but letters inside a word are never reordered.
        pat = re.compile(
            r"[^A-Za-z0-9]*".join(
                re.escape(w) for w in term.split()), re.I)
        return bool(pat.search(norm))

    missing_latin = [t for t in (must_contain or ())
                     if t and not _latin_present(t)]
    tofu = find_tofu(extracted)

    ok = not missing_indic and not missing_latin and not tofu
    return {
        "ok": ok,
        "pages": len(pages),
        "missing_indic": missing_indic,
        "missing_terms": missing_latin,
        "tofu": tofu,
    }
