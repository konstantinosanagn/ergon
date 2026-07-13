"""Tier-3 detail fetcher: ICIMSProvider.fetch_detail.

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py`` (the Workday Task-3 pattern), including its non-raising
discipline for a truthy non-dict/malformed JSON-LD shape.

Verified endpoint (recon, 7/7 hosts incl. framebuster-protected; re-confirmed live against
``careers-winco.icims.com``): ``GET https://{host}/jobs/{id}/job?in_iframe=1`` — iCIMS accepts a
missing/garbage trailing slug, but ``in_iframe=1`` is REQUIRED (without it the page has no
``application/ld+json`` block at all). The detail page carries the JD in that JSON-LD
``JobPosting`` block."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.icims import ICIMSProvider


class _FakeFetcher:
    def __init__(self, html: object) -> None:
        self._html = html
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._html


def _page(description: str = "<p>JD...</p>") -> str:
    return (
        '<html><body><script type="application/ld+json">'
        '{"@type":"JobPosting","description":"' + description + '"}'
        "</script></body></html>"
    )


def test_icims_fetch_detail_returns_description_costco_shape() -> None:
    fetcher = _FakeFetcher(_page("<p>Real JD text for the costco posting...</p>"))
    ref = DetailRef(
        id="1",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the costco posting...</p>"
    assert fetcher.calls == ["https://careers-costco.icims.com/jobs/12345/job?in_iframe=1"]


def test_icims_fetch_detail_returns_description_dollargeneral_shape() -> None:
    fetcher = _FakeFetcher(_page("<p>Real JD text for the dollar general posting...</p>"))
    ref = DetailRef(
        id="2",
        source="icims",
        token=None,
        apply_url="https://retail-dollargeneral.icims.com/jobs/98765/store-manager/job",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the dollar general posting...</p>"
    assert fetcher.calls == ["https://retail-dollargeneral.icims.com/jobs/98765/job?in_iframe=1"]


def test_icims_fetch_detail_falls_back_to_listing_url() -> None:
    fetcher = _FakeFetcher(_page("<p>Fallback JD via listing_url...</p>"))
    ref = DetailRef(
        id="3",
        source="icims",
        token=None,
        apply_url=None,
        listing_url="https://careers-costco.icims.com/jobs/55555/warehouse-associate/login",
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD via listing_url...</p>"
    assert fetcher.calls == ["https://careers-costco.icims.com/jobs/55555/job?in_iframe=1"]


def test_icims_fetch_detail_no_jsonld_is_none() -> None:
    fetcher = _FakeFetcher("<html><body>no ld+json here</body></html>")
    ref = DetailRef(
        id="4",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_icims_fetch_detail_no_job_posting_type_is_none() -> None:
    html = (
        '<html><body><script type="application/ld+json">'
        '{"@type":"Organization","name":"Costco"}'
        "</script></body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="5",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_icims_fetch_detail_empty_description_is_none() -> None:
    fetcher = _FakeFetcher(_page("   "))
    ref = DetailRef(
        id="6",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_icims_fetch_detail_non_dict_jsonld_is_none() -> None:
    # A JSON-LD block that parses but isn't a dict (or list-of-dicts) shape must not raise.
    html = (
        '<html><body><script type="application/ld+json">'
        '"just a string"'
        "</script></body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="7",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_icims_fetch_detail_malformed_jsonld_is_none() -> None:
    # Invalid JSON in the ld+json block must not raise.
    html = (
        '<html><body><script type="application/ld+json">'
        '{"@type": "JobPosting", "description": }'
        "</script></body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="8",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_icims_fetch_detail_non_string_description_is_none() -> None:
    html = (
        '<html><body><script type="application/ld+json">'
        '{"@type":"JobPosting","description":{"nested":"not-a-string"}}'
        "</script></body></html>"
    )
    fetcher = _FakeFetcher(html)
    ref = DetailRef(
        id="9",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_icims_fetch_detail_non_icims_apply_url_is_none() -> None:
    # No iCIMS host suffix and no iCIMS-shaped path (no "/jobs/search", "/careers-home/jobs", or
    # "/jobs/{id}/") -> matches() rejects it -> None without ever calling the fetcher.
    fetcher = _FakeFetcher(_page())
    ref = DetailRef(
        id="10",
        source="icims",
        token=None,
        apply_url="https://example.com/careers/openings/12345",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_icims_fetch_detail_unparseable_no_id_is_none() -> None:
    fetcher = _FakeFetcher(_page())
    ref = DetailRef(
        id="11",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/search?pr=0",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_icims_fetch_detail_no_urls_is_none() -> None:
    fetcher = _FakeFetcher(_page())
    ref = DetailRef(
        id="12", source="icims", token=None, apply_url=None, listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_icims_fetch_detail_fetcher_exception_is_none() -> None:
    class _RaisingFetcher:
        async def get_text(self, url: str, **kw: object) -> str:
            raise TimeoutError("boom")

    ref = DetailRef(
        id="13",
        source="icims",
        token=None,
        apply_url="https://careers-costco.icims.com/jobs/12345/some-title/login",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: ICIMSProvider().fetch_detail(ref, _RaisingFetcher()))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher(_page())))
    assert desc is None
