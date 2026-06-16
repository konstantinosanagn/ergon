"""Post-normalization enrichment: infer job level from the title, parse structured geo from
location strings, and look up company sector. Applied by the search orchestrator after a
provider normalizes a posting, before the query filter runs.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files

from .models import JobLevel, JobPosting, Location

__all__ = ["infer_level", "normalize_geo", "enrich_in_place", "load_sector_index", "SectorIndex"]

# Ordered: first hit wins, so list the strongest seniority signal first.
_LEVEL_PATTERNS: list[tuple[JobLevel, re.Pattern[str]]] = [
    (JobLevel.INTERN, re.compile(r"\b(intern|internship|co-?op|apprentice|werk?student)\b", re.I)),
    (
        JobLevel.EXECUTIVE,
        re.compile(r"\b(chief|c[etfoi]o|cxo|cmo|cpo|svp|evp|vp|vice president|head of)\b", re.I),
    ),
    (JobLevel.DIRECTOR, re.compile(r"\bdirector\b", re.I)),
    (JobLevel.MANAGER, re.compile(r"\b(manager|mgr|people lead)\b", re.I)),
    (JobLevel.PRINCIPAL, re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    (JobLevel.STAFF, re.compile(r"\bstaff\b", re.I)),
    (JobLevel.LEAD, re.compile(r"\b(lead|tech lead|team lead)\b", re.I)),
    (JobLevel.SENIOR, re.compile(r"\b(senior|sr\.?|snr)\b", re.I)),
    (JobLevel.JUNIOR, re.compile(r"\b(junior|jr\.?|jnr)\b", re.I)),
    (
        JobLevel.ENTRY,
        re.compile(r"\b(entry[- ]level|new ?grad|graduate|associate|trainee|early career)\b", re.I),
    ),
]


def infer_level(title: str) -> JobLevel:
    """Infer seniority from a job title. Returns UNKNOWN when no signal is present."""
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.search(title or ""):
            return level
    return JobLevel.UNKNOWN


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

    # First segment is the city unless it's the only segment and looks like a country/state.
    if loc.city is None and segments:
        first = segments[0]
        flow = first.lower().rstrip(".")
        if flow not in _COUNTRY_ALIASES and flow not in _US_STATES and "remote" not in flow:
            loc.city = first
    return loc


def enrich_in_place(job: JobPosting) -> JobPosting:
    """Set ``job.level`` from the title and normalize each location (in place)."""
    if job.level is JobLevel.UNKNOWN:
        job.level = infer_level(job.title)
    for loc in job.locations:
        normalize_geo(loc)
    return job


class SectorIndex:
    """Company -> sector lookup, by registry key and by domain."""

    def __init__(self, by_key: dict[str, str], by_domain: dict[str, str]) -> None:
        self._by_key = by_key
        self._by_domain = by_domain

    def get(self, *, key: str | None = None, domain: str | None = None) -> str | None:
        if key and key.lower() in self._by_key:
            return self._by_key[key.lower()]
        if domain and domain.lower() in self._by_domain:
            return self._by_domain[domain.lower()]
        return None

    def __len__(self) -> int:
        return len(self._by_key)


@lru_cache(maxsize=1)
def load_sector_index() -> SectorIndex:
    """Load the bundled company->sector dataset. Tolerant of a missing/empty file."""
    by_key: dict[str, str] = {}
    by_domain: dict[str, str] = {}
    try:
        text = (files("jobspine.registry.data") / "sectors.json").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return SectorIndex(by_key, by_domain)
    data = json.loads(text)
    for key, entry in data.get("companies", {}).items():
        sector = entry.get("sector")
        if not sector:
            continue
        by_key[key.lower()] = sector
        domain = entry.get("domain")
        if domain:
            by_domain[domain.lower()] = sector
    return SectorIndex(by_key, by_domain)
