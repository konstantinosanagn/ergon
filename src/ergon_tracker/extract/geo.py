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
    "usa": "United States",
    "can": "Canada",
    "gbr": "United Kingdom",
    "deu": "Germany",
    "fra": "France",
    "ind": "India",
    "mex": "Mexico",
    "bra": "Brazil",
    "jpn": "Japan",
    "chn": "China",
    "aus": "Australia",
    "nld": "Netherlands",
    "esp": "Spain",
    "ita": "Italy",
    "irl": "Ireland",
    "sgp": "Singapore",
    "pol": "Poland",
    "swe": "Sweden",
    "che": "Switzerland",
    "prt": "Portugal",
    "isr": "Israel",
    "kor": "South Korea",
    "nzl": "New Zealand",
    "aut": "Austria",
    "bel": "Belgium",
    "dnk": "Denmark",
    "nor": "Norway",
    "fin": "Finland",
    "cze": "Czech Republic",
    "rou": "Romania",
    "ukr": "Ukraine",
    "arg": "Argentina",
    "chl": "Chile",
    "col": "Colombia",
    "phl": "Philippines",
    "idn": "Indonesia",
    "vnm": "Vietnam",
    "tha": "Thailand",
    "mys": "Malaysia",
    "zaf": "South Africa",
    "nga": "Nigeria",
    "egy": "Egypt",
    "tur": "Turkey",
    "grc": "Greece",
    "hun": "Hungary",
    "are": "United Arab Emirates",
    "twn": "Taiwan",
    "hkg": "Hong Kong",
    "sau": "Saudi Arabia",
    "ken": "Kenya",
    "mar": "Morocco",
}
_COUNTRY_ALIASES.update(_ISO3_COUNTRY)  # alpha-3 are safe to resolve anywhere

_ISO2_COUNTRY: dict[str, str] = {
    "us": "United States",
    "ca": "Canada",
    "gb": "United Kingdom",
    "de": "Germany",
    "fr": "France",
    "in": "India",
    "mx": "Mexico",
    "br": "Brazil",
    "jp": "Japan",
    "cn": "China",
    "au": "Australia",
    "nl": "Netherlands",
    "es": "Spain",
    "it": "Italy",
    "ie": "Ireland",
    "sg": "Singapore",
    "pl": "Poland",
    "se": "Sweden",
    "ch": "Switzerland",
    "pt": "Portugal",
    "il": "Israel",
    "kr": "South Korea",
    "nz": "New Zealand",
    "at": "Austria",
    "be": "Belgium",
    "dk": "Denmark",
    "no": "Norway",
    "fi": "Finland",
    "cz": "Czech Republic",
    "ro": "Romania",
    "ua": "Ukraine",
    "ar": "Argentina",
    "cl": "Chile",
    "co": "Colombia",
    "ph": "Philippines",
    "id": "Indonesia",
    "vn": "Vietnam",
    "th": "Thailand",
    "my": "Malaysia",
    "za": "South Africa",
    "ng": "Nigeria",
    "eg": "Egypt",
    "tr": "Turkey",
    "gr": "Greece",
    "hu": "Hungary",
    "ae": "United Arab Emirates",
    "tw": "Taiwan",
    "hk": "Hong Kong",
    "sa": "Saudi Arabia",
    "ke": "Kenya",
    "uk": "United Kingdom",
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

# Leading numeric-code prefix on a segment ("35-Xiamen", "14-Kuala Lumpur", "1DBM"): HRIS site
# codes prepend a facility number before the real place. Strip a leading run of digits followed by
# a hyphen (never a bare number, which is a whole segment handled by _LEADING_COUNT_RE / noise).
_LEADING_NUM_DASH_RE = re.compile(r"^\d+\s*-\s*")

# Trailing facility/noise words on an otherwise-clean city segment ("Clarkston Campus", "Worth
# Branch", "Augusta Temporary", "Fleury-Les-Aubrais Cedex"). Dropped only from the END so the
# leading real city survives ("Clarkston Campus" -> "Clarkston"). "Cedex" is the French
# poste-restante marker; "Branch"/"Temporary"/"Campus"/"Office"/"HQ" are US HRIS facility tags.
_TRAILING_NOISE_WORDS = {
    "campus",
    "office",
    "hq",
    "branch",
    "temporary",
    "cedex",
    "metro",
    "downtown",
}
_TRAILING_NOISE_RE = re.compile(
    r"(?:\s+(?:" + "|".join(_TRAILING_NOISE_WORDS) + r"))+$",
    re.IGNORECASE,
)

# Compound-city place-type suffixes: when a "<City> <Suffix>" segment is a REAL two-word city
# ("Auburn Hills", "Clifton Park", "Santa Fe Springs", "Lake Worth") the gazetteer often only holds
# the first word ("Auburn"), so a leading-prefix match would truncate the true city. These suffixes
# mark the full segment as the intended city, so we KEEP it whole rather than trust a shorter prefix.
# Curated to genuine settlement suffixes (never facility words like "Drydock"/"Depot"), so
# "Boston Drydock" -> "Boston" and "Barcelona Gran Vía" -> "Barcelona" are unaffected.
_COMPOUND_CITY_SUFFIXES = {
    "hills",
    "park",
    "city",
    "springs",
    "heights",
    "airpark",
}

# Non-city placeholder segments: geocoder / HRIS sentinels that must never emit a city.
# "Any City" (Workday placeholder), "Remote Work"/"Work" (remote sentinels), "Select"/"University".
_NON_CITY_SEGMENTS = {
    "any city",
    "anywhere",  # "Remote - Anywhere" placeholder; never a city
    "work",
    "remote work",
    "select",
    "university",
    "temporary services",
    "fully",  # residue of "Fully Remote -" prefix; the real city is a later segment
    "southwest",
    "northeast",
    "northwest",
    "southeast",
}

# US-signal detection (used by enrich's conservative Workday-US country default).
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_US_STATE_NAME_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(n) for n in sorted(_US_STATE_NAMES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

# A state/country NAME immediately followed by a hyphen (PeopleSoft "Kansas-Topeka",
# "United States-Texas-Garden City"). Longest-first so "united states" wins over "united". Used to
# turn those hyphens into commas without touching hyphenated place names ("Winston-Salem").
_STATE_COUNTRY_DASH = re.compile(
    r"\b("
    + "|".join(
        re.escape(n)
        for n in sorted(
            _US_STATE_NAMES | _COUNTRY_NAMES | set(_COUNTRY_ALIASES), key=len, reverse=True
        )
    )
    + r")\s*-\s*",
    re.IGNORECASE,
)

# UPPERCASE state/country CODE followed by a hyphen ("CA-Irvine", "USA-WV-Heaters", "MY-Kuala
# Lumpur"). Case-SENSITIVE + uppercase-only so ordinary hyphenated words ("Co-op", "de-facto",
# "in-house") are never split — real HRIS location codes are uppercase. The lookahead also accepts a
# numeric site-code run before the next dash ("MY-14-Kuala Lumpur"), so the country code peels off
# and the leading number is dropped in _clean, leaving the real city.
_CODE_DASH = re.compile(
    r"\b("
    + "|".join(
        c.upper()
        for c in sorted(
            set(_US_STATES) | set(_ISO2_COUNTRY) | set(_ISO3_COUNTRY), key=len, reverse=True
        )
    )
    + r")-(?=[A-Za-z]|\d+-)",
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
    s = _LEADING_NUM_DASH_RE.sub("", segment)  # "35-Xiamen" -> "Xiamen"
    s = _LEADING_COUNT_RE.sub("", s)
    s = _NOISE_RE.sub("", s)
    s = re.sub(r"(?i)^greater\s+", "", s)
    return s.strip(" -,.").strip()


def _strip_facility_tail(segment: str) -> str:
    """Drop a trailing facility/marker tag ("Clarkston Campus" -> "Clarkston", "Worth Branch" ->
    "Worth", "Fleury-Les-Aubrais Cedex" -> "Fleury-Les-Aubrais") ONLY when the remaining head is a
    real place: a single word (a bare city name the gazetteer may not hold, e.g. "Clarkston") or an
    itself-gazetteer city ("Salt Lake City Temporary" -> "Salt Lake City"). A multi-word non-city
    head is a building name ("Modesto A. Maidique Campus", "Biscayne Bay Campus") and is left intact
    so it is rejected as a sub-location rather than mistaken for a city."""
    head = _TRAILING_NOISE_RE.sub("", segment).strip(" -,.").strip()
    if not head or head == segment:
        return segment
    if len(head.split()) == 1 or _gaz_match(head, prefix=False) is not None:
        return head
    return segment


def _split_place_hyphen(segment: str) -> list[str]:
    """Split a residual PeopleSoft ``A-B`` segment when it repeats a place or trails a description.

    After the state/country-name and code dashes are turned into commas, some segments still hold a
    hyphen joining two *word groups* ("Las Vegas-Las Vegas", "Irvine-USA IRVINE CA DEFENSE SYSTEMS",
    "North Rhine Westphalia-Düsseldorf"). We split such a hyphen only when it separates real word
    groups — i.e. an adjacent group is multi-word, OR the two single-word sides are identical
    ("Katy-Katy", "Puebla-Puebla"). Genuine hyphenated names stay intact: "Winston-Salem",
    "Lapu-Lapu", "Baden-Württemberg" are single tokens on each side and not duplicates.
    """
    if "-" not in segment or " - " in segment:
        return [segment]
    parts = [p.strip() for p in segment.split("-") if p.strip()]
    if len(parts) < 2:
        return [segment]
    # Split when a part is multi-word (a described PeopleSoft residue such as "Newburgh-Deaconess-
    # Encompass Health ...", "Irvine-USA IRVINE CA ...") or when a part repeats ("Katy-Katy",
    # "Lake Worth-Lake Worth-Lake Worth") — the real city is the leading part _resolve_city keeps.
    multiword = any(" " in p for p in parts)
    lowered = [p.lower() for p in parts]
    dup = len(set(lowered)) < len(lowered)
    # Protect a genuine hyphenated dual-name: exactly two parts, a gazetteer city on the left and a
    # single-word non-gazetteer tail ("Tel Aviv-Yafo"). "Winston-Salem"/"Fleury-Les-Aubrais" have no
    # spaces and no duplicate parts, so they never reach the split branch at all.
    dual_name = (
        len(parts) == 2
        and " " not in parts[1]
        and _gaz_match(parts[0], prefix=False) is not None
        and _gaz_match(parts[1], prefix=False) is None
    )
    if (dup or multiword) and not dual_name:
        return parts
    return [segment]


# A "<City> <ST>" tail where ST is a bare US state abbreviation ("Oakdale MN", "Corinth MS"):
# a duplicated state-code suffix that the gazetteer full-match misses. Stripped to recover the city.
_TRAILING_STATE_ABBR_RE = re.compile(
    r"\s+(" + "|".join(sorted(_US_STATES, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
)


def _strip_trailing_state_abbr(segment: str) -> str:
    """Drop a trailing bare US state abbreviation from a multi-word segment ("Oakdale MN"
    -> "Oakdale"). Only when >=2 words remain-before, so "MN" alone stays a state, not a city."""
    if len(segment.split()) < 2:
        return segment
    return _TRAILING_STATE_ABBR_RE.sub("", segment).strip()


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
        _explicit_key = loc.country.strip().lower()
        _explicit_country = _COUNTRY_ALIASES.get(_explicit_key)
        if _explicit_country is None and len(_explicit_key) == 2:
            # ISO alpha-2 fallback ("GB" -> United Kingdom, "DE" -> Germany): _COUNTRY_ALIASES
            # only covers the codes that double as common aliases (us/uk/uae); the rest live in
            # _ISO2_COUNTRY. Safe here because this field is an EXPLICIT country (not a bare
            # segment), so there's no US-state collision risk to guard against.
            _explicit_country = _ISO2_COUNTRY.get(_explicit_key)
        loc.country = _explicit_country if _explicit_country is not None else loc.country
    if not loc.raw:
        return loc
    raw = loc.raw.strip()
    if "remote" in raw.lower():
        loc.is_remote = True
    # A parenthetical often carries the DISAMBIGUATING country/state ("Remote (US)", "London
    # (Canada)", "Hamilton (New Zealand)"). _NOISE_RE strips all parentheticals as noise a few lines
    # down, which silently drops that qualifier and lets an ambiguous city fall to the wrong gazetteer
    # default (London -> UK, Hamilton -> Canada). Resolve an EXPLICIT country/US-state token from the
    # parentheses first — only authoritative tokens (country names/aliases or full US state names,
    # never a bare 2-letter code like "(CA)" which is California-or-Canada) — so it wins over the
    # gazetteer without inventing a signal.
    if loc.country is None:
        for _inner in re.findall(r"\(([^)]*)\)", raw):
            for _piece in re.split(r"[,/;]", _inner):
                _key = _piece.strip().lower()
                if _key in _COUNTRY_ALIASES:
                    loc.country = _COUNTRY_ALIASES[_key]
                elif _key in _US_STATE_NAMES:
                    loc.country = "United States"
                    if loc.region is None:
                        loc.region = _piece.strip()
                if loc.country is not None:
                    break
            if loc.country is not None:
                break
    # PeopleSoft "Country-State-City" / "State-City" (hyphen-delimited): split ONLY a hyphen that
    # follows a known state/country NAME, so "Kansas-Topeka" and "United States-Texas-Garden City"
    # split while hyphenated place names ("Winston-Salem", "St. Leon-Rot", "Baden-Württemberg") and
    # postal codes are never broken. Handles multi-location ("Kansas-Topeka, Kansas-Wichita").
    raw = _STATE_COUNTRY_DASH.sub(r"\1,", raw)
    raw = _CODE_DASH.sub(r"\1,", raw)
    cleaned = re.sub(r"\s+[-–—]\s+", ",", raw)
    # Split each comma-part on a residual place-hyphen ("Las Vegas-Las Vegas", "Irvine-USA IRVINE
    # CA ...") BEFORE cleaning, so a duplicated/described city collapses to a resolvable segment.
    raw_parts = [sub for part in re.split(r"[,/|;]", cleaned) for sub in _split_place_hyphen(part)]
    segments = [c for c in (_clean(s) for s in raw_parts) if c]
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


def _is_compound_city(segment: str) -> bool:
    """A ``<City> <Suffix>`` segment whose last word is a settlement suffix ("Auburn Hills",
    "Lake Worth") and whose leading word(s) are themselves a gazetteer city — a real two-word
    city the gazetteer only holds under its first word, so it must be kept WHOLE, not truncated."""
    words = segment.split()
    if len(words) < 2 or words[-1].lower() not in _COMPOUND_CITY_SUFFIXES:
        return False
    return _gaz_match(" ".join(words[:-1]), prefix=False) is not None


def _resolve_city(segments: list[str], *, known_country: str | None) -> tuple[str, str] | None:
    """Pick the best city for ``segments``; returns ``(city, gazetteer_country|"")``."""
    # Normalize each segment: reject known non-city placeholders ("Any City", "Remote Work",
    # "Select"), drop a trailing facility tag ("Clarkston Campus" -> "Clarkston") and a trailing
    # bare US state abbreviation ("Oakdale MN" -> "Oakdale").
    norm: list[str] = []
    for seg in segments:
        if seg.lower() in _NON_CITY_SEGMENTS:
            continue
        norm.append(_strip_trailing_state_abbr(_strip_facility_tail(seg)))
    segments = norm or segments

    # (a) Full-segment gazetteer match, in order. Runs before the compound rule so a first bare
    # gazetteer segment wins ("Spokane - Spokane Valley" -> "Spokane", not "Spokane Valley").
    # A segment that is ALSO a US state name ("Washington", "Wyoming") is DEFERRED: in the common
    # "Country, State, City" order ("United States, Washington, Redmond") the state precedes the
    # real city, so a non-state gazetteer city elsewhere is preferred; the state is used only if it
    # is the sole gazetteer hit ("New York" alone stays New York).
    deferred: tuple[str, str] | None = None
    for seg in segments:
        match = _gaz_match(seg, prefix=False)
        if match is None:
            continue
        if seg.lower() in _US_STATE_NAMES:
            deferred = deferred or (match[0], match[1])
            continue
        return match[0], match[1]
    if deferred is not None:
        return deferred

    # (b) A real two-word city the gazetteer stores under its first word only ("Auburn Hills",
    # "Santa Fe Springs"): keep the FULL segment rather than let (c)'s prefix match truncate it.
    for seg in segments:
        if _is_compound_city(seg):
            prefix = _gaz_match(" ".join(seg.split()[:-1]), prefix=False)
            country = prefix[1] if prefix else ""
            return seg, country

    # (c) Leading word-prefix gazetteer match (sub-locations). Guard against false
    # friends: when the country is already known, only trust a prefix-city whose
    # gazetteer country agrees with it.
    for seg in segments:
        match = _gaz_match(seg, prefix=True)
        if match is not None and (known_country is None or match[1] == known_country):
            return match[0], match[1]

    # (d) First place-like segment that is not a country/state name or a sub-location.
    for seg in segments:
        low = seg.lower()
        if low in _COUNTRY_ALIASES or low in _US_STATES or low in _US_STATE_NAMES:
            continue
        # A bare ISO-2 country code ("MX", "PL", "JP") is a country, never a city.
        if low in _ISO2_COUNTRY:
            continue
        if low in _NON_CITY_SEGMENTS or not _has_alpha(seg) or "remote" in low:
            continue
        # A multi-word segment led by a compass descriptor ("Northeast FA", "Southwest Region")
        # is a sales-territory tag, not a city — the real city is a later segment.
        if low.split()[0] in _NON_CITY_SEGMENTS and len(low.split()) > 1:
            continue
        if _is_sublocation(seg):
            continue
        return seg, ""
    return None
