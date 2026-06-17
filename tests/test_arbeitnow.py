"""Arbeitnow provider unit tests (offline, respx)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from ergon_tracker import RemoteType
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, SearchQuery
from ergon_tracker.providers.arbeitnow import ArbeitnowProvider

pytestmark = pytest.mark.anyio

API = "https://www.arbeitnow.com/api/job-board-api"


def _provider() -> ArbeitnowProvider:
    return ArbeitnowProvider()


def _page1() -> dict:
    return json.loads(load_fixture("arbeitnow_sample.json"))


def _page2() -> dict:
    return json.loads(load_fixture("arbeitnow_sample_page2.json"))


def test_matches_always_none_aggregator() -> None:
    assert ArbeitnowProvider.matches("arbeitnow.com") is None
    assert ArbeitnowProvider.matches("https://www.arbeitnow.com/api/job-board-api") is None


async def test_fetch_single_page() -> None:
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=_page1()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    assert route.called
    assert len(raws) == 2
    assert all(r.source == "arbeitnow" and r.token is None for r in raws)
    # slug is used as the source job id.
    assert raws[0].source_job_id == _page1()["data"][0]["slug"]
    assert raws[0].company == "Schulz Digital GmbH"


async def test_remote_derived_from_bool() -> None:
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=_page1()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    jobs = [_provider().normalize(r) for r in raws]
    # Fixture: first job remote=true, second remote=false.
    assert jobs[0].remote is RemoteType.REMOTE
    assert jobs[0].locations[0].is_remote is True
    assert jobs[1].remote is RemoteType.ONSITE
    assert jobs[1].locations[0].is_remote is False


async def test_normalize_full_field_mapping() -> None:
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=_page1()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    job = _provider().normalize(raws[0])
    p = _page1()["data"][0]
    assert job.source == "arbeitnow"
    assert job.company == "Schulz Digital GmbH"
    assert job.title == p["title"]
    assert job.remote is RemoteType.REMOTE
    assert job.employment_type is EmploymentType.FULL_TIME  # fixture job_types=["full time"]
    assert job.locations[0].raw == "Cologne"
    assert job.apply_url == p["url"]
    assert job.posted_at == datetime.fromtimestamp(p["created_at"], tz=timezone.utc)
    assert job.description_html
    assert job.raw == raws[0].payload


async def test_pagination_when_limit_exceeds_page() -> None:
    """When limit > page size, additional pages are fetched and concatenated."""
    with respx.mock:
        route = respx.get(API)
        route.side_effect = [
            httpx.Response(200, json=_page1()),  # page 1 -> 2 jobs
            httpx.Response(200, json=_page2()),  # page 2 -> 1 job
        ]
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=3), f)
    assert route.call_count == 2
    assert len(raws) == 3
    # page-order preserved: page1 jobs first, then page2.
    assert raws[2].source_job_id == _page2()["data"][0]["slug"]


async def test_limit_satisfied_by_first_page_skips_pagination() -> None:
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=_page1()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=1), f)
    assert route.call_count == 1  # no extra page fetched
    assert len(raws) == 1


def test_employment_mapping() -> None:
    from ergon_tracker.providers.arbeitnow import _employment

    assert _employment([]) is EmploymentType.UNKNOWN
    assert _employment(None) is EmploymentType.UNKNOWN
    assert _employment(["full time"]) is EmploymentType.FULL_TIME
    assert _employment(["Praktikum"]) is EmploymentType.INTERNSHIP
    assert _employment(["berufserfahren"]) is EmploymentType.OTHER  # unknown free-text


def test_parse_epoch_handles_garbage() -> None:
    from ergon_tracker.providers.arbeitnow import _parse_epoch

    assert _parse_epoch(None) is None
    assert _parse_epoch("not-a-number") is None
    assert _parse_epoch(1781589630) == datetime.fromtimestamp(1781589630, tz=timezone.utc)
