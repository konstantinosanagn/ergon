"""Tier-3 detail fetcher: EightfoldProvider.fetch_detail.

Offline only -- a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``, including its non-raising discipline for a truthy
non-dict payload shape, plus eightfold's specific tenant-derivation asymmetry (host vs.
token-fallback vs. unresolvable white-label domain)."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.eightfold import EightfoldProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._p


def _ef_payload(job_description: str = "<p>JD...</p>") -> dict:
    return {"job_description": job_description}


def test_eightfold_fetch_detail_returns_description_tenant_host_shape() -> None:
    payload = _ef_payload("<p>Real JD text for the acme posting...</p>")
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="eightfold",
        token=None,
        apply_url="https://acme.eightfold.ai/careers/job/42478672",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the acme posting...</p>"
    assert fetcher.calls == ["https://acme.eightfold.ai/api/apply/v2/jobs/42478672"]


def test_eightfold_fetch_detail_white_label_with_token_uses_token_as_tenant() -> None:
    payload = _ef_payload("<p>Real JD text for the white-labeled posting...</p>")
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="2",
        source="eightfold",
        token="starbucks",
        apply_url="https://careers.starbucks.com/careers/job/99887766",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the white-labeled posting...</p>"
    assert fetcher.calls == ["https://starbucks.eightfold.ai/api/apply/v2/jobs/99887766"]


def test_eightfold_fetch_detail_white_label_without_token_is_none() -> None:
    payload = _ef_payload()
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="3",
        source="eightfold",
        token=None,
        apply_url="https://careers.starbucks.com/careers/job/99887766",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_eightfold_fetch_detail_falls_back_to_listing_url_for_id_with_token_tenant() -> None:
    # apply_url absent -> id must come from listing_url; tenant must come from the token
    # fallback (NOT from listing_url's host -- see the asymmetry test below).
    payload = _ef_payload("<p>Fallback JD via listing_url...</p>")
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="4",
        source="eightfold",
        token="acme",
        apply_url=None,
        listing_url="https://acme.eightfold.ai/careers/job/13579",
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD via listing_url...</p>"
    assert fetcher.calls == ["https://acme.eightfold.ai/api/apply/v2/jobs/13579"]


def test_eightfold_fetch_detail_tenant_never_falls_back_to_listing_url_host() -> None:
    # The important asymmetry: id derivation falls back to listing_url, but tenant derivation
    # does NOT -- it only ever looks at apply_url's host (else the token). Here listing_url is
    # a perfectly valid eightfold host, but apply_url is absent and there's no token, so tenant
    # resolution must fail and no fetch must happen, even though a tenant COULD be read off
    # listing_url if we (incorrectly) fell back to it.
    payload = _ef_payload()
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="4b",
        source="eightfold",
        token=None,
        apply_url=None,
        listing_url="https://acme.eightfold.ai/careers/job/13579",
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, fetcher))
    assert desc is None
    assert fetcher.calls == []


def test_eightfold_fetch_detail_missing_job_description_is_none() -> None:
    payload: dict = {"someOtherKey": "x"}
    ref = DetailRef(
        id="5",
        source="eightfold",
        token=None,
        apply_url="https://acme.eightfold.ai/careers/job/1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_eightfold_fetch_detail_empty_job_description_is_none() -> None:
    payload = _ef_payload("   ")
    ref = DetailRef(
        id="6",
        source="eightfold",
        token=None,
        apply_url="https://acme.eightfold.ai/careers/job/1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_eightfold_fetch_detail_non_string_job_description_is_none() -> None:
    payload = {"job_description": {"nested": "not-a-string"}}
    ref = DetailRef(
        id="7",
        source="eightfold",
        token=None,
        apply_url="https://acme.eightfold.ai/careers/job/1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_eightfold_fetch_detail_truthy_non_dict_payload_is_none() -> None:
    # A truthy non-dict payload (e.g. a bare string/list) must not raise.
    fetcher = _FakeFetcher("oops-not-json-object")
    ref = DetailRef(
        id="8",
        source="eightfold",
        token=None,
        apply_url="https://acme.eightfold.ai/careers/job/1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, fetcher))
    assert desc is None


def test_eightfold_fetch_detail_no_urls_is_none() -> None:
    payload = _ef_payload()
    ref = DetailRef(
        id="9", source="eightfold", token=None, apply_url=None, listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_eightfold_fetch_detail_fetch_failure_is_none() -> None:
    class _RaisingFetcher:
        async def get_json(self, url: str, **kw: object) -> object:
            raise RuntimeError("boom")

    ref = DetailRef(
        id="10",
        source="eightfold",
        token=None,
        apply_url="https://acme.eightfold.ai/careers/job/1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, _RaisingFetcher()))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None


def test_eightfold_fetch_detail_recovers_location_string() -> None:
    from ergon_tracker.models import DetailFetch

    payload = {"job_description": "<p>JD.</p>", "location": "Phoenix, AZ USA 85040"}
    ref = DetailRef(
        id="1", source="eightfold", token=None,
        apply_url="https://acme.eightfold.ai/careers/job/42", listing_url=None, content_sig="s",
    )
    res = anyio.run(lambda: EightfoldProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert isinstance(res, DetailFetch)
    assert res.locations[0].raw == "Phoenix, AZ USA 85040"
