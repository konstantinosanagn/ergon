"""Unit tests for the Breezy HR provider (respx-mocked, offline).

Fixture ``breezy_jobs.json`` is a trimmed capture of the live
``https://10-4-truck-recruiting.breezy.hr/json`` response (token "10-4-truck-recruiting"),
augmented with a remote contractor and an intern position to exercise the field mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.breezy import BreezyProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "10-4-truck-recruiting"
BOARD_URL = f"https://{TOKEN}.breezy.hr/json"


def _fixture() -> list:
    return json.loads((FIXTURES / "breezy_jobs.json").read_text())


def test_matches_recognizes_hosts() -> None:
    p = BreezyProvider
    assert p.matches("https://10-4-truck-recruiting.breezy.hr/json") == "10-4-truck-recruiting"
    assert p.matches("https://acme.breezy.hr") == "acme"
    assert p.matches("acme.breezy.hr/p/some-role") == "acme"
    assert p.matches("https://www.breezy.hr") is None
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BreezyProvider().fetch(TOKEN, SearchQuery(), f)

        assert str(route.calls.last.request.url) == BOARD_URL

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "breezy"
    assert r0.source_job_id == "e63605aeb81c"
    assert r0.company == "10-4 Truck Recruiting"
    assert r0.token == TOKEN
    assert r0.url == (
        "https://10-4-truck-recruiting.breezy.hr/p/"
        "e63605aeb81c-cdl-a-home-daily-truck-driving-position"
    )
    assert r0.payload["name"] == "CDL A Home Daily Truck Driving Position"


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BreezyProvider().fetch(TOKEN, SearchQuery(), f)

    job = BreezyProvider().normalize(raws[0])

    assert job.id == make_job_id("breezy", "e63605aeb81c")
    assert job.source == "breezy"
    assert job.source_job_id == "e63605aeb81c"
    assert job.title == "CDL A Home Daily Truck Driving Position"
    assert job.company == "10-4 Truck Recruiting"
    assert job.apply_url == (
        "https://10-4-truck-recruiting.breezy.hr/p/"
        "e63605aeb81c-cdl-a-home-daily-truck-driving-position"
    )

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Tacoma"
    assert loc.region == "Washington"
    assert loc.country == "United States"
    assert loc.raw == "Tacoma, WA"
    assert loc.is_remote is False

    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Local - Home Daily"
    assert job.salary is None

    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert (job.posted_at.year, job.posted_at.month, job.posted_at.day) == (2026, 6, 4)

    assert job.description_html is not None and job.description_html.startswith("<p")
    assert job.description_text is not None and "home daily" in job.description_text.lower()
    assert job.raw == raws[0].payload


async def test_normalize_remote_contractor() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BreezyProvider().fetch(TOKEN, SearchQuery(), f)

    job = BreezyProvider().normalize(raws[1])
    assert job.title == "Senior Backend Engineer"
    # type.name "Contractor" -> CONTRACT
    assert job.employment_type is EmploymentType.CONTRACT
    # location.is_remote=True -> REMOTE
    assert job.remote is RemoteType.REMOTE
    assert job.locations and job.locations[0].is_remote is True
    assert job.department == "Engineering"
    assert job.description_html is None
    assert job.description_text is None


async def test_normalize_intern() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BreezyProvider().fetch(TOKEN, SearchQuery(), f)

    job = BreezyProvider().normalize(raws[2])
    assert job.title == "Dispatch Intern"
    # type.name "Intern" -> INTERNSHIP
    assert job.employment_type is EmploymentType.INTERNSHIP
    assert job.locations[0].city == "Dallas"
    assert job.locations[0].region == "Texas"
    assert job.remote is RemoteType.UNKNOWN


async def test_fetch_empty_or_non_list() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BreezyProvider().fetch(TOKEN, SearchQuery(), f)
    assert raws == []
