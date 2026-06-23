"""Unit tests for the Tier-2 mint shell's pure logic (extraction + store-write; no browser)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from token_mint import MintError, extract_token, mint_from_state, summarize  # noqa: E402

from ergon_tracker.token_store import TokenStore  # noqa: E402

_STATE = {
    "cookies": {"_abck": "AKAMAI~SENSOR~VALUE", "ga": "x"},
    "local_storage": {"myjobstoken": "LS-TOKEN"},
    "session_storage": {"sid": "SS-TOKEN"},
    "xhr": [
        {"url": "https://x.test/static/app.js", "headers": {"accept": "*/*"}},
        {"url": "https://x.test/api/load-jobs", "headers": {"X-CSRF-Token": "CSRF-123"}},
    ],
}


def test_extract_from_each_source():
    assert extract_token(_STATE, {"cookie": "_abck"}) == "AKAMAI~SENSOR~VALUE"
    assert extract_token(_STATE, {"local_storage": "myjobstoken"}) == "LS-TOKEN"
    assert extract_token(_STATE, {"session_storage": "sid"}) == "SS-TOKEN"
    # xhr_header: URL substring match + case-insensitive header name
    assert (
        extract_token(
            _STATE, {"xhr_header": {"url_contains": "load-jobs", "header": "x-csrf-token"}}
        )
        == "CSRF-123"
    )


def test_extract_misses_return_none():
    assert extract_token(_STATE, {"cookie": "nope"}) is None
    assert (
        extract_token(_STATE, {"xhr_header": {"url_contains": "load-jobs", "header": "absent"}})
        is None
    )
    assert extract_token({}, {"cookie": "_abck"}) is None


def test_mint_from_state_writes_store(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    target = {"extract": {"cookie": "_abck"}, "ttl_seconds": 1800, "refresh_on": [403]}
    val = mint_from_state("fastenal", _STATE, target, store)
    assert val == "AKAMAI~SENSOR~VALUE"
    assert store.get("fastenal") == "AKAMAI~SENSOR~VALUE"
    assert store.should_refresh_on("fastenal", 403) and not store.should_refresh_on("fastenal", 401)


def test_mint_missing_token_raises_and_does_not_write(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    target = {"extract": {"cookie": "absent"}, "ttl_seconds": 60}
    with pytest.raises(MintError):
        mint_from_state("x", _STATE, target, store)
    assert store.get("x") is None  # nothing written on failure


def test_summary_never_leaks_the_token():
    target = {"extract": {"cookie": "_abck"}, "ttl_seconds": 1800, "refresh_on": [403]}
    line = summarize("fastenal", "SUPER-SECRET-VALUE", target)
    assert "SUPER-SECRET-VALUE" not in line and "18 chars" in line
