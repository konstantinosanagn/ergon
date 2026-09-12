"""Unit tests for the PeopleClick/PeopleFluent provider (respx-mocked, offline)."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import (
    EmploymentType,
    JobLevel,
    RawJob,
    RemoteType,
    SearchQuery,
    make_job_id,
)
from ergon.providers.peopleclick import PeopleClickProvider

pytestmark = pytest.mark.anyio

BASE = "https://careers.peopleclick.com/careerscp"
SEARCH = f"{BASE}/client_mit/external/search/search.html"
RESULT = f"{BASE}/client_mit/external/results/searchResult.html"
GETJOBS = f"{BASE}/api/client_mit/external/site/getJobs"


def _job(jid: int, title: str, loc: str, dept: str, **extra: str) -> dict:
    return {
        "jobPostId": jid,
        "identity": {"id": jid},
        "attributes": {
            "FLD_JP_POSTING_TITLE": title,
            "JPM_LOCATION": loc,
            "FLD_JP_DEPARTMENT": dept,
            **extra,
        },
    }


# Live-probed MIT attribute keys/values (careers.peopleclick.com/client_mit).
_MIT_ATTRS = {
    "JPM_DURATION": "Full-time (Hybrid)",
    "FLD_JPM_HIRING_RANGE_MIN": "$127,500",
    "FLD_JPM_HIRING_RANGE_MAX": "$167,570",
    "FLD_JPM_PAY_GRADE": "11",
    "JPM_EXPERIENCE": "Not Indicated",
    "JPM_EDUCATION": "Not Indicated",
    "JP_POSTEDON": "Sep 11, 2026",
    "JPM_DESCRIPTION": "<strong>DIRECTOR OF ADVANCEMENT</strong>, <em>Media Lab</em>, to serve.",
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
                    _job(
                        101,
                        "Software Developer 2",
                        "Cambridge, MA",
                        "Chemical Engineering",
                        **_MIT_ATTRS,
                    ),
                    _job(
                        102,
                        "Research Scientist",
                        "Cambridge, MA",
                        "Physics",
                        JPM_DURATION="Part-time",
                        FLD_JPM_PAY_GRADE="Senior",
                        JPM_EXPERIENCE="Minimum of 5 years of related experience",
                        JPM_EDUCATION="Bachelor's Degree",
                    ),
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


async def test_normalize_maps_attribute_metadata() -> None:
    """The attributes map's own metadata keys (probe: MIT) -> structured JobPosting fields."""
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleClickProvider().fetch("client_mit|MIT", SearchQuery(), f)

    j0 = PeopleClickProvider().normalize(raws[0])
    assert j0.employment_type is EmploymentType.FULL_TIME  # JPM_DURATION "Full-time (Hybrid)"
    assert j0.remote is RemoteType.HYBRID  # same field's parenthesised workplace
    assert j0.salary is not None
    assert (j0.salary.min_amount, j0.salary.max_amount) == (127500.0, 167570.0)
    assert j0.salary.currency == "USD"
    assert j0.posted_at == datetime(2026, 9, 11)  # JP_POSTEDON "Sep 11, 2026"
    assert j0.description_html is not None
    assert "DIRECTOR OF ADVANCEMENT" in j0.description_html
    # MIT's live values for these two are the placeholder "Not Indicated" -> never guessed.
    assert (j0.years_experience_min, j0.years_experience_max) == (None, None)
    assert j0.degree_min is None
    assert j0.level is JobLevel.UNKNOWN  # a numeric pay grade is not a seniority vocab word

    j1 = PeopleClickProvider().normalize(raws[1])
    assert j1.employment_type is EmploymentType.PART_TIME
    assert j1.level is JobLevel.SENIOR  # FLD_JPM_PAY_GRADE carrying a vocab word
    assert j1.years_experience_min == 5  # JPM_EXPERIENCE prose
    assert j1.degree_min == "bachelor"  # JPM_EDUCATION vocab
    assert j1.remote is RemoteType.UNKNOWN  # "Part-time" says nothing about workplace
    assert j1.salary is None and j1.posted_at is None


def test_normalize_unknown_vocab_never_raises() -> None:
    """Unrecognised values fall back to UNKNOWN/None instead of raising."""
    raw = RawJob(
        source="peopleclick",
        source_job_id="9",
        company="MIT",
        payload={
            "FLD_JP_POSTING_TITLE": "Custodian",
            "JPM_DURATION": "Flexitime",
            "FLD_JPM_HIRING_RANGE_MIN": "Commensurate with experience",
            "FLD_JPM_PAY_GRADE": "11",
            "JPM_EDUCATION": "Vocational",
            "JP_POSTEDON": "11/09/2026",
        },
    )
    job = PeopleClickProvider().normalize(raw)
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.remote is RemoteType.UNKNOWN
    assert job.level is JobLevel.UNKNOWN
    assert job.salary is None
    assert job.degree_min is None
    assert job.posted_at is None


async def test_remote_location_still_reads_remote() -> None:
    """With no workplace word in JPM_DURATION, the location text stays the fallback signal."""
    raw = RawJob(
        source="peopleclick",
        source_job_id="10",
        company="MIT",
        payload={"FLD_JP_POSTING_TITLE": "Analyst", "JPM_LOCATION": "Remote - US"},
    )
    job = PeopleClickProvider().normalize(raw)
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].is_remote is True


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
