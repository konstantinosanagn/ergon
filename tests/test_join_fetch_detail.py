"""Tier-3 detail fetcher: JoinProvider.fetch_detail (join.com = 43,228 postings, list-only).

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py`` (the Workday Task-3 pattern), including its
non-raising discipline for a truthy non-dict payload shape."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.join import JoinProvider

_APPLY_URL = "https://join.com/companies/acme/jobs/123456"


class _FakeFetcher:
    def __init__(self, text: str | None) -> None:
        self._text = text
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        if self._text is None:
            raise RuntimeError("boom")
        return self._text


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
    fetcher = _FakeFetcher(None)  # raises inside get_text
    ref = _make_ref(_APPLY_URL)
    desc = anyio.run(lambda: JoinProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = _make_ref(None, None)
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher(None)))
    assert desc is None
