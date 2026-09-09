"""Unit tests for the Jobvite provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import RemoteType, SearchQuery, make_job_id
from ergon.providers.jobvite import JobviteProvider

pytestmark = pytest.mark.anyio

COMPANY = "buckman"
VIEWALL = f"https://jobs.jobvite.com/{COMPANY}/jobs/viewall"


def _card(slug: str, title: str, loc: str) -> str:
    return (
        f'<li class="row"><a href="/{COMPANY}/job/{slug}">'
        f'<div class="jv-job-list-name">{title}</div>'
        f'<div class="jv-job-list-location">{loc}</div></a></li>'
    )


def _page() -> str:
    cards = _card("AAA11111", "Software Engineer", "Memphis, TN, United States") + _card(
        "BBB22222", "Remote Data Analyst", "Remote - US"
    )
    return f"<html><body><ul>{cards}</ul></body></html>"


def _mock(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__startswith=VIEWALL).mock(return_value=httpx.Response(200, html=_page()))


def test_matches_company_and_careers_paths() -> None:
    p = JobviteProvider
    assert p.matches("https://jobs.jobvite.com/buckman/jobs/viewall") == "buckman"
    assert p.matches("https://jobs.jobvite.com/buckman/job/oD2jAfwi") == "buckman"
    # Engage redirect form /careers/{company}/...
    assert p.matches("https://jobs.jobvite.com/careers/internetbrands/jobs") == "internetbrands"
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_parses_cards() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobviteProvider().fetch(COMPANY, SearchQuery(), f)
    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "jobvite"
    assert r0.source_job_id == "AAA11111"
    assert r0.company == "buckman"
    assert r0.url == "https://jobs.jobvite.com/buckman/job/AAA11111"


async def test_normalize_location_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobviteProvider().fetch(COMPANY, SearchQuery(), f)

    onsite = JobviteProvider().normalize(raws[0])
    assert onsite.id == make_job_id("jobvite", "AAA11111")
    assert onsite.title == "Software Engineer"
    assert onsite.locations[0].raw == "Memphis, TN, United States"
    assert onsite.remote is RemoteType.UNKNOWN
    assert onsite.posted_at is None
    assert onsite.salary is None

    remote = JobviteProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobviteProvider().fetch(COMPANY, SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_dead_tenant_returns_empty() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith="https://jobs.jobvite.com/nope/jobs/viewall").mock(
            return_value=httpx.Response(404, html="not found")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JobviteProvider().fetch("nope", SearchQuery(), f)
    assert raws == []
