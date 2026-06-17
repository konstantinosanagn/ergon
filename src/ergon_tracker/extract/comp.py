"""Compensation (salary) extractor.

Pulls a structured :class:`~ergon_tracker.models.Salary` out of free-text job postings.

Two-stage behaviour:

1. If the provider already supplied a structured salary (Ashby/Lever/Greenhouse
   often do), trust it and pass it straight through.
2. Otherwise parse the description text (falling back to the title) with a set of
   currency/amount/interval rules tuned to be *robust against false positives* —
   we would rather return ``None`` than invent a number from a ZIP code, a phone
   number, a ``401(k)`` mention, "5+ years", or an equity percentage.

The rules live here rather than in the frozen contract so they can later be
swapped for a trained model behind the same ``FieldExtractor`` seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Salary, SalaryInterval
from .base import ExtractInput, register_extractor

__all__ = ["CompExtractor", "parse_salary"]


# --- currency -----------------------------------------------------------------

_SYMBOL_TO_CCY = {
    "$": "USD",
    "US$": "USD",
    "C$": "CAD",
    "CA$": "CAD",
    "A$": "AUD",
    "£": "GBP",
    "€": "EUR",
}
_KNOWN_CODES = {"USD", "CAD", "AUD", "GBP", "EUR"}


def _currency(token: str | None) -> str | None:
    if not token:
        return None
    t = token.strip()
    if t in _SYMBOL_TO_CCY:
        return _SYMBOL_TO_CCY[t]
    up = t.upper()
    if up in _SYMBOL_TO_CCY:
        return _SYMBOL_TO_CCY[up]
    if up in _KNOWN_CODES:
        return up
    return None


# --- regexes ------------------------------------------------------------------

# Order matters: multi-char symbols/codes before the bare ``$``.
_CUR = r"CA\$|C\$|US\$|A\$|\$|£|€|USD|CAD|AUD|GBP|EUR"
_NUM = r"\d[\d.,]*\d|\d"

_AMOUNT = re.compile(
    rf"(?P<pre>{_CUR})?\s*"
    rf"(?P<num>{_NUM})"
    rf"(?P<k>\s*[kK])?"
    rf"(?:\s*(?P<post>USD|CAD|AUD|GBP|EUR))?",
    re.IGNORECASE,
)

# A range separator that sits *between* two amounts and nothing else.
_SEP = re.compile(r"^\s*(?:-|–|—|to|and|through)\s*$", re.IGNORECASE)

# Retirement plans — never salary. Skip "401k"/"401(k)" unless money-prefixed.
_RETIREMENT = re.compile(r"(?<![\$£€])\b401\s*\(?\s*k\s*\)?", re.IGNORECASE)

# Magnitude suffix/word right after an amount marks a company-scale figure
# ("$5M", "$2B", "$500 million", "$8 trillion") — personal pay is never millions.
_MAGNITUDE = re.compile(r"\s*(?:millions?|billions?|trillions?|mn|bn|tn|[mb])\b", re.IGNORECASE)

# A salary almost never exceeds this per period; bigger ⇒ financial/scale figure.
_MAX_COMP = 1_000_000.0

# Financial / corporate-scale context. A money mention sitting next to one of
# these is funding, revenue, valuation, AUM, or a head/user count — not pay.
_FINANCIAL = re.compile(
    r"\b(?:funding|funded|fund(?:s|\s+size)?|raised|raise|raising|"
    r"valuation|valued|revenue|arr|mrr|series\s+[a-e]\b|market\s+cap|aum|"
    r"assets\s+under\s+management|investments?|invest(?:ed|ors?|ing)?|"
    r"backed\s+by|portfolio|in\s+sales|in\s+revenue|grants?|budget|to\s+spend|"
    r"customers?|users?|employees?|people|members?|downloads?|installs?|"
    r"companies|countries|clients?|subscribers?|nationalities|businesses|"
    r"creators?|startups?|enterprises?)\b",
    re.IGNORECASE,
)

# Salary cue words used to give nearby numbers the benefit of the doubt.
_CUE = re.compile(
    r"\b(?:salary|salaries|compensation|comp(?:ensation)?|pay|payscale|"
    r"wage|wages|ote|on[- ]target\s+earnings|remuneration|"
    r"base(?:\s+(?:pay|salary))?|earn(?:s|ings)?|range)\b",
    re.IGNORECASE,
)

# Interval immediately following an amount, e.g. "/year", "per hour", "annually".
_INTERVAL = re.compile(
    r"\s*(?:(?:/|per\s+|an?\s+)\s*)?"
    r"(?P<unit>annually|annum|annual|yearly|year|yr|hourly|hour|hr|"
    r"monthly|month|mo|weekly|week|wk|daily|day|h)\b",
    re.IGNORECASE,
)
_PA = re.compile(r"\s*p\.?\s*a\.?(?![a-z])", re.IGNORECASE)

_CUR_TAIL = re.compile(rf"\s*(?:{_CUR})?\s*$", re.IGNORECASE)
_UP_TO = re.compile(
    r"(?:up\s*to|upto|maximum|max(?:\.|imum)?\s+of|under|no\s+more\s+than)\s*$", re.I
)
_FROM = re.compile(
    r"(?:from|starting(?:\s+at)?|start(?:s|ing)?\s+at|at\s+least|minimum|min\.?\s+of|above|"
    r"north\s+of)\s*$",
    re.IGNORECASE,
)


# --- number parsing -----------------------------------------------------------


def _parse_number(num: str, has_k: bool) -> float | None:
    """Parse a localized number string into a float (US ``80,000.00`` & EU ``80.000,00``)."""
    s = num.strip()
    has_comma = "," in s
    has_dot = "." in s
    try:
        if has_comma and has_dot:
            # The right-most separator is the decimal point.
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")  # EU: 80.000,00
            else:
                s = s.replace(",", "")  # US: 80,000.00
        elif has_comma:
            parts = s.split(",")
            if len(parts) == 2 and len(parts[1]) != 3:
                s = s.replace(",", ".")  # decimal comma: 1,5
            else:
                s = s.replace(",", "")  # thousands: 120,000 / 1,234,567
        elif has_dot:
            parts = s.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                s = s.replace(".", "")  # thousands: 1.234.567 / 120.000
            # else: genuine decimal, keep as-is
        val = float(s)
    except ValueError:
        return None
    if has_k:
        val *= 1000
    return val


# --- interval detection -------------------------------------------------------

_UNIT_MAP: dict[str, SalaryInterval] = {}
for _u in ("annually", "annum", "annual", "yearly", "year", "yr"):
    _UNIT_MAP[_u] = SalaryInterval.YEAR
for _u in ("hourly", "hour", "hr", "h"):
    _UNIT_MAP[_u] = SalaryInterval.HOUR
for _u in ("monthly", "month", "mo"):
    _UNIT_MAP[_u] = SalaryInterval.MONTH
for _u in ("weekly", "week", "wk"):
    _UNIT_MAP[_u] = SalaryInterval.WEEK
for _u in ("daily", "day"):
    _UNIT_MAP[_u] = SalaryInterval.DAY


def _interval_after(text: str, pos: int) -> SalaryInterval | None:
    window = text[pos : pos + 18]
    if _PA.match(window):
        return SalaryInterval.YEAR
    m = _INTERVAL.match(window)
    if not m:
        return None
    return _UNIT_MAP.get(m.group("unit").lower())


# --- candidate model ----------------------------------------------------------


@dataclass
class _Cand:
    start: int
    end: int
    min_amount: float | None
    max_amount: float | None
    currency: str | None
    has_k: bool
    is_range: bool
    interval: SalaryInterval | None
    near_cue: bool


@dataclass
class _Amt:
    start: int
    end: int
    num_pos: int
    value: float
    currency: str | None
    has_k: bool


def _scan_amounts(text: str) -> list[_Amt]:
    retire_spans = [m.span() for m in _RETIREMENT.finditer(text)]
    out: list[_Amt] = []
    for m in _AMOUNT.finditer(text):
        value = _parse_number(m.group("num"), bool(m.group("k")))
        if value is None or value <= 0:
            continue
        span = m.span()
        if any(span[0] < re_end and rs < span[1] for rs, re_end in retire_spans):
            continue
        # skip percentages (equity, raises): "0.5%", "$50%"
        tail = text[m.end() : m.end() + 1]
        if tail == "%":
            continue
        # skip magnitude figures: "$5M", "$2B", "$500 million", "$8 trillion".
        if _MAGNITUDE.match(text[m.end() : m.end() + 12]):
            continue
        out.append(
            _Amt(
                start=span[0],
                end=span[1],
                num_pos=m.start("num"),
                value=value,
                currency=_currency(m.group("pre") or m.group("post")),
                has_k=bool(m.group("k")),
            )
        )
    return out


def _near(spans: list[tuple[int, int]], start: int, end: int, before: int, after: int) -> bool:
    for cs, ce in spans:
        if ce <= start and start - ce <= before:
            return True
        if cs >= end and cs - end <= after:
            return True
    return False


def _near_cue(cues: list[tuple[int, int]], start: int, end: int) -> bool:
    return _near(cues, start, end, 60, 30)


def _build_candidates(text: str) -> list[_Cand]:
    amts = _scan_amounts(text)
    cues = [m.span() for m in _CUE.finditer(text)]
    fins = [m.span() for m in _FINANCIAL.finditer(text)]
    # Drop amounts embedded in a financial/scale context unless a comp cue is
    # at least as close (a real salary cue wins over a stray distractor word).
    amts = [
        a
        for a in amts
        if not (_near(fins, a.start, a.end, 28, 22) and not _near_cue(cues, a.start, a.end))
    ]
    cands: list[_Cand] = []
    i = 0
    while i < len(amts):
        a = amts[i]
        # Try to merge a..b into a range when only a separator sits between them.
        if i + 1 < len(amts):
            b = amts[i + 1]
            if _SEP.match(text[a.end : b.start]):
                lo, hi = a.value, b.value
                if lo > hi:
                    lo, hi = hi, lo
                ccy = a.currency or b.currency
                end = b.end
                interval = _interval_after(text, end)
                cands.append(
                    _Cand(
                        start=a.start,
                        end=end,
                        min_amount=lo or None,
                        max_amount=hi or None,
                        currency=ccy,
                        has_k=a.has_k or b.has_k,
                        is_range=True,
                        interval=interval,
                        near_cue=_near_cue(cues, a.start, end),
                    )
                )
                i += 2
                continue

        # Single amount — possibly an open-ended bound ("from $90k" / "up to $200k").
        # Drop any trailing currency token so the cue word sits at the end of the window.
        before = _CUR_TAIL.sub("", text[max(0, a.num_pos - 20) : a.num_pos])
        interval = _interval_after(text, a.end)
        smin: float | None
        smax: float | None
        if _UP_TO.search(before):
            smin, smax = None, a.value
        elif _FROM.search(before):
            smin, smax = a.value, None
        else:
            smin, smax = a.value, a.value
        cands.append(
            _Cand(
                start=a.start,
                end=a.end,
                min_amount=smin,
                max_amount=smax,
                currency=a.currency,
                has_k=a.has_k,
                is_range=False,
                interval=interval,
                near_cue=_near_cue(cues, a.start, a.end),
            )
        )
        i += 1
    return cands


def _accept(c: _Cand) -> bool:
    ref = c.max_amount if c.max_amount is not None else c.min_amount
    # Personal pay is never seven figures per period — reject scale figures.
    if ref is not None and ref > _MAX_COMP:
        return False
    if c.is_range:
        # A range needs a positive comp signal: currency, a "k", or an interval.
        # Bare numeric ranges that only sit near a cue are too often stray
        # numbers (e.g. accounting codes), so require a stronger signal.
        return bool(c.currency or c.has_k or c.interval is not None)
    if c.currency:
        return True
    if c.has_k and (c.near_cue or c.interval is not None):
        return True
    if c.interval is not None:
        return True
    return bool(c.near_cue and ref is not None and ref >= 1000)


def _score(c: _Cand) -> int:
    score = 0
    if c.is_range:
        score += 4
    if c.currency:
        score += 3
    if c.near_cue:
        score += 2
    if c.interval is not None:
        score += 1
    return score


def _infer_interval(c: _Cand) -> SalaryInterval:
    ref = c.max_amount if c.max_amount is not None else c.min_amount
    if ref is not None and ref >= 1000:
        return SalaryInterval.YEAR
    return SalaryInterval.HOUR


def parse_salary(text: str | None) -> Salary | None:
    """Best-effort salary parse from free text; ``None`` when nothing confident is found."""
    if not text:
        return None
    accepted = [c for c in _build_candidates(text) if _accept(c)]
    if not accepted:
        return None
    best = max(accepted, key=lambda c: (_score(c), -c.start))
    if best.min_amount is None and best.max_amount is None:
        return None
    interval = best.interval or _infer_interval(best)
    return Salary(
        min_amount=best.min_amount,
        max_amount=best.max_amount,
        currency=best.currency,
        interval=interval,
    )


class CompExtractor:
    """Extract a :class:`Salary` from a posting, trusting structured data first."""

    name = "comp"

    def extract(self, inp: ExtractInput) -> Salary | None:
        existing = inp.structured_salary
        if existing is not None and (
            existing.min_amount is not None or existing.max_amount is not None
        ):
            return existing
        return parse_salary(inp.description_text) or parse_salary(inp.title)


register_extractor(CompExtractor())
