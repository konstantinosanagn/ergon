from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROSSWALK = ROOT / "scripts" / "linkedin_industry_to_sector.json"

VALID_SECTORS = {
    "AI/ML",
    "Aerospace/Defense",
    "Automotive/Mobility",
    "Banking/Finance",
    "Biotech/Pharma",
    "Consulting/Services",
    "Consumer/Lifestyle",
    "Crypto/Web3",
    "Cybersecurity",
    "E-commerce/Retail",
    "Education",
    "Energy/Climate",
    "Fintech",
    "Food/Beverage",
    "Gaming",
    "Government/Public",
    "Healthcare",
    "Insurance",
    "Logistics/SupplyChain",
    "Manufacturing/Industrial",
    "Media/Entertainment",
    "Other",
    "RealEstate/PropTech",
    "Semiconductors/Hardware",
    "Software/SaaS",
    "Telecom",
    "Travel/Hospitality",
}


def test_crosswalk_values_are_valid_sectors() -> None:
    data = json.loads(CROSSWALK.read_text())
    assert len(data) >= 60, f"crosswalk too small ({len(data)})"
    bad = {v for v in data.values() if v not in VALID_SECTORS}
    assert not bad, f"invalid sector labels in crosswalk: {bad}"


def test_crosswalk_keys_are_lowercased() -> None:
    data = json.loads(CROSSWALK.read_text())
    assert all(k == k.lower() for k in data), "crosswalk keys must be lowercased"


def test_crosswalk_covers_high_frequency_industries() -> None:
    data = json.loads(CROSSWALK.read_text())
    # a few anchor mappings that must be correct
    assert data["computer software"] == "Software/SaaS"
    assert data["banking"] == "Banking/Finance"
    assert data["biotechnology"] == "Biotech/Pharma"
    assert data["semiconductors"] == "Semiconductors/Hardware"
    assert data["hospital & health care"] == "Healthcare"
    assert data["computer games"] == "Gaming"
