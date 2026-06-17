"""Unit tests for the Rippling provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.rippling import RipplingProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
JOBS_URL = "https://api.rippling.com/platform/api/ats/v1/board/11fs-group-ltd/jobs"


def _fixture() -> list:
    return json.loads((FIXTURES / "rippling_jobs.json").read_text())


def test_matches_recognizes_host() -> None:
    p = RipplingProvider
    assert p.matches("https://ats.rippling.com/foo") == "foo"
    assert p.matches("https://ats.rippling.com/11fs-group-ltd/jobs") == "11fs-group-ltd"
    assert p.matches("ats.rippling.com/1nhealth/jobs/abc-123") == "1nhealth"
    assert p.matches("ats.rippling.com/foo?q=1#x") == "foo"
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RipplingProvider().fetch("11fs-group-ltd", SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "rippling"
    assert r0.source_job_id == "3c36e9c8-1bee-45a2-94fe-09f10ccbe10f"
    assert r0.company == "11fs-group-ltd"
    assert r0.token == "11fs-group-ltd"
    assert r0.url == "https://ats.rippling.com/11fs-group-ltd/jobs/3c36e9c8-1bee-45a2-94fe-09f10ccbe10f"
    assert r0.payload["name"] == "Senior Sales Executive"


async def test_normalize_onsite_job() -> None:
    with respx.mock:
        respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RipplingProvider().fetch("11fs-group-ltd", SearchQuery(), f)

    job = RipplingProvider().normalize(raws[0])

    assert job.id == make_job_id("rippling", "3c36e9c8-1bee-45a2-94fe-09f10ccbe10f")
    assert job.title == "Senior Sales Executive"
    assert job.company == "11fs-group-ltd"
    assert job.department == "Pulse"
    assert job.apply_url == raws[0].url
    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.salary is None
    assert job.posted_at is None
    assert job.description_html is None
    assert job.description_text is None
    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.raw == "London, United Kingdom"
    assert loc.city == "London"
    assert loc.country == "United Kingdom"
    assert loc.is_remote is False
    assert job.raw == raws[0].payload


async def test_normalize_remote_job() -> None:
    with respx.mock:
        respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RipplingProvider().fetch("11fs-group-ltd", SearchQuery(), f)

    job = RipplingProvider().normalize(raws[1])

    assert job.title == "Director, Strategic Solutions"
    assert job.department == "Business Development"
    assert job.remote is RemoteType.REMOTE
    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.raw == "Remote (United States)"
    # Parenthesized labels are not split into city/country.
    assert loc.city is None
    assert loc.country is None
    assert loc.is_remote is True


async def test_fetch_handles_dict_wrapper() -> None:
    with respx.mock:
        respx.get(JOBS_URL).mock(return_value=httpx.Response(200, json={"jobs": _fixture()}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RipplingProvider().fetch("11fs-group-ltd", SearchQuery(), f)
    assert len(raws) == 2
