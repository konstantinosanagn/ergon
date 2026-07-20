#!/usr/bin/env python3
"""Build a deterministic company -> sector map from US H-1B NAICS data.

Data source (FREE): USCIS H-1B Employer Data Hub annual export CSVs
  https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub/h-1b-employer-data-hub-files
  file pattern: h1b_datahubexport-<YEAR>.csv
  columns: Fiscal Year, Employer, Initial Approval, Initial Denial,
           Continuing Approval, Continuing Denial, NAICS (2-digit),
           Tax ID, State, City, ZIP

The DOL OFLC LCA disclosure data (which carries full 6-digit NAICS) is
bot-blocked (HTTP 403) for automated download, so we use the USCIS hub
which exposes the NAICS *2-digit sector* only.

Output: scripts/sector_naics.json
  {"company_key": {"sector": "<vocab>", "source": "naics", "naics": "54"}, ...}
for matched companies only. Does NOT modify seed.json / sectors.json.
"""
import csv
import json
import os
import re
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, ".h1b_cache")
SEED = os.path.join(ROOT, "src/ergon_tracker/registry/data/seed.json")
SECTORS = os.path.join(ROOT, "src/ergon_tracker/registry/data/sectors.json")
OUT = os.path.join(HERE, "sector_naics.json")

YEARS = [2023, 2022, 2021]  # most recent USCIS exports available
URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{}.csv"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# NAICS 2-digit sector -> ergon_tracker 27-label vocab.
# 2-digit conflates some sub-sectors; we pick the dominant/best-fit label.
# 54 (Professional/Scientific/Technical) is dominated by 5415 Computer Systems
# Design in a tech-skewed registry, so it maps to Software/SaaS (see report).
NAICS_VOCAB = {
    "11": "Other",                    # Agriculture/Forestry/Fishing (no vocab fit)
    "21": "Energy/Climate",           # Mining, Oil & Gas Extraction
    "22": "Energy/Climate",           # Utilities
    "23": "Manufacturing/Industrial", # Construction
    "31": "Manufacturing/Industrial", # Manufacturing
    "32": "Manufacturing/Industrial", # Manufacturing
    "33": "Manufacturing/Industrial", # Manufacturing
    "42": "E-commerce/Retail",        # Wholesale Trade
    "44": "E-commerce/Retail",        # Retail Trade
    "45": "E-commerce/Retail",        # Retail Trade
    "48": "Logistics/SupplyChain",    # Transportation
    "49": "Logistics/SupplyChain",    # Warehousing
    "51": "Software/SaaS",            # Information (software/internet/data)
    "52": "Banking/Finance",          # Finance and Insurance
    "53": "RealEstate/PropTech",      # Real Estate, Rental, Leasing
    "54": "Software/SaaS",            # Professional/Scientific/Technical (5415-heavy)
    "55": "Other",                    # Management of Companies (holding cos)
    "56": "Consulting/Services",      # Administrative & Support Services
    "61": "Education",                # Educational Services
    "62": "Healthcare",               # Health Care & Social Assistance
    "71": "Media/Entertainment",      # Arts, Entertainment, Recreation
    "72": "Travel/Hospitality",       # Accommodation & Food Services
    "81": "Other",                    # Other Services
    "92": "Government/Public",         # Public Administration
    # "99" Nonclassifiable -> skipped (no signal)
}

LEGAL = {
    "inc", "incorporated", "llc", "corp", "corporation", "co", "company",
    "ltd", "limited", "lp", "llp", "pllc", "plc", "gmbh", "ag", "sa", "sas",
    "bv", "nv", "oy", "ab", "srl", "spa", "pvt", "pte", "pty", "kk", "kg",
    "the", "group", "holdings", "holding", "usa", "us", "na",
}


def norm(name: str) -> str:
    """Aggressive normalization: lowercase, drop legal suffix tokens, strip
    all non-alphanumerics."""
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"[.,/&'\"()-]", " ", s)
    toks = [t for t in s.split() if t and t not in LEGAL]
    return re.sub(r"[^a-z0-9]", "", "".join(toks))


def dba_variants(name: str):
    """Yield normalized candidate keys, splitting on DBA / formerly etc."""
    out = []
    low = name.lower()
    parts = re.split(r"\b(?:dba|d/b/a|formerly|aka|fka)\b", low)
    for p in parts:
        n = norm(p)
        if n:
            out.append(n)
    if not out:
        n = norm(name)
        if n:
            out.append(n)
    return out


def fetch(year: int) -> str:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"h1b_{year}.csv")
    if os.path.exists(path) and os.path.getsize(path) > 100000:
        return path
    url = URL.format(year)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def build_employer_naics():
    """normalized employer name -> dominant 2-digit NAICS (weighted by
    total petitions across years)."""
    # key -> {naics: weight}
    weights = defaultdict(lambda: defaultdict(int))
    for y in YEARS:
        path = fetch(y)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                emp = (row.get("Employer") or "").strip()
                naics = (row.get("NAICS") or "").strip()
                if not emp or not naics or naics == "99":
                    continue
                try:
                    w = (int(row.get("Initial Approval") or 0)
                         + int(row.get("Initial Denial") or 0)
                         + int(row.get("Continuing Approval") or 0)
                         + int(row.get("Continuing Denial") or 0))
                except ValueError:
                    w = 1
                w = max(w, 1)
                for k in dba_variants(emp):
                    weights[k][naics] += w
    emp_naics = {}
    for k, d in weights.items():
        # dominant NAICS; tie-break deterministically by code
        naics = max(sorted(d.items()), key=lambda x: x[1])[0]
        emp_naics[k] = naics
    return emp_naics


def build_seed_index():
    """normalized key -> company_key. Built from both the registry key and
    the domain's main label."""
    with open(SEED) as fh:
        seed = json.load(fh)["companies"]
    idx = {}
    for ck, meta in seed.items():
        nk = norm(ck)
        if nk and nk not in idx:
            idx[nk] = ck
        dom = (meta or {}).get("domain")
        if dom:
            label = dom.split("//")[-1].split("/")[0]
            label = label.split(".")[0] if "." in label else label
            nd = norm(label)
            if nd and nd not in idx:
                idx[nd] = ck
    return seed, idx


def main():
    emp_naics = build_employer_naics()
    seed, idx = build_seed_index()

    out = {}
    for nk, ck in idx.items():
        naics = emp_naics.get(nk)
        if not naics:
            continue
        sector = NAICS_VOCAB.get(naics)
        if not sector:
            continue
        # don't overwrite if same company_key already matched
        if ck in out:
            continue
        out[ck] = {"sector": sector, "source": "naics", "naics": naics}

    with open(OUT, "w") as fh:
        json.dump(dict(sorted(out.items())), fh, indent=1)

    # ---- validation ----
    with open(SECTORS) as fh:
        curated = json.load(fh)["companies"]
    total_seed = len(seed)
    matched = len(out)
    both = [c for c in out if c in curated]
    correct = sum(1 for c in both if out[c]["sector"] == curated[c]["sector"])
    print(f"data source: USCIS H-1B Employer Data Hub {YEARS}")
    print(f"distinct normalized employers: {len(emp_naics)}")
    print(f"registry companies: {total_seed}")
    print(f"matched: {matched}  coverage: {matched/total_seed*100:.1f}%")
    print(f"overlap with curated gold: {len(both)}")
    if both:
        print(f"accuracy vs curated: {correct}/{len(both)} = "
              f"{correct/len(both)*100:.1f}%")
    # per-naics breakdown of accuracy
    from collections import Counter
    naics_n = Counter(out[c]["naics"] for c in both)
    naics_ok = Counter(out[c]["naics"] for c in both
                       if out[c]["sector"] == curated[c]["sector"])
    print("\nper-NAICS accuracy on overlap (naics: correct/total -> vocab):")
    for code in sorted(naics_n, key=lambda c: -naics_n[c]):
        print(f"  {code}: {naics_ok[code]}/{naics_n[code]}"
              f" -> {NAICS_VOCAB[code]}")

    # Family-level accuracy: NAICS cannot encode fine vocab splits (AI/ML,
    # Cybersecurity, Fintech, Crypto, Semiconductors all live under generic
    # parent codes). Credit a match if the curated label is a NAICS-resolvable
    # sibling of the predicted family.
    FAMILY = {
        "54": {"Software/SaaS", "AI/ML", "Cybersecurity", "Consulting/Services",
               "Crypto/Web3", "Fintech", "Biotech/Pharma", "Gaming"},
        "51": {"Software/SaaS", "AI/ML", "Cybersecurity", "Media/Entertainment",
               "Telecom", "Crypto/Web3", "Gaming"},
        "52": {"Banking/Finance", "Fintech", "Insurance", "Crypto/Web3"},
        "33": {"Manufacturing/Industrial", "Semiconductors/Hardware",
               "Aerospace/Defense", "Automotive/Mobility", "Healthcare"},
        "32": {"Manufacturing/Industrial", "Biotech/Pharma", "Food/Beverage",
               "Energy/Climate", "Consumer/Lifestyle"},
        "31": {"Manufacturing/Industrial", "Food/Beverage", "Consumer/Lifestyle"},
        "62": {"Healthcare", "Biotech/Pharma"},
        "42": {"E-commerce/Retail", "Logistics/SupplyChain"},
        "44": {"E-commerce/Retail", "Consumer/Lifestyle"},
        "45": {"E-commerce/Retail", "Consumer/Lifestyle"},
        "48": {"Logistics/SupplyChain", "Travel/Hospitality"},
        "49": {"Logistics/SupplyChain"},
        "72": {"Travel/Hospitality", "Food/Beverage"},
        "22": {"Energy/Climate"}, "21": {"Energy/Climate"},
        "53": {"RealEstate/PropTech"}, "61": {"Education"},
        "71": {"Media/Entertainment", "Gaming"},
        "92": {"Government/Public"}, "23": {"Manufacturing/Industrial",
               "RealEstate/PropTech"},
        "56": {"Consulting/Services"}, "11": {"Other"}, "55": {"Other"},
        "81": {"Other", "Consulting/Services"},
    }
    fam_ok = sum(1 for c in both
                 if curated[c]["sector"] in FAMILY.get(out[c]["naics"], set())
                 or curated[c]["sector"] == out[c]["sector"])
    print(f"\nfamily-level accuracy (NAICS-resolvable): "
          f"{fam_ok}/{len(both)} = {fam_ok/len(both)*100:.1f}%")


if __name__ == "__main__":
    main()
