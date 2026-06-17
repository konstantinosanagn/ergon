"""Adzuna provider unit tests (offline, respx)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SalaryInterval, SearchQuery
from ergon_tracker.providers.adzuna import AdzunaProvider, _country_slug

pytestmark = pytest.mark.anyio

_PAYLOAD = {
    "count": 1,
    "results": [
        {
            "id": "4321",
            "title": "Senior Software Engineer",
            "company": {"display_name": "Acme GmbH"},
            "location": {"display_name": "Berlin, Germany", "area": ["Germany", "Berlin"]},
            "salary_min": 70000,
            "salary_max": 90000,
            "contract_time": "full_time",
            "contract_type": "permanent",
            "created": "2026-06-10T08:30:00Z",
            "redirect_url": "https://www.adzuna.de/details/4321",
            "description": "Build things.",
            "category": {"label": "IT Jobs"},
        }
    ],
}


def _provider() -> AdzunaProvider:
    return AdzunaProvider()


def _configured(monkeypatch) -> None:
    creds = {"ADZUNA_APP_ID": "test_id", "ADZUNA_APP_KEY": "test_key"}
    monkeypatch.setattr("ergon_tracker.providers.adzuna.get_env", lambda k: creds.get(k))


def test_matches_always_none_aggregator() -> None:
    assert AdzunaProvider.matches("adzuna.com") is None
    assert AdzunaProvider.is_aggregator is True


def test_country_slug_mapping() -> None:
    assert _country_slug("Germany") == "de"
    assert _country_slug("United States") == "us"
    assert _country_slug("UK") == "gb"
    assert _country_slug(None) == "us"  # default
    assert _country_slug("Narnia") == "us"  # unknown -> default


async def test_fetch_skips_without_keys(monkeypatch) -> None:
    monkeypatch.setattr("ergon_tracker.providers.adzuna.get_env", lambda k: None)
    with respx.mock:
        route = respx.get(url__startswith="https://api.adzuna.com").mock(
            return_value=httpx.Response(200, json=_PAYLOAD)
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(keywords="engineer"), f)
    assert raws == []
    assert not route.called  # no network call when unconfigured


async def test_fetch_sends_credentials_and_country(monkeypatch) -> None:
    _configured(monkeypatch)
    url = "https://api.adzuna.com/v1/api/jobs/de/search/1"
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(200, json=_PAYLOAD))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch(
                "", SearchQuery(keywords="engineer", country="Germany", city="Berlin"), f
            )
    assert route.called
    params = route.calls.last.request.url.params
    assert params.get("app_id") == "test_id"
    assert params.get("app_key") == "test_key"
    assert params.get("what") == "engineer"
    assert params.get("where") == "Berlin"
    assert len(raws) == 1
    assert raws[0].source == "adzuna"
    assert raws[0].company == "Acme GmbH"


async def test_normalize_full_field_mapping(monkeypatch) -> None:
    _configured(monkeypatch)
    url = "https://api.adzuna.com/v1/api/jobs/de/search/1"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=_PAYLOAD))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(country="de"), f)
    job = _provider().normalize(raws[0])
    assert job.title == "Senior Software Engineer"
    assert job.company == "Acme GmbH"
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.remote is RemoteType.UNKNOWN
    assert job.department == "IT Jobs"
    assert job.locations[0].country == "Germany"
    assert job.salary is not None
    assert job.salary.min_amount == 70000
    assert job.salary.max_amount == 90000
    assert job.salary.currency == "EUR"  # de -> EUR
    assert job.salary.interval is SalaryInterval.YEAR
    assert job.posted_at == datetime(2026, 6, 10, 8, 30, tzinfo=timezone.utc)
    assert job.apply_url == "https://www.adzuna.de/details/4321"


async def test_us_salary_currency(monkeypatch) -> None:
    _configured(monkeypatch)
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=_PAYLOAD))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(country="United States"), f)
    job = _provider().normalize(raws[0])
    assert job.salary.currency == "USD"
