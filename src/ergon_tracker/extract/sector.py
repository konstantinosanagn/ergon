"""Company -> sector classification (table-backed)."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files

from .base import ExtractInput, register_extractor

__all__ = ["SectorIndex", "load_sector_index", "SectorExtractor", "name_sector"]

# High-precision company-NAME -> sector tokens. ONLY unambiguous industry words belong here:
# a company literally named "<X> Bank" / "<X> Hospitality" / "<X> Manufacturing" is in that
# industry. This is the opposite of description-text classification (dropped at ~24% accuracy):
# the company's own name naming its industry is a strong signal. Applied ONLY when the curated
# table misses, so it never overrides authoritative data; opaque brand names stay "unknown".
# Deliberately omits generic words ("industries", "technology", "group", "partners", "labs",
# "energy", "motors") that carry no reliable sector signal.
_NAME_SECTOR: dict[str, str] = {
    # Healthcare
    "healthcare": "Healthcare", "health system": "Healthcare", "hospital": "Healthcare",
    "hospitals": "Healthcare", "clinic": "Healthcare", "clinics": "Healthcare",
    "medical center": "Healthcare", "eyecare": "Healthcare", "dental": "Healthcare",
    "dentistry": "Healthcare", "orthodontics": "Healthcare", "autism": "Healthcare",
    "hospice": "Healthcare", "home health": "Healthcare", "oncology": "Healthcare",
    "pediatrics": "Healthcare", "cardiology": "Healthcare", "physicians": "Healthcare",
    # Biotech/Pharma
    "pharma": "Biotech/Pharma", "pharmaceutical": "Biotech/Pharma",
    "pharmaceuticals": "Biotech/Pharma", "biopharma": "Biotech/Pharma",
    "therapeutics": "Biotech/Pharma", "biosciences": "Biotech/Pharma",
    "biologics": "Biotech/Pharma", "genomics": "Biotech/Pharma",
    # Banking/Finance
    "bank": "Banking/Finance", "bancorp": "Banking/Finance", "bancshares": "Banking/Finance",
    "credit union": "Banking/Finance",
    # Insurance
    "insurance": "Insurance", "reinsurance": "Insurance", "indemnity": "Insurance",
    # Manufacturing/Industrial
    "manufacturing": "Manufacturing/Industrial", "machining": "Manufacturing/Industrial",
    "fabrication": "Manufacturing/Industrial", "foundry": "Manufacturing/Industrial",
    # Education
    "university": "Education", "college": "Education", "academy": "Education",
    "school": "Education", "schools": "Education", "education": "Education",
    "educacao": "Education", "educacion": "Education", "polytechnic": "Education",
    # Travel/Hospitality
    "hospitality": "Travel/Hospitality", "hotel": "Travel/Hospitality",
    "hotels": "Travel/Hospitality", "resort": "Travel/Hospitality",
    "resorts": "Travel/Hospitality", "cruises": "Travel/Hospitality",
    # Aerospace/Defense
    "aerospace": "Aerospace/Defense", "defense": "Aerospace/Defense",
    "defence": "Aerospace/Defense", "avionics": "Aerospace/Defense",
    "munitions": "Aerospace/Defense", "armaments": "Aerospace/Defense",
    # Semiconductors/Hardware
    "semiconductor": "Semiconductors/Hardware", "semiconductors": "Semiconductors/Hardware",
    "microelectronics": "Semiconductors/Hardware", "photonics": "Semiconductors/Hardware",
    # Logistics/SupplyChain
    "logistics": "Logistics/SupplyChain", "freight": "Logistics/SupplyChain",
    "warehousing": "Logistics/SupplyChain", "trucking": "Logistics/SupplyChain",
    # RealEstate/PropTech
    "realty": "RealEstate/PropTech", "real estate": "RealEstate/PropTech",
    # Food/Beverage
    "brewing": "Food/Beverage", "brewery": "Food/Beverage", "winery": "Food/Beverage",
    "distillery": "Food/Beverage", "bakery": "Food/Beverage",
    # Telecom
    "telecom": "Telecom", "telecommunications": "Telecom", "broadband": "Telecom",
    # Crypto/Web3
    "crypto": "Crypto/Web3", "blockchain": "Crypto/Web3", "web3": "Crypto/Web3",
    # Cybersecurity
    "cybersecurity": "Cybersecurity",
    # Consulting/Services
    "consulting": "Consulting/Services", "consultants": "Consulting/Services",
    # Gaming
    "esports": "Gaming",
    # Fintech
    "fintech": "Fintech",
}
# Longest tokens first so "real estate" / "credit union" win over any substring word match.
_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_NAME_SECTOR, key=len, reverse=True)) + r")\b"
)


def _fold(s: str) -> str:
    """Accent-fold + lowercase so 'Educação' matches 'educacao'."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).lower()


def name_sector(company: str | None) -> str | None:
    """High-precision sector from an unambiguous industry word in the company name, else None."""
    if not company:
        return None
    m = _NAME_RE.search(_fold(company))
    return _NAME_SECTOR[m.group(1)] if m else None


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
        # (1) Authoritative curated table (company key/domain -> sector). (2) Fallback: an
        # unambiguous industry word in the company's OWN name ("X Bank", "X Hospitality").
        # A description-text fallback was measured at ~24% accuracy (JDs name-drop many
        # industries) and dropped; the company-name signal is far higher precision (~100% on a
        # live spot-check), so returning None ("unknown") still beats a mostly-wrong guess.
        table = load_sector_index().get(key=inp.company_key, domain=inp.company_domain)
        return table if table else name_sector(inp.company)


register_extractor(SectorExtractor())
