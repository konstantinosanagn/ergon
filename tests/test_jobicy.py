"""Jobicy provider unit tests (offline, respx)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from ergon_tracker import RemoteType
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, SalaryInterval, SearchQuery
from ergon_tracker.providers.jobicy import JobicyProvider

pytestmark = pytest.mark.anyio

API = "https://jobicy.com/api/v2/remote-jobs"


def _provider() -> JobicyProvider:
    return JobicyProvider()


def test_matches_always_none_aggregator() -> None:
    assert JobicyProvider.matches("jobicy.com") is None
    assert JobicyProvider.matches("https://jobicy.com/api/v2/remote-jobs") is None


async def test_fetch_returns_all_jobs() -> None:
    payload = json.loads(load_fixture("jobicy_sample.json"))
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    assert route.called
    assert len(raws) == 3
    assert all(r.source == "jobicy" and r.token is None for r in raws)
    assert raws[0].source_job_id == "146869"
    assert raws[0].company == "Abbott"
    assert (
        raws[0].url
        == "https://jobicy.com/jobs/146869-clinical-sales-specialist-electrophysiology-laa-st-louis-mo"
    )


async def test_fetch_respects_query_limit() -> None:
    payload = json.loads(load_fixture("jobicy_sample.json"))
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=2), f)
    assert len(raws) == 2
    assert raws[0].source_job_id == "146869"
    # limit is forwarded to the API as the `count` query param.
    assert route.calls.last.request.url.params["count"] == "2"


async def test_normalize_full_field_mapping() -> None:
    payload = json.loads(load_fixture("jobicy_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    jobs = [_provider().normalize(r) for r in raws]

    job = jobs[0]
    assert job.source == "jobicy"
    assert job.company == "Abbott"
    assert job.title == "Clinical Sales Specialist, Electrophysiology – LAA (St. Louis, MO)"
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].is_remote is True
    assert job.locations[0].raw == "USA"
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.posted_at == datetime(2026, 6, 16, 4, 19, 59, tzinfo=timezone.utc)
    assert job.apply_url and job.description_html
    assert job.raw == raws[0].payload

    # Salary parsed with YEAR interval + currency.
    assert job.salary is not None
    assert job.salary.min_amount == 78000
    assert job.salary.max_amount == 156000
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR

    # Every normalized job is remote regardless of salary presence.
    assert all(j.remote is RemoteType.REMOTE for j in jobs)
    # Job without salary fields -> no Salary invented.
    assert jobs[2].salary is None


async def test_salary_accepts_annual_aliases() -> None:
    """The documented annualSalaryMin/Max aliases parse just like the live salaryMin/Max."""
    raw_payload = {
        "id": 1,
        "jobTitle": "Engineer",
        "companyName": "Acme",
        "jobGeo": "Anywhere",
        "jobType": ["Contract"],
        "url": "https://jobicy.com/jobs/1",
        "annualSalaryMin": 100000,
        "annualSalaryMax": 150000,
        "salaryCurrency": "EUR",
    }
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json={"jobs": [raw_payload]}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    job = _provider().normalize(raws[0])
    assert job.employment_type is EmploymentType.CONTRACT
    assert job.salary is not None
    assert job.salary.min_amount == 100000
    assert job.salary.max_amount == 150000
    assert job.salary.currency == "EUR"
    assert job.salary.interval is SalaryInterval.YEAR


async def test_empty_jobs_key_yields_nothing() -> None:
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json={"jobs": []}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    assert raws == []
