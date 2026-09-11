"""A sweep shard must never lose its whole run to the job timeout.

freshness-sweep shards 2 (rippling) and 10 (smartrecruiters) hit the 180-minute job timeout every
day; the CLI writes its sidecar once at the end, so both contributed nothing, and smartrecruiters —
the #2 source — was never swept. They also printed nothing, so nobody could say where they stalled.

The deadline box: once past it, no board is dispatched and no confirm fetch is made; an unswept
board is counted ``deadline_skipped`` and stays undetermined — never "empty", never expired. A
periodic progress line says how far the shard got.
"""

from __future__ import annotations

import logging
import sqlite3
import time

import anyio
import pytest
from tests.test_freshness_sweep import _build_index, _job_row, _job_status, _raw

from ergon.index import freshness
from ergon.index.freshness import sweep_all_boards

_NOW = "2026-09-11T00:00:00+00:00"


class _Provider:
    """Live board is EMPTY (every stored id departed) and each fetch takes ``delay`` seconds."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay, self.calls = delay, 0

    async def fetch(self, token, query, fetcher):  # noqa: ANN001, ANN202
        self.calls += 1
        await anyio.sleep(self.delay)
        return [_raw("survivor")]  # one live id -> a real sweep expires the other two


def _boards(tmp_path, n: int, source: str = "greenhouse"):  # noqa: ANN001, ANN202
    jobs = []
    for b in range(n):
        for i in range(3):
            row = _job_row(f"{source}-b{b}-{i}", source=source, source_job_id=f"id{i}")
            row["board_token"] = f"b{b}"
            jobs.append(row)
        jobs[-1]["source_job_id"] = "survivor"
    idx = _build_index(tmp_path, jobs)
    boards = sorted({(source, j["board_token"]) for j in jobs})
    return idx, boards


def _run(idx, boards, prov, monkeypatch, *, deadline, concurrency=4):  # noqa: ANN001, ANN202
    monkeypatch.setattr(freshness, "get_provider", lambda name: prov)
    con = sqlite3.connect(idx)
    try:
        return anyio.run(
            lambda: sweep_all_boards(
                boards,
                con,
                fetcher=None,
                concurrency=concurrency,
                now=lambda: _NOW,
                deadline=deadline,
            )
        )
    finally:
        con.close()


def test_past_deadline_nothing_is_fetched_or_expired(tmp_path, monkeypatch) -> None:
    idx, boards = _boards(tmp_path, 5)
    prov = _Provider()
    stats = _run(idx, boards, prov, monkeypatch, deadline=time.monotonic() - 1)
    assert prov.calls == 0
    assert stats["greenhouse"]["deadline_skipped"] == 5
    assert stats["greenhouse"]["checked"] == 0 and stats["greenhouse"]["expired"] == 0
    for b in range(5):
        for i in range(2):
            assert _job_status(idx, f"greenhouse-b{b}-{i}")[0] == "active", (
                "unswept must stay active"
            )


def test_no_deadline_is_the_old_behaviour(tmp_path, monkeypatch) -> None:
    idx, boards = _boards(tmp_path, 3)
    prov = _Provider()
    stats = _run(idx, boards, prov, monkeypatch, deadline=None)
    assert prov.calls == 3 and stats["greenhouse"]["deadline_skipped"] == 0
    assert stats["greenhouse"]["expired"] == 6


def test_deadline_mid_run_keeps_what_was_determined(tmp_path, monkeypatch) -> None:
    """The point of the box: partial work is written, the rest is left undetermined."""
    idx, boards = _boards(tmp_path, 6)
    prov = _Provider(delay=0.25)
    stats = _run(idx, boards, prov, monkeypatch, deadline=time.monotonic() + 0.3, concurrency=1)
    s = stats["greenhouse"]
    assert 1 <= prov.calls < 6, prov.calls
    assert s["checked"] == prov.calls and s["deadline_skipped"] == 6 - prov.calls
    assert s["expired"] == 2 * prov.calls


def test_search_index_confirms_stop_at_the_deadline(tmp_path, monkeypatch) -> None:
    """smartrecruiters is a bulk-relist source: candidates need a confirm fetch; none past the deadline."""
    idx, boards = _boards(tmp_path, 2, source="smartrecruiters")
    prov = _Provider()
    stats = _run(idx, boards, prov, monkeypatch, deadline=time.monotonic() - 1)
    assert prov.calls == 0
    assert stats["smartrecruiters"]["deadline_skipped"] == 2
    assert stats["smartrecruiters"]["expired"] == 0


class _RelistThenConfirm(_Provider):
    """Relist finishes before the deadline and names two departures; every confirm comes after."""

    def __init__(self) -> None:
        super().__init__(delay=0.35)
        self.confirms = 0

    async def fetch_detail(self, ref, fetcher):  # noqa: ANN001, ANN202
        self.confirms += 1
        return None  # "confirmed dead" -- would expire the row if the guard were missing


def test_confirm_fetches_stop_at_the_deadline(tmp_path, monkeypatch) -> None:
    idx, boards = _boards(tmp_path, 1, source="smartrecruiters")
    prov = _RelistThenConfirm()
    stats = _run(idx, boards, prov, monkeypatch, deadline=time.monotonic() + 0.3, concurrency=1)
    s = stats["smartrecruiters"]
    assert prov.calls == 1 and s["candidates"] == 2, "the relist itself ran before the deadline"
    assert prov.confirms == 0, "no confirm fetch may be made past the deadline"
    assert s["unconfirmed"] == 2 and s["expired"] == 0
    assert _job_status(idx, "smartrecruiters-b0-0")[0] == "active"


def test_progress_is_reported(tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    idx, boards = _boards(tmp_path, 4)
    with caplog.at_level(logging.INFO, logger="ergon.index.freshness"):
        _run(idx, boards, _Provider(), monkeypatch, deadline=None)
    lines = [r.getMessage() for r in caplog.records if "[freshness]" in r.getMessage()]
    assert lines and "4/4 boards" in lines[-1] and "checked=4" in lines[-1]
