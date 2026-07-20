"""Async HTTP fetching infrastructure shared by all providers.

``AsyncFetcher`` provides bounded global concurrency, per-host token-bucket rate limiting,
retries that honor ``Retry-After``, and a lightweight per-host circuit breaker. Providers
should never construct their own ``httpx`` client — they receive an ``AsyncFetcher`` and call
``get_json`` / ``post_json`` / ``get_text``.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import urlsplit

import anyio
import httpx
import stamina
from aiolimiter import AsyncLimiter

from .exceptions import FetchError, RateLimitError, TransientHTTPError

__all__ = ["AsyncFetcher", "ConditionalResult", "DEFAULT_HEADERS"]

# Per-host (per-``_rate_key``) IN-FLIGHT concurrency cap -- independent of, and layered UNDER,
# the global ``AsyncFetcher._limiter``. Confirmed root cause (2026-07 Workable cascade): with a
# high global concurrency (e.g. 50) and a single shared host like apply.workable.com pinned to a
# strict 3 req/s token bucket, every coroutine piles onto that one host's bucket queue at once;
# the pileup itself (not the bucket) is what trips the per-host circuit breaker and cascades to a
# 100%-fail run (conc=6 -> 20% fail, conc=40 -> 100% fail, single 120-long cascade). This cap
# bounds how many requests to one host are ever in flight (queued on the rate bucket + sending)
# regardless of how many coroutines target it, WITHOUT slowing a host whose token bucket is
# already the binding constraint (e.g. SmartRecruiters at 3/s stays 3/s: 8 concurrent waiters
# draining a 3/s bucket is still bound by the bucket, not the semaphore).
_DEFAULT_PER_HOST_CONCURRENCY = int(os.environ.get("ERGON_PER_HOST_CONCURRENCY", "8"))


@dataclass(frozen=True)
class ConditionalResult:
    """Outcome of a conditional GET (If-None-Match / If-Modified-Since).

    ``not_modified`` True means the server returned 304 and ``body`` is None (nothing
    re-downloaded — the caller carries forward its cached data). Otherwise ``body`` holds the
    fresh bytes and ``etag``/``last_modified`` are the new validators to persist for next time.
    """

    not_modified: bool
    status_code: int
    etag: str | None = None
    last_modified: str | None = None
    body: bytes | None = None


DEFAULT_HEADERS = {
    "User-Agent": (
        "ergon_tracker/0.1 (+https://github.com/kanagn/ergon_tracker) Mozilla/5.0 (compatible; ergon_tracker bot)"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

_RETRYABLE_STATUS = {500, 502, 503, 504}

# Hosts whose subdomains are INDEPENDENT backends -> rate-limit per full host, not per domain.
# (Workday tenants live in separate data centers; collapsing them would serialize multi-tenant
# searches.) Oracle Fusion/ORC and iCIMS are the same shape: each tenant is a separate customer's
# careers site on separate infra, so collapsing to the registrable domain creates a false
# bottleneck (e.g. Oracle's 109 tenants all sharing one ``oraclecloud.com`` bucket) instead of
# unblocking real parallel draining across tenants.
# Eightfold is the same shape: every customer is served from ``{tenant}.eightfold.ai`` (list AND
# ``/api/apply/v2/jobs/{id}`` detail), so collapsing to ``eightfold.ai`` made a single 429 from ONE
# tenant trip the shared circuit breaker for ALL of them — measured 72% detail-fetch failure across
# eightfold (morganstanley/netflix/…) in the drain. Per-host keying gives each tenant its own
# bucket + breaker, mirroring the workday/oracle/icims carve-out.
_PER_TENANT_HOSTS = ("myworkdayjobs.com", "oraclecloud.com", "icims.com", "eightfold.ai")
# Shared backends with stricter limits than the default — (max_rate, period_seconds).
# These always win over the constructor's per_host_rate. The workable/bamboohr/smartrecruiters
# caps were added after a clustered crawl window threw a 2,181x-429 storm against them
# (build-2026-06-21-18); they are high-tenant shared backends that don't tolerate a sustained
# default rate. Interleaving (build_index._interleave_by_ats) spreads the load; these are the
# belt-and-suspenders per-backend ceilings.
_DOMAIN_RATE_OVERRIDES: dict[str, tuple[float, float]] = {
    "recruitee.com": (2.0, 1.0),
    "personio.de": (3.0, 1.0),
    "workable.com": (3.0, 1.0),  # empirically throttle-bound: 429-storms at the 5/s default
    "bamboohr.com": (3.0, 1.0),
    "smartrecruiters.com": (3.0, 1.0),
    "applicantpro.com": (3.0, 1.0),  # small-tenant ATS; keep the public list endpoint polite
    "adp.com": (1.0, 6.0),  # ADP WFN soft-blocks (404/503) on bursts; ~1 req/6s is the safe rate
}


# DRAIN-ONLY per-host rate raises. Each default cap was set for a *sustained LIST-crawl storm*, but
# the per-posting/board DETAIL endpoint is a different backend that live-probes far higher (SR 10/s
# sustained; workable 8/s sustained across 60 boards, both 0x 429). The cap is domain-wide (shared
# with the daily crawl), so we DON'T raise it globally: the drain workflow -- which shares
# build-index's concurrency group and thus NEVER runs while the crawl does -- sets these env vars,
# and only then does the cap rise for that process. The crawl process never sets them -> stays at the
# conservative default -> zero storm-risk regression.
# Every single-host detail endpoint here was capacity-probed with escalating SUSTAINED bursts to the
# actual 429 knee (not the earlier stop-on-first-429 floor): SR clean to 76/s, rippling to 49/s,
# bamboohr to 30/s -- ALL flat-latency, no throttle. Their detail backend is a different (CDN-fronted)
# origin than the LIST-crawl that set the tiny 3-5/s caps. So the drain caps are set aggressively
# (well within the probed-clean range, with the graceful-429 backoff -- Retry-After honored, 429s
# excluded from the breaker -- as the safety net). join stays modest: it's latency-bound at ~7/s
# actual by its 22-hop redirect chains, so a higher token cap changes nothing. Each value maps to
# one-or-more registrable domains (ukg spans ultipro.com + ukg.net).
_DRAIN_RATE_ENV: dict[str, tuple[str, ...]] = {
    "ERGON_SR_DETAIL_RATE": ("smartrecruiters.com",),
    "ERGON_WORKABLE_DETAIL_RATE": ("workable.com",),
    "ERGON_RIPPLING_DETAIL_RATE": ("rippling.com",),
    "ERGON_JOIN_DETAIL_RATE": ("join.com",),
    "ERGON_BAMBOOHR_DETAIL_RATE": ("bamboohr.com",),
    "ERGON_JOBVITE_DETAIL_RATE": ("jobvite.com",),
    "ERGON_UKG_DETAIL_RATE": ("ultipro.com", "ukg.net"),
}


def _apply_drain_rate_overrides() -> None:
    for env, domains in _DRAIN_RATE_ENV.items():
        raw = os.environ.get(env)
        if not raw:
            continue
        try:
            rate = float(raw)
        except ValueError:
            continue
        if rate > 0:
            for domain in domains:
                _DOMAIN_RATE_OVERRIDES[domain] = (rate, 1.0)


_apply_drain_rate_overrides()
# Two-level public suffixes, so the registrable domain is computed correctly.
_TWO_LEVEL_TLDS = {
    "co.uk",
    "org.uk",
    "ac.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.nz",
    "co.jp",
    "co.in",
    "com.br",
    "com.mx",
    "com.sg",
    "com.hk",
    "co.za",
    "com.tr",
    "co.il",
    "com.cn",
}


def _rate_key(host: str) -> str:
    """Key for per-host rate limiting + circuit breaking.

    Collapses subdomains to the registrable domain so shared backends (every
    ``*.recruitee.com`` / ``*.jobs.personio.de``) throttle together rather than each subdomain
    getting its own quota and hammering the shared backend into 429s. Per-tenant hosts
    (Workday) stay keyed on the full host.
    """
    host = host.split("@")[-1].split(":")[0].lower()
    if not host:
        return host
    if any(host == h or host.endswith("." + h) for h in _PER_TENANT_HOSTS):
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in _TWO_LEVEL_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    delta = dt.timestamp() - time.time()
    return max(0.0, delta)


class _CircuitBreaker:
    """Trips open after ``threshold`` consecutive failures; cools down for ``cooldown`` s."""

    def __init__(self, threshold: int = 5, cooldown: float = 30.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0

    def check(self, host: str) -> None:
        if self._open_until and time.monotonic() < self._open_until:
            raise FetchError(f"circuit open for {host} (cooling down)")

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = time.monotonic() + self._cooldown


class AsyncFetcher:
    def __init__(
        self,
        *,
        concurrency: int = 16,
        per_host_rate: int = 5,
        per_host_period: float = 1.0,
        per_host_concurrency: int = _DEFAULT_PER_HOST_CONCURRENCY,
        timeout: float = 25.0,
        retries: int = 3,
        cache: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._limiter = anyio.CapacityLimiter(concurrency)
        self._host_limiters: dict[str, AsyncLimiter] = {}
        self._host_concurrency_limiters: dict[str, anyio.CapacityLimiter] = {}
        # Per-host (per-``_rate_key``) wall-clock accounting for the crawl deadline-box. These are
        # bookkeeping only -- they NEVER pace or block a request (that stays the job of the token
        # bucket + circuit breaker). They let the crawl controller ask ``is_over_budget(host, ...)``
        # and stop DISPATCHING new boards to a host that has blown its time budget (e.g. join.com,
        # whose 5-jobs/page pagination makes it the slowest slice of the run); already-issued
        # requests are unaffected and untouched boards simply stay un-crawled this run.
        self._host_first_seen: dict[str, float] = {}
        self._host_request_count: dict[str, int] = defaultdict(int)
        self._host_busy_seconds: dict[str, float] = defaultdict(float)
        self._per_host_rate = per_host_rate
        self._per_host_period = per_host_period
        self._per_host_concurrency = per_host_concurrency
        self._retries = retries
        self._breakers: dict[str, _CircuitBreaker] = defaultdict(_CircuitBreaker)
        self._owns_client = client is None
        self._client = client or self._build_client(
            timeout=timeout, cache=cache, concurrency=concurrency
        )

    @staticmethod
    def _build_client(*, timeout: float, cache: bool, concurrency: int = 16) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "headers": DEFAULT_HEADERS,
            "follow_redirects": True,
            # httpx's default is 20 -- too low for join.com's evergreen-repost redirect chains
            # (live-observed 22-23 hops from a stale posting URL to its current live repost; see
            # providers/join.py). Raised to 30 so the WHOLE chain is followed internally by one
            # `_client.request(...)` call -- i.e. ONE token against the per-host rate limiter in
            # `AsyncFetcher.request` (host bucket acquired once per call, not once per hop) --
            # rather than providers hand-rolling hop-by-hop redirect following that burns one
            # rate-limit token per hop.
            "max_redirects": 30,
            "http2": True,
            # The connection pool must never starve the global concurrency limiter: keep at least
            # `concurrency` live connections available (httpx defaults to 100, which silently caps a
            # higher crawl concurrency). HTTP/2 multiplexing means this rarely opens that many sockets.
            "limits": httpx.Limits(
                max_connections=concurrency + 32,
                max_keepalive_connections=concurrency,
            ),
        }
        if cache:
            try:
                import hishel

                return cast(httpx.AsyncClient, hishel.AsyncCacheClient(**kwargs))  # type: ignore[attr-defined]
            except ImportError:  # pragma: no cover - hishel is a core dep, defensive only
                pass
        return httpx.AsyncClient(**kwargs)

    def _host_limiter(self, key: str) -> AsyncLimiter:
        limiter = self._host_limiters.get(key)
        if limiter is None:
            rate, period = _DOMAIN_RATE_OVERRIDES.get(
                key, (self._per_host_rate, self._per_host_period)
            )
            limiter = AsyncLimiter(rate, period)
            self._host_limiters[key] = limiter
        return limiter

    def _host_concurrency_limiter(self, key: str) -> anyio.CapacityLimiter:
        """Per-host in-flight cap (see :data:`_DEFAULT_PER_HOST_CONCURRENCY`). Lazily created,
        same synchronous get-or-create shape as :meth:`_host_limiter` -- safe without a guard
        lock because there is no ``await`` between the dict lookup and the assignment, so no
        other coroutine can interleave and hand out a second limiter object for the same key."""
        limiter = self._host_concurrency_limiters.get(key)
        if limiter is None:
            limiter = anyio.CapacityLimiter(self._per_host_concurrency)
            self._host_concurrency_limiters[key] = limiter
        return limiter

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).netloc
        key = _rate_key(host)  # registrable domain (shared backends throttle together)
        breaker = self._breakers[key]
        breaker.check(key)
        # Deadline-box accounting: stamp the first time we touched this host (so the wall-clock
        # budget covers the ENTIRE span the host was in play, including queue/rate waits) and count
        # the attempt. ``started`` also feeds cumulative busy-seconds below.
        started = time.monotonic()
        self._host_first_seen.setdefault(key, started)
        self._host_request_count[key] += 1
        # Global limiter (outer) -> per-host in-flight cap -> per-host rate wait + send. The
        # concurrency cap sits INSIDE the global limiter so it only ever bounds pileup onto a
        # single host, never overall throughput; it sits AROUND the rate wait so a host whose
        # token bucket is already the binding constraint (e.g. workable.com at 3/s) is unaffected
        # -- the bucket, not this semaphore, remains what paces actual sends.
        try:
            async with self._limiter, self._host_concurrency_limiter(key), self._host_limiter(key):
                return await self._request_with_retries(method, url, host, breaker, **kwargs)
        finally:
            self._host_busy_seconds[key] += time.monotonic() - started

    def is_over_budget(self, host: str, budget_seconds: float) -> bool:
        """True once ``host`` has been in play for at least ``budget_seconds`` of wall-clock.

        The deadline-box the crawl checks BEFORE dispatching another board to a host: measured
        from the first request issued to that host (``_rate_key``-collapsed, so all shared-backend
        subdomains share one budget). A host never yet requested is never over budget. Cheap and
        pure -- it only reads a timestamp, it does not throttle or cancel anything in flight.
        """
        first = self._host_first_seen.get(_rate_key(urlsplit(host).netloc or host))
        if first is None:
            return False
        return (time.monotonic() - first) >= budget_seconds

    def host_wall_elapsed(self, host: str) -> float:
        """Wall-clock seconds since the first request to ``host`` (0.0 if never requested)."""
        first = self._host_first_seen.get(_rate_key(urlsplit(host).netloc or host))
        return 0.0 if first is None else time.monotonic() - first

    def host_request_count(self, host: str) -> int:
        """Number of requests issued to ``host`` this run (``_rate_key``-collapsed)."""
        return self._host_request_count.get(_rate_key(urlsplit(host).netloc or host), 0)

    def host_busy_seconds(self, host: str) -> float:
        """Cumulative in-request seconds spent on ``host`` (sums overlapping requests)."""
        return self._host_busy_seconds.get(_rate_key(urlsplit(host).netloc or host), 0.0)

    def slowest_hosts(self, n: int = 3) -> list[dict[str, Any]]:
        """The ``n`` hosts that have been in play LONGEST this run -- the crawl's slow tail.

        Pure read over the same per-host accounting the deadline-box uses (``_host_first_seen`` /
        busy-seconds / request count); it never throttles or blocks anything. Ranked by wall-clock
        time in play (``wall_s``), which is what actually holds the crawl's tail open. Each entry is
        ``{host, wall_s, busy_s, requests}`` -- exactly the shape the progress heartbeat serialises
        so a watcher can see WHICH host is the long pole. Empty until the first request is issued.
        """
        now = time.monotonic()
        rows = [
            {
                "host": host,
                "wall_s": round(now - first, 1),
                "busy_s": round(self._host_busy_seconds.get(host, 0.0), 1),
                "requests": self._host_request_count.get(host, 0),
            }
            for host, first in self._host_first_seen.items()
        ]
        rows.sort(key=lambda r: cast(float, r["wall_s"]), reverse=True)
        return rows[: max(0, n)]

    def reset_host_accounting(self) -> None:
        """Clear all per-host wall-clock/count accounting (start a fresh deadline-box run)."""
        self._host_first_seen.clear()
        self._host_request_count.clear()
        self._host_busy_seconds.clear()

    @property
    def global_concurrency(self) -> float:
        """The global in-flight cap (``CapacityLimiter`` total tokens).

        Configurable via the constructor's ``concurrency`` arg -- the crawl raises it to 150-200
        to keep fast hosts busy while slow ones drain. Raising it never raises the per-host
        in-flight cap (:data:`_DEFAULT_PER_HOST_CONCURRENCY`), which is layered underneath.
        """
        return self._limiter.total_tokens

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        host: str,
        breaker: _CircuitBreaker,
        **kwargs: Any,
    ) -> httpx.Response:
        retry_on = (httpx.TransportError, RateLimitError, TransientHTTPError)
        async for attempt in stamina.retry_context(
            on=retry_on, attempts=self._retries, wait_initial=0.5, wait_max=10.0
        ):
            with attempt:
                try:
                    resp = await self._client.request(method, url, **kwargs)
                except httpx.TransportError:
                    breaker.record_failure()
                    raise

                if resp.status_code == 429:
                    # Deliberately NOT a breaker failure: 429 means "throttled", not "down", and
                    # the per-host token bucket + this Retry-After wait are already the
                    # mechanism that paces requests to a rate-limited host. Confirmed root cause
                    # (2026-07 Workable cascade): with retries=3, counting every 429 toward the
                    # breaker meant ONE logical rate-limited call recorded up to 3 breaker
                    # failures -- against a threshold of 5, just ~2 concurrent/consecutive 429s
                    # (6 recorded failures) tripped the breaker OPEN for the full 30s cooldown,
                    # failing every subsequent request to the host instantly and cascading a
                    # transient rate-limit window into a total outage. The breaker still exists
                    # to detect a genuinely DOWN host (5xx / transport errors below).
                    ra = _retry_after_seconds(resp)
                    if ra:
                        await anyio.sleep(min(ra, 30.0))
                    raise RateLimitError(f"429 Too Many Requests from {host}", retry_after=ra)

                if resp.status_code in _RETRYABLE_STATUS:
                    breaker.record_failure()
                    raise TransientHTTPError(f"{resp.status_code} from {host}")

                breaker.record_success()
                return resp

        raise FetchError(f"exhausted retries for {url}")  # pragma: no cover - safety net

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        resp = await self.request("POST", url, json=json, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        resp = await self.request("GET", url, **kwargs)
        resp.raise_for_status()
        return resp.text

    async def conditional_get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        **kwargs: Any,
    ) -> ConditionalResult:
        """GET ``url`` with validators; return a 304 (no body) or 200 (body + new validators).

        The cross-build crawl efficiency primitive: pass the validators stored from the last
        crawl; a 304 means the board is unchanged so nothing is re-downloaded. Unlike
        ``get_json``/``get_text`` this never raises on 304 and never parses an empty body.
        """
        headers = dict(kwargs.pop("headers", None) or {})
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = await self.request("GET", url, headers=headers, **kwargs)
        not_modified = resp.status_code == 304
        if not not_modified:
            resp.raise_for_status()
        return ConditionalResult(
            not_modified=not_modified,
            status_code=resp.status_code,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
            body=None if not_modified else resp.content,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
