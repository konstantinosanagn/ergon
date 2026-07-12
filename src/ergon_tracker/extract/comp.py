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

import math
import re
from dataclasses import dataclass
from typing import Any

from ..models import Salary, SalaryInterval
from .base import ExtractInput, register_extractor

__all__ = ["CompExtractor", "coerce_amount", "parse_salary"]


def coerce_amount(value: Any) -> float | None:
    """Coerce a salary amount (number or numeric string) to a positive finite float, or None.

    Shared by providers whose structured payloads carry a min/max salary bound as either a
    number or a string (recruitee, teamtailor, join). Rejects ``bool`` (a ``bool`` is an
    ``int`` subclass and would otherwise silently coerce to 0.0/1.0), non-numeric strings,
    ``NaN``/``Inf`` (valid ``float()`` literals), and non-positive values, so no garbage
    ``Salary`` ever reaches the index.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        f = float(value)
        return f if math.isfinite(f) and f > 0 else None
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if not stripped:
            return None
        try:
            f = float(stripped)
        except ValueError:
            return None
        return f if math.isfinite(f) and f > 0 else None
    return None


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
    rf"(?P<k>\s*[kK](?![A-Za-z]))?"  # boundary: don't eat the K of "Key"/"Kapitus"
    rf"(?:\s*(?P<post>USD|CAD|AUD|GBP|EUR))?",
    re.IGNORECASE,
)

# A range separator that sits *between* two amounts and nothing else.  Tolerates
# an interval token glued to the first amount ("$97,000/year - $127,000/year").
_SEP = re.compile(
    r"^\s*(?:/\s*(?:year|yr|annum|hour|hr)|per\s+(?:year|annum|hour))?"
    r"\s*(?:-|–|—|to|and|through)\s*$",
    re.IGNORECASE,
)

# Retirement plans — never salary. Skip "401k"/"401(k)" unless money-prefixed.
_RETIREMENT = re.compile(r"(?<![\$£€])\b401\s*\(?\s*k\s*\)?", re.IGNORECASE)

# Magnitude suffix/word right after an amount marks a company-scale figure
# ("$5M", "$2B", "$500 million", "$42 Mil", "$8 trillion") — pay is never millions.
_MAGNITUDE = re.compile(r"\s*(?:millions?|billions?|trillions?|mil|mn|bn|tn|[mb])\b", re.IGNORECASE)

# Per-interval sanity bands: pay outside these is a fee, perk, stipend, or a
# financial/scale figure ("$50/month gym", "$5,250 per year tuition", "$1.50/hr
# shift differential"), not a wage.
_BANDS: dict[SalaryInterval, tuple[float, float]] = {
    SalaryInterval.HOUR: (7, 300),
    SalaryInterval.DAY: (56, 3_000),
    SalaryInterval.WEEK: (280, 15_000),
    SalaryInterval.MONTH: (1_200, 60_000),
    SalaryInterval.YEAR: (15_000, 600_000),
}

# Financial / corporate-scale context. A money mention sitting next to one of
# these is funding, revenue, valuation, or AUM — not pay.
_FINANCIAL = re.compile(
    r"\b(?:funding|funded|fund(?:s|\s+size)?|raised|raise|raising|"
    r"valuation|valued|revenue|arr|mrr|series\s+[a-e]\b|market\s+cap|aum|"
    r"assets\s+under\s+management|investments?|invest(?:ed|ors?|ing)?|"
    r"backed\s+by|portfolio|in\s+sales|in\s+revenue|grants?|budget|to\s+spend)\b",
    re.IGNORECASE,
)

# Head/user-count context ("10,000 customers", "$5,000 per employee"). Unlike the
# financial words above, these never annotate a proper "$X - $Y" currency range —
# pay-transparency ranges legitimately sit inside sentences like "US employees in
# New York: $121,700 - $152,100" — so count words spare currency-marked ranges.
_SCALE = re.compile(
    r"\b(?:customers?|users?|employees?|people|members?|downloads?|installs?|"
    r"companies|countries|clients?|subscribers?|nationalities|businesses|"
    r"creators?|startups?|enterprises?)\b",
    re.IGNORECASE,
)

# Measurement/duration unit right after a number: "100 lbs", "15 hours / week",
# "10-40 hours", "12,000 acres" — quantities, never pay. (Interval words like
# "hour"/"hourly" are matched by _INTERVAL first; the plurals here are the
# "N hours" quantity form, which _INTERVAL deliberately does not match.)
_UNIT_AFTER = re.compile(
    r"\s*(?:lbs?|pounds?|kgs?|kilograms?|(?:metric\s+)?tons?|tonnes?|"
    r"hours?|hrs?|heures?|minutes?|mins?|days?|weeks?|months?|years?|yrs?|"
    r"miles?|km|acres?|feet|ft|inch(?:es)?|meters?|metres?|shifts?|sq\b|square)\b",
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


# Frequency word shortly *before* the amount: "Hourly Rate: $28.00",
# "annual salary: $184,000". Bounded so a stray "per week" a sentence away
# can't relabel an annual figure (and callers band-check the result anyway).
_INTERVAL_BEFORE = re.compile(
    r"\b(?P<unit>annual(?:ly|ized)?|yearly|hourly|monthly|weekly|daily|"
    r"per\s+(?:year|annum|hour|month|week|day))\b[^\n$€£\d]{0,20}$",
    re.IGNORECASE,
)
_BEFORE_MAP: list[tuple[str, SalaryInterval]] = [
    ("annu", SalaryInterval.YEAR),
    ("year", SalaryInterval.YEAR),
    ("hour", SalaryInterval.HOUR),
    ("month", SalaryInterval.MONTH),
    ("week", SalaryInterval.WEEK),
    ("da", SalaryInterval.DAY),
]


def _interval_before(text: str, pos: int) -> SalaryInterval | None:
    m = _INTERVAL_BEFORE.search(text[max(0, pos - 30) : pos])
    if not m:
        return None
    unit = re.sub(r"^per\s+", "", m.group("unit").lower())
    for prefix, itv in _BEFORE_MAP:
        if unit.startswith(prefix):
            return itv
    return None


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
    near_cue_tight: bool


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
        # skip digits glued into a larger token (URLs, SKUs, hashes: "5dd72739dad").
        # Anchor on the number itself — the match can start at leading whitespace —
        # and let a symbol-led currency ("USD$23") separate digits from prose.
        pre_tok = m.group("pre")
        anchor = m.start("pre") if pre_tok else m.start("num")
        prev = text[anchor - 1 : anchor] if anchor > 0 else ""
        glued_prev = bool(prev) and (prev.isalnum() or prev in "_/")
        if glued_prev and not (pre_tok and pre_tok[0] in "$€£"):
            continue
        # skip glued trailing letters ("5o6x7a" URL junk, "400M" magnitude).
        tail = text[m.end() : m.end() + 1]
        if tail.isalpha():
            continue
        # skip percentages (equity, raises): "0.5%", "$50%"
        if tail == "%":
            continue
        # skip plain quantities: "100 lbs", "15 hours / week", "12,000 acres" —
        # but only bare numbers; "$24.50 Hours: ..." is pay next to layout text.
        if not pre_tok and _UNIT_AFTER.match(text[m.end() : m.end() + 16]):
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
    return _near(cues, start, end, 100, 30)


def _near_cue_tight(cues: list[tuple[int, int]], start: int, end: int) -> bool:
    """A cue directly against the amount — required for bare, unmarked numbers."""
    return _near(cues, start, end, 40, 12)


def _effective_interval(c: _Cand) -> SalaryInterval:
    return c.interval or _infer_interval(c)


def _band_fits(interval: SalaryInterval, *vals: float | None) -> bool:
    lo_band, hi_band = _BANDS[interval]
    return all(v is None or lo_band <= v <= hi_band for v in vals)


def _in_band(c: _Cand) -> bool:
    return _band_fits(_effective_interval(c), c.min_amount, c.max_amount)


def _pick_interval(
    text: str,
    start: int,
    end: int,
    lo: float | None,
    hi: float | None,
    money_marked: bool,
) -> SalaryInterval | None:
    """Explicit frequency for an amount span: after the span, else (band-checked) before.

    The before-window is only trusted for currency/K-marked amounts — "5 days per
    week on a 1099 Contractor basis" must not turn a bare 1099 into weekly pay.
    """
    interval = _interval_after(text, end)
    if interval is None and money_marked:
        before = _interval_before(text, start)
        if before is not None and _band_fits(before, lo, hi):
            interval = before
    return interval


def _build_candidates(text: str) -> list[_Cand]:
    amts = _scan_amounts(text)
    cues = [m.span() for m in _CUE.finditer(text)]
    fins = [m.span() for m in _FINANCIAL.finditer(text)]
    cands: list[_Cand] = []
    i = 0
    while i < len(amts):
        a = amts[i]
        # Try to merge a..b into a range when only a separator sits between them.
        if i + 1 < len(amts):
            b = amts[i + 1]
            if _SEP.match(text[a.end : b.start]):
                lo, hi = a.value, b.value
                # Spread a lone trailing/leading K across the range: "$70-130k",
                # "$190k-237".
                if b.has_k and not a.has_k and lo <= 999 and lo * 1000 <= hi * 10:
                    lo *= 1000
                elif a.has_k and not b.has_k and hi <= 999 and hi * 1000 >= lo:
                    hi *= 1000
                if lo > hi:
                    lo, hi = hi, lo
                ccy = a.currency or b.currency
                end = b.end
                marked = bool(ccy or a.has_k or b.has_k)
                interval = _pick_interval(text, a.start, end, lo, hi, marked)
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
                        near_cue_tight=_near_cue_tight(cues, a.start, end),
                    )
                )
                i += 2
                continue

        # Single amount — possibly an open-ended bound ("from $90k" / "up to $200k").
        # Drop any trailing currency token so the cue word sits at the end of the window.
        before = _CUR_TAIL.sub("", text[max(0, a.num_pos - 20) : a.num_pos])
        interval = _pick_interval(
            text, a.start, a.end, a.value, a.value, bool(a.currency or a.has_k)
        )
        smin: float | None
        smax: float | None
        if _UP_TO.search(before):
            smin, smax = None, a.value
        elif _FROM.search(before):
            smin, smax = a.value, None
        elif text[a.end : a.end + 1] == "+":
            smin, smax = a.value, None  # "$85,000+" — open-ended above
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
                near_cue_tight=_near_cue_tight(cues, a.start, a.end),
            )
        )
        i += 1

    # Drop candidates embedded in a financial or head-count context, unless a
    # comp cue is nearby or the amount carries an explicit in-band pay interval
    # ("$17.20 hourly" survives "Employee Discount"; "$5,250 per year tuition"
    # fails the band). Count words spare currency-marked ranges (see _SCALE).
    scales = [m.span() for m in _SCALE.finditer(text)]
    out: list[_Cand] = []
    for c in cands:
        rescued = c.near_cue or (c.interval is not None and _in_band(c))
        if _near(fins, c.start, c.end, 28, 22) and not rescued:
            continue
        if (
            _near(scales, c.start, c.end, 28, 22)
            and not rescued
            and not (c.is_range and c.currency)
        ):
            continue
        out.append(c)
    return out


def _merge_range_blocks(text: str, ranges: list[_Cand]) -> list[_Cand]:
    """Collapse per-state/per-level range blocks into min-of-mins / max-of-maxes.

    Pay-transparency postings often list one range per location or level within a
    few lines of each other; the posting-level truth is the envelope of them all.
    Only ranges with compatible currency and interval, separated by a short gap,
    are merged.
    """
    merged: list[_Cand] = []
    for c in sorted(ranges, key=lambda r: r.start):
        if merged:
            p = merged[-1]
            gap_ok = 0 <= c.start - p.end <= 160
            ccy_ok = p.currency is None or c.currency is None or p.currency == c.currency
            itv_ok = p.interval is None or c.interval is None or p.interval == c.interval
            if gap_ok and ccy_ok and itv_ok:
                merged[-1] = _Cand(
                    start=p.start,
                    end=c.end,
                    min_amount=min(v for v in (p.min_amount, c.min_amount) if v is not None),
                    max_amount=max(v for v in (p.max_amount, c.max_amount) if v is not None),
                    currency=p.currency or c.currency,
                    has_k=p.has_k or c.has_k,
                    is_range=True,
                    interval=p.interval or c.interval,
                    near_cue=p.near_cue or c.near_cue,
                    near_cue_tight=p.near_cue_tight or c.near_cue_tight,
                )
                continue
        merged.append(c)
    return merged


def _accept(c: _Cand) -> bool:
    ref = c.max_amount if c.max_amount is not None else c.min_amount
    if ref is None:
        return False
    # Pay outside the plausible band for its interval is a fee/perk/scale figure.
    if not _in_band(c):
        return False
    if c.is_range:
        # A range needs a positive comp signal: currency, a "k", an interval, or a
        # nearby cue word ("Salary: 120,000 - 140,000") — the band check above
        # already weeds out stray numeric ranges (years, ZIP spans, accounting
        # codes rarely land inside a plausible pay band *and* next to a cue).
        return bool(c.currency or c.has_k or c.interval is not None or c.near_cue_tight)
    if c.interval is not None:
        return True
    if (c.currency or c.has_k) and c.near_cue:
        return True
    return bool(c.near_cue_tight and ref >= 1000)


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
    cands = _build_candidates(text)
    # Two passes: ranges first (merging per-state/per-level blocks), singles after.
    ranges = _merge_range_blocks(text, [c for c in cands if c.is_range and _accept(c)])
    singles = [c for c in cands if not c.is_range and _accept(c)]
    accepted = [c for c in ranges if _accept(c)] + singles
    if not accepted:
        return None
    best = max(accepted, key=lambda c: (_score(c), -c.start))
    if best.min_amount is None and best.max_amount is None:
        return None
    interval = _effective_interval(best)
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
