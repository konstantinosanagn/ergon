"""Extensive DETERMINISTIC sector classifier (substring gazetteer + domain-TLD + suffix rules).

Validate-first against the curated sectors.json. Run with --apply to fill unclassified.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
SECTORS = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "sectors.json"

# Distinctive SUBSTRINGS (>=4 chars, low false-positive) -> sector. Ordered specific->general;
# first hit wins. Concatenated slugs like "epicgames"/"shieldai"/"energyvault" are caught.
_SUBSTR: list[tuple[str, tuple[str, ...]]] = [
    ("Biotech/Pharma", ("biotech", "pharma", "genomic", "therapeut", "biosci", "molecular", "vaccine", "oncolog", "antibod", "peptide", "lifescience", "biolog", "protein", "crispr", "diagnostic")),
    ("Healthcare", ("health", "medical", "dental", "clinic", "hospital", "patient", "caregiv", "medtech", "pharmacy", "wellness", "behavioral", "therapy", "nursing", "homecare", "telehealth", "orthod", "veterin", "physio")),
    ("Crypto/Web3", ("crypto", "blockchain", "web3", "defi", "onchain", "ledger", "bitcoin", "ethereum", "stablecoin", "tokeniz", "dao")),
    ("Cybersecurity", ("cyber", "security", "infosec", "firewall", "appsec", "pentest", "threatintel", "endpoint security", "zerotrust", "secureworks")),
    ("Semiconductors/Hardware", ("semiconduct", "silicon", "microchip", "fpga", "wafer", "hardware", "electronics", "robotic", "sensors", "lidar", "photonic", "chipmaker")),
    ("Aerospace/Defense", ("aerospace", "aviation", "satellite", "spacecraft", "rocket", "defense", "defence", "missile", "orbital", "avionic", "drones", "uav")),
    ("Automotive/Mobility", ("automotiv", "vehicle", "mobility", "rideshare", "carshare", "fleet", "autonomous", "motors", "ev charging", "escooter", "emobility")),
    ("Energy/Climate", ("energy", "solar", "climate", "renewab", "battery", "cleantech", "geotherm", "carbon", "sustainab", "powergrid", "hydrogen", "windpower", "decarbon", "greentech")),
    ("Gaming", ("games", "gaming", "esports", "gamestudio", "playstation", "videogame", "gamedev")),
    ("Fintech", ("fintech", "payments", "paytech", "lending", "neobank", "remittance", "payroll", "bnpl", "wallet", "billing", "invoicing", "spend management")),
    ("Banking/Finance", ("banking", "bancorp", "capital", "securities", "asset management", "equities", "financ", "investment", "brokerage", "hedge", "wealth", "tradingfirm", "fund management", "venture")),
    ("Insurance", ("insurance", "insurtech", "assurance", "underwrit", "reinsur", "actuar")),
    ("Logistics/SupplyChain", ("logistic", "supplychain", "supply chain", "freight", "shipping", "cargo", "warehous", "fulfillment", "lastmile", "courier", "trucking", "3pl")),
    ("RealEstate/PropTech", ("realestate", "real estate", "proptech", "realty", "mortgage", "housing", "rental", "property management", "homebuild", "brokerage real")),
    ("Education", ("education", "edtech", "learning", "academy", "university", "college", "tutor", "bootcamp", "elearning", "schooldistrict", "curriculum", "kindergarten")),
    ("Travel/Hospitality", ("travel", "hospitality", "tourism", "airline", "booking", "resort", "vacation", "hotelier", "cruise")),
    ("Food/Beverage", ("foods", "beverage", "restaurant", "coffee", "brewery", "brewing", "grocery", "kitchen", "winery", "distillery", "snacks", "bakery", "foodservice")),
    ("Telecom", ("telecom", "wireless", "broadband", "fiber", "cellular", "telco", "5gnetwork")),
    ("Media/Entertainment", ("media", "entertainment", "music", "film", "podcast", "streaming", "publishing", "broadcast", "newsroom", "studios", "records")),
    ("E-commerce/Retail", ("ecommerce", "e-commerce", "commerce", "retail", "shopify", "marketplace", "dropship", "merch", "storefront")),
    ("Consumer/Lifestyle", ("beauty", "fashion", "fitness", "apparel", "cosmetic", "lifestyle", "skincare", "footwear", "sportswear", "jewelry", "wellness brand")),
    ("Manufacturing/Industrial", ("manufactur", "industrial", "factory", "machinery", "steelworks", "materials", "construction", "fabricat", "foundry", "toolmaker", "plastics", "welding")),
    ("Government/Public", ("government", "cityof", "county", "municipal", "federal", "nonprofit", "publicsector", "ngo", "stategov")),
    ("Consulting/Services", ("consult", "advisory", "staffing", "recruit", "outsourc", "managed services", "systemsintegr", "professionalservices", "accountancy", "lawfirm", "legalservices")),
    ("AI/ML", ("artificial intelligence", "machine learning", "deeplearning", "generative", "computer vision", "neural", "datascience")),
    ("Software/SaaS", ("software", "saas", "cloud", "platform", "devtools", "developer", "analytics", "database", "automation", "infrastructure", "techlabs", "appdev", "lowcode", "nocode", "crm", "erp")),
]
_SUB_COMPILED = [(s, re.compile("|".join(re.escape(k) for k in ks), re.I)) for s, ks in _SUBSTR]

# Domain TLD / second-level signals (strong industry hints from new gTLDs).
_TLD: dict[str, str] = {
    ".ai": "AI/ML", ".health": "Healthcare", ".care": "Healthcare", ".games": "Gaming",
    ".gg": "Gaming", ".finance": "Banking/Finance", ".capital": "Banking/Finance",
    ".bank": "Banking/Finance", ".insurance": "Insurance", ".energy": "Energy/Climate",
    ".eco": "Energy/Climate", ".auto": "Automotive/Mobility", ".cars": "Automotive/Mobility",
    ".realestate": "RealEstate/PropTech", ".estate": "RealEstate/PropTech",
    ".homes": "RealEstate/PropTech", ".travel": "Travel/Hospitality", ".media": "Media/Entertainment",
    ".fm": "Media/Entertainment", ".tv": "Media/Entertainment", ".shop": "E-commerce/Retail",
    ".store": "E-commerce/Retail", ".law": "Consulting/Services", ".legal": "Consulting/Services",
    ".edu": "Education", ".academy": "Education", ".dev": "Software/SaaS", ".app": "Software/SaaS",
    ".cloud": "Software/SaaS", ".software": "Software/SaaS", ".tech": "Software/SaaS",
    ".io": "Software/SaaS", ".so": "Software/SaaS",
}
# Word-boundary rules for short/ambiguous tokens (avoid substring false positives).
_WORD: list[tuple[str, re.Pattern[str]]] = [
    ("AI/ML", re.compile(r"(^|[ \-_.])ai($|[ \-_.])|\bml\b|\bllm\b|\bgpt\b", re.I)),
    ("Crypto/Web3", re.compile(r"\bnft\b|\bcoin\b|\bdao\b", re.I)),
    ("Gaming", re.compile(r"\bgame\b|\bplay\b", re.I)),
    ("Banking/Finance", re.compile(r"\bbank\b|\bvc\b|\bfund\b", re.I)),
    ("Fintech", re.compile(r"\bpay\b|\bfin\b", re.I)),
    ("Energy/Climate", re.compile(r"\bgrid\b|\bwind\b|\bev\b", re.I)),
    ("Aerospace/Defense", re.compile(r"\bspace\b|\baero\b", re.I)),
]


def classify(key: str, domain: str | None) -> str | None:
    text = f"{key} {domain or ''}".replace("-", " ").replace("_", " ").lower()
    raw = f"{key}{domain or ''}".lower()  # for concatenated substrings
    for sector, pat in _SUB_COMPILED:
        if pat.search(raw) or pat.search(text):
            return sector
    if domain:
        d = domain.lower()
        for tld, sector in _TLD.items():
            if d.endswith(tld):
                return sector
    for sector, pat in _WORD:
        if pat.search(text):
            return sector
    return None


def main() -> None:
    apply = "--apply" in sys.argv
    seed = json.loads(SEED.read_text())["companies"]
    sec = json.loads(SECTORS.read_text())
    existing = sec["companies"]

    correct = total = matched = 0
    for key, entry in existing.items():
        gold = entry.get("sector")
        if not gold:
            continue
        total += 1
        pred = classify(key, entry.get("domain") or seed.get(key, {}).get("domain"))
        if pred is not None:
            matched += 1
            correct += pred == gold
    print(f"VALIDATION vs {total} curated:")
    print(f"  coverage: {matched / total:.0%}  accuracy-on-labeled: {correct / matched:.0%}  overall: {correct / total:.0%}")

    todo = [k for k in seed if k not in existing]
    preds = {k: classify(k, seed[k].get("domain")) for k in todo}
    labeled = sum(1 for v in preds.values() if v)
    print(f"\nUNCLASSIFIED: {len(todo)}  would-label: {labeled} ({labeled / len(todo):.0%})")

    if apply:
        for k, v in preds.items():
            if v:
                existing[k] = {"sector": v, "domain": seed[k].get("domain"), "heuristic": True}
        SECTORS.write_text(json.dumps(sec, ensure_ascii=True, indent=1) + "\n")
        print(f"applied: sectors.json now {len(existing)} entries")


if __name__ == "__main__":
    main()
