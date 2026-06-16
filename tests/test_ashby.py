"""Ashby provider unit tests (offline, respx)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from jobspine import EmploymentType, RemoteType, SalaryInterval
from jobspine.http import AsyncFetcher
from jobspine.providers.ashby import AshbyProvider

pytestmark = pytest.mark.anyio

API = "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"


def _provider() -> AshbyProvider:
    return AshbyProvider()


def test_matches_extracts_token_from_host_and_url() -> None:
    assert AshbyProvider.matches("jobs.ashbyhq.com/ramp") == "ramp"
    assert AshbyProvider.matches("https://jobs.ashbyhq.com/openai/some-job") == "openai"
    assert AshbyProvider.matches("jobs.ashbyhq.com") is None
    assert AshbyProvider.matches("boards.greenhouse.io/acme") is None


async def test_fetch_builds_url_and_token() -> None:
    payload = json.loads(load_fixture("ashby_sample.json"))
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("ramp", _query(), f)
    assert route.called
    assert len(raws) == 3
    assert all(r.source == "ashby" and r.company == "ramp" and r.token == "ramp" for r in raws)
    assert raws[0].source_job_id == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert raws[0].url == "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert raws[0].payload["title"].strip() == "Security Engineer, Cloud"


async def test_normalize_full_field_mapping() -> None:
    payload = json.loads(load_fixture("ashby_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("ramp", _query(), f)

    jobs = [_provider().normalize(r) for r in raws]

    # Job 0: remote FullTime with annual USD salary.
    sec = jobs[0]
    assert sec.source == "ashby"
    assert sec.company == "ramp"
    assert sec.title == "Security Engineer, Cloud"
    assert sec.remote is RemoteType.REMOTE
    assert sec.employment_type is EmploymentType.FULL_TIME
    assert sec.department == "Engineering"
    assert sec.apply_url == (
        "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245/application"
    )
    assert sec.posted_at == datetime(2026, 4, 7, 17, 12, 35, 753000, tzinfo=timezone.utc)
    assert sec.salary is not None
    assert sec.salary.min_amount == 211400
    assert sec.salary.max_amount == 290600
    assert sec.salary.currency == "USD"
    assert sec.salary.interval is SalaryInterval.YEAR
    assert len(sec.locations) == 1
    loc = sec.locations[0]
    assert loc.city == "New York City"
    assert loc.region == "NY"
    assert loc.country == "USA"
    assert loc.raw == "New York, NY (HQ)"
    assert loc.is_remote is True
    assert sec.raw == raws[0].payload
    assert sec.description_html and sec.description_text

    # Job 1: internship → INTERNSHIP, monthly salary interval.
    intern = jobs[1]
    assert intern.employment_type is EmploymentType.INTERNSHIP
    assert intern.remote is RemoteType.REMOTE
    assert intern.salary is not None
    assert intern.salary.interval is SalaryInterval.MONTH
    assert intern.salary.min_amount == 11700

    # Job 2: onsite FullTime in London, no salary component.
    gtm = jobs[2]
    assert gtm.remote is RemoteType.ONSITE
    assert gtm.employment_type is EmploymentType.FULL_TIME
    assert gtm.salary is None
    assert gtm.locations[0].country == "United Kingdom"
    assert gtm.locations[0].is_remote is False


def test_normalize_missing_fields_default_to_unknown() -> None:
    from jobspine.models import RawJob

    raw = RawJob(source="ashby", source_job_id="x1", company="acme", payload={"title": "Eng"})
    job = _provider().normalize(raw)
    assert job.remote is RemoteType.UNKNOWN  # isRemote absent -> None -> UNKNOWN
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.salary is None
    assert job.posted_at is None
    assert job.locations == []
    assert job.department is None


def _query():  # type: ignore[no-untyped-def]
    from jobspine.models import SearchQuery

    return SearchQuery()
