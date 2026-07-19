"""Unit tests for the ADP Workforce Now Recruitment provider (respx-mocked, offline)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import SearchQuery
from ergon_tracker.providers.adp import ADPProvider

pytestmark = pytest.mark.anyio

CID = "3993975e-194c-4504-9c5e-9e6017ca5023"
API = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"


def _rec(i: int) -> dict:
    return {
        "itemID": f"item{i}",
        "requisitionTitle": f"Job {i}",
        "postDate": "2026-06-17T13:40:00.000-04:00",
        "workLevelCode": {"shortName": "Full-Time"},
        "requisitionLocations": [
            {
                "address": {"cityName": "York", "countrySubdivisionLevel1": {"codeValue": "PA"}},
                "nameCode": {"shortName": "South York Plaza, York, PA, US"},
            }
        ],
        "customFieldGroup": {
            "stringFields": [
                {"stringValue": f"EXT{i}", "nameCode": {"codeValue": "ExternalJobID"}},
                {"stringValue": "Banking", "nameCode": {"codeValue": "HomeDepartment"}},
            ]
        },
    }


def _mock(respx_mock: respx.MockRouter, total: int, server_cap: int = 50) -> None:
    """Mock the job-requisitions GET, reproducing ADP's quirks: ``$skip=N`` is INCLUSIVE of index
    ``N-1`` (one-row overlap), and the server caps rows per call at ``server_cap`` regardless of
    requested ``$top``."""

    def handler(request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlsplit(str(request.url)).query)
        top = int(q["$top"][0])
        skip = int(q["$skip"][0])
        start = skip - 1 if skip > 0 else 0  # ADP off-by-one
        eff = min(top, server_cap)
        items = [_rec(i) for i in range(start, min(start + eff, total))]
        return httpx.Response(200, json={"jobRequisitions": items})

    respx_mock.get(url__startswith=API).mock(side_effect=handler)


# --- matches --------------------------------------------------------------


def test_matches_workforcenow_cid() -> None:
    url = f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={CID}&ccId=x&lang=en_US"
    assert ADPProvider.matches(url) == CID


def test_matches_cloud_host_carries_host() -> None:
    url = f"https://workforcenow.cloud.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={CID}"
    assert ADPProvider.matches(url) == f"{CID}|workforcenow.cloud.adp.com"


def test_matches_rejects_non_adp_and_vanity() -> None:
    assert ADPProvider.matches("https://boards.greenhouse.io/acme") is None
    # myjobs.adp.com is a DIFFERENT ADP system (vanity token, no cid) -> not this provider.
    assert ADPProvider.matches("https://myjobs.adp.com/advantestcareers") is None
    # right host but no cid GUID -> None
    assert ADPProvider.matches("https://workforcenow.adp.com/mascsr/default/login.html") is None


# --- fetch / pagination ---------------------------------------------------


@respx.mock
async def test_fetch_paginates_past_overlap_and_server_cap() -> None:
    # 120 jobs, server caps 50/call: must collect all 120 distinct despite the $skip overlap.
    _mock(respx.mock, total=120, server_cap=50)
    async with AsyncFetcher() as f:
        raws = await ADPProvider().fetch(CID, SearchQuery(), f)
    ids = [r.source_job_id for r in raws]
    assert len(ids) == 120
    assert len(set(ids)) == 120  # no dupes despite one-row page overlap


@respx.mock
async def test_fetch_respects_limit() -> None:
    _mock(respx.mock, total=120)
    async with AsyncFetcher() as f:
        raws = await ADPProvider().fetch(CID, SearchQuery(limit=10), f)
    assert len(raws) == 10


# --- normalize ------------------------------------------------------------


@respx.mock
async def test_normalize_fields() -> None:
    _mock(respx.mock, total=1)
    prov = ADPProvider()
    async with AsyncFetcher() as f:
        raws = await prov.fetch(f"{CID}||ACNB Corp", SearchQuery(), f)
    job = prov.normalize(raws[0])
    assert job.title == "Job 0"
    assert job.company == "ACNB Corp"  # display name carried in the token
    assert job.locations[0].city == "York"
    assert job.locations[0].region == "PA"
    assert f"cid={CID}" in job.apply_url and "jobId=EXT0" in job.apply_url


# --- fetch_detail: 404-vs-transient hardening contract ----------------------
#
# Live-verified (2026-07, LCNB CORP tenant): the per-posting detail resource is the SAME
# job-requisitions API keyed by a single id in the path -- it accepts either the internal itemID
# or the ExternalJobID the apply_url's ``jobId`` param carries. A nonexistent id does NOT 404 --
# it returns HTTP 200 with a SKELETON record that omits ``itemID`` entirely.

DETAIL_APPLY_URL = (
    f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    f"?cid={CID}&jobId=589881&lang=en_US"
)
DETAIL_URL = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
    f"job-requisitions/589881?cid={CID}"
)


class _FakeFetcher:
    """Stands in for AsyncFetcher: returns a fixed JSON payload, or raises a fixed exception."""

    def __init__(self, *, payload: object = None, exc: BaseException | None = None) -> None:
        self._payload = payload
        self._exc = exc
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._payload


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", DETAIL_URL)
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


def _detail_ref(apply_url: str | None = DETAIL_APPLY_URL) -> DetailRef:
    return DetailRef(
        id="9201258168513_1",
        source="adp",
        token=CID,
        apply_url=apply_url,
        listing_url=None,
        content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await ADPProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await ADPProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await ADPProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await ADPProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_description() -> None:
    fetcher = _FakeFetcher(
        payload={"itemID": "9201258168513_1", "requisitionDescription": "<p>Join us.</p>"}
    )
    res = await ADPProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert res == "<p>Join us.</p>"


async def test_fetch_detail_skeleton_soft_404_returns_none() -> None:
    """Verified live: a nonexistent id returns HTTP 200 with a SKELETON payload -- no ``itemID``
    at all -- rather than a 404 status."""
    fetcher = _FakeFetcher(payload={"links": [], "customFieldGroup": {"stringFields": []}})
    res = await ADPProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_enveloped_record_without_top_itemid_raises() -> None:
    """Defensive two-factor guard: a 200 lacking a TOP-LEVEL itemID but still carrying record
    content (a hypothetical future enveloped-but-live shape) is an unrecognised shape -> RAISES,
    never the skeleton soft-404 -> None path (which would false-expire a live posting)."""
    enveloped = {"jobRequisition": {"itemID": "x1", "requisitionDescription": "<p>Join.</p>"}}
    fetcher = _FakeFetcher(payload=enveloped)
    with pytest.raises(RuntimeError):
        await ADPProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_missing_url_raises() -> None:
    fetcher = _FakeFetcher(payload={})
    with pytest.raises(RuntimeError):
        await ADPProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)


async def test_fetch_detail_unbuildable_url_raises() -> None:
    """A non-ADP host (or missing cid/jobId) -> no derivable detail URL -> RAISES, never treated
    as gone."""
    fetcher = _FakeFetcher(payload={})
    bad = "https://example.com/careers/12345"
    with pytest.raises(RuntimeError):
        await ADPProvider().fetch_detail(_detail_ref(apply_url=bad), fetcher)


async def test_fetch_detail_present_itemid_no_description_raises() -> None:
    """A 200 that DOES echo itemID but carries no requisitionDescription text is indeterminate,
    not gone."""
    fetcher = _FakeFetcher(payload={"itemID": "9201258168513_1"})
    with pytest.raises(RuntimeError):
        await ADPProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_non_dict_payload_raises() -> None:
    fetcher = _FakeFetcher(payload=["not", "a", "dict"])
    with pytest.raises(RuntimeError):
        await ADPProvider().fetch_detail(_detail_ref(), fetcher)
