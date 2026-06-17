"""Tests for the registry re-verifier's pure selection logic (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_registry import select  # noqa: E402

_COMPANIES = {
    "a": {"ats": "greenhouse", "token": "a"},
    "b": {"ats": "lever", "token": "b"},
    "c": {"ats": "greenhouse", "token": "c"},
    "d": {"ats": "join", "token": "d"},
    "e": {"ats": "greenhouse", "token": "e"},
}


def test_select_all() -> None:
    assert set(select(_COMPANIES, [], None)) == set(_COMPANIES)


def test_select_filters_by_ats() -> None:
    assert set(select(_COMPANIES, ["greenhouse"], None)) == {"a", "c", "e"}
    assert set(select(_COMPANIES, ["join", "lever"], None)) == {"b", "d"}


def test_select_sample_strides_and_caps() -> None:
    picked = select(_COMPANIES, [], 2)
    assert len(picked) == 2
    assert set(picked) <= set(_COMPANIES)
    # sample >= population returns everything (no over-selection)
    assert set(select(_COMPANIES, [], 99)) == set(_COMPANIES)
