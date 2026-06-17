"""Company -> sector classification (table-backed)."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from .base import ExtractInput, register_extractor

__all__ = ["SectorIndex", "load_sector_index", "SectorExtractor"]


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
        text = (files("ergon_tracker.registry.data") / "sectors.json").read_text(encoding="utf-8")
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


class SectorExtractor:
    name = "sector"

    def extract(self, inp: ExtractInput) -> str | None:
        # Authoritative table only (curated + offline joins). A description-text fallback was
        # measured at ~24% accuracy (JDs name-drop many industries) and dropped — returning
        # None ("unknown") beats a mostly-wrong guess.
        return load_sector_index().get(key=inp.company_key, domain=inp.company_domain)


register_extractor(SectorExtractor())
