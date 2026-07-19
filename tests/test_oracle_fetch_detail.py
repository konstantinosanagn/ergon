"""Tier-3 detail fetcher: OracleProvider.fetch_detail (ORC / Fusion HCM per-requisition details
resource, verified live 12/12 tenants, no auth).

Offline only -- a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py`` (the Workday Task-3 pattern), including its non-raising
discipline for a truthy non-dict payload shape."""

from __future__ import annotations

import anyio
import pytest

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


def test_oracle_fetch_detail_missing_description_raises() -> None:
    # Well-formed item, but no JD-relevant field present -- not a verified soft-404, so raise
    # (indeterminate/keep) rather than return None (which would expire a still-live posting).
    payload: dict = {"items": [{"Id": "17517", "SomeOtherField": "x"}]}
    ref = DetailRef(
        id="4",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_empty_description_raises() -> None:
    payload = _orc_payload("   ", None, None)
    ref = DetailRef(
        id="5",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_non_dict_description_raises() -> None:
    # ``ExternalDescriptionStr`` truthy but not a string -- no usable JD text -- indeterminate, raise.
    payload = {"items": [{"Id": "17517", "ExternalDescriptionStr": {"nested": "not-a-string"}}]}
    ref = DetailRef(
        id="6",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_truthy_non_dict_payload_raises() -> None:
    # A whole payload that's truthy but not a dict (e.g. a bare list/string) is an unclassifiable
    # shape -- not a verified soft-404 -- so it must raise, not silently expire the posting.
    payload = "oops"
    ref = DetailRef(
        id="7",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_non_dict_item_raises() -> None:
    # ``items`` truthy but its first element not a dict is an unclassifiable shape -- not a
    # verified soft-404 -- so it must RAISE (indeterminate/keep), not return None (which would
    # expire a still-live posting on an ambiguous signal).
    payload = {"items": ["oops"]}
    ref = DetailRef(
        id="12",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_non_oracle_apply_url_raises() -> None:
    # An unbuildable detail URL is NOT evidence of death -- raise (keep), never expire.
    payload = _orc_payload()
    ref = DetailRef(
        id="8",
        source="oracle",
        token=None,
        apply_url="https://example.com/not-an-oracle-url/job/x",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_no_job_segment_raises() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="9",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/notjob/x",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_no_sites_segment_raises() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="10",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/job/R286249",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_oracle_fetch_detail_no_urls_raises() -> None:
    payload = _orc_payload()
    ref = DetailRef(
        id="11",
        source="oracle",
        token=None,
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(
        id="1", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None


def test_oracle_fetch_detail_recovers_structured_location() -> None:
    from ergon_tracker.models import DetailFetch

    payload = _orc_payload("<p>JD.</p>", None, None)
    payload["items"][0]["PrimaryLocation"] = "Orlando, FL, United States"
    payload["items"][0]["PrimaryLocationCountry"] = "US"
    ref = DetailRef(
        id="1",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R1",
        listing_url=None,
        content_sig="s",
    )
    res = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert isinstance(res, DetailFetch)
    assert res.locations[0].raw == "Orlando, FL, United States"
    assert res.locations[0].country == "US"


def test_oracle_fetch_detail_recovers_when_description_empty_but_secondary_populated() -> None:
    # Measured bug (fixed): ~25% of failed oracle postings have an EMPTY ExternalDescriptionStr but
    # real content in ExternalResponsibilitiesStr/ExternalQualificationsStr. The parser must NOT bail
    # on an empty primary field -- it must return the secondary sections.
    payload = _orc_payload(
        description="",
        responsibilities="<p>Own the roadmap.</p>",
        qualifications="<p>BS in CS, 5+ years.</p>",
    )
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Own the roadmap.</p>\n<p>BS in CS, 5+ years.</p>"


def test_oracle_fetch_detail_raises_when_all_sections_empty() -> None:
    # No JD-relevant text on a 200 is indeterminate (not a verified soft-404) -- raise, don't expire.
    payload = _orc_payload(description="", responsibilities=None, qualifications=None)
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="oracle",
        token=None,
        apply_url="https://ehac.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/R1",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: OracleProvider().fetch_detail(ref, fetcher))
