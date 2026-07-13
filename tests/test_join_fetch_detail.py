"""Tier-3 detail fetcher: JoinProvider.fetch_detail (join.com = 43,228 postings, list-only).

Offline only — a FakeFetcher stands in for AsyncFetcher for the non-redirect cases; no live
network calls. Mirrors ``tests/test_workday_fetch_detail.py`` (the Workday Task-3 pattern),
including its non-raising discipline for a truthy non-dict payload shape.

Also covers the join-specific long-redirect-chain case: join.com auto-reposts evergreen jobs,
producing 22-23-hop redirect chains from a stale posting URL to its current live repost.
``AsyncFetcher``'s shared httpx client is built with ``max_redirects=30`` (see ``http.py``), so
the WHOLE chain is followed internally by httpx inside a single ``AsyncFetcher.request`` call —
one rate-limit token per posting, not one per hop. Those tests use a real ``AsyncFetcher`` wired
to an ``httpx.MockTransport`` so httpx's own redirect-following is exercised end to end, rather
than a hand-rolled hop-by-hop helper (join.py no longer has one)."""
from __future__ import annotations

import anyio
import httpx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.join import JoinProvider

_APPLY_URL = "https://join.com/companies/acme/jobs/123456"


class _FakeResponse:
    def __init__(
        self, *, status_code: int = 200, text: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


class _FakeFetcher:
    """Simulates ``AsyncFetcher.get_text`` for the non-redirect cases: one ``request()`` call
    returns a 200 (or raises), and ``get_text`` layers ``raise_for_status`` + ``.text`` on top —
    the same shape as the real ``AsyncFetcher.get_text``."""

    def __init__(self, text: str | None) -> None:
        self._text = text
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.calls.append(url)
        if self._text is None:
            raise RuntimeError("boom")
        return _FakeResponse(status_code=200, text=self._text)

    async def get_text(self, url: str, **kw: object) -> str:
        resp = await self.request("GET", url, **kw)
        resp.raise_for_status()
        return resp.text


def _next_data_html(job_json: str) -> str:
    return (
        "<html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"initialState":{"job":' + job_json + "}}}}"
        "</script>"
        "</body></html>"
    )


def _make_ref(apply_url: str | None, listing_url: str | None = None, id_: str = "1") -> DetailRef:
    return DetailRef(
        id=id_,
        source="join",
        token="acme",
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def test_join_fetch_detail_returns_schema_description() -> None:
    html = _next_data_html('{"schemaDescription":"<p>JD...</p>"}')
    fetcher = _FakeFetcher(html)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>JD...</p>"
    assert fetcher.calls == [_APPLY_URL]


def test_join_fetch_detail_falls_back_to_description_when_schema_empty() -> None:
    html = _next_data_html('{"schemaDescription":"","description":"Plain markdown JD text"}')
    fetcher = _FakeFetcher(html)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc == "Plain markdown JD text"


def test_join_fetch_detail_falls_back_to_description_when_schema_missing() -> None:
    html = _next_data_html('{"description":"Plain markdown JD text 2"}')
    fetcher = _FakeFetcher(html)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc == "Plain markdown JD text 2"


def test_join_fetch_detail_no_job_key_is_none() -> None:
    html = (
        "<html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"initialState":{"jobs":{"items":[]}}}}}'
        "</script>"
        "</body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_join_fetch_detail_no_next_data_script_is_none() -> None:
    fetcher = _FakeFetcher("<html><body>no next data here</body></html>")
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_join_fetch_detail_malformed_json_is_none() -> None:
    html = (
        "<html><head></head><body>"
        '<script id="__NEXT_DATA__" type="application/json">{not valid json</script>'
        "</body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_join_fetch_detail_non_dict_job_is_none() -> None:
    # ``job`` truthy but not a dict must not raise (the SmartRecruiters/Workday regression).
    html = _next_data_html('"oops"')
    fetcher = _FakeFetcher(html)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_join_fetch_detail_both_urls_none_is_none() -> None:
    fetcher = _FakeFetcher(_next_data_html('{"schemaDescription":"<p>JD...</p>"}'))
    ref = _make_ref(None, None)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_join_fetch_detail_non_join_urls_is_none() -> None:
    fetcher = _FakeFetcher(_next_data_html('{"schemaDescription":"<p>JD...</p>"}'))
    ref = _make_ref("https://example.com/not-a-join-url", "https://also-not-join.com/x")
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_join_fetch_detail_falls_back_to_listing_url() -> None:
    listing_url = "https://join.com/companies/acme/jobs/789"
    html = _next_data_html('{"schemaDescription":"<p>Fallback via listing_url</p>"}')
    fetcher = _FakeFetcher(html)
    ref = _make_ref(None, listing_url)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback via listing_url</p>"
    assert fetcher.calls == [listing_url]


def test_join_fetch_detail_get_text_failure_is_none() -> None:
    fetcher = _FakeFetcher(None)  # raises inside request()
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


# --- Long redirect chains: exercised via a real AsyncFetcher + httpx.MockTransport, so httpx's
# own client-level ``max_redirects`` (set to 30 in http.py) does the redirect-following --------


def _mock_transport(*, redirect_hops: int, final_html: str) -> httpx.MockTransport:
    """A transport that returns ``redirect_hops`` 302s (each pointing at the next hop), then a
    final 200 with ``final_html`` — simulating join's evergreen-repost redirect chain at the
    transport level, so httpx's real client-side redirect loop (not an application-level
    hop-by-hop helper) is what resolves it."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n = state["n"]
        state["n"] += 1
        if n < redirect_hops:
            return httpx.Response(302, headers={"location": f"{_APPLY_URL}?hop={n + 1}"})
        return httpx.Response(200, text=final_html)

    return httpx.MockTransport(handler)


def _fetcher_with_transport(transport: httpx.MockTransport) -> AsyncFetcher:
    client = httpx.AsyncClient(transport=transport, follow_redirects=True, max_redirects=30)
    return AsyncFetcher(client=client)


async def _fetch_counting_requests(fetcher: AsyncFetcher, ref: DetailRef) -> tuple[str | None, int]:
    """Run ``JoinProvider.fetch_detail`` while counting how many ``AsyncFetcher.request`` calls
    it makes (i.e. how many rate-limit tokens it spends) — must be exactly 1 regardless of how
    many redirect hops httpx follows underneath."""
    calls = 0
    orig_request = fetcher.request

    async def counting_request(*a: object, **kw: object) -> httpx.Response:
        nonlocal calls
        calls += 1
        return await orig_request(*a, **kw)  # type: ignore[arg-type]

    fetcher.request = counting_request  # type: ignore[method-assign]
    try:
        desc = await JoinProvider().fetch_detail(ref, fetcher)
    finally:
        await fetcher._client.aclose()
    return desc, calls


def test_join_fetch_detail_follows_long_redirect_chain_to_live_page() -> None:
    # join's evergreen-repost chains run 22-23 hops before landing on the live posting. The
    # shared client's max_redirects=30 (see http.py) must follow the whole chain in ONE
    # AsyncFetcher.request call.
    html = _next_data_html('{"schemaDescription":"<p>Live JD after 22 hops</p>"}')
    fetcher = _fetcher_with_transport(_mock_transport(redirect_hops=22, final_html=html))
    ref = _make_ref(_APPLY_URL)
    desc, calls = anyio.run(_fetch_counting_requests, fetcher, ref)
    assert desc == "<p>Live JD after 22 hops</p>"
    assert calls == 1  # one rate-limit token per posting, not one per hop


def test_join_fetch_detail_exactly_at_redirect_cap_succeeds() -> None:
    # 30 redirect hops == the client's max_redirects exactly -> still resolves (boundary check).
    html = _next_data_html('{"schemaDescription":"<p>Right at the cap</p>"}')
    fetcher = _fetcher_with_transport(_mock_transport(redirect_hops=30, final_html=html))
    ref = _make_ref(_APPLY_URL)
    desc, calls = anyio.run(_fetch_counting_requests, fetcher, ref)
    assert desc == "<p>Right at the cap</p>"
    assert calls == 1


def test_join_fetch_detail_exceeding_redirect_cap_is_none() -> None:
    # One hop past the client's max_redirects -> httpx.TooManyRedirects internally, caught by
    # fetch_detail's broad except, returns None (never raises out of fetch_detail).
    html = _next_data_html('{"schemaDescription":"<p>Unreachable</p>"}')
    fetcher = _fetcher_with_transport(_mock_transport(redirect_hops=31, final_html=html))
    ref = _make_ref(_APPLY_URL)
    desc, calls = anyio.run(_fetch_counting_requests, fetcher, ref)
    assert desc is None
    assert calls == 1


def test_base_fetch_detail_is_none() -> None:
    ref = _make_ref(None, None)
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher(None)))
    assert desc is None
