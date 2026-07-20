"""Bounded worker pool: concurrency ceiling, full drain, per-item crash isolation, streaming."""

from __future__ import annotations

from collections.abc import Iterator

import anyio
import pytest

from ergon_tracker.crawl_pool import gather, run_pool, stream_pool


async def test_pool_bounds_concurrency() -> None:
    """No more than N handlers ever run at once, even with many more items than workers."""
    n = 4
    total = 40
    live = 0
    peak = 0

    async def handler(_: int) -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await anyio.sleep(0.005)  # hold the slot so overlap is observable
        live -= 1

    stats = await run_pool(range(total), handler, concurrency=n)

    assert peak <= n  # the ceiling is never breached
    assert peak == n  # ... and it is actually reached (workers stay busy)
    assert stats.processed == total


async def test_pool_drains_all_items() -> None:
    """Every item is processed exactly once regardless of ordering."""
    seen: list[int] = []

    async def handler(x: int) -> int:
        seen.append(x)
        return x * x

    results = await gather(range(25), handler, concurrency=6)

    assert sorted(seen) == list(range(25))
    assert sorted(r.result for r in results) == [x * x for x in range(25)]
    assert all(r.ok for r in results)


async def test_pool_isolates_a_failing_item() -> None:
    """One item raising is captured, does not cancel siblings, and the pool still drains."""
    processed: list[int] = []

    async def handler(x: int) -> int:
        if x == 7:
            raise ValueError("boom")
        processed.append(x)
        return x

    stats = await run_pool(range(20), handler, concurrency=5)

    assert stats.processed == 20
    assert stats.failed == 1
    assert stats.succeeded == 19
    assert len(stats.errors) == 1
    assert isinstance(stats.errors[0], ValueError)
    # Every non-failing item still ran.
    assert sorted(processed) == [x for x in range(20) if x != 7]


async def test_pool_multiple_failures_all_captured() -> None:
    async def handler(x: int) -> int:
        if x % 3 == 0:
            raise RuntimeError(f"fail {x}")
        return x

    results = await gather(range(30), handler, concurrency=8)

    failed = [r for r in results if not r.ok]
    assert {r.item for r in failed} == {x for x in range(30) if x % 3 == 0}
    assert all(isinstance(r.error, RuntimeError) for r in failed)


async def test_pool_streams_results_as_they_complete() -> None:
    """stream_pool yields each result; fast items surface before slow ones."""
    order: list[int] = []

    async def handler(x: int) -> int:
        # Larger x finishes sooner -> completion order differs from input order.
        await anyio.sleep((10 - x) * 0.005)
        return x

    async with stream_pool(range(10), handler, concurrency=10) as results:
        async for res in results:
            order.append(res.result)

    assert sorted(order) == list(range(10))
    assert order != list(range(10))  # genuinely reordered by completion


async def test_pool_streaming_early_break_tears_down() -> None:
    """Breaking out of the stream early does not hang and does not error."""

    async def handler(x: int) -> int:
        await anyio.sleep(0.001)
        return x

    got = 0
    with anyio.fail_after(5):
        async with stream_pool(range(1000), handler, concurrency=4) as results:
            async for _ in results:
                got += 1
                if got >= 3:
                    break
    assert got == 3


async def test_pool_empty_input() -> None:
    async def handler(x: int) -> int:  # pragma: no cover - never called
        return x

    stats = await run_pool([], handler, concurrency=4)
    assert stats.processed == 0


async def test_pool_rejects_bad_concurrency() -> None:
    async def handler(x: int) -> int:  # pragma: no cover - never called
        return x

    with pytest.raises(ValueError, match="concurrency"):
        await run_pool(range(5), handler, concurrency=0)


async def test_pool_memory_is_bounded_not_prefetched() -> None:
    """The item source is consumed lazily -- the pool never pulls the whole (huge) window up front."""
    pulled = 0
    release = anyio.Event()

    def source() -> Iterator[int]:
        nonlocal pulled
        for i in range(1_000_000):
            pulled += 1
            yield i

    async def handler(_: int) -> None:
        await release.wait()  # block every worker so nothing drains

    async def drive() -> None:
        with anyio.move_on_after(0.2):
            await run_pool(source(), handler, concurrency=4)
        release.set()

    await drive()
    # With 4 workers + a zero-buffer queue, only a tiny bounded number of items is ever pulled,
    # never the million. (workers in-flight + at most one queued + feeder's current item.)
    assert pulled < 50
