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
from .base import ExtractInput, _vocab, register_extractor

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
    "R$": "BRL",
}
_KNOWN_CODES = {"USD", "CAD", "AUD", "GBP", "EUR", "BRL"}


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
_CUR = r"CA\$|C\$|US\$|A\$|R\$|\$|£|€|USD|CAD|AUD|GBP|EUR|BRL"
_NUM = r"\d[\d.,]*\d|\d"

_AMOUNT = re.compile(
    rf"(?P<pre>{_CUR})?\s*"
    rf"(?P<num>{_NUM})"
    rf"(?P<k>\s*[kK](?![A-Za-z]))?"  # boundary: don't eat the K of "Key"/"Kapitus"
    # Trailing currency: a 3-letter code ("USD") OR a symbol immediately/closely following the
    # number — the dominant German convention ("16,42 €", "2000€", "45.000 € brutto") writes the
    # symbol AFTER, not before. Reuses the full `_CUR` alternation (codes + symbols) so this is
    # language-neutral: a trailing "$"/"£"/"R$" resolves to the same currency either side.
    rf"(?:\s*(?P<post>{_CUR}))?",
    re.IGNORECASE,
)

# French/Spanish convention writes thousands with a plain (or non-breaking) SPACE, not a dot/comma
# ("45 000 €", "1 867,02€") — the base ``_NUM`` above has no space in its character class on
# purpose (English/German never group with spaces), so a French/Spanish-only amount regex adds an
# alternative that greedily eats "\d{1,3}(space\d{3})+" groups, optionally followed by a decimal
# tail, BEFORE falling back to the plain ``_NUM``. Kept as its own table entry (not merged into
# the shared ``_AMOUNT``) so English/German amount-scanning is byte-for-byte unchanged.
_NUM_SPACED = r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?"
_NUM_FRES = rf"(?:{_NUM_SPACED}|{_NUM})"
_AMOUNT_FRES = re.compile(
    rf"(?P<pre>{_CUR})?\s*"
    rf"(?P<num>{_NUM_FRES})"
    rf"(?P<k>\s*[kK](?![A-Za-z]))?"
    rf"(?:\s*(?P<post>{_CUR}))?",
    re.IGNORECASE,
)
_AMOUNT_TABLE: dict[str, re.Pattern[str]] = {"fr": _AMOUNT_FRES, "es": _AMOUNT_FRES}


def _amount_pattern(lang: str) -> re.Pattern[str]:
    """The amount regex for ``lang`` — English/German always get the original ``_AMOUNT``."""
    return _AMOUNT_TABLE.get(lang, _AMOUNT)


# Cross-cutting false-positive guard (all languages): a short, part-hours-shaped number
# ("38,5", "40") immediately followed by an hours-per-week token is a work-schedule figure, never
# pay — the "38,5 h/Woche" trap. Checked before any interval/cue logic so it can never be rescued.
_HOURS_NUMBER = re.compile(r"^\d{1,2}(?:,\d)?$")
_HOURS_TOKEN_AFTER = re.compile(
    r"\s*(?:std\.?|stunden|h\s*/\s*woche|h/semaine|heures/semaine|h/semana|"
    r"horas\s+semanales|ore\s+settimanali|ore/settimana|h/settimana|uur\s+per\s+week)\b",
    re.IGNORECASE,
)

# A range separator that sits *between* two amounts and nothing else.  Tolerates
# an interval token glued to the first amount ("$97,000/year - $127,000/year").
_SEP_EN = re.compile(
    r"^\s*(?:/\s*(?:year|yr|annum|hour|hr)|per\s+(?:year|annum|hour))?"
    r"\s*(?:-|–|—|to|and|through)\s*$",
    re.IGNORECASE,
)
# German: "bis" ("90.000 € bis 130.000 €") and "und" (the second half of "zwischen X und Y") —
# same tolerance for a glued interval token on the first amount.
_SEP_DE = re.compile(
    r"^\s*(?:/\s*(?:jahr|monat|stunde)|pro\s+(?:jahr|monat|stunde))?"
    r"\s*(?:-|–|—|bis|und)\s*$",
    re.IGNORECASE,
)
# French: "à"/"et" (the second half of "entre X et Y") glued to a "/an|/mois|/heure|/jour" or
# "par ..." interval token on the first amount.
_SEP_FR = re.compile(
    r"^\s*(?:/\s*(?:an|mois|heure|jour)|par\s+(?:an|mois|heure|jour))?"
    r"\s*(?:-|–|—|à|et)\s*$",
    re.IGNORECASE,
)
# Spanish: "y" (the second half of "entre X y Y") / "a" (the second half of "de X a Y").
_SEP_ES = re.compile(
    r"^\s*(?:/\s*(?:a[ñn]o|mes|hora|d[ií]a)|al\s+(?:a[ñn]o|mes)|por\s+(?:hora|d[ií]a))?"
    r"\s*(?:-|–|—|y|a)\s*$",
    re.IGNORECASE,
)
_SEP_TABLE: dict[str, re.Pattern[str]] = {"en": _SEP_EN, "de": _SEP_DE, "fr": _SEP_FR, "es": _SEP_ES}

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
    # Ceiling raised 600k -> 2M: the old cap silently rejected top-of-market ranges that are real
    # wages in 2026 (Anthropic $405k-$625k, Netflix staff, quant/HFT total-comp ranges), because a
    # range's MAX just clearing the cap killed the whole match. 2M/yr is still comfortably below
    # company-scale figures, which are separately guarded by _MAGNITUDE (millions/M/B) and
    # _FINANCIAL (funding/revenue/valuation) plus the currency+keyword positive anchor.
    SalaryInterval.YEAR: (15_000, 2_000_000),
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
_CUE_EN = re.compile(
    r"\b(?:salary|salaries|compensation|comp(?:ensation)?|pay|payscale|"
    r"wage|wages|ote|on[- ]target\s+earnings|remuneration|"
    r"base(?:\s+(?:pay|salary))?|earn(?:s|ings)?|range)\b",
    re.IGNORECASE,
)
# German cue nouns + "brutto" (gross — the dominant convention, and itself a strong comp signal).
_CUE_DE = re.compile(
    r"\b(?:gehalt|jahresgehalt|monatsgehalt|stundenlohn|lohn|vergütung|entgelt|"
    r"einstiegsgehalt|brutto|netto)\w*\b",
    re.IGNORECASE,
)
# French cue nouns + "brut"/"net"/"TJM" (freelance day-rate label — itself a strong comp signal).
_CUE_FR = re.compile(
    r"\b(?:salaire|salaires|r[ée]mun[ée]ration\w*|fixe|package|brut|nets?|tjm)\b",
    re.IGNORECASE,
)
# Spanish cue nouns + "bruto"/"neto"/"líquido" (CL) + "SBA"/"RBA" (Salario/Retribución Bruta
# Anual — always gross-annual on its own).
_CUE_ES = re.compile(
    r"\b(?:salario\w*|sueldo\w*|retribuci[oó]n\w*|remuneraci[oó]n\w*|n[oó]mina\w*|"
    r"bruto\w*|neto\w*|l[ií]quido\w*|sba|rba)\b",
    re.IGNORECASE,
)
_CUE_TABLE: dict[str, re.Pattern[str]] = {"en": _CUE_EN, "de": _CUE_DE, "fr": _CUE_FR, "es": _CUE_ES}

# Interval immediately following an amount, e.g. "/year", "per hour", "annually".
_INTERVAL_EN = re.compile(
    r"\s*(?:(?:/|per\s+|an?\s+)\s*)?"
    r"(?P<unit>annually|annum|annual|yearly|year|yr|hourly|hour|hr|"
    r"monthly|month|mo|weekly|week|wk|daily|day|h)\b",
    re.IGNORECASE,
)
# German: "pro/je/im Jahr/Monat/Stunde", "jährlich/monatlich/stündlich" — often with a
# "brutto"/"netto" gross/net qualifier sitting between the amount and the interval word ("59.000
# EUR brutto pro Jahr", "1.580 € brutto im Monat"), so that qualifier is optionally absorbed here
# too. Also matches slash-glued forms ("€/Stunde", "€/Std.") and the "Std."/"Std" abbreviation —
# the trailing-symbol currency fix above means the amount span often already swallows the "€", so
# the interval word can immediately follow a "/" with no space.
_INTERVAL_DE = re.compile(
    r"\s*(?:/\s*)?(?:brutto|netto)?\s*(?:pro\s+|je\s+|im\s+)?"
    r"(?P<unit>jährlich\b|jahr\b|monatlich\b|monat\b|stündlich\b|stunde\b|std\.|std\b)",
    re.IGNORECASE,
)
# French: "par an/mois/heure/jour", "annuel(le)", "mensuel(le)", "de l'heure". "sur 12/13 mois" is
# recognized too (13e-mois convention) so the payment-count modifier alone still resolves to a
# YEAR interval instead of leaving the amount unmarked — it does NOT multiply the figure by
# 12/13 (the stated amount is already the annual one).
_INTERVAL_FR = re.compile(
    r"\s*(?:brut|net)?s?\s*(?:/\s*|par\s+|de\s+l['’])?"
    r"(?P<unit>annuelles?\b|annuels?\b|an\b|mensuelles?\b|mensuels?\b|mois\b|"
    r"jour(?:n[ée]e)?\b|heures?\b|sur\s+1[23]\s+mois\b)",
    re.IGNORECASE,
)
# Spanish: "al año/mes", "anual", "mensual", "por hora/día". "14 pagas"/"12 pagas" (payment-count
# convention) is deliberately NOT specially handled — see comp.py module docstring notes in the
# multilingual spec; the base annual/monthly cue still resolves the interval correctly either way.
_INTERVAL_ES = re.compile(
    r"\s*(?:/\s*)?(?:bruto|neto)?\s*(?:al\s+|por\s+)?"
    r"(?P<unit>a[ñn]o\b|anual\w*\b|mes\b|mensual\w*\b|hora\w*\b|d[ií]a\b)",
    re.IGNORECASE,
)
_INTERVAL_TABLE: dict[str, re.Pattern[str]] = {
    "en": _INTERVAL_EN,
    "de": _INTERVAL_DE,
    "fr": _INTERVAL_FR,
    "es": _INTERVAL_ES,
}
_PA = re.compile(r"\s*p\.?\s*a\.?(?![a-z])", re.IGNORECASE)  # "p.a." — language-neutral

_CUR_TAIL = re.compile(rf"\s*(?:{_CUR})?\s*$", re.IGNORECASE)
_UP_TO_EN = re.compile(
    r"(?:up\s*to|upto|maximum|max(?:\.|imum)?\s+of|under|no\s+more\s+than)\s*$", re.I
)
_UP_TO_DE = re.compile(r"(?:bis\s*zu|maximal|max\.?)\s*$", re.IGNORECASE)
_UP_TO_FR = re.compile(r"(?:jusqu['’]?\s*[àa]|maximum|max\.?)\s*$", re.IGNORECASE)
_UP_TO_ES = re.compile(r"(?:hasta|m[áa]ximo|m[áa]x\.?)\s*$", re.IGNORECASE)
_UP_TO_TABLE: dict[str, re.Pattern[str]] = {
    "en": _UP_TO_EN,
    "de": _UP_TO_DE,
    "fr": _UP_TO_FR,
    "es": _UP_TO_ES,
}

_FROM_EN = re.compile(
    r"(?:from|starting(?:\s+at)?|start(?:s|ing)?\s+at|at\s+least|minimum|min\.?\s+of|above|"
    r"north\s+of)\s*$",
    re.IGNORECASE,
)
_FROM_DE = re.compile(r"(?:mindestens|ab|wenigstens)\s*$", re.IGNORECASE)
_FROM_FR = re.compile(r"(?:[àa]\s+partir\s+de|minimum(?:\s+de)?|d[èe]s|au\s+moins)\s*$", re.IGNORECASE)
_FROM_ES = re.compile(r"(?:desde|a\s+partir\s+de|m[íi]nimo(?:\s+de)?|al\s+menos)\s*$", re.IGNORECASE)
_FROM_TABLE: dict[str, re.Pattern[str]] = {
    "en": _FROM_EN,
    "de": _FROM_DE,
    "fr": _FROM_FR,
    "es": _FROM_ES,
}


# --- number parsing -----------------------------------------------------------


def _parse_number(num: str, has_k: bool) -> float | None:
    """Parse a localized number string into a float (US ``80,000.00`` & EU ``80.000,00``)."""
    # Strip a French/Spanish space-thousands grouping ("45 000", "1 867,02") — a no-op for
    # English/German input, whose amount regex never captures a space in the first place, so this
    # cannot change their behavior.
    s = num.strip().replace("\xa0", "").replace(" ", "")
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
# German unit words — distinct strings from the English ones above, so merging them into the
# same lookup table is safe (no key collisions, no change to English resolution).
for _u in ("jährlich", "jahr"):
    _UNIT_MAP[_u] = SalaryInterval.YEAR
for _u in ("monatlich", "monat"):
    _UNIT_MAP[_u] = SalaryInterval.MONTH
for _u in ("stündlich", "stunde", "std", "std."):
    _UNIT_MAP[_u] = SalaryInterval.HOUR
# French unit words.
for _u in ("annuel", "annuelle", "annuels", "annuelles", "an"):
    _UNIT_MAP[_u] = SalaryInterval.YEAR
for _u in ("mensuel", "mensuelle", "mensuels", "mensuelles", "mois"):
    _UNIT_MAP[_u] = SalaryInterval.MONTH
for _u in ("jour", "journee", "journée"):
    _UNIT_MAP[_u] = SalaryInterval.DAY
for _u in ("heure", "heures"):
    _UNIT_MAP[_u] = SalaryInterval.HOUR
_UNIT_MAP["sur 12 mois"] = SalaryInterval.YEAR
_UNIT_MAP["sur 13 mois"] = SalaryInterval.YEAR
# Spanish unit words.
for _u in ("año", "ano", "anual"):
    _UNIT_MAP[_u] = SalaryInterval.YEAR
for _u in ("mes", "mensual"):
    _UNIT_MAP[_u] = SalaryInterval.MONTH
for _u in ("dia", "día"):
    _UNIT_MAP[_u] = SalaryInterval.DAY
for _u in ("hora", "horas"):
    _UNIT_MAP[_u] = SalaryInterval.HOUR


def _interval_after(text: str, pos: int, lang: str = "en") -> SalaryInterval | None:
    window = text[pos : pos + 18]
    if _PA.match(window):
        return SalaryInterval.YEAR
    m = _vocab(lang, _INTERVAL_TABLE).match(window)
    if not m:
        return None
    return _UNIT_MAP.get(m.group("unit").lower())


# Frequency word shortly *before* the amount: "Hourly Rate: $28.00",
# "annual salary: $184,000". Bounded so a stray "per week" a sentence away
# can't relabel an annual figure (and callers band-check the result anyway).
_INTERVAL_BEFORE_EN = re.compile(
    r"\b(?P<unit>annual(?:ly|ized)?|yearly|hourly|monthly|weekly|daily|"
    r"per\s+(?:year|annum|hour|month|week|day))\b[^\n$€£\d]{0,20}$",
    re.IGNORECASE,
)
_INTERVAL_BEFORE_DE = re.compile(
    r"\b(?P<unit>jährlich|monatlich|stündlich|(?:pro|je)\s+(?:jahr|monat|stunde))\b"
    r"[^\n$€£\d]{0,20}$",
    re.IGNORECASE,
)
# French: "TJM" (freelance daily rate label) resolves to DAY even with no explicit "/jour"
# following, same as "annuel"/"par an" resolves to YEAR.
_INTERVAL_BEFORE_FR = re.compile(
    r"\b(?P<unit>tjm|par\s+jour|journalier\w*|annuel\w*|par\s+an|mensuel\w*|par\s+mois|"
    r"par\s+heure)\b[^\n$€£\d]{0,20}$",
    re.IGNORECASE,
)
# Spanish: "SBA"/"RBA" (Salario/Retribución Bruta Anual) resolves to YEAR on its own.
_INTERVAL_BEFORE_ES = re.compile(
    r"\b(?P<unit>sba|rba|anual\w*|al\s+a[ñn]o|mensual\w*|al\s+mes|por\s+hora)\b"
    r"[^\n$€£\d]{0,20}$",
    re.IGNORECASE,
)
_INTERVAL_BEFORE_TABLE: dict[str, re.Pattern[str]] = {
    "en": _INTERVAL_BEFORE_EN,
    "de": _INTERVAL_BEFORE_DE,
    "fr": _INTERVAL_BEFORE_FR,
    "es": _INTERVAL_BEFORE_ES,
}
_BEFORE_MAP_EN: list[tuple[str, SalaryInterval]] = [
    ("annu", SalaryInterval.YEAR),
    ("year", SalaryInterval.YEAR),
    ("hour", SalaryInterval.HOUR),
    ("month", SalaryInterval.MONTH),
    ("week", SalaryInterval.WEEK),
    ("da", SalaryInterval.DAY),
]
_BEFORE_MAP_DE: list[tuple[str, SalaryInterval]] = [
    ("pro jahr", SalaryInterval.YEAR),
    ("je jahr", SalaryInterval.YEAR),
    ("jähr", SalaryInterval.YEAR),
    ("pro monat", SalaryInterval.MONTH),
    ("je monat", SalaryInterval.MONTH),
    ("monat", SalaryInterval.MONTH),
    ("pro stunde", SalaryInterval.HOUR),
    ("je stunde", SalaryInterval.HOUR),
    ("stünd", SalaryInterval.HOUR),
]
_BEFORE_MAP_FR: list[tuple[str, SalaryInterval]] = [
    ("tjm", SalaryInterval.DAY),
    ("par jour", SalaryInterval.DAY),
    ("journalier", SalaryInterval.DAY),
    ("par an", SalaryInterval.YEAR),
    ("annuel", SalaryInterval.YEAR),
    ("par mois", SalaryInterval.MONTH),
    ("mensuel", SalaryInterval.MONTH),
    ("par heure", SalaryInterval.HOUR),
]
_BEFORE_MAP_ES: list[tuple[str, SalaryInterval]] = [
    ("sba", SalaryInterval.YEAR),
    ("rba", SalaryInterval.YEAR),
    ("anual", SalaryInterval.YEAR),
    ("al año", SalaryInterval.YEAR),
    ("al ano", SalaryInterval.YEAR),
    ("mensual", SalaryInterval.MONTH),
    ("al mes", SalaryInterval.MONTH),
    ("por hora", SalaryInterval.HOUR),
]
_BEFORE_MAP_TABLE: dict[str, list[tuple[str, SalaryInterval]]] = {
    "en": _BEFORE_MAP_EN,
    "de": _BEFORE_MAP_DE,
    "fr": _BEFORE_MAP_FR,
    "es": _BEFORE_MAP_ES,
}


def _interval_before(text: str, pos: int, lang: str = "en") -> SalaryInterval | None:
    pattern = _vocab(lang, _INTERVAL_BEFORE_TABLE)
    m = pattern.search(text[max(0, pos - 30) : pos])
    if not m:
        return None
    unit = re.sub(r"^per\s+", "", m.group("unit").lower())
    for prefix, itv in _vocab(lang, _BEFORE_MAP_TABLE):
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


def _scan_amounts(text: str, lang: str = "en") -> list[_Amt]:
    retire_spans = [m.span() for m in _RETIREMENT.finditer(text)]
    out: list[_Amt] = []
    for m in _amount_pattern(lang).finditer(text):
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
        # skip hours-per-week figures glued to an hours token ("38,5 h/Woche", "40 Std.") —
        # never pay, regardless of language (see _HOURS_TOKEN_AFTER).
        if _HOURS_NUMBER.match(m.group("num")) and _HOURS_TOKEN_AFTER.match(
            text[m.end() : m.end() + 20]
        ):
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


def _effective_interval(c: _Cand, lang: str = "en") -> SalaryInterval:
    return c.interval or _infer_interval(c, lang)


def _band_fits(interval: SalaryInterval, *vals: float | None) -> bool:
    lo_band, hi_band = _BANDS[interval]
    return all(v is None or lo_band <= v <= hi_band for v in vals)


def _in_band(c: _Cand, lang: str = "en") -> bool:
    return _band_fits(_effective_interval(c, lang), c.min_amount, c.max_amount)


def _pick_interval(
    text: str,
    start: int,
    end: int,
    lo: float | None,
    hi: float | None,
    money_marked: bool,
    lang: str = "en",
) -> SalaryInterval | None:
    """Explicit frequency for an amount span: after the span, else (band-checked) before.

    The before-window is only trusted for currency/K-marked amounts — "5 days per
    week on a 1099 Contractor basis" must not turn a bare 1099 into weekly pay.
    """
    interval = _interval_after(text, end, lang)
    if interval is None and money_marked:
        before = _interval_before(text, start, lang)
        if before is not None and _band_fits(before, lo, hi):
            interval = before
    return interval


def _build_candidates(text: str, lang: str = "en") -> list[_Cand]:
    amts = _scan_amounts(text, lang)
    cues = [m.span() for m in _vocab(lang, _CUE_TABLE).finditer(text)]
    fins = [m.span() for m in _FINANCIAL.finditer(text)]
    cands: list[_Cand] = []
    i = 0
    while i < len(amts):
        a = amts[i]
        # Try to merge a..b into a range when only a separator sits between them.
        if i + 1 < len(amts):
            b = amts[i + 1]
            if _vocab(lang, _SEP_TABLE).match(text[a.end : b.start]):
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
                interval = _pick_interval(text, a.start, end, lo, hi, marked, lang)
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
            text, a.start, a.end, a.value, a.value, bool(a.currency or a.has_k), lang
        )
        smin: float | None
        smax: float | None
        if _vocab(lang, _UP_TO_TABLE).search(before):
            smin, smax = None, a.value
        elif _vocab(lang, _FROM_TABLE).search(before):
            smin, smax = a.value, None
        elif text[a.end : a.end + 1] == "+":
            smin, smax = a.value, None  # "$85,000+" — open-ended above
        elif lang == "de":
            # German convention: a bare, unqualified single figure ("Stundenlohn 13,90 €",
            # "1.580 € brutto im Monat") reads as a floor, not an exact-and-only value — unlike
            # English, where a bare "$85,000" is taken at face value unless explicitly marked
            # open ("+", "from", "up to").
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
        rescued = c.near_cue or (c.interval is not None and _in_band(c, lang))
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


def _accept(c: _Cand, lang: str = "en") -> bool:
    ref = c.max_amount if c.max_amount is not None else c.min_amount
    if ref is None:
        return False
    # Pay outside the plausible band for its interval is a fee/perk/scale figure.
    if not _in_band(c, lang):
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


def _infer_interval(c: _Cand, lang: str = "en") -> SalaryInterval:
    ref = c.max_amount if c.max_amount is not None else c.min_amount
    # German bare figures without an explicit interval word ("2000€ Bruttogehalt", "Bruttogehalt
    # mindestens 3.000,- EUR") read, by convention, as a monthly gross wage in this plausible
    # range — unlike English, where a bare "$85,000" defaults to annual. Only larger bare figures
    # (beyond a plausible monthly gross) fall through to the annual default below.
    if lang == "de" and ref is not None and 1000 <= ref < 20_000:
        return SalaryInterval.MONTH
    if ref is not None and ref >= 1000:
        return SalaryInterval.YEAR
    return SalaryInterval.HOUR


def parse_salary(text: str | None, lang: str = "en") -> Salary | None:
    """Best-effort salary parse from free text; ``None`` when nothing confident is found."""
    if not text:
        return None
    cands = _build_candidates(text, lang)
    # Two passes: ranges first (merging per-state/per-level blocks), singles after.
    ranges = _merge_range_blocks(text, [c for c in cands if c.is_range and _accept(c, lang)])
    singles = [c for c in cands if not c.is_range and _accept(c, lang)]
    accepted = [c for c in ranges if _accept(c, lang)] + singles
    if not accepted:
        return None
    best = max(accepted, key=lambda c: (_score(c), -c.start))
    if best.min_amount is None and best.max_amount is None:
        return None
    interval = _effective_interval(best, lang)
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
        lang = inp.language or "en"
        return parse_salary(inp.description_text, lang) or parse_salary(inp.title, lang)


register_extractor(CompExtractor())
