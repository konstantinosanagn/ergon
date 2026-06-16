"""Himalayas provider unit tests (offline, respx)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from jobspine import RemoteType
from jobspine.http import AsyncFetcher
from jobspine.models import EmploymentType, SalaryInterval, SearchQuery
from jobspine.providers.himalayas import HimalayasProvider

pytestmark = pytest.mark.anyio

API = "https://himalayas.app/jobs/api"


def _provider() -> HimalayasProvider:
    return HimalayasProvider()


def test_matches_always_none_aggregator() -> None:
    assert HimalayasProvider.matches("himalayas.app") is None
    assert HimalayasProvider.matches("https://himalayas.app/jobs/api") is None


async def test_fetch_returns_all_jobs() -> None:
    payload = json.loads(load_fixture("himalayas_sample.json"))
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    assert route.called
    assert len(raws) == 3
    assert all(r.source == "himalayas" and r.token is None for r in raws)
    assert raws[0].company == "Nymphis Technologies"
    # No numeric id is exposed; the guid is the canonical source_job_id.
    assert raws[0].source_job_id.startswith("https://himalayas.app/")


async def test_fetch_respects_query_limit() -> None:
    payload = json.loads(load_fixture("himalayas_sample.json"))
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=2), f)
    assert len(raws) == 2
    # limit is forwarded to the API as the `limit` query param.
    assert route.calls.last.request.url.params["limit"] == "2"


async def test_normalize_full_field_mapping() -> None:
    payload = json.loads(load_fixture("himalayas_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    jobs = [_provider().normalize(r) for r in raws]

    # Job index 2 carries a full salary + single location restriction.
    job = jobs[2]
    assert job.source == "himalayas"
    assert job.company == "Remote"
    assert job.title == "Senior Backend Engineer (Elixir)"
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].is_remote is True
    # locationRestrictions is a list -> first entry becomes Location.raw.
    assert job.locations[0].raw == "Germany"
    assert job.employment_type is EmploymentType.FULL_TIME
    # pubDate is an epoch int -> UTC datetime.
    assert job.posted_at == datetime.fromtimestamp(1781589519, tz=timezone.utc)
    assert (
        job.apply_url
        == "https://himalayas.app/companies/remote/jobs/senior-backend-engineer-elixir-7339256917"
    )
    assert job.description_html
    assert job.raw == raws[2].payload

    assert job.salary is not None
    assert job.salary.min_amount == 53300
    assert job.salary.max_amount == 119850
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR

    # Every normalized job is remote regardless of salary presence.
    assert all(j.remote is RemoteType.REMOTE for j in jobs)
    # Jobs with null minSalary/maxSalary -> no Salary invented.
    assert jobs[0].salary is None
    assert jobs[1].salary is None


async def test_salary_defaults_currency_to_usd_when_amounts_present() -> None:
    raw_payload = {
        "title": "Engineer",
        "companyName": "Acme",
        "locationRestrictions": ["United States"],
        "employmentType": "Contract",
        "minSalary": 90000,
        "maxSalary": 120000,
        "currency": None,
        "pubDate": 1781589519,
        "applicationLink": "https://himalayas.app/companies/acme/jobs/engineer",
        "guid": "https://himalayas.app/companies/acme/jobs/engineer",
    }
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json={"jobs": [raw_payload]}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    job = _provider().normalize(raws[0])
    assert job.employment_type is EmploymentType.CONTRACT
    assert job.salary is not None
    assert job.salary.currency == "USD"  # defaulted because amounts present but currency unset
    assert job.salary.interval is SalaryInterval.YEAR


async def test_location_restrictions_empty_falls_back_to_remote_only() -> None:
    raw_payload = {
        "title": "Engineer",
        "companyName": "Acme",
        "locationRestrictions": [],
        "guid": "https://himalayas.app/companies/acme/jobs/engineer",
    }
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json={"jobs": [raw_payload]}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    job = _provider().normalize(raws[0])
    assert job.locations[0].raw is None
    assert job.locations[0].is_remote is True
    assert job.salary is None
