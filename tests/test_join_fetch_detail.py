"""Tier-3 detail fetcher: JoinProvider.fetch_detail (join.com = 43,228 postings, list-only).

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py`` (the Workday Task-3 pattern), including its
non-raising discipline for a truthy non-dict payload shape.

Also covers the join-specific redirect cap (``_get_text_following_redirects`` /
``_MAX_DETAIL_REDIRECTS`` in ``providers/join.py``): join.com auto-reposts evergreen jobs,
producing 22-23-hop redirect chains from a stale posting URL to its current live repost.
``AsyncFetcher``'s shared httpx client follows redirects with the default ``max_redirects=20``
and would raise ``TooManyRedirects`` short of the live page, so join follows redirects itself,
hop by hop via ``fetcher.request(..., follow_redirects=False)``, up to a 30-hop cap. The
``_FakeFetcher`` below simulates that hop-by-hop protocol: each ``request()`` call returns a
302 with a ``Location`` header until ``redirect_hops`` is exhausted, then a final 200 page."""
from __future__ import annotations

import anyio
import httpx

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

    @property
    def has_redirect_location(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


class _FakeFetcher:
    """Simulates ``AsyncFetcher.request`` for the manual hop-by-hop redirect follow.

    Each call returns a 302 (with a ``Location`` header pointing at the next hop) until
    ``redirect_hops`` calls have been made, then a final 200 whose body is ``text``.
    ``redirect_hops=0`` (the default) means the very first call already returns the final page —
    the shape every pre-existing (non-redirect) test in this file relies on."""

    def __init__(self, text: str | None, *, redirect_hops: int = 0) -> None:
        self._text = text
        self._redirect_hops = redirect_hops
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.calls.append(url)
        if self._text is None:
            raise RuntimeError("boom")
        hop = len(self.calls) - 1
        if hop < self._redirect_hops:
            return _FakeResponse(
                status_code=302, headers={"location": f"{url}?hop={hop + 1}"}
            )
        return _FakeResponse(status_code=200, text=self._text)


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


def test_join_fetch_detail_follows_long_redirect_chain_to_live_page() -> None:
    # join's evergreen-repost chains run 22-23 hops before landing on the live posting.
    # httpx's shared-client default max_redirects=20 would raise TooManyRedirects one or two
    # hops short of this; join's own redirect follow (cap=30) must reach the live page.
    html = _next_data_html('{"schemaDescription":"<p>Live JD after 22 hops</p>"}')
    fetcher = _FakeFetcher(html, redirect_hops=22)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Live JD after 22 hops</p>"
    assert len(fetcher.calls) == 23  # 22 redirect hops + the final 200


def test_join_fetch_detail_exactly_at_redirect_cap_succeeds() -> None:
    # 30 hops == _MAX_DETAIL_REDIRECTS exactly -> still resolves (boundary check).
    html = _next_data_html('{"schemaDescription":"<p>Right at the cap</p>"}')
    fetcher = _FakeFetcher(html, redirect_hops=30)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Right at the cap</p>"
    assert len(fetcher.calls) == 31


def test_join_fetch_detail_exceeding_redirect_cap_is_none() -> None:
    # One hop past _MAX_DETAIL_REDIRECTS -> TooManyRedirects internally, caught, returns None
    # (never raises out of fetch_detail).
    html = _next_data_html('{"schemaDescription":"<p>Unreachable</p>"}')
    fetcher = _FakeFetcher(html, redirect_hops=31)
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = _make_ref(None, None)
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher(None)))
    assert desc is None
