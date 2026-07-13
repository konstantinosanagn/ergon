"""Tier-3 detail fetcher: OracleProvider.fetch_detail (ORC / Fusion HCM per-requisition details
resource, verified live 12/12 tenants, no auth).

Offline only -- a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py`` (the Workday Task-3 pattern), including its non-raising
discipline for a truthy non-dict payload shape."""
from __future__ import annotations

import anyio

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.oracle import OracleProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append((url, kw.get("params")))
        return self._p


def _orc_payload(
    description: str = "<p>Full JD. 5+ years. Bachelor's required.</p>",
    responsibilities: str | None = "<p>Own the roadmap.</p>",
    qualifications: str | None = "<p>BS in CS or equivalent.</p>",
) -> dict:
    """Real response shape: the requisition lives in ``items[0]`` (verified live), NOT flat --
    same envelope as the list endpoint's ``recruitingCEJobRequisitions`` response."""
    item: dict[str, object] = {"Id": "17517", "Title": "x", "ExternalDescriptionStr": description}
    if responsibilities is not None:
        item["ExternalResponsibilitiesStr"] = responsibilities
    if qualifications is not None:
        item["ExternalQualificationsStr"] = qualifications
    return {"items": [item], "count": 1, "hasMore": False}


def test_oracle_fetch_detail_returns_combined_html_plain_host_shape() -> None:
    payload = _orc_payload(
        "<p>Real JD text for the ehac posting...</p>",
        "<p>Ship features end to end.</p>",
        "<p>3+ years experience.</p>",
    )
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, fetcher))
    assert desc == (
        "<p>Real JD text for the ehac posting...</p>\n"
        "<p>Ship features end to end.</p>\n"
        "<p>3+ years experience.</p>"
    )
    assert len(fetcher.calls) == 1
    url, params = fetcher.calls[0]
    assert url == (
        "https://ehac.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails"
    )
    assert params == {
        "onlyData": "true",
        "finder": "ById;Id=R286249,siteNumber=CX_1",
    }


def test_oracle_fetch_detail_returns_description_saasfaprod_host_shape() -> None:
    payload = _orc_payload("<p>Real JD text for the saasfaprod posting...</p>", None, None)
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="2",
        source="oracle",
        token=None,
        apply_url=(
            "https://fa-eqyy-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/"
            "en/sites/CX_31001/job/123456"
        ),
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Real JD text for the saasfaprod posting...</p>"
    assert len(fetcher.calls) == 1
    url, params = fetcher.calls[0]
    assert url == (
        "https://fa-eqyy-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails"
    )
    assert params == {
        "onlyData": "true",
        "finder": "ById;Id=123456,siteNumber=CX_31001",
    }


def test_oracle_fetch_detail_falls_back_to_listing_url() -> None:
    payload = _orc_payload("<p>Fallback JD via listing_url...</p>", None, None)
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="3",
        source="oracle",
        token=None,
        apply_url=None,
        listing_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD via listing_url...</p>"
    assert len(fetcher.calls) == 1
    url, params = fetcher.calls[0]
    assert url == (
        "https://ehac.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails"
    )
    assert params == {
        "onlyData": "true",
        "finder": "ById;Id=R286249,siteNumber=CX_1",
    }


def test_oracle_fetch_detail_missing_description_is_none() -> None:
    payload: dict = {"items": [{"Id": "17517", "SomeOtherField": "x"}]}
    ref = DetailRef(
        id="4",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_empty_description_is_none() -> None:
    payload = _orc_payload("   ", None, None)
    ref = DetailRef(
        id="5",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_non_dict_description_is_none() -> None:
    # ``ExternalDescriptionStr`` truthy but not a string must not raise.
    payload = {"items": [{"Id": "17517", "ExternalDescriptionStr": {"nested": "not-a-string"}}]}
    ref = DetailRef(
        id="6",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_truthy_non_dict_payload_is_none() -> None:
    # A whole payload that's truthy but not a dict (e.g. a bare list/string) must not raise.
    payload = "oops"
    ref = DetailRef(
        id="7",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_non_dict_item_is_none() -> None:
    # ``items`` truthy but its first element not a dict must not raise (the SmartRecruiters-class
    # regression, at the envelope level this time).
    payload = {"items": ["oops"]}
    ref = DetailRef(
        id="12",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_non_oracle_apply_url_is_none() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="8",
        source="oracle",
        token=None,
        apply_url="https://example.com/not-an-oracle-url/job/x",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_no_job_segment_is_none() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="9",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/notjob/x",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_no_sites_segment_is_none() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="10",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_oracle_fetch_detail_no_urls_is_none() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="11", source="oracle", token=None, apply_url=None, listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None,
                     content_sig="s")
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None
