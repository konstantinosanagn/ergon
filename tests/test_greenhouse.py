"""Unit tests for the Greenhouse provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobspine.http import AsyncFetcher
from jobspine.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from jobspine.providers.greenhouse import GreenhouseProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs"


def _fixture() -> dict:
    return json.loads((FIXTURES / "greenhouse_sample.json").read_text())


def test_matches_recognizes_all_hosts() -> None:
    p = GreenhouseProvider
    assert p.matches("https://boards.greenhouse.io/airbnb") == "airbnb"
    assert p.matches("https://job-boards.greenhouse.io/stripe") == "stripe"
    assert p.matches("https://boards-api.greenhouse.io/v1/boards/airbnb/jobs") == "airbnb"
    assert p.matches("boards.greenhouse.io/airbnb?gh_jid=1#x") == "airbnb"
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs_and_hits_content_endpoint() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await GreenhouseProvider().fetch("airbnb", SearchQuery(), f)

        request = route.calls.last.request
        assert request.url.params["content"] == "true"
        assert str(request.url).startswith(BOARD_URL)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "greenhouse"
    assert r0.source_job_id == "7995153"
    assert r0.company == "Airbnb"
    assert r0.token == "airbnb"
    assert r0.url == "https://careers.airbnb.com/positions/7995153?gh_jid=7995153"
    assert r0.payload["title"] == "Acquisition Manager"


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await GreenhouseProvider().fetch("airbnb", SearchQuery(), f)

    provider = GreenhouseProvider()
    job = provider.normalize(raws[0])

    assert job.id == make_job_id("greenhouse", "7995153")
    assert job.title == "Acquisition Manager"
    assert job.company == "Airbnb"
    assert [loc.raw for loc in job.locations] == ["Berlin, Germany"]
    assert job.remote is RemoteType.HYBRID  # from metadata "Workplace Type"
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.department == "Sales"
    assert job.salary is None
    assert job.apply_url == "https://careers.airbnb.com/positions/7995153?gh_jid=7995153"
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.posted_at.year == 2026
    assert job.updated_at is not None and job.updated_at.tzinfo is not None
    # content is HTML-entity-encoded at the source; we unescape it.
    assert job.description_html is not None and job.description_html.startswith("<div")
    assert "&lt;" not in job.description_html
    assert job.description_text is not None and "Airbnb" in job.description_text
    assert job.raw == raws[0].payload


async def test_normalize_remote_from_metadata_and_location() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await GreenhouseProvider().fetch("airbnb", SearchQuery(), f)

    job = GreenhouseProvider().normalize(raws[1])
    # offices[] yields the structured "Beijing, China"; remote is driven by metadata.
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].raw == "Beijing, China"
    assert job.department == "Software Engineering"
