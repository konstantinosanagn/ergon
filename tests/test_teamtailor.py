"""Unit tests for the Teamtailor provider (respx-mocked, offline).

Fixture ``teamtailor_jobs.json`` is a trimmed capture of the live
``https://1komma5.teamtailor.com/jobs.json`` JSON Feed (token "1komma5"). The public
jobs.json is summary-oriented: it carries title/url/date and an embedded schema.org
``_jobposting`` block (company + jobLocation), but no employmentType in this sample.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.teamtailor import TeamtailorProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BOARD_URL = "https://1komma5.teamtailor.com/jobs.json"


def _fixture() -> dict:
    return json.loads((FIXTURES / "teamtailor_jobs.json").read_text())


def test_matches_recognizes_hosts() -> None:
    p = TeamtailorProvider
    assert p.matches("https://1komma5.teamtailor.com/jobs.json") == "1komma5"
    assert p.matches("https://acme.teamtailor.com") == "acme"
    assert p.matches("acme.teamtailor.com/jobs/123-some-role") == "acme"
    # www and the keyed api host are not company boards
    assert p.matches("https://www.teamtailor.com") is None
    assert p.matches("https://api.teamtailor.com/v1/jobs") is None
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TeamtailorProvider().fetch("1komma5", SearchQuery(), f)

        assert str(route.calls.last.request.url) == BOARD_URL

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "teamtailor"
    assert r0.source_job_id == "942c032b-91ee-4fc4-a044-c5e7971f3a1e"
    assert r0.company == "1KOMMA5° Sverige"
    assert r0.token == "1komma5"
    assert r0.url == (
        "https://1komma5.teamtailor.com/jobs/"
        "7793185-utesaljare-energilosningar-till-1komma5-i-stockholm"
    )
    assert r0.payload["title"] == "Utesäljare energilösningar till 1KOMMA5° i Stockholm"


async def test_normalize_maps_available_fields() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TeamtailorProvider().fetch("1komma5", SearchQuery(), f)

    job = TeamtailorProvider().normalize(raws[0])

    assert job.id == make_job_id("teamtailor", "942c032b-91ee-4fc4-a044-c5e7971f3a1e")
    assert job.source == "teamtailor"
    assert job.title == "Utesäljare energilösningar till 1KOMMA5° i Stockholm"
    assert job.company == "1KOMMA5° Sverige"
    assert job.apply_url == (
        "https://1komma5.teamtailor.com/jobs/"
        "7793185-utesaljare-energilosningar-till-1komma5-i-stockholm"
    )

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Stockholm"
    assert loc.region == "Mälardalen"
    assert loc.country == "SE"
    assert loc.raw == "Stockholm, Mälardalen, SE"

    # No remote/employment signals in this feed -> UNKNOWN, never invented.
    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.department is None

    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert (job.posted_at.year, job.posted_at.month, job.posted_at.day) == (2026, 5, 26)

    assert job.description_html is not None
    assert job.description_text is not None and len(job.description_text) > 0
    assert job.raw == raws[0].payload


async def test_normalize_second_item() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TeamtailorProvider().fetch("1komma5", SearchQuery(), f)

    job = TeamtailorProvider().normalize(raws[1])
    assert job.title.startswith("Innesäljare")
    assert job.locations[0].city == "Stockholm"
    assert job.apply_url is not None


async def test_fetch_empty_or_missing_items() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TeamtailorProvider().fetch("1komma5", SearchQuery(), f)
    assert raws == []


def test_normalize_company_falls_back_to_token() -> None:
    from ergon_tracker.models import RawJob

    raw = RawJob(
        source="teamtailor",
        source_job_id="abc",
        company="acme",  # fetch() falls back to token when no hiringOrganization
        token="acme",
        payload={
            "id": "abc",
            "title": "Remote Engineer",
            "url": "https://acme.teamtailor.com/jobs/1-remote-engineer",
        },
    )
    job = TeamtailorProvider().normalize(raw)
    assert job.company == "acme"
    assert job.title == "Remote Engineer"
    # "remote" in the title -> REMOTE
    assert job.remote is RemoteType.REMOTE
    assert job.locations == []
    assert job.employment_type is EmploymentType.UNKNOWN
