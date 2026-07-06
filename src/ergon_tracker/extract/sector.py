"""Company -> sector classification (table-backed)."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files

from .base import ExtractInput, register_extractor

__all__ = [
    "SectorIndex",
    "load_sector_index",
    "SectorExtractor",
    "name_sector",
    "company_sector",
]

# High-precision company-NAME -> sector tokens. ONLY unambiguous industry words belong here:
# a company literally named "<X> Bank" / "<X> Hospitality" / "<X> Manufacturing" is in that
# industry. This is the opposite of description-text classification (dropped at ~24% accuracy):
# the company's own name naming its industry is a strong signal. Applied ONLY when the curated
# table misses, so it never overrides authoritative data; opaque brand names stay "unknown".
# Deliberately omits generic words ("industries", "technology", "group", "partners", "labs",
# "energy", "motors") that carry no reliable sector signal.
_NAME_SECTOR: dict[str, str] = {
    # Healthcare
    "healthcare": "Healthcare",
    "health system": "Healthcare",
    "hospital": "Healthcare",
    "hospitals": "Healthcare",
    "clinic": "Healthcare",
    "clinics": "Healthcare",
    "medical center": "Healthcare",
    "eyecare": "Healthcare",
    "dental": "Healthcare",
    "dentistry": "Healthcare",
    "orthodontics": "Healthcare",
    "autism": "Healthcare",
    "hospice": "Healthcare",
    "home health": "Healthcare",
    "oncology": "Healthcare",
    "pediatrics": "Healthcare",
    "cardiology": "Healthcare",
    "physicians": "Healthcare",
    # Biotech/Pharma
    "pharma": "Biotech/Pharma",
    "pharmaceutical": "Biotech/Pharma",
    "pharmaceuticals": "Biotech/Pharma",
    "biopharma": "Biotech/Pharma",
    "therapeutics": "Biotech/Pharma",
    "biosciences": "Biotech/Pharma",
    "biologics": "Biotech/Pharma",
    "genomics": "Biotech/Pharma",
    # Banking/Finance
    "bank": "Banking/Finance",
    "bancorp": "Banking/Finance",
    "bancshares": "Banking/Finance",
    "credit union": "Banking/Finance",
    # Insurance
    "insurance": "Insurance",
    "reinsurance": "Insurance",
    "indemnity": "Insurance",
    # Manufacturing/Industrial
    "manufacturing": "Manufacturing/Industrial",
    "machining": "Manufacturing/Industrial",
    "fabrication": "Manufacturing/Industrial",
    "foundry": "Manufacturing/Industrial",
    # Education
    "university": "Education",
    "college": "Education",
    "academy": "Education",
    "school": "Education",
    "schools": "Education",
    "education": "Education",
    "educacao": "Education",
    "educacion": "Education",
    "polytechnic": "Education",
    # Travel/Hospitality
    "hospitality": "Travel/Hospitality",
    "hotel": "Travel/Hospitality",
    "hotels": "Travel/Hospitality",
    "resort": "Travel/Hospitality",
    "resorts": "Travel/Hospitality",
    "cruises": "Travel/Hospitality",
    # Aerospace/Defense
    "aerospace": "Aerospace/Defense",
    "defense": "Aerospace/Defense",
    "defence": "Aerospace/Defense",
    "avionics": "Aerospace/Defense",
    "munitions": "Aerospace/Defense",
    "armaments": "Aerospace/Defense",
    # Semiconductors/Hardware
    "semiconductor": "Semiconductors/Hardware",
    "semiconductors": "Semiconductors/Hardware",
    "microelectronics": "Semiconductors/Hardware",
    "photonics": "Semiconductors/Hardware",
    # Logistics/SupplyChain
    "logistics": "Logistics/SupplyChain",
    "freight": "Logistics/SupplyChain",
    "warehousing": "Logistics/SupplyChain",
    "trucking": "Logistics/SupplyChain",
    # RealEstate/PropTech
    "realty": "RealEstate/PropTech",
    "real estate": "RealEstate/PropTech",
    # Food/Beverage
    "brewing": "Food/Beverage",
    "brewery": "Food/Beverage",
    "winery": "Food/Beverage",
    "distillery": "Food/Beverage",
    "bakery": "Food/Beverage",
    # Telecom
    "telecom": "Telecom",
    "telecommunications": "Telecom",
    "broadband": "Telecom",
    # Crypto/Web3
    "crypto": "Crypto/Web3",
    "blockchain": "Crypto/Web3",
    "web3": "Crypto/Web3",
    # Cybersecurity
    "cybersecurity": "Cybersecurity",
    # Consulting/Services
    "consulting": "Consulting/Services",
    "consultants": "Consulting/Services",
    # Gaming
    "esports": "Gaming",
    "games": "Gaming",
    "gaming": "Gaming",
    # Fintech
    "fintech": "Fintech",
    # --- gaps surfaced by the 700-company sector benchmark (2026-07-06), high-precision keywords ---
    # Energy/Climate: "Siemens Energy", "Dominion Energy", "NextEra Energy", "NRG Energy", ...
    "energy": "Energy/Climate",
    "solar": "Energy/Climate",
    "renewables": "Energy/Climate",
    "renewable energy": "Energy/Climate",
    # Manufacturing/Industrial. NB "industries"/"industrial" are deliberately NOT here — too broad
    # ("Anduril Industries" is Defense, many "X Industries" span sectors); only concrete materials.
    "steel": "Manufacturing/Industrial",
    "chemicals": "Manufacturing/Industrial",
    "packaging": "Manufacturing/Industrial",
    "plastics": "Manufacturing/Industrial",
    # Banking/Finance: asset managers, hedge funds, securities houses ("capital"/"partners" omitted —
    # too ambiguous with VC / consulting).
    "asset management": "Banking/Finance",
    "securities": "Banking/Finance",
    "investments": "Banking/Finance",
    "capital markets": "Banking/Finance",
    # Biotech/Pharma
    "diagnostics": "Biotech/Pharma",
    "biotech": "Biotech/Pharma",
    # Logistics/SupplyChain
    "supply chain": "Logistics/SupplyChain",
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


# Exact company-NAME -> sector for the largest opaque-brand employers the curated table/registry
# and name-token rules miss (e.g. "Domino's", "Red Bull", "Anduril"). These carry huge job counts
# in the index (Domino's alone is ~6% of all postings) but have no industry word in their name, so
# they'd stay "unknown". Hand-curated, high-confidence, job-count-weighted; keyed by the normalized
# company name so every posting from that employer matches regardless of source/registry key.
_COMPANY_SECTOR_RAW: dict[str, str] = {
    "Domino's": "Food/Beverage",
    "Red Bull": "Food/Beverage",
    "Greene King": "Food/Beverage",
    "Insomnia Cookies": "Food/Beverage",
    "Insomniacookies": "Food/Beverage",
    "Guzman y Gomez": "Food/Beverage",
    "CROSSMARK": "Consulting/Services",
    "Turner & Townsend": "Consulting/Services",
    "Securitas": "Consulting/Services",
    "Inetum": "Consulting/Services",
    "Devoteam": "Consulting/Services",
    "Ramboll": "Consulting/Services",
    "Capco": "Consulting/Services",
    "Deloitte": "Consulting/Services",
    "Dexterra": "Consulting/Services",
    "Veolia Environnement": "Energy/Climate",
    "Veolia": "Energy/Climate",
    "Home Instead": "Healthcare",
    "Vohra": "Healthcare",
    "Lifestance": "Healthcare",
    "LifeStance Health": "Healthcare",
    "AgeCare": "Healthcare",
    "albanymed": "Healthcare",
    "Eurofins": "Biotech/Pharma",
    "Anduril Industries": "Aerospace/Defense",
    "Anduril": "Aerospace/Defense",
    "Boxlunch": "E-commerce/Retail",
    "advanceauto": "E-commerce/Retail",
    "METRO/MAKRO": "E-commerce/Retail",
    "Sears": "E-commerce/Retail",
    "Frasers Group": "E-commerce/Retail",
    "Maersk": "Logistics/SupplyChain",
    "Equinox": "Consumer/Lifestyle",
    "Relais & Châteaux": "Travel/Hospitality",
    "Minor International": "Travel/Hospitality",
    "Sika": "Manufacturing/Industrial",
    "Sika AG": "Manufacturing/Industrial",
    "Smiths Group": "Manufacturing/Industrial",
    "Cornerstone Building Brands": "Manufacturing/Industrial",
    "KIPP": "Education",
    "SIXT": "Automotive/Mobility",
    "Monro, Inc.": "Automotive/Mobility",
    "NBCUniversal": "Media/Entertainment",
    "Dentsu Creative (MKTG)": "Media/Entertainment",
    # second tier (job-count-weighted, high-confidence opaque brands)
    "Talan": "Consulting/Services",
    "Nagarro": "Consulting/Services",
    "Sutherland": "Consulting/Services",
    "WNS Global Services": "Consulting/Services",
    "Wavestone": "Consulting/Services",
    "Egis Group": "Consulting/Services",
    "1komma5grad": "Energy/Climate",
    "alfalaval": "Manufacturing/Industrial",
    "airproducts": "Manufacturing/Industrial",
    "Mattel": "Consumer/Lifestyle",
    "POP MART Americas Inc.": "Consumer/Lifestyle",
    "Vuori, Inc": "Consumer/Lifestyle",
    "Family Resource Home Care": "Healthcare",
    "Dungarvin": "Healthcare",
    "Caring Senior Service": "Healthcare",
    "Ally Behavior Centers": "Healthcare",
    "ConvenientMD": "Healthcare",
    "All Care Therapies": "Healthcare",
    "altamed": "Healthcare",
    "Simonmed": "Healthcare",
    "Shieldai": "Aerospace/Defense",
    "Eataly North America": "Food/Beverage",
    "The Wonderful Company": "Food/Beverage",
    "Reitmans (Canada) Ltée/Ltd": "E-commerce/Retail",
    "BTG Pactual": "Banking/Finance",
    "Canonical": "Software/SaaS",
    "Linkedin": "Software/SaaS",
    "Ubisoft": "Gaming",
    # third tier — curated from the LIVE 1.07M index's top unknown brands (incl. lowercased ATS
    # tenant tokens like 'jpmc'/'tjx' that appear as the company string)
    "Starbucks": "Food/Beverage",
    "panerabread": "Food/Beverage",
    "jpmc": "Banking/Finance",
    "citi": "Banking/Finance",
    "pnc": "Banking/Finance",
    "hsbc": "Banking/Finance",
    "scotiabank": "Banking/Finance",
    "Goldman Sachs": "Banking/Finance",
    "morganstanley": "Banking/Finance",
    "tjx": "E-commerce/Retail",
    "lowes": "E-commerce/Retail",
    "abercrombie": "E-commerce/Retail",
    "petco": "E-commerce/Retail",
    "meijer": "E-commerce/Retail",
    "signetjewelers": "E-commerce/Retail",
    "H&M Group": "E-commerce/Retail",
    "hyatt": "Travel/Hospitality",
    "thermofisher": "Biotech/Pharma",
    "massgeneralbrigham": "Healthcare",
    "Mount Sinai": "Healthcare",
    "cvs shared services resources": "Healthcare",
    "thales": "Aerospace/Defense",
    "SpaceX": "Aerospace/Defense",
    "eaton": "Manufacturing/Industrial",
    "qualcomm": "Semiconductors/Hardware",
    "jll": "RealEstate/PropTech",
    "greystar": "RealEstate/PropTech",
    "Sopra Steria": "Consulting/Services",
    "Burns & McDonnell": "Consulting/Services",
    # fourth tier — deeper LIVE-index slice (recognizable brands + lowercased ATS tenant tokens)
    "City of New York": "Government/Public",
    "usbank": "Banking/Finance",
    "truist": "Banking/Finance",
    "fifththird": "Banking/Finance",
    "ocbc": "Banking/Finance",
    "tapestry": "E-commerce/Retail",
    "homedepot": "E-commerce/Retail",
    "michaels": "E-commerce/Retail",
    "sephora": "E-commerce/Retail",
    "jcrew": "E-commerce/Retail",
    "pfchangs": "Food/Beverage",
    "totalwine": "Food/Beverage",
    "geisinger": "Healthcare",
    "medtronic": "Healthcare",
    "houstonmethodist": "Healthcare",
    "avera": "Healthcare",
    "gehc": "Healthcare",
    "wellstar": "Healthcare",
    "msd": "Biotech/Pharma",
    "gsk": "Biotech/Pharma",
    "infineon": "Semiconductors/Hardware",
    "tranetechnologies": "Manufacturing/Industrial",
    "freudenberg": "Manufacturing/Industrial",
    "zeissgroup": "Manufacturing/Industrial",
    "lithia": "Automotive/Mobility",
    "motorolasolutions": "Telecom",
    "verizon": "Telecom",
    "rutgers": "Education",
    "Speechify": "Software/SaaS",
    "WPP Media": "Media/Entertainment",
    "dentsuaegis": "Media/Entertainment",
    "EPAM Systems": "Consulting/Services",
    "nttdata": "Consulting/Services",
    "CGI Technologies and Solutions, Inc.": "Consulting/Services",
    "arcadis": "Consulting/Services",
    "bechtel": "Consulting/Services",
}


@lru_cache(maxsize=1)
def _company_sector_map() -> dict[str, str]:
    """Normalized-company-name -> sector (built once from the curated raw map)."""
    from ..dedup import normalize_company

    out: dict[str, str] = {}
    for raw, sector in _COMPANY_SECTOR_RAW.items():
        key = normalize_company(raw)
        if key:
            out[key] = sector
    return out


def company_sector(company: str | None) -> str | None:
    """Exact, high-precision sector for a known large opaque-brand employer, else None."""
    if not company:
        return None
    from ..dedup import normalize_company

    return _company_sector_map().get(normalize_company(company))


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
        if table:
            return table
        # (2) Exact match for large opaque-brand employers (Domino's, Anduril, ...). (3) Unambiguous
        # industry word in the company's own name. Both high-precision; else "unknown" (None).
        return company_sector(inp.company) or name_sector(inp.company)


register_extractor(SectorExtractor())
