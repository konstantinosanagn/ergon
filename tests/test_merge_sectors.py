from __future__ import annotations

import pytest

ms = pytest.importorskip("scripts.merge_sectors")


def test_apply_priority_gapfill_and_no_override() -> None:
    seed = {"a": {"domain": None}, "b": {"domain": "b.com"}, "c": {"domain": None}}
    curated = {"a": "Software/SaaS"}  # 'a' already known → untouched
    sources = {
        "edgar": {"b": "Banking/Finance"},
        "wikidata": {},
        "slug": {},
        "pdl": {"b": "Insurance", "c": "Healthcare"},
    }
    out = ms.apply_priority(seed, curated, sources, ["edgar", "wikidata", "slug", "pdl"])
    assert "a" not in out  # curated skipped entirely
    assert out["b"] == {
        "sector": "Banking/Finance",
        "domain": "b.com",
        "source": "edgar",
    }  # edgar wins; pdl does NOT override
    assert out["c"] == {
        "sector": "Healthcare",
        "domain": None,
        "source": "pdl",
    }  # pdl gap-fills unknown c
