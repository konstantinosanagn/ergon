"""Tier-3 detail fetcher: WorkdayProvider.fetch_detail (biggest Tier-3 lever — 533,798 postings
across 2,228 tenants at 0% JD text).

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_provider_fetch_detail.py`` (the SmartRecruiters Task-3 pattern), including its
non-raising discipline for a truthy non-dict payload shape."""

from __future__ import annotations

import anyio
import pytest

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


def test_workday_fetch_detail_missing_job_posting_info_raises() -> None:
    payload: dict = {"someOtherKey": {}}
    ref = DetailRef(
        id="4",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_missing_job_description_raises() -> None:
    payload = {"jobPostingInfo": {"title": "Driver"}}
    ref = DetailRef(
        id="5",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_empty_job_description_raises() -> None:
    payload = _wd_payload("   ")
    ref = DetailRef(
        id="6",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_non_dict_job_posting_info_raises() -> None:
    # ``jobPostingInfo`` truthy but not a dict is INDETERMINATE (the SmartRecruiters regression).
    payload = {"jobPostingInfo": "oops"}
    ref = DetailRef(
        id="7",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_non_dict_job_description_raises() -> None:
    # ``jobDescription`` truthy but not a string-shaped dict is INDETERMINATE.
    payload = {"jobPostingInfo": {"jobDescription": {"nested": "not-a-string"}}}
    ref = DetailRef(
        id="8",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/job/x/y",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_non_workday_apply_url_raises() -> None:
    payload = _wd_payload()
    ref = DetailRef(
        id="9",
        source="workday",
        token=None,
        apply_url="https://example.com/not-a-workday-url/job/x",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_no_job_segment_raises() -> None:
    payload = _wd_payload()
    ref = DetailRef(
        id="10",
        source="workday",
        token=None,
        apply_url="https://sekologistics.wd503.myworkdayjobs.com/seko_logistics/notjob/x",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_workday_fetch_detail_no_urls_raises() -> None:
    payload = _wd_payload()
    ref = DetailRef(
        id="11",
        source="workday",
        token=None,
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(
        id="1", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None


def test_workday_fetch_detail_recovers_structured_location() -> None:
    # The cxs response carries jobPostingInfo.country.descriptor (+ location string) even when the
    # list feed only had a "N Locations" placeholder -> DetailFetch(text, locations) so the merge
    # can fill the index row's NULL country (Workday is ~44k of the whole country gap).
    from ergon_tracker.models import DetailFetch

    payload = {
        "jobPostingInfo": {
            "jobDescription": "<p>Full JD.</p>",
            "location": "Remote - USA",
            "country": {"descriptor": "United States of America", "id": "bc33"},
            "additionalLocations": ["Remote - Canada"],
        }
    }
    ref = DetailRef(
        id="1",
        source="workday",
        token=None,
        apply_url="https://calix.wd1.myworkdayjobs.com/external/job/Remote---USA/Role_R-1",
        listing_url=None,
        content_sig="s",
    )
    res = anyio.run(lambda: WorkdayProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert isinstance(res, DetailFetch)
    assert res.text == "<p>Full JD.</p>"
    assert res.locations and res.locations[0].country == "United States of America"
    assert res.locations[0].raw == "Remote - USA"


def test_workday_fetch_detail_no_location_stays_bare_str() -> None:
    # No location/country in jobPostingInfo -> unchanged bare-str contract.
    res = anyio.run(
        lambda: WorkdayProvider().fetch_detail(
            DetailRef(
                id="1",
                source="workday",
                token=None,
                apply_url="https://x.wd1.myworkdayjobs.com/s/job/L/R_1",
                listing_url=None,
                content_sig="s",
            ),
            _FakeFetcher(_wd_payload("<p>JD only.</p>")),
        )
    )
    assert res == "<p>JD only.</p>"


def test_workday_cxs_locations_helper() -> None:
    P = WorkdayProvider._cxs_locations
    assert P({}) == []
    assert P({"location": "Boston, MA"})[0].raw == "Boston, MA"
    only_country = P({"country": {"descriptor": "Canada"}})
    assert only_country[0].country == "Canada"
    assert P({"country": "not-a-dict"}) == []
