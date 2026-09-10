"""Out-of-band fetch lanes must obey the same per-host guards as ``AsyncFetcher.request``.

Four providers reach a third party without ever calling ``request``: dayforce and paycom drive a
headless browser, tesla and peoplesoft use a curl_cffi session directly. They therefore ran with
no rate limit, no circuit breaker and no budget accounting — and stayed invisible to
``slowest_hosts`` and to the crawl's deadline-box, which is what makes an over-budget host
detectable at all.

``host_slot`` wraps that work in the same guards. It deliberately does NOT take the global
limiter: a browser session runs for tens of seconds, and holding one of the few global slots that
long would starve the ordinary HTTP crawl.
"""

from __future__ import annotations

import time

import anyio
import pytest

from ergon.exceptions import FetchError
from ergon.http import AsyncFetcher


async def test_host_slot_counts_the_attempt_and_wall_time() -> None:
    """Budget accounting is what the deadline-box reads; an unaccounted lane is invisible to it."""
    async with AsyncFetcher(per_host_rate=100) as f:
        assert f.host_request_count("example.test") == 0
        async with f.host_slot("https://example.test/board"):
            await anyio.sleep(0.05)
        assert f.host_request_count("example.test") == 1
        assert f.host_busy_seconds("example.test") >= 0.05
        assert f.host_wall_elapsed("example.test") >= 0.05


async def test_host_slot_feeds_the_budget_check() -> None:
    async with AsyncFetcher(per_host_rate=100) as f:
        async with f.host_slot("https://example.test/board"):
            await anyio.sleep(0.05)
        assert f.is_over_budget("example.test", 0.01) is True
        assert f.is_over_budget("example.test", 60.0) is False


async def test_host_slot_applies_the_rate_limit() -> None:
    """A slow bucket must actually pace the out-of-band lane, not wave it through."""
    async with AsyncFetcher(per_host_rate=2) as f:  # 2/s
        start = time.monotonic()
        for _ in range(3):
            async with f.host_slot("https://slow.test/x"):
                pass
        assert time.monotonic() - start >= 0.4, "three acquisitions at 2/s should have waited"


async def test_failure_trips_the_breaker_and_it_then_refuses() -> None:
    """A failing out-of-band lane must open the breaker, exactly like a failing request would."""
    async with AsyncFetcher(per_host_rate=100) as f:
        for _ in range(5):  # _Breaker threshold
            with pytest.raises(RuntimeError):
                async with f.host_slot("https://broken.test/x"):
                    raise RuntimeError("board fetch blew up")
        with pytest.raises(FetchError):
            async with f.host_slot("https://broken.test/x"):
                pass  # pragma: no cover - the breaker must refuse before the body runs


async def test_success_does_not_trip_the_breaker() -> None:
    async with AsyncFetcher(per_host_rate=100) as f:
        for _ in range(10):
            async with f.host_slot("https://fine.test/x"):
                pass
        async with f.host_slot("https://fine.test/x"):
            pass  # still open for business


async def test_host_slot_does_not_hold_the_global_limiter() -> None:
    """Deliberate: a long browser session must not starve the ordinary HTTP crawl.

    If host_slot took the global limiter, a single 45s session would occupy one of very few
    slots. Two slots on DIFFERENT hosts must be holdable simultaneously.
    """
    async with AsyncFetcher(per_host_rate=100) as f:
        both_held = False

        async def hold(url: str, ready: anyio.Event, go: anyio.Event) -> None:
            async with f.host_slot(url):
                ready.set()
                await go.wait()

        r1, r2, go = anyio.Event(), anyio.Event(), anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(hold, "https://a.test/x", r1, go)
            tg.start_soon(hold, "https://b.test/x", r2, go)
            with anyio.fail_after(2):
                await r1.wait()
                await r2.wait()
            both_held = True
            go.set()
        assert both_held


async def test_providers_that_bypass_the_client_use_it() -> None:
    """Guard against a new bypass: any provider driving a browser or curl_cffi must wrap it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "ergon" / "providers"
    offenders = []
    for p in sorted(root.glob("*.py")):
        src = p.read_text(encoding="utf-8")
        drives_own_transport = "async_playwright()" in src or "AsyncSession(" in src
        # The CALL, not the word — a comment mentioning host_slot must not satisfy this. (My
        # first version checked `"host_slot" in src` and a mutation that deleted the wrapper but
        # left its comment still passed.)
        if drives_own_transport and ".host_slot(" not in src:
            offenders.append(p.name)
    assert offenders == [], (
        f"provider(s) reaching a third party outside AsyncFetcher without host_slot: {offenders}"
    )
