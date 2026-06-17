"""Unit tests for the join.com provider (respx-mocked, offline).

Fixture ``join_page.html`` is a trimmed careers page whose ``__NEXT_DATA__`` blob mirrors the
real ``https://join.com/companies/{token}`` shape (company + jobs.items + pagination).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.join import JoinProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://join.com/companies/onetwosocial"


def _html() -> str:
    return (FIXTURES / "join_page.html").read_text()


def test_matches_recognizes_company_urls() -> None:
    p = JoinProvider
    assert p.matches("https://join.com/companies/onetwosocial") == "onetwosocial"
    assert p.matches("join.com/companies/acme-gmbh/jobs/123-role") == "acme-gmbh"
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)
        assert route.called

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "join"
    assert r0.source_job_id == "16084660"
    assert r0.company == "OneTwoSocial"
    assert r0.token == "onetwosocial"
    assert r0.url == (
        "https://join.com/companies/onetwosocial/jobs/16084660-social-motion-designer"
    )


async def test_normalize_first_job() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)

    job = JoinProvider().normalize(raws[0])
    assert job.id == make_job_id("join", "16084660")
    assert job.title == "Social Motion Designer"
    assert job.company == "OneTwoSocial"
    assert job.apply_url.endswith("/jobs/16084660-social-motion-designer")
    assert job.locations and job.locations[0].city == "Munich"
    assert job.locations[0].country == "Germany"
    assert job.remote is RemoteType.ONSITE
    assert job.employment_type is EmploymentType.FULL_TIME  # "Employee"
    assert job.department == "Design"
    assert job.salary is None
    assert job.description_text is None
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert (job.posted_at.year, job.posted_at.month) == (2026, 4)


async def test_normalize_second_job_intern_remote() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)

    job = JoinProvider().normalize(raws[1])
    assert job.title == "Marketing Intern"
    assert job.employment_type is EmploymentType.INTERNSHIP  # "Intern"
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].city == "Berlin"


async def test_fetch_no_next_data_returns_empty() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html>no data</html>"))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)
    assert raws == []
