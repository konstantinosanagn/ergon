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


def test_rebuild_table_locks_only_handcurated_and_rederives_sources() -> None:
    companies = {
        "hand1": {"sector": "Software/SaaS"},  # sourceless hand-curated → locked, preserved as-is
        "stalewd": {
            "sector": "Media/Entertainment",
            "source": "wikidata",
        },  # source-tagged, absent from sources → dropped
        "edg1": {
            "sector": "Insurance",
            "source": "edgar",
        },  # source-tagged, still in sources → re-derived
    }
    seed = {
        "hand1": {"domain": None},
        "stalewd": {"domain": None},
        "edg1": {"domain": None},
        "new": {"domain": None},
    }
    sources = {
        "edgar": {"edg1": "Insurance", "new": "Banking/Finance"},
        "wikidata": {},
        "slug": {},
        "pdl": {},
    }
    out = ms.rebuild_table(companies, seed, sources, ["edgar", "wikidata", "slug", "pdl"])
    assert out["hand1"] == {"sector": "Software/SaaS"}  # hand-curated preserved verbatim
    assert "stalewd" not in out  # stale wikidata dropped (not hand-curated, not re-produced)
    assert out["edg1"] == {
        "sector": "Insurance",
        "domain": None,
        "source": "edgar",
    }  # re-derived
    assert out["new"] == {
        "sector": "Banking/Finance",
        "domain": None,
        "source": "edgar",
    }  # new gap-fill
