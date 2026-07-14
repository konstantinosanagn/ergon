"""Rate-limit key: shared backends collapse to the registrable domain; Workday stays per-host."""

from __future__ import annotations

import pytest

from ergon_tracker.http import _rate_key


def test_shared_backend_subdomains_collapse() -> None:
    assert _rate_key("channable.recruitee.com") == "recruitee.com"
    assert _rate_key("foo.recruitee.com") == "recruitee.com"
    assert _rate_key("acme.jobs.personio.de") == "personio.de"


def test_workday_stays_per_tenant() -> None:
    assert _rate_key("nvidia.wd5.myworkdayjobs.com") == "nvidia.wd5.myworkdayjobs.com"
    assert _rate_key("salesforce.wd12.myworkdayjobs.com") == "salesforce.wd12.myworkdayjobs.com"


def test_oracle_and_icims_stay_per_tenant() -> None:
    # Each Oracle/iCIMS tenant is a separate customer's careers site on separate infra (same
    # shape as Workday) -> collapsing to the registrable domain is a false bottleneck.
    assert _rate_key("ehac.fa.us6.oraclecloud.com") == "ehac.fa.us6.oraclecloud.com"
    assert _rate_key("eeho.fa.us2.oraclecloud.com") == "eeho.fa.us2.oraclecloud.com"
    assert _rate_key("careers-costco.icims.com") == "careers-costco.icims.com"


def test_eightfold_stays_per_tenant() -> None:
    # Every eightfold customer is served from its own ``{tenant}.eightfold.ai`` subdomain (list AND
    # detail). Collapsing to ``eightfold.ai`` shared one circuit breaker across all tenants, so a
    # single tenant's 429 tripped the breaker for every other tenant -> measured 72% detail-fetch
    # failure in the drain. Per-tenant keying isolates each backend.
    assert _rate_key("morganstanley.eightfold.ai") == "morganstanley.eightfold.ai"
    assert _rate_key("marriott.eightfold.ai") == "marriott.eightfold.ai"


def test_single_host_providers_unchanged() -> None:
    assert _rate_key("boards-api.greenhouse.io") == "greenhouse.io"
    assert _rate_key("api.lever.co") == "lever.co"
    assert _rate_key("remoteok.com") == "remoteok.com"


def test_two_level_tld() -> None:
    assert _rate_key("acme.co.uk") == "acme.co.uk"
    assert _rate_key("jobs.acme.co.uk") == "acme.co.uk"


def test_throttle_prone_backends_have_stricter_rate_caps() -> None:
    # Workable/BambooHR/SmartRecruiters threw a 429 storm under the default rate; their per-domain
    # caps must be present and below the AsyncFetcher default (5/s) so a dense window can't burst.
    from ergon_tracker.http import _DOMAIN_RATE_OVERRIDES

    for dom in ("workable.com", "bamboohr.com", "smartrecruiters.com"):
        assert dom in _DOMAIN_RATE_OVERRIDES, f"{dom} missing a per-domain rate cap"
        rate, period = _DOMAIN_RATE_OVERRIDES[dom]
        assert rate / period < 5.0  # stricter than the constructor default


def test_host_limiter_uses_domain_override() -> None:
    # The limiter for a capped backend must reflect the override, not the constructor default.
    from ergon_tracker.http import _DOMAIN_RATE_OVERRIDES, AsyncFetcher

    f = AsyncFetcher(per_host_rate=5)
    lim = f._host_limiter("workable.com")
    assert lim.max_rate == _DOMAIN_RATE_OVERRIDES["workable.com"][0]


def test_self_built_client_raises_max_redirects_above_httpx_default() -> None:
    # join.com's evergreen-repost chains run 22-23 hops -- above httpx's own default of 20 --
    # so the self-built client (the one every provider actually uses in production) must raise
    # its max_redirects, letting a single AsyncFetcher.request/get_text call follow the WHOLE
    # chain internally (one rate-limit token per call, not one per hop; see providers/join.py).
    from ergon_tracker.http import AsyncFetcher

    f = AsyncFetcher()
    assert f._client.max_redirects == 30
    assert f._client.follow_redirects is True


# --- per-host in-flight concurrency cap (Workable cascade fix, bug 1) --------------------------
# Confirmed root cause: a high GLOBAL concurrency (e.g. 50) piling every coroutine onto a single
# shared host's strict token bucket (e.g. workable.com at 3/s) is what drove the pileup that
# tripped the circuit breaker and cascaded to a 100%-fail run. The per-host CapacityLimiter below
# bounds how many requests to one host are ever in flight, independent of the global limiter and
# without slowing a host whose token bucket is already the real throttle.


def test_default_per_host_concurrency_is_eight() -> None:
    from ergon_tracker.http import AsyncFetcher

    assert AsyncFetcher()._per_host_concurrency == 8


def test_per_host_concurrency_configurable_via_constructor() -> None:
    from ergon_tracker.http import AsyncFetcher

    assert AsyncFetcher(per_host_concurrency=3)._per_host_concurrency == 3


def test_per_host_concurrency_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    import ergon_tracker.http as http_mod

    monkeypatch.setenv("ERGON_PER_HOST_CONCURRENCY", "2")
    try:
        importlib.reload(http_mod)
        assert http_mod.AsyncFetcher()._per_host_concurrency == 2
    finally:
        monkeypatch.delenv("ERGON_PER_HOST_CONCURRENCY", raising=False)
        importlib.reload(http_mod)  # restore the real module state for every later test


def test_single_host_never_exceeds_per_host_concurrency_cap() -> None:
    # 50 tasks all hit ONE host with a generous (non-binding) rate bucket and a slow handler --
    # if the cap weren't enforced, all 50 would be in flight (sending) simultaneously.
    import anyio
    import httpx

    from ergon_tracker.http import AsyncFetcher

    in_flight = 0
    peak = 0
    guard = anyio.Lock()

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        async with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        await anyio.sleep(0.05)
        async with guard:
            in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = AsyncFetcher(
        client=client, concurrency=50, per_host_rate=1000, per_host_period=1.0
    )

    async def main() -> None:
        async with fetcher, anyio.create_task_group() as tg:
            for _ in range(50):
                tg.start_soon(fetcher.get_json, "https://onehost.test/x")

    anyio.run(main)
    assert peak <= 8


def test_per_host_cap_does_not_slow_an_already_rate_gated_host() -> None:
    # A host bound by its own token bucket (e.g. smartrecruiters.com at 3/s) must sustain that
    # rate unchanged -- the concurrency cap (8) sits ABOVE the bucket's throughput, so it never
    # becomes the binding constraint even with many more waiters than the cap.
    import time

    import anyio
    import httpx

    from ergon_tracker.http import AsyncFetcher

    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = AsyncFetcher(client=client, concurrency=50)

    async def main() -> float:
        async with fetcher:
            start = time.monotonic()
            async with anyio.create_task_group() as tg:
                for _ in range(9):  # one burst (3) + two full periods (3 + 3) at 3 req/s
                    tg.start_soon(fetcher.get_json, "https://smartrecruiters.com/x")
            return time.monotonic() - start

    elapsed = anyio.run(main)
    # 9 requests at a strict 3/s bucket takes >= ~2s (burst of 3 free, then 2 more seconds for
    # the remaining 6); the per-host concurrency cap (8) must not add extra delay beyond that.
    assert elapsed >= 1.8


# --- circuit breaker must not trip on rate-limiting (Workable cascade fix, bug 3) --------------
# Confirmed root cause: 429s were counted toward the breaker on EVERY retry attempt, so with
# retries=3 a single logical rate-limited call could record up to 3 breaker failures; against a
# threshold of 5, ~2 concurrent/consecutive 429s tripped the breaker open for its full 30s
# cooldown, failing every subsequent request to the host instantly. 429 handling belongs to the
# per-host token bucket + Retry-After wait, not the breaker (which exists to detect a DOWN host).


def test_repeated_429s_never_trip_circuit_breaker() -> None:
    import anyio
    import httpx

    from ergon_tracker.exceptions import RateLimitError
    from ergon_tracker.http import AsyncFetcher

    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = AsyncFetcher(client=client, retries=1)

    async def main() -> None:
        async with fetcher:
            # Many more logical 429s than the breaker's threshold (5) -- every one must still
            # surface as a plain RateLimitError, never a "circuit open" FetchError.
            for _ in range(10):
                with pytest.raises(RateLimitError):
                    await fetcher.get_json("https://ratelimited.test/x")

    anyio.run(main)


def test_repeated_5xx_still_trips_circuit_breaker() -> None:
    # Contrast case: the breaker must still protect against a genuinely DOWN host (5xx errors),
    # so excluding 429s must not have disabled the breaker outright.
    import anyio
    import httpx

    from ergon_tracker.exceptions import FetchError, TransientHTTPError
    from ergon_tracker.http import AsyncFetcher

    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = AsyncFetcher(client=client, retries=1)

    async def main() -> None:
        async with fetcher:
            for _ in range(5):  # 5 logical failures == the breaker's default threshold
                with pytest.raises(TransientHTTPError):
                    await fetcher.get_json("https://down.test/x")
            # Breaker now open: the next call fails fast without hitting the transport at all.
            with pytest.raises(FetchError, match="circuit open"):
                await fetcher.get_json("https://down.test/x")

    anyio.run(main)
