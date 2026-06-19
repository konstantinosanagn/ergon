"""Geo normalization (rules baseline): fill city/region/country/remote from a location string.

Geo is handled as a per-``Location`` normalizer rather than a posting-level FieldExtractor,
because it refines existing ``Location`` objects in place.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files

from ..models import Location

__all__ = ["normalize_geo", "city_match_terms", "city_matches"]

# Metro/synonym groups: names that denote the SAME city a user means when they type the key.
# High-precision — only true aliases and constituent boroughs/districts (a borough of NYC IS NYC),
# never neighboring metro suburbs (San Jose is NOT San Francisco). Used to widen a city filter so
# "New York" also returns "New York City"/"Brooklyn"/"NYC" labelled postings. Short tokens (<=3,
# e.g. "nyc"/"sf"/"dc") are matched EXACTLY against the parsed city only — never as a substring of
# free text, where they would false-match ("dc" in "dca").
_METRO_GROUPS: list[tuple[str, ...]] = [
    (
        "new york",
        "new york city",
        "nyc",
        "manhattan",
        "brooklyn",
        "queens",
        "the bronx",
        "bronx",
        "staten island",
    ),
    ("san francisco", "sf"),
    ("washington", "washington dc", "washington d.c.", "dc"),
    ("los angeles", "l.a."),
]
_METRO_ALIASES: dict[str, tuple[str, ...]] = {
    member: group for group in _METRO_GROUPS for member in group
}


def city_match_terms(city: str) -> list[str]:
    """Lowercased terms that denote the same city as ``city`` (incl. metro/borough synonyms)."""
    key = city.lower().strip()
    return list(_METRO_ALIASES.get(key, (key,)))


def city_matches(city_query: str, loc_city: str | None, loc_raw: str | None) -> bool:
    """True if a parsed location matches a city filter, with metro-synonym widening.

    Mirrors the index SQL (query.py) so the SDK live path and the index agree. Matches the parsed
    city EXACTLY (trimmed) against the alias set. Deliberately NOT a substring of the raw text:
    "New York"/"Washington" are also US STATE names, so substring matching pulls in whole-state
    postings (e.g. "Armonk, New York") and "Brooklyn Park, MN". Exact city-column match captures
    the labelled variants ("New York City", "Brooklyn", "NYC") without those false positives; users
    wanting free-text location matching use the separate ``location`` filter.
    """
    lc = (loc_city or "").strip().lower()
    return any(lc == t for t in city_match_terms(city_query))


# Country aliases -> canonical name (extend freely).
_COUNTRY_ALIASES: dict[str, str] = {
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "united kingdom": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "uae": "United Arab Emirates",
}
_COUNTRY_NAMES = {
    "united states",
    "united kingdom",
    "canada",
    "germany",
    "france",
    "spain",
    "italy",
    "netherlands",
    "ireland",
    "india",
    "australia",
    "singapore",
    "japan",
    "china",
    "brazil",
    "mexico",
    "poland",
    "sweden",
    "switzerland",
    "portugal",
    "israel",
    "south korea",
    "new zealand",
    "austria",
    "belgium",
    "denmark",
    "norway",
    "finland",
    "czech republic",
    "romania",
    "ukraine",
    "argentina",
    "chile",
    "colombia",
    "philippines",
    "indonesia",
    "vietnam",
    "thailand",
    "malaysia",
    "south africa",
    "nigeria",
    "egypt",
    "turkey",
    "greece",
    "hungary",
    "united arab emirates",
}
for _name in _COUNTRY_NAMES:
    _COUNTRY_ALIASES.setdefault(_name, _name.title())

_US_STATES = {
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "de",
    "fl",
    "ga",
    "hi",
    "id",
    "il",
    "in",
    "ia",
    "ks",
    "ky",
    "la",
    "me",
    "md",
    "ma",
    "mi",
    "mn",
    "ms",
    "mo",
    "mt",
    "ne",
    "nv",
    "nh",
    "nj",
    "nm",
    "ny",
    "nc",
    "nd",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "vt",
    "va",
    "wa",
    "wv",
    "wi",
    "wy",
    "dc",
}


# Full US state names -> US (note: "georgia"/"washington"/"new york" default to the US state).
_US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
}

# Noise tokens to drop from ATS location strings before matching.
_NOISE_RE = re.compile(
    r"\s*\(.*?\)"
    r"|\b(remote|hybrid|on-?site|locations?|metropolitan area|metro area|greater area"
    r"|bay area|area|region|multiple|various)\b",
    re.IGNORECASE,
)
_LEADING_COUNT_RE = re.compile(r"^\d+\s+")

# Generic sub-location / facility words. A segment built around one of these (e.g.
# "Depot 2", "LA Depot") is not a city and must never be emitted as one.
_SUBLOCATION_WORDS = {
    "depot",
    "drydock",
    "warehouse",
    "plant",
    "campus",
    "gate",
    "terminal",
    "dock",
    "hub",
    "yard",
    "facility",
    "site",
    "office",
    "building",
    "floor",
    "annex",
    "wing",
}


@lru_cache(maxsize=1)
def _cities() -> dict[str, str]:
    """Lowercased city -> canonical country (bundled gazetteer). Tolerant if missing."""
    try:
        text = (files("ergon_tracker.registry.data") / "cities.json").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return {}
    data = json.loads(text)
    return {k.lower(): v for k, v in data.get("cities", {}).items()}


@lru_cache(maxsize=1)
def _folded_cities() -> dict[str, str]:
    """Accent-folded lowercased city -> canonical country (for accent-insensitive lookup)."""
    return {_fold(k): v for k, v in _cities().items()}


def _fold(s: str) -> str:
    """Accent-fold to ASCII-ish and lowercase (e.g. "İstanbul" -> "istanbul")."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _fold_ascii(s: str) -> str:
    """Accent-fold to a clean ASCII form while preserving the original casing."""
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _gaz_match(segment: str, *, prefix: bool) -> tuple[str, str] | None:
    """Resolve a segment against the accent-folded gazetteer.

    With ``prefix=False`` only a full-segment match is accepted. With ``prefix=True`` the
    longest *leading* word-prefix that is a gazetteer city is accepted (e.g.
    "Boston Drydock" -> "Boston"). Returns ``(clean_city, country)`` or ``None``.
    """
    folded = _folded_cities()
    words = segment.split()
    if not words:
        return None
    lengths = range(len(words), 0, -1) if prefix else (len(words),)
    for n in lengths:
        candidate = " ".join(words[:n])
        country = folded.get(_fold(candidate))
        if country is not None:
            return _fold_ascii(candidate).strip(), country
    return None


def _clean(segment: str) -> str:
    s = _LEADING_COUNT_RE.sub("", segment)
    s = _NOISE_RE.sub("", s)
    s = re.sub(r"(?i)^greater\s+", "", s)
    return s.strip(" -,.").strip()


def _has_alpha(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def _is_sublocation(segment: str) -> bool:
    """A segment that looks like a facility/sub-location fragment (not a city name)."""
    if any(ch.isdigit() for ch in segment):
        return True
    return any(w.lower() in _SUBLOCATION_WORDS for w in segment.split())


def normalize_geo(loc: Location) -> Location:
    """Deterministically fill ``city``/``region``/``country`` from ``raw`` (in place).

    All-deterministic: split on separators, strip ATS noise, then resolve country by
    (1) explicit country token, (2) US state name/abbrev, (3) city -> country gazetteer.
    """
    # Canonicalize an explicitly-provided country ("US"/"USA"/full names) so the index does
    # not fragment "US" vs "United States". Applied to the explicit field only — segment
    # parsing below keeps its own state-collision-aware resolution (e.g. "CA" = California).
    if loc.country:
        loc.country = _COUNTRY_ALIASES.get(loc.country.strip().lower(), loc.country)
    if not loc.raw:
        return loc
    raw = loc.raw.strip()
    if "remote" in raw.lower():
        loc.is_remote = True
    cleaned = re.sub(r"\s+[-–—]\s+", ",", raw)
    segments = [c for c in (_clean(s) for s in re.split(r"[,/|]", cleaned)) if c]
    if not segments:
        return loc

    # (1) Country from explicit country tokens / US state names/abbreviations.
    if loc.country is None:
        for seg in reversed(segments):
            low = seg.lower()
            if low in _COUNTRY_ALIASES:
                loc.country = _COUNTRY_ALIASES[low]
                break
            if low in _US_STATES or low in _US_STATE_NAMES:
                loc.country = "United States"
                if loc.region is None:
                    loc.region = seg if low in _US_STATE_NAMES else seg.upper()
                break
            for tok in re.split(r"[-\s]+", low):
                if tok in _COUNTRY_ALIASES:
                    loc.country = _COUNTRY_ALIASES[tok]
                    break
                if tok in _US_STATES:
                    loc.country = "United States"
                    break
            if loc.country is not None:
                break

    # (2) City from the gazetteer. Prefer a full-segment match (works even when the
    # segment is also a state/country name, e.g. "New York", "Singapore"), then fall
    # back to a leading word-prefix match for sub-locations ("Boston Drydock" -> Boston),
    # then to the generic "first place-like segment" heuristic. Resolving a gazetteer
    # city also fills the country when it is still unknown.
    if loc.city is None:
        resolved = _resolve_city(segments, known_country=loc.country)
        if resolved is not None:
            loc.city, gaz_country = resolved
            if loc.country is None and gaz_country:
                loc.country = gaz_country

    # (3) Country from the gazetteer when still unknown (e.g. a pre-set city).
    if loc.country is None:
        for seg in segments:
            match = _gaz_match(seg, prefix=True)
            if match is not None:
                loc.country = match[1]
                break
    return loc


def _resolve_city(segments: list[str], *, known_country: str | None) -> tuple[str, str] | None:
    """Pick the best city for ``segments``; returns ``(city, gazetteer_country|"")``."""
    # (a) Full-segment gazetteer match, in order.
    for seg in segments:
        match = _gaz_match(seg, prefix=False)
        if match is not None:
            return match[0], match[1]

    # (b) Leading word-prefix gazetteer match (sub-locations). Guard against false
    # friends: when the country is already known, only trust a prefix-city whose
    # gazetteer country agrees with it.
    for seg in segments:
        match = _gaz_match(seg, prefix=True)
        if match is not None and (known_country is None or match[1] == known_country):
            return match[0], match[1]

    # (c) First place-like segment that is not a country/state name or a sub-location.
    for seg in segments:
        low = seg.lower()
        if low in _COUNTRY_ALIASES or low in _US_STATES or low in _US_STATE_NAMES:
            continue
        if not _has_alpha(seg) or "remote" in low:
            continue
        if _is_sublocation(seg):
            continue
        return seg, ""
    return None
