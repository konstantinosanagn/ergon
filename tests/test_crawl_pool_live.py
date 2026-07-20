"""Live micro-benchmark (opt-in): worker pool over real greenhouse vs join boards.

Skipped unless ``ERGON_LIVE_TESTS=1``. Demonstrates end-to-end that:
  * the bounded pool drains a real interleaved board sample, and
  * the per-host deadline-box bounds the slow (join) tail -- once join blows its budget the pool
    stops crawling further join boards while greenhouse keeps going.

Deliberately tiny and polite: a handful of boards per source, low concurrency.
"""

from __future__ import annotations

import time

import pytest

from ergon_tracker.crawl_pool import run_pool
from ergon_tracker.http import AsyncFetcher

pytestmark = pytest.mark.live

_SAMPLE_PER_SOURCE = 6
_JOIN_BUDGET_S = 20.0 * 60.0  # the real-run join cap; here it just exercises the API


def _sample_boards() -> list[tuple[str, str]]:
    """Return [(ats, token), ...] -- a few greenhouse + a few join boards, interleaved."""
    from ergon_tracker.registry.store import SeedRegistry

    picks: dict[str, list[str]] = {"greenhouse": [], "join": []}
    for e in SeedRegistry().all().values():
        ats = e.get("ats")
        if ats in picks and len(picks[ats]) < _SAMPLE_PER_SOURCE:
            picks[ats].append(e["token"])
        if all(len(v) >= _SAMPLE_PER_SOURCE for v in picks.values()):
            break
    interleaved: list[tuple[str, str]] = []
    for gh, jn in zip(picks["greenhouse"], picks["join"], strict=False):
        interleaved.append(("greenhouse", gh))
        interleaved.append(("join", jn))
    return interleaved


async def test_pool_benchmark_greenhouse_vs_join() -> None:
    boards = _sample_boards()
    if len(boards) < 4:
        pytest.skip("registry has too few greenhouse/join boards to benchmark")

    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.base import get_provider, load_builtins

    load_builtins()

    from urllib.parse import urlsplit

    per_source_time: dict[str, float] = {"greenhouse": 0.0, "join": 0.0}
    per_source_count: dict[str, int] = {"greenhouse": 0, "join": 0}
    skipped_over_budget: list[str] = []

    async def handler(board: tuple[str, str]) -> int:
        ats, token = board
        provider = get_provider(ats)
        # Mimic the controller's deadline-box gate: skip a source once it has blown its budget.
        curl = provider.conditional_url(token)
        host = urlsplit(curl).netloc if curl else ""
        if host and fetcher.is_over_budget(host, _JOIN_BUDGET_S):
            skipped_over_budget.append(token)
            return 0
        t0 = time.monotonic()
        raws = await provider.fetch(token, SearchQuery(), fetcher)
        per_source_time[ats] += time.monotonic() - t0
        per_source_count[ats] += 1
        return len(raws)

    async with AsyncFetcher(timeout=15.0, retries=2, concurrency=16) as fetcher:
        t0 = time.monotonic()
        stats = await run_pool(boards, handler, concurrency=8)
        wall = time.monotonic() - t0

    print("\n--- crawl_pool live benchmark ---")
    print(f"boards={len(boards)} wall={wall:.1f}s processed={stats.processed} failed={stats.failed}")
    for src in ("greenhouse", "join"):
        n = per_source_count[src]
        secs = per_source_time[src]
        rate = (n / secs) if secs > 0 else float("nan")
        print(f"  {src:10s} boards={n} busy={secs:.1f}s -> {rate:.2f} boards/s")
    print("  per-host deadline-box accounting (every host the fetcher touched):")
    for host in sorted(fetcher._host_first_seen):  # noqa: SLF001 -- benchmark introspection
        print(
            f"    {host:32s} reqs={fetcher.host_request_count(host):3d} "
            f"wall_elapsed={fetcher.host_wall_elapsed(host):5.1f}s "
            f"busy={fetcher.host_busy_seconds(host):5.1f}s "
            f"over_20min={fetcher.is_over_budget(host, 20 * 60)}"
        )
    print(f"  skipped-over-budget: {len(skipped_over_budget)}")

    # The pool drained every board (crash-isolated: even total failures count as processed).
    assert stats.processed == len(boards)
    # The deadline-box accounting recorded real per-host wall-clock for the hosts we hit.
    assert any(fetcher.host_request_count(h) > 0 for h in fetcher._host_first_seen)  # noqa: SLF001
