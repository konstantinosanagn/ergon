"""Unit tests for the Workday provider (offline, respx-mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobspine.http import AsyncFetcher
from jobspine.models import RawJob, RemoteType, SearchQuery
from jobspine.providers.workday import WorkdayProvider

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


pytestmark = pytest.mark.anyio

JOBS_URL = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
TOKEN = "nvidia|wd5|NVIDIAExternalCareerSite"


def _posting(i: int) -> dict[str, object]:
    return {
        "title": f"Engineer {i}",
        "externalPath": f"/job/US-CA-Santa-Clara/Engineer-{i}_JR{1000 + i}",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted 3 Days Ago",
        "bulletFields": [f"JR{1000 + i}"],
    }


def _paged_handler(total: int) -> tuple[list[int], object]:
    """Return (offsets_seen, side_effect) that serves ``total`` postings, 20 per page."""
    offsets_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset = int(body["offset"])
        offsets_seen.append(offset)
        page = [_posting(i) for i in range(offset, min(offset + 20, total))]
        return httpx.Response(200, json={"total": total, "jobPostings": page})

    return offsets_seen, handler


# --- token / matches --------------------------------------------------------


def test_token_parse_format_round_trip() -> None:
    token = WorkdayProvider.make_token("nvidia", "wd5", "NVIDIAExternalCareerSite")
    assert token == TOKEN
    tenant, wd, site = WorkdayProvider._parse_token(token)
    assert (tenant, wd, site) == ("nvidia", "wd5", "NVIDIAExternalCareerSite")


def test_parse_token_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        WorkdayProvider._parse_token("nvidia|wd5")


def test_matches_on_careers_url() -> None:
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/foo"
    assert WorkdayProvider.matches(url) == TOKEN


def test_matches_on_cxs_url() -> None:
    assert WorkdayProvider.matches(JOBS_URL) == TOKEN


def test_matches_tenant_appears_twice() -> None:
    url = "https://salesforce.wd12.myworkdayjobs.com/salesforce/External_Career_Site"
    assert WorkdayProvider.matches(url) == "salesforce|wd12|External_Career_Site"


def test_matches_bare_host_without_site_is_none() -> None:
    assert WorkdayProvider.matches("https://nvidia.wd5.myworkdayjobs.com/") is None


def test_matches_rejects_non_workday() -> None:
    assert WorkdayProvider.matches("https://boards.greenhouse.io/acme") is None


# --- fetch / concurrent pagination -----------------------------------------


async def test_fetch_paginates_concurrently_over_total() -> None:
    offsets_seen, handler = _paged_handler(total=45)
    with respx.mock:
        route = respx.post(JOBS_URL)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    # 45 results across pages of 20 → offsets 0, 20, 40 → 3 POSTs, all jobs returned.
    assert route.call_count == 3
    assert sorted(offsets_seen) == [0, 20, 40]
    assert len(raw) == 45
    assert all(isinstance(r, RawJob) for r in raw)
    # Stable ordering by offset preserved.
    assert raw[0].payload["title"] == "Engineer 0"
    assert raw[-1].payload["title"] == "Engineer 44"


async def test_fetch_builds_urls_and_ids() -> None:
    _, handler = _paged_handler(total=2)
    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    job = raw[0]
    assert job.source == "workday"
    assert job.company == "nvidia"
    assert job.token == TOKEN
    assert job.source_job_id == "/job/US-CA-Santa-Clara/Engineer-0_JR1000"
    assert job.url == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        "/job/US-CA-Santa-Clara/Engineer-0_JR1000"
    )


async def test_fetch_passes_keywords_as_search_text() -> None:
    seen_text: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_text.append(json.loads(request.content)["searchText"])
        return httpx.Response(200, json={"total": 1, "jobPostings": [_posting(0)]})

    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            await WorkdayProvider().fetch(TOKEN, SearchQuery(keywords="cuda kernel"), f)

    assert seen_text == ["cuda kernel"]


async def test_fetch_respects_limit_of_20_in_body() -> None:
    seen_limit: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_limit.append(json.loads(request.content)["limit"])
        return httpx.Response(200, json={"total": 1, "jobPostings": [_posting(0)]})

    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    assert seen_limit == [20]


# --- normalize --------------------------------------------------------------


async def test_normalize_maps_fields() -> None:
    _, handler = _paged_handler(total=1)
    with respx.mock:
        respx.post(JOBS_URL).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raw = await WorkdayProvider().fetch(TOKEN, SearchQuery(), f)

    provider = WorkdayProvider()
    job = provider.normalize(raw[0])
    assert job.title == "Engineer 0"
    assert job.company == "nvidia"
    assert job.source == "workday"
    assert job.apply_url == raw[0].url
    assert job.locations[0].raw == "US, CA, Santa Clara"
    assert job.remote is RemoteType.UNKNOWN
    assert job.posted_at is not None  # "Posted 3 Days Ago" is parseable
    assert job.raw["externalPath"] == raw[0].payload["externalPath"]


def test_normalize_detects_remote() -> None:
    raw = RawJob(
        source="workday",
        source_job_id="/job/x_JR1",
        company="nvidia",
        token=TOKEN,
        url="https://example",
        payload={"title": "Remote Engineer", "locationsText": "Remote, US", "postedOn": "weird"},
    )
    job = WorkdayProvider().normalize(raw)
    assert job.remote is RemoteType.REMOTE
    assert job.posted_at is None  # unparseable postedOn → None, never invented


# --- fixture sanity ---------------------------------------------------------


def test_real_fixture_normalizes() -> None:
    data = json.loads(load_fixture("workday_sample.json"))
    posting = data["jobPostings"][0]
    raw = RawJob(
        source="workday",
        source_job_id=posting["externalPath"],
        company="nvidia",
        token=TOKEN,
        url=f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite{posting['externalPath']}",
        payload=posting,
    )
    job = WorkdayProvider().normalize(raw)
    assert job.title == posting["title"]
    assert job.company == "nvidia"
    assert data["total"] == 2000
