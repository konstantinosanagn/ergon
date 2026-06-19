"""Unit tests for the JobDiva provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.jobdiva import JobDivaProvider

pytestmark = pytest.mark.anyio

HASH = "k9jdnw75xg463bhcrcwzphygelka7w00a7qs6jxqkxju09w8pb19z92eu18p5gj3"
AUTH = "https://ws.jobdiva.com/candPortal/rest/auth/a"
SEARCH = "https://ws.jobdiva.com/candPortal/rest/job/searchjobsportal"
PORTAL = f"https://www1.jobdiva.com/portal/?a={HASH}"


def _job(jid: int, title: str, *, location: str = "Austin, TX", remote: str | None = None) -> dict:
    return {
        "id": jid,
        "title": title,
        "refNo": f"REF-{jid}",
        "company": "Confidential",
        "postDate": 1781809458000,
        "positionType": "Contract",
        "workingRemote": remote,
        "location": location,
        "otherLocations": [],
        "jobDescription": f"<p>{title}</p>",
    }


def _payload(jobs: list[dict], total: int) -> dict:
    return {"total": total, "data": jobs}


def _mock(respx_mock: respx.MockRouter, *, with_portal: bool = False) -> None:
    if with_portal:
        respx_mock.get(PORTAL).mock(
            return_value=httpx.Response(200, text="<html>var teamid=167; compid=0;</html>")
        )
    respx_mock.get(AUTH).mock(
        return_value=httpx.Response(200, json={"token": "sess123", "a": HASH})
    )
    jobs = [
        _job(101, "Software Engineer", location="Austin, TX"),
        _job(102, "Data Analyst (Remote)", location="New York, NY", remote="Remote"),
    ]
    respx_mock.post(SEARCH).mock(return_value=httpx.Response(200, json=_payload(jobs, total=2)))


def test_matches_jobdiva_hosts() -> None:
    p = JobDivaProvider
    assert p.matches("https://www1.jobdiva.com/portal/?a=abc") == "www1.jobdiva.com"
    assert p.matches("ws.jobdiva.com") == "ws.jobdiva.com"
    assert p.matches("https://boards.greenhouse.io/airbnb") is None


def test_parse_token_variants() -> None:
    assert JobDivaProvider._parse("h") == ("h", None, None)
    assert JobDivaProvider._parse("h|167") == ("h", "167", None)
    assert JobDivaProvider._parse("h|167|Acme Corp") == ("h", "167", "Acme Corp")
    # empty teamid field, explicit company
    assert JobDivaProvider._parse("h||Acme Corp") == ("h", None, "Acme Corp")


async def test_fetch_with_explicit_teamid_and_company() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobDivaProvider().fetch(f"{HASH}|167|Acme Staffing", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"Acme Staffing"}  # not the "Confidential" payload value
    assert raws[0].source == "jobdiva"
    assert raws[0].source_job_id == "101"
    assert "jobid=101" in raws[0].url


async def test_fetch_auto_discovers_teamid_from_portal() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock, with_portal=True)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobDivaProvider().fetch(f"{HASH}||Acme Staffing", SearchQuery(), f)
    assert len(raws) == 2


async def test_normalize_fields() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobDivaProvider().fetch(f"{HASH}|167|Acme Staffing", SearchQuery(), f)

    onsite = JobDivaProvider().normalize(raws[0])
    assert onsite.id == make_job_id("jobdiva", "101")
    assert onsite.title == "Software Engineer"
    assert onsite.company == "Acme Staffing"
    assert onsite.locations[0].raw == "Austin, TX"
    assert onsite.remote is RemoteType.UNKNOWN
    assert onsite.description_html == "<p>Software Engineer</p>"
    assert onsite.posted_at is not None

    remote = JobDivaProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE  # from the workingRemote field, not the location
    assert remote.locations[0].raw == "New York, NY"


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobDivaProvider().fetch(f"{HASH}|167|Acme", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_degrades_on_auth_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(AUTH).mock(return_value=httpx.Response(400, text='"Invalid"'))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobDivaProvider().fetch(f"{HASH}|167|Acme", SearchQuery(), f)
    assert raws == []


async def test_fetch_degrades_when_no_teamid() -> None:
    # hash-only token, portal page has no teamid -> no fetch
    with respx.mock as respx_mock:
        respx_mock.get(PORTAL).mock(return_value=httpx.Response(200, text="<html>nothing</html>"))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobDivaProvider().fetch(HASH, SearchQuery(), f)
    assert raws == []
