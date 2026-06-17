"""Unit tests for the Oracle Recruiting Cloud provider (respx-mocked, offline)."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.oracle import OracleProvider

pytestmark = pytest.mark.anyio

HOST = "eeho.fa.us2.oraclecloud.com"
SITE = "CX_1"
API = f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def _req(jid: str, title: str, loc: str, code: str = "ORA_ON_SITE") -> dict:
    return {
        "Id": jid,
        "Title": title,
        "PostedDate": "2026-06-16",
        "PrimaryLocation": loc,
        "PrimaryLocationCountry": "United States",
        "WorkplaceTypeCode": code,
        "ShortDescriptionStr": "<p>Build things.</p>",
        "Department": "Engineering",
    }


def _wrapper(reqs: list[dict], total: int) -> dict:
    return {"items": [{"TotalJobsCount": total, "requisitionList": reqs}], "totalResults": 1}


def _mock(respx_mock: respx.MockRouter) -> None:
    """offset=0 -> 2 reqs (total=2); offset=25 -> empty list (terminates)."""

    def handler(request: httpx.Request) -> httpx.Response:
        finder = request.url.params.get("finder", "")
        if "offset=0" in finder:
            return httpx.Response(
                200,
                json=_wrapper(
                    [
                        _req("325810", "Senior Construction Manager", "Austin, TX, United States"),
                        _req("325811", "Remote Data Engineer", "United States", code="ORA_REMOTE"),
                    ],
                    total=2,
                ),
            )
        return httpx.Response(200, json=_wrapper([], total=2))

    respx_mock.get(url__startswith=API).mock(side_effect=handler)


def test_matches_career_and_rest_urls() -> None:
    p = OracleProvider
    assert (
        p.matches(
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions"
        )
        == "eeho.fa.us2.oraclecloud.com|CX_1"
    )
    # site defaults to CX_1 when absent
    assert p.matches("https://enno.fa.ap1.oraclecloud.com/") == "enno.fa.ap1.oraclecloud.com|CX_1"
    # explicit other site number
    assert (
        p.matches(
            "https://x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1002/job/5"
        )
        == "x.fa.ocs.oraclecloud.com|CX_1002"
    )
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_paginates_requisitionlist() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await OracleProvider().fetch(f"{HOST}|{SITE}", SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "oracle"
    assert r0.source_job_id == "325810"
    assert r0.url == f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/325810"


async def test_normalize_fields_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await OracleProvider().fetch(f"{HOST}|{SITE}", SearchQuery(), f)

    onsite = OracleProvider().normalize(raws[0])
    assert onsite.id == make_job_id("oracle", "325810")
    assert onsite.title == "Senior Construction Manager"
    assert onsite.department == "Engineering"
    assert onsite.remote is RemoteType.ONSITE
    assert onsite.locations[0].raw == "Austin, TX, United States"
    assert onsite.description_html == "<p>Build things.</p>"
    assert onsite.description_text is None
    assert onsite.salary is None
    posted = onsite.posted_at.astimezone(timezone.utc)
    assert (posted.year, posted.month, posted.day) == (2026, 6, 16)

    remote = OracleProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await OracleProvider().fetch(f"{HOST}|{SITE}", SearchQuery(limit=1), f)
    assert len(raws) == 1
