"""Unit tests for the Phenom provider (respx-mocked, offline)."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon.providers.phenom import PhenomProvider

pytestmark = pytest.mark.anyio

HOST = "careers.activisionblizzard.com"
API = f"https://{HOST}/widgets"


def _job(seq: str, title: str, city: str, remote: str = "ON-SITE") -> dict:
    return {
        "jobSeqNo": seq,
        "title": title,
        "city": city,
        "state": "California",
        "country": "United States",
        "cityStateCountry": f"{city}, California, United States",
        "postedDate": "2026-06-13T00:00:00.000+0000",
        "type": "Full-Time",
        "checkRemote": remote,
        "category": "Engineering",
        "descriptionTeaser": "Build games.",
        "applyUrl": f"https://{HOST}/job/{seq}/apply",
    }


def _mock(respx_mock: respx.MockRouter) -> None:
    """from=0 -> 2 jobs (totalHits=2); from=100 -> empty (terminates)."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        jobs = (
            [
                _job("111", "Senior Engineer", "San Francisco"),
                _job("222", "Remote Designer", "Los Angeles", remote="REMOTE"),
            ]
            if body.get("from") == 0
            else []
        )
        return httpx.Response(
            200, json={"refineSearch": {"status": 200, "totalHits": 2, "data": {"jobs": jobs}}}
        )

    respx_mock.post(API).mock(side_effect=handler)


def test_matches_host_and_path() -> None:
    p = PhenomProvider
    assert p.matches("https://x.phenompeople.com/careers") == "x.phenompeople.com"
    # vanity host only matches with a phenom path shape
    assert p.matches("https://careers.activisionblizzard.com/search-results") == HOST
    assert p.matches("https://careers.activisionblizzard.com/job/ABC123") == HOST
    assert p.matches("https://careers.activisionblizzard.com/about") is None
    assert p.matches("https://boards.greenhouse.io/airbnb") is None


async def test_fetch_paginates() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PhenomProvider().fetch(HOST, SearchQuery(), f)
    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "phenom"
    assert r0.source_job_id == "111"
    assert r0.url == f"https://{HOST}/job/111"


async def test_normalize_fields() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PhenomProvider().fetch(HOST, SearchQuery(), f)

    onsite = PhenomProvider().normalize(raws[0])
    assert onsite.id == make_job_id("phenom", "111")
    assert onsite.title == "Senior Engineer"
    assert onsite.department == "Engineering"
    assert onsite.remote is RemoteType.ONSITE
    assert onsite.employment_type is EmploymentType.FULL_TIME
    assert onsite.locations[0].raw == "San Francisco, California, United States"
    assert onsite.description_text == "Build games."
    assert onsite.apply_url == f"https://{HOST}/job/111/apply"
    posted = onsite.posted_at.astimezone(timezone.utc)
    assert (posted.year, posted.month, posted.day) == (2026, 6, 13)

    remote = PhenomProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PhenomProvider().fetch(HOST, SearchQuery(limit=1), f)
    assert len(raws) == 1
