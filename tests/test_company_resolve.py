"""Tests for company entity-resolution match keys (precision + recall on known cases)."""

from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "_company_resolve", ROOT / "scripts" / "company_resolve.py"
)
cr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cr)  # type: ignore[union-attr]


def _covered(legal: str, registry_names: list[str]) -> bool:
    return cr.is_covered(legal, cr.build_key_index(registry_names))


def test_legal_suffix_and_descriptor_names_match_short_keys():
    # The bulk case: SEC legal name -> our short slug, via descriptor stripping.
    assert _covered("Cisco Systems, Inc.", ["cisco"])
    assert _covered("Palantir Technologies Inc", ["palantir"])
    assert _covered("Stripe, Inc.", ["stripe"])
    assert _covered("NVIDIA CORP", ["nvidia"])


def test_brand_rename_via_alias():
    assert _covered("Alphabet Inc.", ["google"])
    assert _covered("Meta Platforms, Inc.", ["meta"])
    assert _covered("ADVANCED MICRO DEVICES INC", ["amd"])
    assert _covered("INTERNATIONAL BUSINESS MACHINES CORP", ["ibm"])


def test_share_class_and_state_suffixes_stripped():
    # SEC names carry share-class/state-of-incorp noise that must not block a match.
    assert _covered("Alphabet Inc. (Class A)", ["google"])  # via alias after stripping (Class A)
    assert _covered("Alphabet Inc. (Class C)", ["google"])
    assert _covered("Berkshire Hathaway Inc. (Class B)", ["berkshire hathaway"])
    assert _covered("1895 Bancorp of Wisconsin, Inc. /MD/", ["1895 bancorp of wisconsin"])


def test_collapse_matches_space_and_hyphen_variants():
    # The space/hyphen-collapse fix: registry slug vs SEC multi-word legal name.
    assert _covered("Molson Coors Beverage Company", ["molson-coors"])
    assert _covered("T-Mobile US, Inc.", ["tmobile"])
    assert _covered("Nasdaq, Inc.", ["nasdaq"])


def test_collapse_stays_exact_not_substring():
    # Collapsed match is EXACT, not substring — "stripe" must not match "stripersonline".
    assert not _covered("Stripe, Inc.", ["stripersonline"])
    assert not _covered("Cintas Corporation", ["cint"])


def test_accent_folding():
    assert _covered("Telefónica", ["telefonica"])


def test_no_false_positive_on_shared_first_word():
    # The classic trap: same first token, different companies must NOT match.
    idx = cr.build_key_index(["american express"])
    assert not cr.is_covered("American Airlines Group Inc", idx)
    idx2 = cr.build_key_index(["general motors"])
    assert not cr.is_covered("General Electric Co", idx2)


def test_unrelated_company_not_covered():
    assert not _covered("Exxon Mobil Corp", ["stripe", "cisco", "palantir"])


def test_match_keys_contains_full_and_core():
    keys = cr.match_keys("Cisco Systems, Inc.")
    assert "cisco" in keys  # core/brand
    assert "cisco systems" in keys  # full normalized
