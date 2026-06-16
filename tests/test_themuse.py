"""The Muse provider unit tests (offline, respx-mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from jobspine import RemoteType
from jobspine.http import AsyncFetcher
from jobspine.models import EmploymentType, SearchQuery
from jobspine.providers.themuse import TheMuseProvider

pytestmark = pytest.mark.anyio

API = "https://www.themuse.com/api/public/jobs"


def _provider() -> TheMuseProvider:
    return TheMuseProvider()


def _job(i: int, **over: object) -> dict[str, object]:
    job: dict[str, object] = {
        "id": 1000 + i,
        "name": f"Engineer {i}",
        "company": {"name": f"Company {i}"},
        "locations": [{"name": "New York, NY"}],
        "levels": [{"name": "Senior Level", "short_name": "senior"}],
        "type": "external",
        "refs": {"landing_page": f"https://www.themuse.com/jobs/company/engineer-{i}"},
        "publication_date": "2026-06-11T00:12:35Z",
        "contents": f"<p>Job {i} description</p>",
        "categories": [{"name": "Engineering"}],
    }
    job.update(over)
    return job


def _paged_handler(total: int) -> tuple[list[int], object]:
    """Serve ``total`` jobs, PAGE_SIZE per page, keyed off the 1-indexed ``page`` param."""
    pages_seen: list[int] = []
    size = TheMuseProvider.PAGE_SIZE

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages_seen.append(page)
        start = (page - 1) * size
        page_jobs = [_job(i) for i in range(start, min(start + size, total))]
        page_count = max(1, -(-total // size))
        return httpx.Response(
            200,
            json={
                "page": page,
                "page_count": page_count,
                "items_per_page": size,
                "total": total,
                "results": page_jobs,
            },
        )

    return pages_seen, handler


# --- matches ---------------------------------------------------------------


def test_matches_always_none_aggregator() -> None:
    assert TheMuseProvider.matches("themuse.com") is None
    assert TheMuseProvider.matches("https://www.themuse.com/api/public/jobs") is None


# --- fetch / concurrent pagination -----------------------------------------


async def test_fetch_paginates_concurrently_to_satisfy_limit() -> None:
    pages_seen, handler = _paged_handler(total=200)
    with respx.mock:
        route = respx.get(API)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=45), f)

    # limit=45 -> ceil(45/20)=3 pages fetched (1,2,3), then sliced to 45.
    assert route.call_count == 3
    assert sorted(pages_seen) == [1, 2, 3]
    assert len(raws) == 45
    # Page order preserved across the concurrent fan-out.
    assert raws[0].payload["name"] == "Engineer 0"
    assert raws[-1].payload["name"] == "Engineer 44"
    assert all(r.source == "themuse" and r.token is None for r in raws)


async def test_fetch_caps_at_max_pages_without_limit() -> None:
    _, handler = _paged_handler(total=10_000)
    with respx.mock:
        route = respx.get(API)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=200) as f:
            raws = await _provider().fetch("", SearchQuery(), f)

    assert route.call_count == TheMuseProvider.MAX_PAGES
    assert len(raws) == TheMuseProvider.MAX_PAGES * TheMuseProvider.PAGE_SIZE


async def test_fetch_single_page_for_small_limit() -> None:
    pages_seen, handler = _paged_handler(total=100)
    with respx.mock:
        route = respx.get(API)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=5), f)

    # ceil(5/20)=1 page; sliced to 5.
    assert route.call_count == 1
    assert pages_seen == [1]
    assert len(raws) == 5


async def test_fetch_passes_location_filter() -> None:
    seen_params: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(request.url.params.get("location"))
        return httpx.Response(200, json={"results": [_job(0)]})

    with respx.mock:
        respx.get(API).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            await _provider().fetch("", SearchQuery(location="Flexible / Remote", limit=1), f)

    assert seen_params == ["Flexible / Remote"]


# --- normalize -------------------------------------------------------------


async def test_normalize_full_field_mapping() -> None:
    pages_seen, handler = _paged_handler(total=1)
    with respx.mock:
        respx.get(API).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)

    job = _provider().normalize(raws[0])
    assert job.source == "themuse"
    assert job.source_job_id == "1000"
    assert job.company == "Company 0"
    assert job.title == "Engineer 0"
    assert job.locations[0].raw == "New York, NY"
    assert job.locations[0].is_remote is False
    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.UNKNOWN  # "external" is not an employment type
    assert job.department == "Engineering"
    assert job.apply_url == "https://www.themuse.com/jobs/company/engineer-0"
    assert job.description_html == "<p>Job 0 description</p>"
    assert job.posted_at == datetime(2026, 6, 11, 0, 12, 35, tzinfo=timezone.utc)
    assert job.raw == raws[0].payload


def test_normalize_detects_remote_location() -> None:
    raw = _provider()._to_raw(_job(0, locations=[{"name": "Flexible / Remote"}]))
    job = _provider().normalize(raw)
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].is_remote is True


def test_normalize_maps_employment_type() -> None:
    raw = _provider()._to_raw(_job(0, type="Full-Time"))
    assert _provider().normalize(raw).employment_type is EmploymentType.FULL_TIME

    raw = _provider()._to_raw(_job(0, type="Internship"))
    assert _provider().normalize(raw).employment_type is EmploymentType.INTERNSHIP


def test_normalize_missing_fields_are_safe() -> None:
    raw = _provider()._to_raw({"id": 7})
    job = _provider().normalize(raw)
    assert job.source_job_id == "7"
    assert job.company == ""
    assert job.title == ""
    assert job.locations == []
    assert job.remote is RemoteType.UNKNOWN
    assert job.department is None
    assert job.apply_url is None
    assert job.posted_at is None


# --- fixture sanity --------------------------------------------------------


def test_real_fixture_normalizes() -> None:
    data = json.loads(load_fixture("themuse_sample.json"))
    assert data["page"] == 1
    assert data["results"]
    raws = [_provider()._to_raw(j) for j in data["results"]]
    jobs = [_provider().normalize(r) for r in raws]
    first = jobs[0]
    assert first.source == "themuse"
    assert first.title  # real title present
    assert first.company  # real company present
    assert first.apply_url and first.apply_url.startswith("https://www.themuse.com")
