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

__all__ = [
    "normalize_geo",
    "city_match_terms",
    "city_matches",
    "country_match_term",
    "country_matches",
    "has_us_signal",
]

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


def country_match_term(country: str) -> str:
    """Canonical lowercased country for a filter, resolving common aliases (USA/US/U.S. -> united
    states; UK/England -> united kingdom). Lets a query use any common spelling and still match the
    geo-normalized country stored on postings."""
    key = country.strip().lower()
    return _COUNTRY_ALIASES.get(key, country.strip()).lower()


def country_matches(country_query: str, loc_country: str | None, loc_raw: str | None) -> bool:
    """True if a parsed location matches a country filter (alias-resolved). Mirrors the index SQL."""
    term = country_match_term(country_query)
    if (loc_country or "").strip().lower() == term:
        return True
    return term in (loc_raw or "").lower()


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

# ISO 3166 country codes, for enterprise HRIS location formats ("Toronto, ON, CA, M5V2T3",
# "Cologne, NW, DE, 51105", "Warsaw, POL"). alpha-3 codes are UNAMBIGUOUS (no US state is 3 letters)
# so they're folded into the aliases and resolve anywhere. alpha-2 codes that COLLIDE with US state
# abbreviations (CA=California/Canada, DE=Delaware/Germany, IN=Indiana/India, CO, ID, IL, ...) are
# resolved ONLY in country position (see the enterprise block in normalize_geo), never as a bare
# "City, XX" (which stays a US state).
_ISO3_COUNTRY: dict[str, str] = {
    "usa": "United States", "can": "Canada", "gbr": "United Kingdom", "deu": "Germany",
    "fra": "France", "ind": "India", "mex": "Mexico", "bra": "Brazil", "jpn": "Japan",
    "chn": "China", "aus": "Australia", "nld": "Netherlands", "esp": "Spain", "ita": "Italy",
    "irl": "Ireland", "sgp": "Singapore", "pol": "Poland", "swe": "Sweden", "che": "Switzerland",
    "prt": "Portugal", "isr": "Israel", "kor": "South Korea", "nzl": "New Zealand", "aut": "Austria",
    "bel": "Belgium", "dnk": "Denmark", "nor": "Norway", "fin": "Finland", "cze": "Czech Republic",
    "rou": "Romania", "ukr": "Ukraine", "arg": "Argentina", "chl": "Chile", "col": "Colombia",
    "phl": "Philippines", "idn": "Indonesia", "vnm": "Vietnam", "tha": "Thailand", "mys": "Malaysia",
    "zaf": "South Africa", "nga": "Nigeria", "egy": "Egypt", "tur": "Turkey", "grc": "Greece",
    "hun": "Hungary", "are": "United Arab Emirates", "twn": "Taiwan", "hkg": "Hong Kong",
    "sau": "Saudi Arabia", "ken": "Kenya", "mar": "Morocco",
}
_COUNTRY_ALIASES.update(_ISO3_COUNTRY)  # alpha-3 are safe to resolve anywhere

_ISO2_COUNTRY: dict[str, str] = {
    "us": "United States", "ca": "Canada", "gb": "United Kingdom", "de": "Germany", "fr": "France",
    "in": "India", "mx": "Mexico", "br": "Brazil", "jp": "Japan", "cn": "China", "au": "Australia",
    "nl": "Netherlands", "es": "Spain", "it": "Italy", "ie": "Ireland", "sg": "Singapore",
    "pl": "Poland", "se": "Sweden", "ch": "Switzerland", "pt": "Portugal", "il": "Israel",
    "kr": "South Korea", "nz": "New Zealand", "at": "Austria", "be": "Belgium", "dk": "Denmark",
    "no": "Norway", "fi": "Finland", "cz": "Czech Republic", "ro": "Romania", "ua": "Ukraine",
    "ar": "Argentina", "cl": "Chile", "co": "Colombia", "ph": "Philippines", "id": "Indonesia",
    "vn": "Vietnam", "th": "Thailand", "my": "Malaysia", "za": "South Africa", "ng": "Nigeria",
    "eg": "Egypt", "tr": "Turkey", "gr": "Greece", "hu": "Hungary", "ae": "United Arab Emirates",
    "tw": "Taiwan", "hk": "Hong Kong", "sa": "Saudi Arabia", "ke": "Kenya", "uk": "United Kingdom",
}

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

# Noise tokens to drop from ATS location strings before matching. The anchored alternatives
# kill Workday multi-location placeholders ("3 Locations", "Multiple Locations", "Several
# Locations") as WHOLE segments before any city/country resolution, so a counter word can
# never survive cleaning and be mistaken for a place name.
_NOISE_RE = re.compile(
    r"^\d+\s+locations?$"
    r"|^(?:multiple|various|several)\s+locations?$"
    r"|\s*\(.*?\)"
    r"|\b(remote|hybrid|on-?site|locations?|metropolitan area|metro area|greater area"
    r"|bay area|area|region|multiple|various)\b",
    re.IGNORECASE,
)
_LEADING_COUNT_RE = re.compile(r"^\d+\s+")

# US-signal detection (used by enrich's conservative Workday-US country default).
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_US_STATE_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in sorted(_US_STATE_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# A state/country NAME immediately followed by a hyphen (PeopleSoft "Kansas-Topeka",
# "United States-Texas-Garden City"). Longest-first so "united states" wins over "united". Used to
# turn those hyphens into commas without touching hyphenated place names ("Winston-Salem").
_STATE_COUNTRY_DASH = re.compile(
    r"\b("
    + "|".join(
        re.escape(n)
        for n in sorted(_US_STATE_NAMES | _COUNTRY_NAMES | set(_COUNTRY_ALIASES), key=len, reverse=True)
    )
    + r")\s*-\s*",
    re.IGNORECASE,
)

# UPPERCASE state/country CODE followed by a hyphen ("CA-Irvine", "USA-WV-Heaters", "MY-Kuala
# Lumpur"). Case-SENSITIVE + uppercase-only so ordinary hyphenated words ("Co-op", "de-facto",
# "in-house") are never split — real HRIS location codes are uppercase.
_CODE_DASH = re.compile(
    r"\b("
    + "|".join(
        c.upper()
        for c in sorted(
            set(_US_STATES) | set(_ISO2_COUNTRY) | set(_ISO3_COUNTRY), key=len, reverse=True
        )
    )
    + r")-(?=[A-Za-z])",
)


def has_us_signal(raw: str | None) -> bool:
    """True when a raw location string carries a US-specific token: a full state name, a
    standalone UPPERCASE two-letter state abbreviation, or a ZIP-like 5-digit token.

    Deliberately conservative — abbreviations count only when uppercase ("IN"/"OR"/"DE"
    lowercased are ordinary words / country codes) — so callers can use a hit as positive
    evidence for a US default without blanket-tagging international strings.
    """
    if not raw:
        return False
    if _US_STATE_NAME_RE.search(raw) or _ZIP_RE.search(raw):
        return True
    return any(
        len(tok) == 2 and tok.isupper() and tok.lower() in _US_STATES
        for tok in re.split(r"[^A-Za-z]+", raw)
    )

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
    # PeopleSoft "Country-State-City" / "State-City" (hyphen-delimited): split ONLY a hyphen that
    # follows a known state/country NAME, so "Kansas-Topeka" and "United States-Texas-Garden City"
    # split while hyphenated place names ("Winston-Salem", "St. Leon-Rot", "Baden-Württemberg") and
    # postal codes are never broken. Handles multi-location ("Kansas-Topeka, Kansas-Wichita").
    raw = _STATE_COUNTRY_DASH.sub(r"\1,", raw)
    raw = _CODE_DASH.sub(r"\1,", raw)
    cleaned = re.sub(r"\s+[-–—]\s+", ",", raw)
    segments = [c for c in (_clean(s) for s in re.split(r"[,/|]", cleaned)) if c]
    if not segments:
        return loc

    # (0) Enterprise HRIS "City, Region, CC[, Postal]": the country code sits after a region,
    # optionally before a postal code. Drop digit-bearing (postal) segments; if >=3 place segments
    # remain and the last is an ISO-2/alias country code, resolve it by POSITION — this is what
    # disambiguates "Toronto, ON, CA" (Canada) from "Sacramento, CA" (California) and "Cologne, NW,
    # DE" (Germany) from "Chicago, IL" (Illinois — only 2 segments, so untouched here).
    if loc.country is None:
        place_segs = [s for s in segments if not any(ch.isdigit() for ch in s)]
        had_postal = len(place_segs) < len(segments)
        # Country-position when there's a region before it (>=3 place segs) OR a postal code marks it
        # as the final locality token ("Frankfurt, DE, 60313"). A bare "City, XX" with no postal stays
        # a US state ("Chicago, IL"), preserving that common case.
        if len(place_segs) >= 3 or (had_postal and len(place_segs) >= 2):
            tail = place_segs[-1].lower()
            code_country = _ISO2_COUNTRY.get(tail) or _COUNTRY_ALIASES.get(tail)
            if code_country:
                loc.country = code_country

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
