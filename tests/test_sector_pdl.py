from __future__ import annotations

import pytest

probe = pytest.importorskip("scripts.probe_pdl_sectors")
sp = pytest.importorskip("scripts.sector_pdl")


def test_allowlist_values_are_valid_and_excludes_coarse() -> None:
    valid = {
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
    assert set(sp.PDL_ALLOWLIST.values()) <= valid
    # coarse/ambiguous buckets must NOT be trusted
    for bad in (
        "internet",
        "information technology and services",
        "financial services",
        "marketing and advertising",
        "consumer goods",
        "telecommunications",
    ):
        assert bad not in sp.PDL_ALLOWLIST


def test_build_pdl_map_gates_allowlist_and_maps_keys() -> None:
    idx = probe.TargetIndex(norm_to_keys={"acme": ["acme"], "globex": ["globex", "globex2"]})
    matches = {"acme": "banking", "globex": "internet"}  # banking in-list, internet excluded
    out = sp.build_pdl_map(matches, idx)
    assert out == {"acme": {"sector": "Banking/Finance", "source": "pdl", "industry": "banking"}}


def test_build_pdl_map_expands_multiple_keys() -> None:
    idx = probe.TargetIndex(norm_to_keys={"acme": ["acme", "acmeinc"]})
    out = sp.build_pdl_map({"acme": "insurance"}, idx)
    assert set(out) == {"acme", "acmeinc"}
    assert out["acmeinc"]["sector"] == "Insurance"


def test_accuracy_on_gold() -> None:
    idx = probe.TargetIndex(gold_norm_to_sector={"acme": "Banking/Finance", "globex": "Insurance"})
    # acme→banking correct; globex→banking wrong (maps Banking/Finance != Insurance)
    assert sp.accuracy_on_gold({"acme": "banking", "globex": "banking"}, idx) == (1, 2)
    # out-of-list industries don't count toward total
    assert sp.accuracy_on_gold({"acme": "internet"}, idx) == (0, 0)
