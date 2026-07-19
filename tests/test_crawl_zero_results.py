"""A board that is crawled SUCCESSFULLY but returns ZERO postings must still be recorded as
"crawled" (so its departed jobs are dropped by carry_forward), not silently ghosted-forward
forever. Before the fix, ``outcome[bkey]["companies"]`` was populated only from successfully-
normalized job records, so a 0-result success never entered ``crawled_keys`` and carry_forward
treated the board as "not crawled -> keep the old jobs".
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.build import build_index_from_fresh_db  # noqa: E402
from ergon_tracker.index.db import connect  # noqa: E402
from ergon_tracker.index.scheduler import BoardState  # noqa: E402
from ergon_tracker.models import JobPosting  # noqa: E402


class _FakeReg:
    def all(self):
        return {"co": {"ats": "greenhouse", "token": "stripe", "domain": "stripe.com"}}


class _ProviderEmpty:
    """A board fetch that SUCCEEDS but yields zero postings this run (all jobs closed/removed)."""

    name = "greenhouse"

    def conditional_url(self, token):
        return None  # force the plain fetch() path, no conditional pre-check

    async def fetch(self, token, query, fetcher):
        return []  # success: the board responded fine, it just has nothing open right now

    def normalize(self, raw):  # pragma: no cover - never reached, raws is empty
        raise AssertionError


class _FetcherNoop:
    def __init__(self, *args, **kwargs):  # tolerate AsyncFetcher(timeout=, retries=) kwargs
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch(monkeypatch):
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    monkeypatch.setattr(store_mod, "SeedRegistry", _FakeReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _ProviderEmpty())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(http_mod, "AsyncFetcher", _FetcherNoop)


def test_crawl_due_zero_results_success_is_recorded_as_crawled(monkeypatch, tmp_path):
    """The board's company key must appear in outcome[...]['companies'] even though zero jobs
    were returned -- a successful-but-empty fetch is still a crawl, not a skip.
    """
    _patch(monkeypatch)
    bs = BoardState(provider="greenhouse", token="stripe", next_due="2000-01-01")
    states = {bs.key: bs}
    fresh_db_path = tmp_path / "fresh.sqlite"

    outcome, _cursor = anyio.run(bi._crawl_due, 10, states, fresh_db_path, "b1")

    assert outcome[bs.key]["error"] is False
    assert outcome[bs.key]["companies"] == {"co"}, (
        "a successfully-crawled board with 0 results must still register its company key so "
        "carry_forward treats it as crawled (and drops departed jobs), not skipped"
    )


def test_zero_result_board_drops_its_stale_jobs_on_next_build(monkeypatch, tmp_path):
    """End-to-end: company 'co' had 1 open job yesterday; today its board is crawled
    successfully but returns 0 postings. The next index build must NOT carry the stale job
    forward -- the board genuinely emptied out.
    """
    _patch(monkeypatch)

    # --- Day 1: company 'co' has one open job, published in the index. ---
    old_job = JobPosting.create(
        source="greenhouse", source_job_id="old-1", company="Co", title="Old Role"
    )
    fresh1 = tmp_path / "fresh1.sqlite"
    from ergon_tracker.index.build import append_jobs
    from ergon_tracker.index.db import fresh_db

    fresh_db(fresh1)
    con1 = connect(fresh1)
    try:
        con1.execute("PRAGMA foreign_keys = OFF")
        append_jobs(con1, [old_job], build_id="b0")
        con1.commit()
    finally:
        con1.close()
    day1_index = tmp_path / "day1.sqlite"
    build_index_from_fresh_db(fresh1, day1_index, build_id="b0")
    assert (
        connect(day1_index, read_only=True).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    )

    # --- Day 2: the SAME board is crawled successfully, now returns 0 postings. ---
    bs = BoardState(provider="greenhouse", token="stripe", next_due="2000-01-01")
    states = {bs.key: bs}
    fresh2 = tmp_path / "fresh2.sqlite"
    outcome, _cursor = anyio.run(bi._crawl_due, 10, states, fresh2, "b1")

    crawled_keys: set = (
        set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    )

    day2_index = tmp_path / "day2.sqlite"
    build_index_from_fresh_db(
        fresh2, day2_index, build_id="b1", prev_db=day1_index, crawled_keys=crawled_keys
    )

    n = connect(day2_index, read_only=True).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert n == 0, (
        f"expected the departed job to be dropped (board crawled successfully with 0 results), "
        f"but {n} stale row(s) were carried forward"
    )
