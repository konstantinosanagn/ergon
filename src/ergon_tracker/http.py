"""Async HTTP fetching infrastructure shared by all providers.

``AsyncFetcher`` provides bounded global concurrency, per-host token-bucket rate limiting,
retries that honor ``Retry-After``, and a lightweight per-host circuit breaker. Providers
should never construct their own ``httpx`` client — they receive an ``AsyncFetcher`` and call
``get_json`` / ``post_json`` / ``get_text``.
"""

from __future__ import annotations

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
# searches.)
_PER_TENANT_HOSTS = ("myworkdayjobs.com",)
# Shared backends with stricter limits than the default — (max_rate, period_seconds).
# These always win over the constructor's per_host_rate.
_DOMAIN_RATE_OVERRIDES: dict[str, tuple[float, float]] = {
    "recruitee.com": (2.0, 1.0),
    "personio.de": (3.0, 1.0),
}
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
        timeout: float = 25.0,
        retries: int = 3,
        cache: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._limiter = anyio.CapacityLimiter(concurrency)
        self._host_limiters: dict[str, AsyncLimiter] = {}
        self._per_host_rate = per_host_rate
        self._per_host_period = per_host_period
        self._retries = retries
        self._breakers: dict[str, _CircuitBreaker] = defaultdict(_CircuitBreaker)
        self._owns_client = client is None
        self._client = client or self._build_client(timeout=timeout, cache=cache)

    @staticmethod
    def _build_client(*, timeout: float, cache: bool) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "headers": DEFAULT_HEADERS,
            "follow_redirects": True,
            "http2": True,
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

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).netloc
        key = _rate_key(host)  # registrable domain (shared backends throttle together)
        breaker = self._breakers[key]
        breaker.check(key)
        async with self._limiter, self._host_limiter(key):
            return await self._request_with_retries(method, url, host, breaker, **kwargs)

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
                    breaker.record_failure()
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
