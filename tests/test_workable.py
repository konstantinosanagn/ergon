"""Unit tests for the Workable provider (offline, respx-mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RawJob, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.workable import WorkableProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
WIDGET_URL = "https://apply.workable.com/api/v1/widget/accounts/zego"


def _fixture() -> dict:
    return json.loads((FIXTURES / "workable_sample.json").read_text())


# --- matches ----------------------------------------------------------------


def test_matches_recognizes_all_hosts() -> None:
    p = WorkableProvider
    assert p.matches("https://apply.workable.com/zego") == "zego"
    assert p.matches("https://apply.workable.com/api/v1/widget/accounts/zego") == "zego"
    assert p.matches("https://zego.workable.com") == "zego"
    assert p.matches("https://apply.workable.com/cleo/j/ABC123") == "cleo"
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


# --- fetch ------------------------------------------------------------------


async def test_fetch_builds_rawjobs_and_hits_widget_endpoint() -> None:
    with respx.mock:
        route = respx.get(WIDGET_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await WorkableProvider().fetch("zego", SearchQuery(), f)

        # Single call, no pagination.
        assert route.call_count == 1
        assert str(route.calls.last.request.url) == WIDGET_URL

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "workable"
    assert r0.source_job_id == "0897A038EC"  # shortcode
    assert r0.company == "Zego"  # account name
    assert r0.token == "zego"
    assert r0.url == "https://apply.workable.com/j/0897A038EC"
    assert r0.payload["title"] == "Claims Fraud Triage Handler"


async def test_fetch_falls_back_to_token_when_no_name() -> None:
    with respx.mock:
        respx.get(WIDGET_URL).mock(
            return_value=httpx.Response(200, json={"jobs": [{"shortcode": "X", "title": "T"}]})
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await WorkableProvider().fetch("zego", SearchQuery(), f)
    assert raws[0].company == "zego"


# --- normalize --------------------------------------------------------------


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(WIDGET_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await WorkableProvider().fetch("zego", SearchQuery(), f)

    provider = WorkableProvider()
    job = provider.normalize(raws[0])

    assert job.id == make_job_id("workable", "0897A038EC")
    assert job.title == "Claims Fraud Triage Handler"
    assert job.company == "Zego"
    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Halifax"
    assert loc.region == "England"
    assert loc.country == "United Kingdom"
    assert loc.is_remote is False
    assert job.remote is RemoteType.ONSITE  # telecommuting False, has location
    assert job.employment_type is EmploymentType.UNKNOWN  # empty employment_type
    assert job.department == "Claims and Fraud"
    assert job.salary is None
    assert job.apply_url == "https://apply.workable.com/j/0897A038EC"
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.posted_at.year == 2026


async def test_normalize_full_time_label() -> None:
    with respx.mock:
        respx.get(WIDGET_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await WorkableProvider().fetch("zego", SearchQuery(), f)

    # second sample posting carries employment_type "Full-time"
    job = WorkableProvider().normalize(raws[1])
    assert job.employment_type is EmploymentType.FULL_TIME


def test_normalize_telecommuting_is_remote() -> None:
    provider = WorkableProvider()
    payload = {
        "shortcode": "R1",
        "title": "Remote Engineer",
        "telecommuting": True,
        "employment_type": "Contract",
        "department": "Engineering",
        "url": "https://apply.workable.com/j/R1",
        "published_on": "2026-05-01",
        "country": "United States",
        "city": "Remote",
        "state": "",
        "locations": [{"country": "United States", "city": "Remote", "region": ""}],
    }
    job = provider.normalize(
        RawJob(source="workable", source_job_id="R1", company="Acme", payload=payload)
    )
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].is_remote is True
    assert job.employment_type is EmploymentType.CONTRACT


def test_normalize_unknown_label_is_other() -> None:
    provider = WorkableProvider()
    payload = {
        "shortcode": "Z1",
        "title": "Odd",
        "employment_type": "Seasonal",
        "country": "Spain",
        "city": "Madrid",
    }
    job = provider.normalize(
        RawJob(source="workable", source_job_id="Z1", company="Acme", payload=payload)
    )
    assert job.employment_type is EmploymentType.OTHER
    assert job.remote is RemoteType.ONSITE


def test_normalize_no_location() -> None:
    provider = WorkableProvider()
    payload = {"shortcode": "N1", "title": "No Loc"}
    job = provider.normalize(
        RawJob(source="workable", source_job_id="N1", company="Acme", payload=payload)
    )
    assert job.locations == []
    assert job.remote is RemoteType.UNKNOWN
