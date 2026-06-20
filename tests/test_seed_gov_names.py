"""Tests for the government-data name-seed builder (pure core, offline)."""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("_seed_gov", ROOT / "scripts" / "seed_gov_names.py")
sg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sg)  # type: ignore[union-attr]


_SPONSORS = {
    "big corp": {"n": 500, "last": "2026-01-01"},
    "mid corp": {"n": 50, "last": "2025-06-01"},
    "small corp": {"n": 2, "last": "2024-01-01"},
    "already here": {"n": 999, "last": "2026-02-01"},  # in registry -> excluded
}


def test_h1b_prioritized_orders_by_n_desc_and_excludes_registry():
    names = sg.h1b_prioritized(_SPONSORS, existing_keys={"already-here"}, limit=10)
    # ordered by filing volume desc; the highest-n company is already in the registry -> dropped.
    assert names == ["big corp", "mid corp", "small corp"]


def test_h1b_prioritized_respects_limit():
    assert sg.h1b_prioritized(_SPONSORS, existing_keys=set(), limit=1) == ["already here"]


def test_edgar_names_excludes_registry_and_caps():
    edgar = {"nvidia": 1, "broadcom": 2, "stripe": 3}
    out = sg.edgar_names(edgar, existing_keys={"stripe"}, limit=10)
    assert set(out) == {"nvidia", "broadcom"}


def test_build_seed_dedups_across_seeds():
    sponsors = {"acme": {"n": 10, "last": "2026-01-01"}}
    edgar = {"acme": 1, "nvidia": 2}  # acme overlaps the H-1B seed -> only once
    out = sg.build_seed(sponsors, edgar, existing_keys=set(), h1b_limit=10, edgar_limit=10)
    assert out.count("acme") == 1
    assert set(out) == {"acme", "nvidia"}
    assert out[0] == "acme"  # H-1B seed comes first
