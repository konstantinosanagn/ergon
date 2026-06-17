"""Remotive provider unit tests (offline, respx)."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
import respx

from conftest import load_fixture
from ergon_tracker import RemoteType
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, SearchQuery
from ergon_tracker.providers.remotive import RemotiveProvider

pytestmark = pytest.mark.anyio

API = "https://remotive.com/api/remote-jobs"


def _provider() -> RemotiveProvider:
    return RemotiveProvider()


def test_matches_always_none_aggregator() -> None:
    assert RemotiveProvider.matches("remotive.com") is None
    assert RemotiveProvider.matches("https://remotive.com/api/remote-jobs") is None


async def test_fetch_reads_jobs_key() -> None:
    payload = json.loads(load_fixture("remotive_sample.json"))
    assert "00-warning" in payload  # fixture keeps the metadata head alongside jobs
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    assert route.called
    assert len(raws) == 3
    assert all(r.source == "remotive" and r.token is None for r in raws)
    # Metadata keys must never leak in as jobs.
    assert all("00-warning" not in r.payload for r in raws)
    assert raws[0].source_job_id == "2090881"
    assert raws[0].company == "Expion Health"


async def test_fetch_respects_query_limit() -> None:
    payload = json.loads(load_fixture("remotive_sample.json"))
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=2), f)
    # limit forwarded server-side AND enforced client-side.
    assert route.calls.last.request.url.params.get("limit") == "2"
    assert len(raws) == 2
    assert raws[0].source_job_id == "2090881"


async def test_all_jobs_are_remote() -> None:
    payload = json.loads(load_fixture("remotive_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    jobs = [_provider().normalize(r) for r in raws]
    assert jobs  # non-empty
    assert all(j.remote is RemoteType.REMOTE for j in jobs)
    assert all(loc.is_remote for j in jobs for loc in j.locations)


async def test_normalize_full_field_mapping() -> None:
    payload = json.loads(load_fixture("remotive_sample.json"))
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=payload))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    job = _provider().normalize(raws[0])
    assert job.source == "remotive"
    assert job.company == "Expion Health"
    assert job.title == "Business Transformation Lead"
    assert job.remote is RemoteType.REMOTE
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Artificial Intelligence"
    assert job.locations[0].raw == "USA"
    assert job.locations[0].is_remote is True
    # Remotive's publication_date carries no timezone -> parsed as naive (not invented as UTC).
    assert job.posted_at == datetime(2026, 6, 12, 21, 30, 58)
    assert job.apply_url == raws[0].payload["url"]
    assert job.description_html
    assert job.raw == raws[0].payload


def test_employment_unknown_when_missing() -> None:
    raw = _provider().fetch  # noqa: F841 - reference only
    from ergon_tracker.providers.remotive import _employment

    assert _employment(None) is EmploymentType.UNKNOWN
    assert _employment("") is EmploymentType.UNKNOWN
    assert _employment("contract") is EmploymentType.CONTRACT
    assert _employment("something_weird") is EmploymentType.OTHER
