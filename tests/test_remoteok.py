"""RemoteOK provider unit tests (offline, respx)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from jobspine import RemoteType
from jobspine.http import AsyncFetcher
from jobspine.models import SearchQuery
from jobspine.providers.remoteok import RemoteOKProvider

pytestmark = pytest.mark.anyio

API = "https://remoteok.com/api"


def _provider() -> RemoteOKProvider:
    return RemoteOKProvider()


def test_matches_always_none_aggregator() -> None:
    assert RemoteOKProvider.matches("remoteok.com") is None
    assert RemoteOKProvider.matches("https://remoteok.com/api") is None


async def test_fetch_skips_metadata_element() -> None:
    payload = json.loads(load_fixture("remoteok_sample.json"))
    assert "legal" in payload[0]  # fixture proves the metadata head is present
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    assert route.called
    # 4 elements in fixture (1 metadata + 3 jobs) -> 3 raw jobs.
    assert len(raws) == 3
    assert all(r.source == "remoteok" and r.token is None for r in raws)
    # The legal/metadata head must never become a job.
    assert all("legal" not in r.payload for r in raws)
    assert raws[0].source_job_id == "1133263"
    assert raws[0].company == "Women Builders Council"


async def test_fetch_respects_query_limit() -> None:
    payload = json.loads(load_fixture("remoteok_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=2), f)
    assert len(raws) == 2
    assert raws[0].source_job_id == "1133263"


async def test_normalize_full_field_mapping() -> None:
    payload = json.loads(load_fixture("remoteok_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    jobs = [_provider().normalize(r) for r in raws]

    job = jobs[0]
    assert job.source == "remoteok"
    assert job.company == "Women Builders Council"
    assert job.title == "sdf"
    assert job.remote is RemoteType.REMOTE  # always remote
    assert job.locations[0].is_remote is True
    assert job.posted_at == datetime(2026, 6, 11, 14, 43, 27, tzinfo=timezone.utc)
    assert job.salary is not None
    assert job.salary.min_amount == 60000
    assert job.salary.max_amount == 90000
    assert job.salary.currency == "USD"
    assert job.apply_url and job.description_html
    assert job.raw == raws[0].payload

    # Every normalized job is remote regardless of salary/location presence.
    assert all(j.remote is RemoteType.REMOTE for j in jobs)
    # Jobs with salary_min/max == 0 -> no Salary invented.
    assert jobs[1].salary is None
