"""H-1B visa-sponsor signal — table-backed, from US DoL OFLC LCA disclosure data.

The Department of Labor publishes (free, official) quarterly disclosure files of every Labor
Condition Application — the petition an employer must file to hire an H-1B worker. An employer
appearing there with a *certified* LCA is a demonstrated visa sponsor. We distill that bulk data
offline (see ``scripts/build_h1b_sponsors.py``) into a compact set of normalized employer names
shipped as ``registry/data/h1b_sponsors.json``, and tag matching postings with ``visa_sponsor``.

Honesty note: this gives **positive** evidence only. A company *in* the set has sponsored H-1B
visas; a company *not* in it is ``unknown`` (the data is historical and name-matching is fuzzy),
never asserted as a non-sponsor. So we only ever set ``visa_sponsor = True``.

Matching is by normalized company name (``dedup.normalize_company``), since LCA employer names
("STRIPE, INC.") and posting company names ("Stripe") both collapse to the same key ("stripe").
"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from importlib.resources import files

from ..dedup import normalize_company

__all__ = ["SponsorIndex", "load_sponsor_index", "is_h1b_sponsor", "h1b_last_filed"]

# Corporate / geographic "continuation" tokens. A posting company name is accepted as a leading
# prefix of an LCA legal name ("spotify" -> "spotify usa") ONLY when the very next token is one of
# these — which signals a legal-suffix variant of the SAME company, not a different firm that
# merely starts with the same word ("linear" must NOT match "linear signs"). This keeps the
# leading-token fallback high-precision (measured: 6.7% registry coverage, no observed bad hits).
_DESCRIPTORS = frozenset(
    {
        "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
        "plc", "lp", "llp", "pbc", "opco", "holding", "holdings", "group", "technologies",
        "technology", "tech", "labs", "laboratories", "systems", "software", "solutions",
        "services", "service", "financial", "capital", "partners", "ventures", "pharmaceuticals",
        "pharma", "sciences", "science", "health", "healthcare", "usa", "us", "na", "america",
        "american", "global", "international", "intl", "worldwide", "ai", "digital", "enterprises",
        "industries", "networks", "communications", "consulting", "bank", "insurance", "studios",
        "media", "brands", "retail", "stores", "motors", "foods", "energy", "power", "biosciences",
        "therapeutics", "robotics", "security", "cloud", "data", "analytics", "payments",
        "business", "com",
    }
)


class SponsorIndex:
    """Normalized employer name -> {n: certified filings, last: most-recent filing ISO date}.

    Matching is two-tier: an exact normalized-name hit, else a *gated* leading-token hit (the
    company name is the leading tokens of a sponsor's legal name and the next token is a known
    corporate/geographic descriptor). The gate is what keeps the fallback precise.
    """

    def __init__(self, records: dict[str, dict[str, object]]) -> None:
        self._records = records
        # First-token buckets make the leading-token scan cheap (no full-set scan per lookup).
        self._by_first: dict[str, list[str]] = defaultdict(list)
        for name in records:
            head = name.split(" ", 1)[0]
            if head:
                self._by_first[head].append(name)

    def _match_key(self, company: str | None) -> str | None:
        """Return the matched sponsor key (exact or gated leading-token), or None."""
        if not company:
            return None
        r = normalize_company(company)
        if not r:
            return None
        if r in self._records:
            return r
        prefix = r + " "
        for name in self._by_first.get(r.split(" ", 1)[0], ()):
            if name.startswith(prefix) and name[len(prefix) :].split(" ", 1)[0] in _DESCRIPTORS:
                return name
        return None

    def is_sponsor(self, company: str | None) -> bool:
        return self._match_key(company) is not None

    def last_filed(self, company: str | None) -> str | None:
        """Most-recent certified-filing date (ISO) for ``company``, or None if not a sponsor."""
        key = self._match_key(company)
        if key is None:
            return None
        last = (self._records.get(key) or {}).get("last")
        return str(last) if last else None

    def __len__(self) -> int:
        return len(self._records)


def _coerce_records(sponsors: object) -> dict[str, dict[str, object]]:
    """Accept several on-disk shapes: {name: {n,last}} | {name: count} | [name, ...]."""
    if isinstance(sponsors, dict):
        out: dict[str, dict[str, object]] = {}
        for name, val in sponsors.items():
            out[name] = val if isinstance(val, dict) else {"n": val, "last": None}
        return out
    if isinstance(sponsors, list):
        return {name: {"n": None, "last": None} for name in sponsors}
    return {}


@lru_cache(maxsize=1)
def load_sponsor_index() -> SponsorIndex:
    """Load the bundled H-1B sponsor index. Tolerant of a missing/empty file (feature no-ops)."""
    try:
        text = (files("ergon_tracker.registry.data") / "h1b_sponsors.json").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return SponsorIndex({})
    data = json.loads(text)
    return SponsorIndex(_coerce_records(data.get("sponsors", {})))


def is_h1b_sponsor(company: str | None) -> bool:
    """True iff ``company`` matches a known H-1B sponsor in the bundled index."""
    return load_sponsor_index().is_sponsor(company)


def h1b_last_filed(company: str | None) -> str | None:
    """Most-recent certified H-1B filing date (ISO) for ``company``, else None."""
    return load_sponsor_index().last_filed(company)
