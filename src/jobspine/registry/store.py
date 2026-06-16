"""Seed registry loader for the company -> ATS -> token map.

The seed ships as packaged data (``jobspine/registry/data/seed.json``) and is loaded via
:mod:`importlib.resources`, so it resolves correctly whether jobspine is run from a source
checkout or installed as a wheel (no ``__file__`` path hacks).

The registry is read-only at runtime: it maps a known company *domain* (and an opaque company
key) to a ``(ats, token, domain)`` triple that the resolver can return without any network
access.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .resolver import Resolution

__all__ = ["SeedRegistry"]

_DATA_PACKAGE = "jobspine.registry.data"
_SEED_FILE = "seed.json"


def _load_seed() -> dict[str, Any]:
    """Read and parse the packaged ``seed.json`` via importlib.resources."""
    text = files(_DATA_PACKAGE).joinpath(_SEED_FILE).read_text(encoding="utf-8")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _normalize_domain(domain: str) -> str:
    """Lower-case a host and strip a leading ``www.`` so lookups are forgiving."""
    host = domain.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _domain_candidates(domain: str) -> list[str]:
    """Yield the host itself then each parent domain (down to two labels).

    ``careers.acme.co.uk`` -> ``careers.acme.co.uk``, ``acme.co.uk``, ``co.uk``. We try the
    most specific first so a subdomain override (if ever added) would win.
    """
    host = _normalize_domain(domain)
    labels = host.split(".")
    candidates: list[str] = []
    for i in range(len(labels) - 1):
        cand = ".".join(labels[i:])
        if cand and cand not in candidates:
            candidates.append(cand)
    if host and host not in candidates:
        candidates.insert(0, host)
    return candidates


class SeedRegistry:
    """In-memory view over the packaged seed registry."""

    def __init__(self) -> None:
        raw = _load_seed()
        self._meta: dict[str, Any] = raw.get("_meta", {}) if isinstance(raw, dict) else {}
        companies = raw.get("companies", {}) if isinstance(raw, dict) else {}
        self._companies: dict[str, dict[str, Any]] = {
            key: entry for key, entry in companies.items() if isinstance(entry, dict)
        }
        # domain -> company key index, built once for O(1) domain lookups.
        self._by_domain: dict[str, str] = {}
        for key, entry in self._companies.items():
            domain = entry.get("domain")
            if isinstance(domain, str) and domain:
                self._by_domain[_normalize_domain(domain)] = key

    def get(self, company_key: str) -> dict[str, Any] | None:
        """Return the raw entry for a company key, or ``None``."""
        return self._companies.get(company_key)

    def lookup_domain(self, domain: str) -> Resolution | None:
        """Resolve a company domain (or careers host) to a :class:`Resolution`.

        Tries the host then each parent domain so ``careers.stripe.com`` still resolves to the
        ``stripe.com`` entry. Returns ``None`` when nothing matches.
        """
        from .resolver import Resolution  # local import avoids a circular import at load time

        for cand in _domain_candidates(domain):
            key = self._by_domain.get(cand)
            if key is None:
                continue
            entry = self._companies[key]
            return Resolution(
                ats=entry.get("ats"),
                token=entry.get("token"),
                domain=entry.get("domain"),
                source=domain,
                matched=True,
            )
        return None

    def all(self) -> dict[str, dict[str, Any]]:
        """Return all company entries keyed by company key."""
        return dict(self._companies)

    def __len__(self) -> int:
        return len(self._companies)
