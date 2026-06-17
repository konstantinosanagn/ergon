"""Unit tests for the BambooHR provider (respx-mocked, offline).

Fixture ``bamboohr_jobs.json`` is a trimmed capture of the live
``https://aca.bamboohr.com/careers/list`` response (token "aca"); the third entry is
edited to exercise the ``isRemote`` mapping path.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.bamboohr import BambooHRProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BOARD_URL = "https://aca.bamboohr.com/careers/list"


def _fixture() -> dict:
    return json.loads((FIXTURES / "bamboohr_jobs.json").read_text())


def test_matches_recognizes_hosts() -> None:
    p = BambooHRProvider
    assert p.matches("https://aca.bamboohr.com/careers/list") == "aca"
    assert p.matches("https://acme.bamboohr.com") == "acme"
    assert p.matches("acme.bamboohr.com/careers/42") == "acme"
    assert p.matches("https://www.bamboohr.com/careers") is None
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

        assert str(route.calls.last.request.url) == BOARD_URL

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "bamboohr"
    assert r0.source_job_id == "39"
    assert r0.company == "aca"
    assert r0.token == "aca"
    assert r0.url == "https://aca.bamboohr.com/careers/39"
    assert r0.payload["jobOpeningName"] == "Aircraft Maintenance Engineer"


async def test_normalize_legacy_location() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

    job = BambooHRProvider().normalize(raws[0])

    assert job.id == make_job_id("bamboohr", "39")
    assert job.source == "bamboohr"
    assert job.source_job_id == "39"
    assert job.title == "Aircraft Maintenance Engineer"
    assert job.company == "aca"
    assert job.apply_url == "https://aca.bamboohr.com/careers/39"

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Edmonton International Airport"
    assert loc.region == "Alberta"
    assert loc.country is None
    assert loc.is_remote is False

    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Maintenance"
    assert job.salary is None
    assert job.posted_at is None
    assert job.description_html is None
    assert job.description_text is None
    assert job.raw == raws[0].payload


async def test_normalize_structured_ats_location() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

    job = BambooHRProvider().normalize(raws[1])
    assert job.title == "Advanced Care Paramedic - Flights"
    # "Part-Time" -> PART_TIME
    assert job.employment_type is EmploymentType.PART_TIME
    loc = job.locations[0]
    assert loc.city == "Edmonton"
    assert loc.region == "Alberta"
    assert loc.country == "Canada"
    assert loc.raw == "Edmonton, Alberta, Canada"
    assert job.remote is RemoteType.UNKNOWN


async def test_normalize_remote_and_internship() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

    job = BambooHRProvider().normalize(raws[2])
    assert job.title == "Remote Dispatch Coordinator"
    # isRemote=true -> REMOTE, with a remote-flagged (otherwise empty) location
    assert job.remote is RemoteType.REMOTE
    assert job.locations and job.locations[0].is_remote is True
    # "Internship" -> INTERNSHIP
    assert job.employment_type is EmploymentType.INTERNSHIP


async def test_fetch_empty_or_missing_result() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"meta": {}}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)
    assert raws == []
