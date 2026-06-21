"""Unit tests for the ADP Workforce Now Recruitment provider (respx-mocked, offline)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
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
