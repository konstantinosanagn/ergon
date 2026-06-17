"""Unit tests for the Eightfold provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.eightfold import EightfoldProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://fcx.eightfold.ai/api/apply/v2/jobs"


def _fixture() -> dict:
    return json.loads((FIXTURES / "eightfold_jobs.json").read_text())


def _mock(respx_mock: respx.MockRouter) -> None:
    """Discovery GET (no params) returns the config dict (with domain + positions);
    a second page returns empty positions so pagination terminates."""
    payload = _fixture()
    config = {"domain": payload["domain"]}  # no-param discovery response
    empty = {"domain": payload["domain"], "count": payload["count"], "positions": []}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "domain" not in params:
            return httpx.Response(200, json=config)
        if params.get("start") == "0":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=empty)

    respx_mock.get(url__startswith=API).mock(side_effect=handler)


def test_matches_recognizes_host() -> None:
    p = EightfoldProvider
    assert p.matches("https://fcx.eightfold.ai/careers") == "fcx"
    assert p.matches("fcx.eightfold.ai/api/apply/v2/jobs") == "fcx"
    assert p.matches("https://whirlpool.eightfold.ai") == "whirlpool"
    assert p.matches("https://www.eightfold.ai") is None
    assert p.matches("https://app.eightfold.ai") is None
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(), f)

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "eightfold"
    assert r0.source_job_id == "42478672"
    assert r0.company == "fcx"
    assert r0.token == "fcx"
    assert r0.url == "https://talent.fmjobs.com/careers/job/42478672"
    assert r0.payload["name"] == "Manager Engineering - Electrical"


async def test_normalize_onsite_job() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(), f)

    job = EightfoldProvider().normalize(raws[0])

    assert job.id == make_job_id("eightfold", "42478672")
    assert job.title == "Manager Engineering - Electrical"
    assert job.company == "fcx"
    assert job.department == "Engineering Services"
    assert job.apply_url == "https://talent.fmjobs.com/careers/job/42478672"
    assert job.remote is RemoteType.ONSITE
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.salary is None
    assert job.description_html is None
    assert job.description_text is None
    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.raw == "New Orleans, LA USA 70112"
    assert loc.is_remote is False
    # t_create = 1781660892 epoch seconds
    assert job.posted_at is not None
    assert job.posted_at.tzinfo is not None
    assert int(job.posted_at.astimezone(timezone.utc).timestamp()) == 1781660892
    assert job.raw == raws[0].payload


async def test_normalize_second_job_department_and_url() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(), f)

    job = EightfoldProvider().normalize(raws[1])
    assert job.title == "Chief Biodiversity and Wildlife"
    assert job.department == "Environmental"
    assert job.apply_url == "https://talent.fmjobs.com/careers/job/42477103"
    assert job.locations[0].raw == "Phoenix, AZ USA 85040"


async def test_fetch_respects_query_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(limit=2), f)
    assert len(raws) == 2


async def test_locked_tenant_returns_empty() -> None:
    """A locked tenant returns {"message": ...} for both discovery and pages -> []."""
    locked = {"message": "Not authorized for PCSX"}
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith="https://whirlpool.eightfold.ai/api/apply/v2/jobs").mock(
            return_value=httpx.Response(200, json=locked)
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("whirlpool", SearchQuery(), f)
    assert raws == []
