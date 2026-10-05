"""Scrub data we never want at rest: payment card numbers and US Social Security numbers.

A caller can volunteer anything ("my card is ..."). The assistant is told never to ask for such data, but a transcript is
stored verbatim, so the stored copy is scrubbed here. Applied to stored transcripts and escalation summaries (which are
also emailed), not to what the caller hears: the live call is unaffected.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"
_DIGIT_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")                 # 13-19 digits with optional space/dash separators
_SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)|(?<![\d-])\d{9}(?![\d-])")
_SPOKEN_SSN = re.compile(r"(?i)(social(?: security)?(?: number)?(?: is)?[:\s]*)((?:\d[\s-]?){9})")


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def scrub_sensitive(text: str) -> str:
    """Replace card numbers (Luhn-valid, 13-19 digits) and SSN-shaped numbers with [REDACTED]."""
    if not text or not any(ch.isdigit() for ch in text):
        return text

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return REDACTED if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    text = _DIGIT_RUN.sub(card, text)
    text = _SPOKEN_SSN.sub(lambda m: m.group(1) + REDACTED, text)
    return _SSN.sub(REDACTED, text)
