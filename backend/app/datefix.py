"""Deterministic correction of weekday names the model attaches to dates.

The model sometimes pairs the right date with the wrong weekday ("Thursday, October 2nd" for a Friday). A caller who writes
"Thursday" in their diary misses the appointment. The tool results carry the correct date, so any "<Weekday>, <Month> <day>"
that the assistant is about to say is recomputed here from the calendar and the weekday word is replaced when it is wrong.

Only an explicit weekday immediately followed by a month and day is touched. Anything else is left exactly as written.
"""
from __future__ import annotations

import re
from datetime import date, datetime

_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_ES_DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_ES_MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

_EN = re.compile(
    r"\b(?P<wd>" + "|".join(_EN_DAYS) + r")(?P<sep>,?\s+(?:the\s+)?)(?P<month>" + "|".join(_EN_MONTHS) + r")\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b",
    re.I)
_ES = re.compile(
    r"\b(?P<wd>lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)(?P<sep>,?\s+(?:el\s+)?)(?P<day>\d{1,2})\s+de\s+(?P<month>" + "|".join(_ES_MONTHS) + r")\b",
    re.I)


def _resolve(month: int, day: int, today: date) -> date | None:
    """The date the caller means: this year, or next year if it is already well in the past."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if (today - d).days <= 30:
            return d
    return None


def _match_case(original: str, replacement: str) -> str:
    return replacement.capitalize() if original[:1].isupper() else replacement.lower()


def fix_weekdays(text: str, today: date | datetime) -> tuple[str, int]:
    """Returns (corrected text, number of weekday words corrected)."""
    if not text:
        return text, 0
    today = today.date() if isinstance(today, datetime) else today
    fixed = 0

    def en(m: re.Match) -> str:
        nonlocal fixed
        d = _resolve(_EN_MONTHS.index(m["month"].capitalize()) + 1, int(m["day"]), today)
        if d is None:
            return m.group(0)
        right = _EN_DAYS[d.weekday()]
        if m["wd"].lower() == right.lower():
            return m.group(0)
        fixed += 1
        return m.group(0).replace(m["wd"], _match_case(m["wd"], right), 1)

    def es(m: re.Match) -> str:
        nonlocal fixed
        d = _resolve(_ES_MONTHS.index(m["month"].lower()) + 1, int(m["day"]), today)
        if d is None:
            return m.group(0)
        right = _ES_DAYS[d.weekday()]
        norm = lambda s: s.lower().replace("é", "e").replace("á", "a")
        if norm(m["wd"]) == norm(right):
            return m.group(0)
        fixed += 1
        return m.group(0).replace(m["wd"], _match_case(m["wd"], right), 1)

    text = _EN.sub(en, text)
    text = _ES.sub(es, text)
    return text, fixed
