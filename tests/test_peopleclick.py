"""Unit tests for the PeopleClick/PeopleFluent provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery, make_job_id
from ergon_tracker.providers.peopleclick import PeopleClickProvider

pytestmark = pytest.mark.anyio

BASE = "https://careers.peopleclick.com/careerscp"
SEARCH = f"{BASE}/client_mit/external/search/search.html"
RESULT = f"{BASE}/client_mit/external/results/searchResult.html"
GETJOBS = f"{BASE}/api/client_mit/external/site/getJobs"


def _job(jid: int, title: str, loc: str, dept: str) -> dict:
    return {
        "jobPostId": jid,
        "identity": {"id": jid},
        "attributes": {"FLD_JP_POSTING_TITLE": title, "JPM_LOCATION": loc, "FLD_JP_DEPARTMENT": dept},
    }


def _mock(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SEARCH).mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    respx_mock.post(RESULT).mock(return_value=httpx.Response(200, text="<html>results</html>"))
    respx_mock.get(GETJOBS).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalHits": 2,
                "hitsPerPage": 50,
                "jobList": [
                    _job(101, "Software Developer 2", "Cambridge, MA", "Chemical Engineering"),
                    _job(102, "Research Scientist", "Cambridge, MA", "Physics"),
                ],
            },
        )
    )


def test_parse_token() -> None:
    assert PeopleClickProvider._parse("client_mit|MIT") == ("client_mit", "MIT")
    assert PeopleClickProvider._parse("client_mit") == ("client_mit", None)


async def test_fetch_and_normalize() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleClickProvider().fetch("client_mit|MIT", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"MIT"}
    j0 = PeopleClickProvider().normalize(raws[0])
    assert j0.id == make_job_id("peopleclick", "101")
    assert j0.title == "Software Developer 2"
    assert j0.locations[0].raw == "Cambridge, MA"
    assert j0.department == "Chemical Engineering"
    assert "101" in j0.apply_url


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleClickProvider().fetch("client_mit|MIT", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_degrades_on_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(SEARCH).mock(return_value=httpx.Response(200, text="ok"))
        respx_mock.post(RESULT).mock(return_value=httpx.Response(200, text="ok"))
        respx_mock.get(GETJOBS).mock(return_value=httpx.Response(200, json={"status": "fail"}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleClickProvider().fetch("client_mit|MIT", SearchQuery(), f)
    assert raws == []
