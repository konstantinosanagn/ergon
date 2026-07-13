"""Tier-3 detail fetcher: WorkdayProvider.fetch_detail (biggest Tier-3 lever — 533,798 postings
across 2,228 tenants at 0% JD text).

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_provider_fetch_detail.py`` (the SmartRecruiters Task-3 pattern), including its
non-raising discipline for a truthy non-dict payload shape."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.workday import WorkdayProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._p


def _wd_payload(job_description: str = "<p>Full JD. 5+ years. Bachelor's required.</p>") -> dict:
    return {"jobPostingInfo": {"jobDescription": job_description}}


def test_workday_fetch_detail_returns_description_seko_shape() -> None:
    payload = _wd_payload("<p>Real JD text for the seko posting...</p>")
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="workday",
        token=None,
        apply_url=(
            "https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/"
            "ORD---Mount-Prospect/DRIVER_R-100914"
        ),
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the seko posting...</p>"
    assert fetcher.calls == [
        "https://sekologistics.wd503.myworkdayjobs.com/wday/cxs/sekologistics/"
        "seko_logistics/job/ORD---Mount-Prospect/DRIVER_R-100914"
    ]


def test_workday_fetch_detail_returns_description_parsons_search_shape() -> None:
    payload = _wd_payload("<p>Real JD text for the parsons posting...</p>")
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="2",
        source="workday",
        token=None,
        apply_url="https://parsons.wd5.myworkdayjobs.com/search/job/USA-Remote/Engineer_R-1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the parsons posting...</p>"
    assert fetcher.calls == [
        "https://parsons.wd5.myworkdayjobs.com/wday/cxs/parsons/search/job/USA-Remote/Engineer_R-1"
    ]


def test_workday_fetch_detail_falls_back_to_listing_url() -> None:
    payload = _wd_payload("<p>Fallback JD via listing_url...</p>")
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="3",
        source="workday",
        token=None,
        apply_url=None,
        listing_url=(
            "https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/"
            "ORD---Mount-Prospect/DRIVER_R-100914"
        ),
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD via listing_url...</p>"
    assert fetcher.calls == [
        "https://sekologistics.wd503.myworkdayjobs.com/wday/cxs/sekologistics/"
        "seko_logistics/job/ORD---Mount-Prospect/DRIVER_R-100914"
    ]


def test_workday_fetch_detail_missing_job_posting_info_is_none() -> None:
    payload: dict = {"someOtherKey": {}}
    ref = DetailRef(
        id="4",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_missing_job_description_is_none() -> None:
    payload = {"jobPostingInfo": {"title": "Driver"}}
    ref = DetailRef(
        id="5",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_empty_job_description_is_none() -> None:
    payload = _wd_payload("   ")
    ref = DetailRef(
        id="6",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_non_dict_job_posting_info_is_none() -> None:
    # ``jobPostingInfo`` truthy but not a dict must not raise (the SmartRecruiters regression).
    payload = {"jobPostingInfo": "oops"}
    ref = DetailRef(
        id="7",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_non_dict_job_description_is_none() -> None:
    # ``jobDescription`` truthy but not a string-shaped dict must not raise.
    payload = {"jobPostingInfo": {"jobDescription": {"nested": "not-a-string"}}}
    ref = DetailRef(
        id="8",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_non_workday_apply_url_is_none() -> None:
    payload = _wd_payload()
    ref = DetailRef(
        id="9",
        source="workday",
        token=None,
        apply_url="https://example.com/not-a-workday-url/job/x",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_no_job_segment_is_none() -> None:
    payload = _wd_payload()
    ref = DetailRef(
        id="10",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/notjob/x",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_workday_fetch_detail_no_urls_is_none() -> None:
    payload = _wd_payload()
    ref = DetailRef(
        id="11", source="workday", token=None, apply_url=None, listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None
