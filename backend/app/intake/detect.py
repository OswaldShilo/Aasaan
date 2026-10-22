"""Script and language detection, and the page-health gate. Pure code (Unicode blocks and a few counts): no model, no network.

The gate answers one question per page: is this text real content, or should the page go to a stronger tier? Every "no" carries a plain
reason that ends up in the quality report, so a page is never dropped or trusted silently.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Optional

# Unicode block -> (script name, language code guess, supported by Indra OCR on scans)
SCRIPTS = [
    (0x0900, 0x097F, "Devanagari", "hi", True), (0x0980, 0x09FF, "Bengali", "bn", True), (0x0A00, 0x0A7F, "Gurmukhi", "pa", False),
    (0x0A80, 0x0AFF, "Gujarati", "gu", False), (0x0B00, 0x0B7F, "Oriya", "or", False), (0x0B80, 0x0BFF, "Tamil", "ta", False),
    (0x0C00, 0x0C7F, "Telugu", "te", False), (0x0C80, 0x0CFF, "Kannada", "kn", False), (0x0D00, 0x0D7F, "Malayalam", "ml", False),
    (0x0400, 0x04FF, "Cyrillic", "ru", True), (0x0370, 0x03FF, "Greek", "el", True), (0x4E00, 0x9FFF, "Han", "zh", True),
    (0x0600, 0x06FF, "Arabic", "ar", False), (0x0590, 0x05FF, "Hebrew", "he", False),
]
OCR_SUPPORTED = {"Latin", "Devanagari", "Bengali", "Cyrillic", "Greek", "Han"}     # scripts Indra OCR reads on scans (README of indra-ocr 0.3.2)
LOW_CONFIDENCE = 0.70
MIN_CHARS = 20


def script_of(ch: str) -> Optional[str]:
    o = ord(ch)
    for lo, hi, name, _, _ in SCRIPTS:
        if lo <= o <= hi:
            return name
    if ch.isalpha() and o < 0x250:       # Basic Latin, Latin-1 and Latin Extended-A/B
        return "Latin"
    return None


def script_profile(text: str) -> dict[str, float]:
    """Share of letters per script, e.g. {"Tamil": 0.9, "Latin": 0.1}."""
    counts = Counter(s for s in (script_of(c) for c in text if c.isalpha()) if s)
    total = sum(counts.values())
    return {k: round(v / total, 3) for k, v in counts.most_common()} if total else {}


def dominant_script(text: str) -> str:
    prof = script_profile(text)
    return next(iter(prof), "Latin")


def language_of(text: str, default: str = "en") -> str:
    """A language guess from the script (a hint only: the learner's chosen language wins elsewhere)."""
    s = dominant_script(text)
    if s == "Latin":
        return default
    for _, _, name, code, _ in SCRIPTS:
        if name == s:
            return code
    return default


def ocr_supports(script: str) -> bool:
    return script in OCR_SUPPORTED


def _is_letter(ch: str) -> bool:
    """A letter, or a combining mark: Indic vowel signs are marks (category M), and `str.isalpha` and regex word characters skip them, which would split every Tamil word."""
    return ch.isalpha() or unicodedata.category(ch).startswith("M")


def words_in(text: str) -> list[str]:
    """Tokens that contain at least two letters (marks count), in any script."""
    return [tok for tok in re.split(r"[\s\.,;:!?()\[\]{}<>/\|\"'`~@#$%^&*+=_-]+", text) if sum(1 for ch in tok if _is_letter(ch)) >= 2]


@dataclass(frozen=True)
class Health:
    ok: bool
    reason: str = ""          # plain words, for the quality report
    code: str = ""            # short machine code: empty | low_confidence | unsupported_script | not_words | garbled


def page_health(text: str, confidence: Optional[float] = None, *, from_ocr: bool = False, expected_script: Optional[str] = None) -> Health:
    """Is this page's text real content? `from_ocr` is True for OCR output (text layers are trusted more)."""
    t = (text or "").strip()
    if len(t) < MIN_CHARS:
        return Health(False, "There is almost no text on this page.", "empty")
    letters = [c for c in t if _is_letter(c)]
    words = words_in(t)
    if not letters or len(words) < 3:
        return Health(False, "The text on this page does not form words.", "not_words")
    if from_ocr:
        script = dominant_script(t)
        if expected_script and expected_script != script and ocr_supports(script) and not ocr_supports(expected_script):
            return Health(False, f"The page looks like {expected_script} text, which the offline reader cannot read; it came out as {script} letters.", "unsupported_script")
        if confidence is not None and confidence < LOW_CONFIDENCE:
            return Health(False, f"The reader was not sure of this page (confidence {confidence:.2f}).", "low_confidence")
        # letters that are mostly isolated single characters or symbols are what a reader produces for a script it cannot read
        if len(words) / max(1, len(t.split())) < 0.4:
            return Health(False, "Most of this page came out as stray characters.", "garbled")
    return Health(True)


def looks_like_unsupported_script_image(text_guess: str) -> bool:
    """True when text already read from a page contains letters of a script OCR cannot read (used when a text layer exists in that script)."""
    return any(s not in OCR_SUPPORTED for s in script_profile(text_guess))
