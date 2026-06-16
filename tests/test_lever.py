"""Unit tests for the Lever provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobspine.http import AsyncFetcher
from jobspine.models import (
    EmploymentType,
    RemoteType,
    SalaryInterval,
    SearchQuery,
    make_job_id,
)
from jobspine.providers.lever import LeverProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
POSTINGS_URL = "https://api.lever.co/v0/postings/spotify"


def _fixture() -> list:
    return json.loads((FIXTURES / "lever_sample.json").read_text())


def test_matches_recognizes_host() -> None:
    p = LeverProvider
    assert p.matches("https://jobs.lever.co/spotify") == "spotify"
    assert p.matches("https://jobs.lever.co/palantir/abc-123/apply") == "palantir"
    assert p.matches("jobs.lever.co/spotify?q=1#x") == "spotify"
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_builds_rawjobs_and_default_params() -> None:
    with respx.mock:
        route = respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await LeverProvider().fetch("spotify", SearchQuery(), f)

        params = route.calls.last.request.url.params
        assert params["mode"] == "json"
        assert "location" not in params
        assert "commitment" not in params

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "lever"
    assert r0.source_job_id == "88499546-e9f7-4403-87a5-240050bd7c5b"
    assert r0.company == "Spotify"
    assert r0.token == "spotify"
    assert r0.url == "https://jobs.lever.co/spotify/88499546-e9f7-4403-87a5-240050bd7c5b"
    assert r0.payload["text"] == "Accounts Payable Analyst"


async def test_fetch_forwards_server_side_filters() -> None:
    with respx.mock:
        route = respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        query = SearchQuery(location="New York", employment_type=EmploymentType.FULL_TIME)
        async with AsyncFetcher(per_host_rate=100) as f:
            await LeverProvider().fetch("spotify", query, f)

        params = route.calls.last.request.url.params
        assert params["location"] == "New York"
        assert params["commitment"] == "Full-time"


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await LeverProvider().fetch("spotify", SearchQuery(), f)

    job = LeverProvider().normalize(raws[0])

    assert job.id == make_job_id("lever", "88499546-e9f7-4403-87a5-240050bd7c5b")
    assert job.title == "Accounts Payable Analyst"
    assert job.company == "Spotify"
    assert [loc.raw for loc in job.locations] == ["New York, NY"]
    assert job.remote is RemoteType.HYBRID
    assert job.employment_type is EmploymentType.FULL_TIME  # "Permanent" -> full_time
    assert job.department == "Finance"
    assert job.salary is not None
    assert job.salary.min_amount == 66189
    assert job.salary.max_amount == 94556
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR
    assert (
        job.apply_url == "https://jobs.lever.co/spotify/88499546-e9f7-4403-87a5-240050bd7c5b/apply"
    )
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.posted_at.year == 2026
    assert job.description_html is not None and job.description_html.startswith("<div")
    assert job.description_text is not None and "Spotify" in job.description_text
    assert job.raw == raws[0].payload


async def test_normalize_without_salary() -> None:
    with respx.mock:
        respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await LeverProvider().fetch("spotify", SearchQuery(), f)

    job = LeverProvider().normalize(raws[1])
    assert job.salary is None
    assert job.remote is RemoteType.HYBRID
    assert job.department == "Advertising"
