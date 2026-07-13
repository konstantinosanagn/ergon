"""Tier-3 detail fetcher: RipplingProvider.fetch_detail.

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``'s non-raising discipline: any unparseable ref, fetch
failure, non-JSON payload, or shape mismatch (including a truthy non-dict payload) returns
``None``, never an exception."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.rippling import RipplingProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._p


def test_rippling_fetch_detail_concatenates_description_dict_shape_1() -> None:
    payload = {"description": {"company": "<p>A</p>", "role": "<p>B</p>"}}
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/11fs-group-ltd/jobs/3c36-uuid-1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>A</p>\n<p>B</p>"
    assert fetcher.calls == [
        "https://api.rippling.com/platform/api/ats/v1/board/11fs-group-ltd/jobs/3c36-uuid-1"
    ]


def test_rippling_fetch_detail_concatenates_description_dict_shape_2() -> None:
    payload = {"description": {"company": "<p>C</p>", "requirements": "<p>D</p>"}}
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="2",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/1nhealth/jobs/9f21-uuid-2",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>C</p>\n<p>D</p>"
    assert fetcher.calls == [
        "https://api.rippling.com/platform/api/ats/v1/board/1nhealth/jobs/9f21-uuid-2"
    ]


def test_rippling_fetch_detail_falls_back_to_listing_url() -> None:
    payload = {"description": {"role": "<p>Fallback JD</p>"}}
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="3",
        source="rippling",
        token=None,
        apply_url=None,
        listing_url="https://ats.rippling.com/acme-co/jobs/uuid-3",
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD</p>"
    assert fetcher.calls == [
        "https://api.rippling.com/platform/api/ats/v1/board/acme-co/jobs/uuid-3"
    ]


def test_rippling_fetch_detail_plain_string_description_returned_directly() -> None:
    payload = {"description": "<p>Plain string JD</p>"}
    ref = DetailRef(
        id="4",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-4",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc == "<p>Plain string JD</p>"


def test_rippling_fetch_detail_missing_description_is_none() -> None:
    payload: dict = {"someOtherKey": {}}
    ref = DetailRef(
        id="5",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-5",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_empty_description_dict_is_none() -> None:
    payload = {"description": {}}
    ref = DetailRef(
        id="6",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-6",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_empty_string_description_is_none() -> None:
    payload = {"description": "   "}
    ref = DetailRef(
        id="7",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-7",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_non_dict_non_str_description_is_none() -> None:
    payload = {"description": ["not", "a", "dict-or-str"]}
    ref = DetailRef(
        id="8",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-8",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_truthy_non_dict_payload_is_none() -> None:
    # ``data`` itself truthy but not a dict must not raise.
    payload = "oops-a-string"
    ref = DetailRef(
        id="9",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-9",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_unparseable_urls_is_none() -> None:
    payload = {"description": {"role": "<p>Should never be fetched</p>"}}
    ref = DetailRef(
        id="10",
        source="rippling",
        token=None,
        apply_url="https://example.com/not-a-rippling-url",
        listing_url="https://example.com/also-not-rippling",
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_no_urls_no_token_is_none() -> None:
    payload = {"description": {"role": "<p>Should never be fetched</p>"}}
    ref = DetailRef(
        id="11", source="rippling", token=None, apply_url=None, listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_rippling_fetch_detail_fetcher_raises_is_none() -> None:
    class _RaisingFetcher:
        async def get_json(self, url: str, **kw: object) -> object:
            raise RuntimeError("boom (e.g. stale 404)")

    ref = DetailRef(
        id="12",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-12",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _RaisingFetcher()))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None
