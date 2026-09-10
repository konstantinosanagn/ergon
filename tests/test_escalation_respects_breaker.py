"""An escalation ladder must not out-run the circuit breaker.

schemaorg and apicapture both fall back to heavier transports when a host blocks them — an HTTP/1.1
client, then curl_cffi with Chrome TLS impersonation. Both bypass the shared ``AsyncFetcher``
client, so neither was rate-limited, breaker-governed or budget-accounted.

Worse, both escalated on a BARE exception, and the breaker raises one. So an open circuit — the
signal meaning "stop touching this host" — was itself the trigger for switching to a transport the
host cannot rate-limit us on. The harder a host pushed back, the harder we hit it.

``CircuitOpenError`` now distinguishes the two cases: a 403 means "this transport is blocked, try a
heavier one"; an open circuit means "stop". These tests pin that both providers re-raise the
second, still escalate on the first, and route the out-of-band lanes through ``host_slot``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ergon.exceptions import CircuitOpenError, FetchError
from ergon.http import AsyncFetcher


def test_circuit_open_is_distinguishable_and_still_a_fetch_error() -> None:
    """Subclassing keeps every existing `except FetchError` handler working."""
    assert issubclass(CircuitOpenError, FetchError)


async def test_breaker_raises_the_specific_type() -> None:
    async with AsyncFetcher(per_host_rate=100) as f:
        for _ in range(5):
            with pytest.raises(RuntimeError):
                async with f.host_slot("https://tripped.test/x"):
                    raise RuntimeError("boom")
        with pytest.raises(CircuitOpenError):
            async with f.host_slot("https://tripped.test/x"):
                pass  # pragma: no cover


# --- schemaorg -----------------------------------------------------------------------------


class _RaisingFetcher:
    """Stands in for AsyncFetcher: get_text always raises whatever it was given."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.slots: list[str] = []

    async def get_text(self, url: str, **kw: Any) -> str:
        raise self._exc

    def host_slot(self, url: str):  # noqa: ANN202 - test double
        from contextlib import asynccontextmanager

        slots = self.slots

        @asynccontextmanager
        async def _cm():  # noqa: ANN202
            slots.append(url)
            yield

        return _cm()


async def test_schemaorg_does_not_escalate_past_an_open_circuit() -> None:
    """Tier 2 and 3 must never be attempted; the breaker verdict propagates untouched."""
    from ergon.providers.schemaorg import _DualFetch

    df = _DualFetch(_RaisingFetcher(CircuitOpenError("circuit open for x (cooling down)")))  # type: ignore[arg-type]
    with pytest.raises(CircuitOpenError):
        await df.get_text("https://blocked.test/jobs")
    # If either fallback had run it would have taken a host_slot first.
    assert df._f.slots == [], "an open circuit must not reach the out-of-band lanes"  # type: ignore[attr-defined]


def _blocked_resp(url: str):  # noqa: ANN202
    req = httpx.Request("GET", url)
    resp = httpx.Response(403, request=req)

    class _R:
        text = ""
        status_code = 403

        @staticmethod
        def raise_for_status() -> None:
            raise httpx.HTTPStatusError("403", request=req, response=resp)

    return _R()


async def test_schemaorg_still_escalates_on_a_real_block() -> None:
    """The protection must survive: a 403 is still a reason to try a heavier transport."""
    from ergon.providers.schemaorg import _DualFetch

    url = "https://blocked.test/jobs"
    req = httpx.Request("GET", url)
    blocked = httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
    df = _DualFetch(_RaisingFetcher(blocked))  # type: ignore[arg-type]

    class _H1:
        async def get(self, u: str, **kw: Any) -> Any:
            return _blocked_resp(u)  # tier 2 is walled too, so tier 3 must run

    class _CC:
        text = "<html>tier3</html>"
        status_code = 200

        async def get(self, u: str, **kw: Any) -> Any:
            return self

        @staticmethod
        def raise_for_status() -> None:
            return None

    df._h1 = _H1()
    df._cc = _CC()
    assert await df.get_text(url) == "<html>tier3</html>"
    # One slot per out-of-band tier: neither may reach the host outside the per-host guards.
    assert df._f.slots == [url, url], (  # type: ignore[attr-defined]
        f"expected both escalation tiers to take a host_slot, got {df._f.slots!r}"  # type: ignore[attr-defined]
    )


# --- apicapture ----------------------------------------------------------------------------


async def test_apicapture_does_not_escalate_past_an_open_circuit(monkeypatch) -> None:
    """The worst shape of this bug: the breaker firing triggering the TLS bypass."""
    from ergon.providers import apicapture

    escalated: list[str] = []

    async def _never(req: Any, fetcher: Any) -> Any:
        escalated.append(req.url)
        raise AssertionError("escalated past an open circuit")

    monkeypatch.setattr(apicapture, "_tls_request", _never)

    class _F:
        async def request(self, method: str, url: str, **kw: Any) -> Any:
            raise CircuitOpenError("circuit open for walled.test (cooling down)")

    with pytest.raises(CircuitOpenError):
        await apicapture.ApiCaptureProvider._detail_send(_F(), _req())  # type: ignore[arg-type]
    assert escalated == []


async def test_apicapture_still_escalates_on_a_transport_error(monkeypatch) -> None:
    """The protection must survive: a real block still reaches the heavier transport."""
    from ergon.providers import apicapture

    escalated: list[str] = []

    async def _record(req: Any, fetcher: Any) -> Any:
        escalated.append(req.url)
        return apicapture._DetailResp(status_code=200, text="{}", url=req.url, headers={})

    monkeypatch.setattr(apicapture, "_tls_request", _record)

    class _F:
        async def request(self, method: str, url: str, **kw: Any) -> Any:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    resp = await apicapture.ApiCaptureProvider._detail_send(_F(), _req())  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert escalated == ["https://walled.test/api/job/1"], "a real block must still escalate"


async def test_apicapture_tls_lane_is_rate_limited_and_breaker_governed() -> None:
    """The lane bypasses the shared client, so its guards must come from host_slot."""
    from ergon.providers import apicapture

    class _Blocked:
        status_code = 403
        text = ""
        url = "https://walled.test/api/job/1"
        headers: dict[str, str] = {}

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, url: str, **kw: Any) -> Any:
            self.calls += 1
            return _Blocked()

    session = _Session()
    async with AsyncFetcher(per_host_rate=100) as f:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(apicapture, "_tls_session", lambda: session)
            for _ in range(6):
                try:
                    resp = await apicapture._tls_request(_req(), f)
                except CircuitOpenError:
                    break
                assert resp.status_code == 403, "the wall response must still reach the caller"
            else:  # pragma: no cover
                pytest.fail("a persistently walled host never tripped the breaker")
    assert session.calls < 6, "the breaker must stop the lane before it keeps hammering the wall"


def _req():  # noqa: ANN202
    from ergon.providers.apicapture import _DetailReq

    return _DetailReq(
        method="GET",
        url="https://walled.test/api/job/1",
        tier="tls",
        follow_redirects=True,
        headers=None,
        json_body=None,
    )
