"""Geo normalization (rules baseline): fill city/region/country/remote from a location string.

Geo is handled as a per-``Location`` normalizer rather than a posting-level FieldExtractor,
because it refines existing ``Location`` objects in place.
"""

from __future__ import annotations

import re

from ..models import Location

__all__ = ["normalize_geo"]

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


def normalize_geo(loc: Location) -> Location:
    """Best-effort fill of ``city``/``region``/``country`` from ``raw`` (in place)."""
    if not loc.raw:
        return loc
    raw = loc.raw.strip()
    if "remote" in raw.lower():
        loc.is_remote = True
    # treat spaced dashes ("Remote - United States") as separators too
    cleaned = re.sub(r"\s+[-–—]\s+", ",", raw)
    segments = [s.strip() for s in re.split(r"[,/|]", cleaned) if s.strip()]
    if not segments:
        return loc

    last = segments[-1].lower().rstrip(".")
    if loc.country is None:
        if last in _COUNTRY_ALIASES:
            loc.country = _COUNTRY_ALIASES[last]
        elif last in _US_STATES:
            loc.country = "United States"
            if loc.region is None:
                loc.region = segments[-1].upper()

    if loc.city is None and segments:
        first = segments[0]
        flow = first.lower().rstrip(".")
        if flow not in _COUNTRY_ALIASES and flow not in _US_STATES and "remote" not in flow:
            loc.city = first
    return loc
