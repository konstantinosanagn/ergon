"""Unit tests for the Recruitee provider (respx-mocked, offline).

Fixture ``recruitee_sample.json`` is a trimmed capture of the live
``https://channable.recruitee.com/api/offers/`` response (token "channable").
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.recruitee import RecruiteeProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BOARD_URL = "https://channable.recruitee.com/api/offers/"


def _fixture() -> dict:
    return json.loads((FIXTURES / "recruitee_sample.json").read_text())


def test_matches_recognizes_hosts() -> None:
    p = RecruiteeProvider
    assert p.matches("https://channable.recruitee.com/api/offers/") == "channable"
    assert p.matches("https://acme.recruitee.com") == "acme"
    assert p.matches("acme.recruitee.com/o/some-role") == "acme"
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RecruiteeProvider().fetch("channable", SearchQuery(), f)

        assert str(route.calls.last.request.url) == BOARD_URL

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "recruitee"
    assert r0.source_job_id == "2631774"
    assert r0.company == "Channable"
    assert r0.token == "channable"
    # apply url prefers careers_apply_url over careers_url
    assert r0.url == "https://jobs.channable.com/o/account-executive-france-2028/c/new"
    assert r0.payload["title"] == "Account Executive France"


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RecruiteeProvider().fetch("channable", SearchQuery(), f)

    job = RecruiteeProvider().normalize(raws[0])

    assert job.id == make_job_id("recruitee", "2631774")
    assert job.source == "recruitee"
    assert job.source_job_id == "2631774"
    assert job.title == "Account Executive France"
    assert job.company == "Channable"
    assert job.apply_url == "https://jobs.channable.com/o/account-executive-france-2028/c/new"

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Utrecht"
    assert loc.region == "Utrecht"
    assert loc.country == "Netherlands"
    assert loc.raw == "Utrecht, Utrecht, Netherlands"
    assert loc.is_remote is False

    # hybrid=True, remote=False, on_site=False -> HYBRID
    assert job.remote is RemoteType.HYBRID
    # employment_type_code "fulltime_fixed_term" -> FULL_TIME
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Sales"
    assert job.salary is None

    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert (job.posted_at.year, job.posted_at.month, job.posted_at.day) == (2026, 6, 8)
    assert job.updated_at is not None and job.updated_at.tzinfo is not None

    assert job.description_html is not None and job.description_html.startswith("<p")
    assert job.description_text is not None and len(job.description_text) > 0
    assert job.raw == raws[0].payload


async def test_normalize_second_offer_employment_permanent() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RecruiteeProvider().fetch("channable", SearchQuery(), f)

    job = RecruiteeProvider().normalize(raws[1])
    assert job.title == "AI Engineer"
    # "fulltime_permanent" -> FULL_TIME
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.locations[0].city == "Utrecht"


async def test_fetch_empty_or_missing_offers() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RecruiteeProvider().fetch("channable", SearchQuery(), f)
    assert raws == []


def test_normalize_apply_url_falls_back_to_careers_url() -> None:
    from ergon_tracker.models import RawJob

    raw = RawJob(
        source="recruitee",
        source_job_id="42",
        company="Acme",
        token="acme",
        payload={
            "id": 42,
            "title": "Engineer",
            "careers_url": "https://jobs.acme.com/o/engineer",
            "remote": True,
        },
    )
    job = RecruiteeProvider().normalize(raw)
    assert job.apply_url == "https://jobs.acme.com/o/engineer"
    assert job.remote is RemoteType.REMOTE
    assert job.locations and job.locations[0].is_remote is True
    assert job.employment_type is EmploymentType.UNKNOWN
