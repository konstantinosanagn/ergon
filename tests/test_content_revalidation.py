"""Membership-only observations must not postpone a real content crawl forever."""

from __future__ import annotations

import anyio
import pytest
from tests.test_delta_crawl_skip import _POSTINGS, _Provider, _Reg, _write_sidecar, bi, idset_hash

from ergon.index.scheduler import (
    BoardState,
    apply_outcome,
    content_crawl_due,
    load_state,
    save_state,
)


@pytest.mark.parametrize("stamp", ["bad-date", "2026-09-11", "2026-09-03"])
def test_unverified_or_old_content_is_due(stamp):
    state = BoardState(provider="greenhouse", token="acme", last_content_crawled=stamp)
    assert content_crawl_due(state, "2026-09-10")


def _first_due_day(state, start="2026-09-10"):
    """The day within the next interval on which an unstamped board's revalidation falls."""
    from datetime import date, timedelta

    d0 = date.fromisoformat(start)
    return next(
        (d0 + timedelta(days=i)).isoformat()
        for i in range(8)
        if content_crawl_due(state, (d0 + timedelta(days=i)).isoformat())
    )


def test_membership_checks_do_not_refresh_content_clock(tmp_path):
    state = BoardState(provider="greenhouse", token="acme", last_content_crawled="2026-09-03")
    for day in range(4, 11):
        apply_outcome(state, today=f"2026-09-{day:02}", changed=False)
    assert state.last_crawled == "2026-09-10"
    assert content_crawl_due(state, "2026-09-10")
    path = tmp_path / "state.json"
    save_state({state.key: state}, path)
    assert load_state(path)[state.key].last_content_crawled == "2026-09-03"


@pytest.mark.parametrize("stamp, calls", [(None, 1), ("2026-09-03", 1), ("2026-09-04", 0)])
def test_crawl_revalidates_unchanged_membership(monkeypatch, tmp_path, stamp, calls):
    import ergon.providers.base as base_mod
    import ergon.registry.store as store_mod

    prov = _Provider()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    today = "2026-09-10"
    if stamp is None:  # an unstamped board is due on its staggered day, not on every day
        today = _first_due_day(BoardState(provider="greenhouse", token="acme"))
    monkeypatch.setattr(bi, "_today", lambda: today)
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    fingerprint = idset_hash({p[0] for p in _POSTINGS})
    _write_sidecar(tmp_path / "index-freshness.sqlite", "greenhouse", "acme", fingerprint)
    state = BoardState(
        provider="greenhouse", token="acme", idset_hash=fingerprint, last_content_crawled=stamp
    )
    out, _ = anyio.run(bi._crawl_due, 10, {state.key: state}, tmp_path / "fresh.sqlite", "eval")
    assert prov.fetch_calls == calls
    assert out[state.key]["not_modified"] is (calls == 0)
    assert state.last_content_crawled == (today if calls else stamp)


def test_failed_fetch_does_not_refresh_content_clock(monkeypatch, tmp_path):
    import ergon.providers.base as base_mod
    import ergon.registry.store as store_mod

    class Broken(_Provider):
        async def fetch(self, token, query, fetcher):
            raise RuntimeError("offline fixture: transient provider failure")

    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: Broken())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    state = BoardState(provider="greenhouse", token="acme")
    out, _ = anyio.run(bi._crawl_due, 10, {state.key: state}, tmp_path / "fresh.sqlite", "eval")
    assert out[state.key]["error"]
    assert state.last_content_crawled is None
