"""Years-of-experience (YoE) extractor.

Parses a required years-of-experience signal from a posting's description (falling back
to the title). It is deliberately conservative: precision over recall. A candidate only
counts when the number is tied to a ``year(s)/yr(s)`` unit *and* either an experience cue
is nearby (e.g. "of experience", "working", "in <field>") or a clear requirement qualifier
is present (e.g. "minimum", "at least", a trailing "+", or an explicit range). Phrases that
talk about something other than experience (vesting schedules, company age, ages, "X years
ago", "the last 5 years") are filtered out. When unsure, returns ``(None, None)``.
"""

from __future__ import annotations

import re

from .base import ExtractInput, register_extractor

__all__ = ["YoeExtractor"]


# --- word -> int (zero..twenty plus the tens) --------------------------------

_WORD_NUMBERS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# Longest first so "seventeen" wins over "seven", etc.
_WORD_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_NUM = rf"(?:\d{{1,2}}|\b(?:{_WORD_ALT})\b)"

# Requirement qualifiers that may precede the number.
_PREFIX = (
    r"minimum of|at minimum|at least|no less than|no more than|"
    r"more than|less than|up to|at most|over|under|"
    r"minimum|maximum|max|min\.?|between|around|about|approximately|roughly"
)

_UNIT = r"(?:years?|yrs?)"

_PHRASE = re.compile(
    rf"(?:(?P<prefix>{_PREFIX})\s+)?"
    rf"(?P<n1>{_NUM})\s*"
    rf"(?:(?P<sep>-|–|—|to|and)\s*(?P<n2>{_NUM})\s*)?"
    rf"\+?\s*"
    rf"{_UNIT}\b"
    rf"(?:\s*(?P<suffix>\+|or\s+more|or\s+above|or\s+greater|or\s+higher|minimum|min|plus))?",
    re.IGNORECASE,
)

# Experience cues looked for around a candidate (excludes a bare "in" on purpose, so that
# future-tense phrases like "in 5 years we grew" are not treated as experience).
_CUE = re.compile(
    r"\b(?:experience|exp|working|profession\w*|industr\w*|background|"
    r"hands[\s-]?on|expertise|track\s+record|developing|building|"
    r"programming|engineering)\b",
    re.IGNORECASE,
)
# "<n> years in <field>" — an in-field cue that only counts *after* the unit.
_IN_FIELD_AFTER = re.compile(r"^\W*in\s+[a-z]", re.IGNORECASE)

# Disqualifiers: text right after the unit that means this is not about experience.
_DQ_AFTER = re.compile(
    r"^\W*(?:ago|old|of\s+age|vesting|vest|cliff|warranty|lease|"
    r"sentence|imprisonment|in\s+prison|in\s+jail|running|"
    r"of\s+growth|of\s+age)\b",
    re.IGNORECASE,
)
# Disqualifiers: text right before the number (timeframes, ages).
_DQ_BEFORE = re.compile(r"\b(?:last|past|next|within|aged?|every|for\s+the)\s+$", re.IGNORECASE)

# Prefixes that, on their own, signal a real requirement (no extra cue needed).
_REQUIRE_PREFIXES = {
    "minimum of",
    "at minimum",
    "at least",
    "no less than",
    "more than",
    "minimum",
    "min",
    "min.",
    "over",
}
# Prefixes that flip the lone number into an upper bound.
_MAX_PREFIXES = {"up to", "at most", "no more than", "less than", "under", "maximum", "max"}

# Reject implausible YoE values (ages, calendar spans, typos).
_MAX_PLAUSIBLE = 50


def _to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


class YoeExtractor:
    """Extract ``(min_years, max_years)`` of required experience from a posting."""

    name = "yoe"

    def extract(self, inp: ExtractInput) -> tuple[int | None, int | None]:
        """Return ``(min_years, max_years)``; ``(None, None)`` when nothing found."""
        for text in (inp.description_text, inp.title):
            if not text:
                continue
            result = self._parse(text)
            if result != (None, None):
                return result
        return (None, None)

    def _parse(self, text: str) -> tuple[int | None, int | None]:
        for m in _PHRASE.finditer(text):
            value = self._value(m)
            if value is None:
                continue
            if self._is_valid(text, m):
                return value
        return (None, None)

    def _value(self, m: re.Match[str]) -> tuple[int | None, int | None] | None:
        n1 = _to_int(m.group("n1"))
        if n1 is None or n1 > _MAX_PLAUSIBLE:
            return None
        raw_n2 = m.group("n2")
        if raw_n2 is not None:
            n2 = _to_int(raw_n2)
            if n2 is None or n2 > _MAX_PLAUSIBLE:
                return None
            lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
            return (lo, hi)
        prefix = (m.group("prefix") or "").strip().lower()
        if prefix in _MAX_PREFIXES:
            return (None, n1)
        return (n1, None)

    def _is_valid(self, text: str, m: re.Match[str]) -> bool:
        before = text[max(0, m.start() - 40) : m.start()]
        after = text[m.end() : m.end() + 45]

        # Hard vetoes: phrase is clearly not about experience.
        if _DQ_AFTER.match(after) or _DQ_BEFORE.search(before):
            return False

        if _CUE.search(before) or _CUE.search(after) or _IN_FIELD_AFTER.match(after):
            return True

        # No cue — accept only when the phrase itself reads as a requirement.
        prefix = (m.group("prefix") or "").strip().lower()
        if prefix in _REQUIRE_PREFIXES:
            return True
        if m.group("n2") is not None:  # an explicit range
            return True
        if m.group("suffix") is not None:
            return True
        return "+" in m.group(0)


register_extractor(YoeExtractor())
