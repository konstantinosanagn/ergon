from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CROSSWALK = ROOT / "scripts" / "linkedin_industry_to_sector.json"

probe = pytest.importorskip("scripts.probe_pdl_sectors")

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


def test_norm_wraps_normalize_company() -> None:
    assert probe.norm("Acme, Inc.") == "acme"
    assert probe.norm("") == ""
    assert probe.norm(None) == ""


def test_build_target_index() -> None:
    seed = {"acme": {"ats": "greenhouse"}, "globex": {"ats": "lever"}, "initech": {"ats": "ashby"}}
    sectors = {"acme": {"sector": "Software/SaaS"}, "globex": {"sector": None}}
    gold = [
        {"company": "Acme Inc", "company_key": "acme", "sector": "Software/SaaS"},
        {"company": "Globex", "company_key": "globex", "sector": None},
    ]
    idx = probe.build_target_index(seed, sectors, gold)
    assert "acme" in idx.registry_norms and "globex" in idx.registry_norms
    assert idx.norm_to_keys["acme"] == ["acme"]
    assert idx.covered_keys == {"acme"}  # only acme has a non-null sector
    assert idx.gold_norm_to_sector == {"acme": "Software/SaaS"}  # null-sector gold dropped


def test_record_industry_extracts_and_scores() -> None:
    rec = {"name": "Acme, Inc.", "industry": "computer software", "size": "11-50", "x": ""}
    out = probe.record_industry(rec)
    assert out == ("acme", "computer software", 3)  # 3 non-empty values
    assert probe.record_industry({"industry": "x"}) is None  # no name → None


def test_join_chunk_filters_and_keeps_most_complete() -> None:
    targets = frozenset({"acme", "globex"})
    lines = [
        json.dumps({"name": "Acme", "industry": "internet"}),
        json.dumps({"name": "Acme Inc", "industry": "computer software", "size": "1", "hq": "SF"}),
        json.dumps({"name": "Nope", "industry": "banking"}),
    ]
    got = probe.join_chunk(lines, targets)
    assert set(got) == {"acme"}  # globex absent, Nope filtered out
    assert got["acme"][0] == "computer software"  # higher completeness wins


def test_run_join_inline_and_parallel_agree() -> None:
    targets = frozenset({"acme", "globex"})
    lines = [
        json.dumps({"name": "Acme", "industry": "internet"}),
        json.dumps({"name": "Globex", "industry": "banking"}),
        json.dumps({"name": "Other", "industry": "retail"}),
    ]
    m1, c1 = probe.run_join(iter(lines), targets, workers=1, chunk_size=2)
    m2, c2 = probe.run_join(iter(lines), targets, workers=2, chunk_size=1)
    assert m1 == m2 == {"acme": "internet", "globex": "banking"}


def test_run_join_memory_bounded_on_large_stream() -> None:
    # 200k synthetic rows, only a few match; peak matches stays tiny (memory-bounded).
    targets = frozenset({"acme"})

    def gen():
        for i in range(200_000):
            yield json.dumps({"name": f"co{i}", "industry": "internet"})
        yield json.dumps({"name": "Acme", "industry": "computer software"})

    matches, _ = probe.run_join(gen(), targets, workers=1, chunk_size=10_000)
    assert matches == {"acme": "computer software"}
