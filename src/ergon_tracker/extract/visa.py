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

__all__ = [
    "SponsorIndex",
    "load_sponsor_index",
    "is_h1b_sponsor",
    "h1b_last_filed",
    "h1b_profile",
    "search_sponsors",
]

# Corporate / geographic "continuation" tokens. A posting company name is accepted as a leading
# prefix of an LCA legal name ("spotify" -> "spotify usa") ONLY when the very next token is one of
# these — which signals a legal-suffix variant of the SAME company, not a different firm that
# merely starts with the same word ("linear" must NOT match "linear signs"). This keeps the
# leading-token fallback high-precision (measured: 6.7% registry coverage, no observed bad hits).
_DESCRIPTORS = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "lp",
        "llp",
        "pbc",
        "opco",
        "holding",
        "holdings",
        "group",
        "technologies",
        "technology",
        "tech",
        "labs",
        "laboratories",
        "systems",
        "software",
        "solutions",
        "services",
        "service",
        "financial",
        "capital",
        "partners",
        "ventures",
        "pharmaceuticals",
        "pharma",
        "sciences",
        "science",
        "health",
        "healthcare",
        "usa",
        "us",
        "na",
        "america",
        "american",
        "global",
        "international",
        "intl",
        "worldwide",
        "ai",
        "digital",
        "enterprises",
        "industries",
        "networks",
        "communications",
        "consulting",
        "bank",
        "insurance",
        "studios",
        "media",
        "brands",
        "retail",
        "stores",
        "motors",
        "foods",
        "energy",
        "power",
        "biosciences",
        "therapeutics",
        "robotics",
        "security",
        "cloud",
        "data",
        "analytics",
        "payments",
        "business",
        "com",
    }
)


def _to_int(value: object) -> int:
    """Best-effort int from an on-disk JSON value (int/float/numeric str), else 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


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
        # Space-collapsed index: maps "brightmachines" -> "bright machines". Our registry stores
        # many companies as concatenated slugs ("brightmachines", "10xgenomics") with no spaces;
        # collapsing both sides lets those still match the spaced LCA legal names.
        self._collapsed: dict[str, str] = {}
        for name in records:
            head = name.split(" ", 1)[0]
            if head:
                self._by_first[head].append(name)
            self._collapsed.setdefault(name.replace(" ", ""), name)

    def _match_key(self, company: str | None) -> str | None:
        """Return the matched sponsor key (exact, space-collapsed, or gated leading-token)."""
        if not company:
            return None
        r = normalize_company(company)
        if not r:
            return None
        if r in self._records:  # 1) exact normalized name
            return r
        collapsed = r.replace(" ", "")
        if collapsed in self._collapsed:  # 2) slug <-> spaced (e.g. "brightmachines")
            return self._collapsed[collapsed]
        prefix = r + " "  # 3) gated leading-token ("spotify" -> "spotify usa")
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

    def profile(self, company: str | None) -> dict[str, object] | None:
        """Full LCA sponsor record for ``company`` — ``{name, filings, last_filed}`` — or None.

        The join key between a live posting's employer and the DoL sponsor directory: lets a job be
        annotated with the employer's filing VOLUME + recency (not just a sponsor yes/no)."""
        key = self._match_key(company)
        if key is None:
            return None
        rec = self._records.get(key) or {}
        last = rec.get("last")
        return {
            "name": key,
            "filings": _to_int(rec.get("n")),
            "last_filed": str(last) if last else None,
        }

    def search(self, query: str | None, limit: int = 20) -> list[dict[str, object]]:
        """Browse the sponsor directory: name-substring match, ranked by filing volume.

        Returns dicts ``{name, filings, last_filed}``. Empty/None query returns the biggest
        sponsors overall. Powers the "directory" so a user can see employers we *know* sponsor
        H-1B even when we can't fetch their jobs (custom/enterprise career sites).
        """
        q = normalize_company(query) if query else ""
        rows = [
            {"name": name, "filings": _to_int(rec.get("n")), "last_filed": rec.get("last")}
            for name, rec in self._records.items()
            if not q or q in name
        ]
        rows.sort(key=lambda r: -_to_int(r["filings"]))
        return rows[:limit]

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


def h1b_profile(company: str | None) -> dict[str, object] | None:
    """Full LCA sponsor record for ``company`` — ``{name, filings, last_filed}`` — or None."""
    return load_sponsor_index().profile(company)


def search_sponsors(query: str | None = None, limit: int = 20) -> list[dict[str, object]]:
    """Browse known H-1B sponsors by name (ranked by filing volume). See SponsorIndex.search."""
    return load_sponsor_index().search(query, limit)
