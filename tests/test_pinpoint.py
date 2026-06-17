"""Unit tests for the Pinpoint provider (respx-mocked, offline).

Fixture ``pinpoint_jobs.json`` is a trimmed capture of the live
``https://aawdc.pinpointhq.com/postings.json`` response (token "aawdc").
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SalaryInterval, SearchQuery, make_job_id
from ergon_tracker.providers.pinpoint import PinpointProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BOARD_URL = "https://aawdc.pinpointhq.com/postings.json"


def _fixture() -> dict:
    return json.loads((FIXTURES / "pinpoint_jobs.json").read_text())


def test_matches_recognizes_hosts() -> None:
    p = PinpointProvider
    assert p.matches("https://aawdc.pinpointhq.com/postings.json") == "aawdc"
    assert p.matches("https://acme.pinpointhq.com") == "acme"
    assert p.matches("acme.pinpointhq.com/en/postings/abc-123") == "acme"
    assert p.matches("https://www.pinpointhq.com") is None
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PinpointProvider().fetch("aawdc", SearchQuery(), f)

        assert str(route.calls.last.request.url) == BOARD_URL

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "pinpoint"
    assert r0.source_job_id == "515428"
    assert r0.company == "aawdc"
    assert r0.token == "aawdc"
    assert r0.url == "https://aawdc.pinpointhq.com/en/postings/fca592d6-2561-4b2a-88b0-15d2b375971a"
    assert r0.payload["title"] == "Warehouse/Fleet Manager"


async def test_normalize_maps_comp_visible_yearly() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PinpointProvider().fetch("aawdc", SearchQuery(), f)

    job = PinpointProvider().normalize(raws[0])

    assert job.id == make_job_id("pinpoint", "515428")
    assert job.source == "pinpoint"
    assert job.source_job_id == "515428"
    assert job.title == "Warehouse/Fleet Manager"
    assert job.company == "aawdc"
    assert job.apply_url == (
        "https://aawdc.pinpointhq.com/en/postings/fca592d6-2561-4b2a-88b0-15d2b375971a"
    )

    # location: city/province empty, but the display name is present.
    assert len(job.locations) == 1
    assert job.locations[0].raw == "North County"

    # workplace_type "onsite" -> ONSITE
    assert job.remote is RemoteType.ONSITE
    # employment_type "full_time" -> FULL_TIME
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Transportation"

    # compensation_visible + numeric min/max -> Salary mapped, frequency "year" -> YEAR
    assert job.salary is not None
    assert job.salary.min_amount == 80000.0
    assert job.salary.max_amount == 100000.0
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR

    # No created/published timestamp in the feed.
    assert job.posted_at is None

    assert job.description_html is not None
    assert job.description_text is not None and len(job.description_text) > 0
    assert job.raw == raws[0].payload


async def test_normalize_comp_visible_hourly_and_employment() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PinpointProvider().fetch("aawdc", SearchQuery(), f)

    job = PinpointProvider().normalize(raws[1])
    # "contract_to_hire" -> CONTRACT
    assert job.employment_type is EmploymentType.CONTRACT
    assert job.salary is not None
    assert job.salary.interval is SalaryInterval.HOUR
    assert job.salary.currency == "USD"


async def test_normalize_comp_hidden_has_no_salary() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PinpointProvider().fetch("aawdc", SearchQuery(), f)

    job = PinpointProvider().normalize(raws[2])
    assert job.source_job_id == "217178"
    # compensation_visible is False -> no salary even though a range is mentioned in prose.
    assert job.salary is None
    # "contract" -> CONTRACT
    assert job.employment_type is EmploymentType.CONTRACT
    # location with a real city/province.
    loc = job.locations[0]
    assert loc.city == "Varies by Listing"
    assert loc.region == "Maryland"


async def test_fetch_empty_or_missing_data() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PinpointProvider().fetch("aawdc", SearchQuery(), f)
    assert raws == []
