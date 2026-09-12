"""Why the smartrecruiters sweep shard took three hours, and the fix.

A local run of the real shard with progress output: every relist finished inside 20 minutes;
smartrecruiters then produced 19,487 departure candidates, and on the largest board 14 of 15
sampled "missing" postings were alive — offset pagination over a 24k-item list that shifts while
250 pages are fetched. The confirm step is doing what it was built for; the cost was the problem:
~19.5k confirms every day at the list endpoint's storm-safe 3 req/s is 1.8 hours, so the shard
never finished and re-confirmed the same alive postings daily. The drain fetches the SAME detail
endpoint at 40/s from its own process, probed clean to 76/s.

Pinned here: an ``AsyncFetcher`` can carry instance-level rate overrides that never leak; the
sweep confirms on that second fetcher while relists stay on the storm-safe one; and confirm order
rotates by day so a deadline cutoff does not starve the same ids every run.
"""

from __future__ import annotations

import sqlite3

import anyio
from tests.test_freshness_sweep import _build_index, _job_row, _raw

import ergon.http as http_mod
from ergon.http import AsyncFetcher
from ergon.index import freshness
from ergon.index.freshness import sweep_all_boards

_NOW = "2026-09-11T00:00:00+00:00"


def test_instance_rate_override_does_not_leak() -> None:
    before = dict(http_mod._DOMAIN_RATE_OVERRIDES)
    fast = AsyncFetcher(rate_overrides={"smartrecruiters.com": (40.0, 1.0)})
    plain = AsyncFetcher()
    assert fast._host_limiter("smartrecruiters.com").max_rate == 40.0
    assert plain._host_limiter("smartrecruiters.com").max_rate == 3.0, "the list cap must survive"
    assert before == http_mod._DOMAIN_RATE_OVERRIDES, "the process-wide table must not change"
    assert fast._host_limiter("example.com").max_rate == 5.0  # untouched hosts keep the default


class _Provider:
    """Relist omits two stored ids; records WHICH fetcher each call came through."""

    def __init__(self) -> None:
        self.relist_fetchers: list[object] = []
        self.confirm_fetchers: list[object] = []
        self.confirmed: list[str] = []

    async def fetch(self, token, query, fetcher):  # noqa: ANN001, ANN202
        self.relist_fetchers.append(fetcher)
        return [_raw("survivor")]

    async def fetch_detail(self, ref, fetcher):  # noqa: ANN001, ANN202
        self.confirm_fetchers.append(fetcher)
        self.confirmed.append(ref.id)
        return "<p>still here</p>"  # alive


def _boards(tmp_path, n: int, per_board: int = 3):  # noqa: ANN001, ANN202
    jobs = []
    for b in range(n):
        for i in range(per_board):
            row = _job_row(f"sr-b{b}-{i}", source="smartrecruiters", source_job_id=f"id{b}-{i}")
            row["board_token"] = f"b{b}"
            jobs.append(row)
        jobs[-1]["source_job_id"] = "survivor"
    return _build_index(tmp_path, jobs), sorted(
        {("smartrecruiters", j["board_token"]) for j in jobs}
    )


def _run(idx, boards, prov, monkeypatch, **kw):  # noqa: ANN001, ANN202
    monkeypatch.setattr(freshness, "get_provider", lambda name: prov)
    con = sqlite3.connect(idx)
    try:
        return anyio.run(
            lambda: sweep_all_boards(
                boards, con, kw.pop("fetcher", None), now=kw.pop("now", lambda: _NOW), **kw
            )
        )
    finally:
        con.close()


def test_confirms_use_the_confirm_fetcher_and_relists_do_not(tmp_path, monkeypatch) -> None:
    idx, boards = _boards(tmp_path, 3)
    prov = _Provider()
    lister, confirmer = object(), object()
    stats = _run(
        idx, boards, prov, monkeypatch, fetcher=lister, confirm_fetcher=confirmer, concurrency=2
    )
    assert stats["smartrecruiters"]["confirmed_alive"] == 6
    assert set(prov.relist_fetchers) == {lister} and len(prov.relist_fetchers) == 3
    assert set(prov.confirm_fetchers) == {confirmer} and len(prov.confirm_fetchers) == 6


def test_without_a_confirm_fetcher_everything_uses_the_one_fetcher(tmp_path, monkeypatch) -> None:
    idx, boards = _boards(tmp_path, 2)
    prov = _Provider()
    only = object()
    _run(idx, boards, prov, monkeypatch, fetcher=only, concurrency=2)
    assert set(prov.relist_fetchers) == set(prov.confirm_fetchers) == {only}


def test_confirm_order_rotates_by_day(tmp_path, monkeypatch) -> None:
    """A deadline cutoff lands on whatever is last, so BOTH the order boards are visited in and
    the order of candidates within a board must differ from day to day."""
    idx, boards = _boards(tmp_path, 8, per_board=6)  # 5 candidates per board: 120 orderings
    board_orders: set[tuple[str, ...]] = set()
    within_b0: set[tuple[str, ...]] = set()  # the SAME board each day, or board rotation masks it
    for day in ("2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"):
        prov = _Provider()
        _run(
            idx,
            boards,
            prov,
            monkeypatch,
            fetcher=object(),
            concurrency=1,
            now=lambda d=day: f"{d}T00:00:00+00:00",
        )
        assert len(prov.confirmed) == 40
        visited = tuple(dict.fromkeys(c.split("-")[1] for c in prov.confirmed))
        board_orders.add(visited)
        within_b0.add(tuple(c for c in prov.confirmed if c.split("-")[1] == "b0"))
    assert len(board_orders) >= 3, f"boards were visited in the same order: {board_orders}"
    assert len(within_b0) >= 3, f"b0's candidates kept the same order: {within_b0}"
