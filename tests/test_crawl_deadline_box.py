"""Sub-phase A: the crawl deadline-box skips DISPATCHING boards on an over-budget host.

An over-budget board must be left exactly as if it were never in this run's window: popped from
``outcome`` (so it is never marked crawled and its prior rows carry forward), its ``BoardState``
untouched (so it stays ``due`` and rolls to the next run). An under-budget board on the same run is
crawled normally -- proving the box is per-host, not global.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.scheduler import BoardState  # noqa: E402
from ergon_tracker.models import JobPosting, RawJob  # noqa: E402


class _FakeReg:
    def all(self):
        return {
            "slowco": {"ats": "greenhouse", "token": "slow"},
            "fastco": {"ats": "greenhouse", "token": "fast"},
        }


class _Provider:
    """No conditional_url (so grab goes straight to fetch); list_host keys the deadline-box."""

    name = "greenhouse"

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return "slow.example" if token == "slow" else "fast.example"

    async def fetch(self, token, query, fetcher):
        if token == "slow":
            raise AssertionError("over-budget board must NOT be fetched")
        return [RawJob(source="greenhouse", source_job_id="1", company="Fast", token=token)]

    def normalize(self, raw):
        return JobPosting.create(
            source="greenhouse", source_job_id=raw.source_job_id, company="Fast", title="Eng"
        )


class _Fetcher:
    """``is_over_budget`` is True only for the slow host -> only the slow board is boxed."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def is_over_budget(self, host, budget):
        return host == "slow.example"


def test_deadline_box_skips_over_budget_host_only(monkeypatch, tmp_path):
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod
    from ergon_tracker.index.db import connect

    monkeypatch.setattr(store_mod, "SeedRegistry", _FakeReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _Provider())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(http_mod, "AsyncFetcher", _Fetcher)

    slow = BoardState(provider="greenhouse", token="slow", next_due="2000-01-01")
    fast = BoardState(provider="greenhouse", token="fast", next_due="2000-01-01")
    states = {slow.key: slow, fast.key: fast}
    fresh = tmp_path / "fresh.sqlite"

    outcome, _cursor = anyio.run(bi._crawl_due, 10, states, fresh, "b1")

    # The slow board was deadline-boxed: popped from outcome, never fetched, state untouched.
    assert slow.key not in outcome
    assert states[slow.key].last_crawled is None  # never marked crawled -> stays due next run
    # The fast board was crawled normally.
    assert fast.key in outcome
    rows = connect(fresh, read_only=True).execute("SELECT company FROM jobs").fetchall()
    assert [r[0] for r in rows] == ["Fast"]


def test_deadline_box_disabled_when_budget_non_positive(monkeypatch, tmp_path):
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    monkeypatch.setattr(store_mod, "SeedRegistry", _FakeReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _Provider())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(http_mod, "AsyncFetcher", _Fetcher)
    monkeypatch.setenv("ERGON_CRAWL_HOST_BUDGET_S", "0")  # disable the box

    slow = BoardState(provider="greenhouse", token="slow", next_due="2000-01-01")
    states = {slow.key: slow}
    fresh = tmp_path / "fresh.sqlite"

    # Box disabled -> the slow board IS dispatched, so its fetch() runs and raises the AssertionError
    # inside grab, which is isolated (recorded as error), NOT re-raised. Proof the box didn't skip it:
    outcome, _cursor = anyio.run(bi._crawl_due, 10, states, fresh, "b1")
    assert slow.key in outcome and outcome[slow.key]["error"] is True
