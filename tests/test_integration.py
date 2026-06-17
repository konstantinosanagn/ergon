"""End-to-end integration: orchestrator fans out across mocked sources, dedups, reports health."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker import SearchQuery
from ergon_tracker.engine import run_search
from ergon_tracker.http import AsyncFetcher

pytestmark = pytest.mark.anyio


def _greenhouse_payload() -> dict:
    return {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Backend Engineer",
                "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1",
                "updated_at": "2026-06-01T00:00:00Z",
                "location": {"name": "Berlin, Germany"},
                "content": "Build APIs.",
                "departments": [{"name": "Engineering"}],
                "offices": [{"name": "Berlin", "location": "Berlin, Germany"}],
                "metadata": [],
            }
        ]
    }


def _lever_payload() -> list[dict]:
    return [
        {
            "id": "abc",
            "text": "Data Scientist",
            "categories": {"team": "Data", "location": "Remote", "commitment": "Full-time"},
            "hostedUrl": "https://jobs.lever.co/spotify/abc",
            "applyUrl": "https://jobs.lever.co/spotify/abc/apply",
            "createdAt": 1717200000000,
            "descriptionPlain": "Do data science.",
            "workplaceType": "remote",
        }
    ]


async def test_run_search_across_two_companies_builds_result_and_health() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json=_greenhouse_payload())
        )
        mock.get(url__startswith="https://api.lever.co/v0/postings/spotify").mock(
            return_value=httpx.Response(200, json=_lever_payload())
        )
        query = SearchQuery(companies=["stripe.com", "spotify.com"])
        async with AsyncFetcher(per_host_rate=100) as fetcher:
            result = await run_search(query, fetcher)

    titles = {j.title for j in result.jobs}
    assert "Senior Backend Engineer" in titles
    assert "Data Scientist" in titles
    health_by_source = {h.source: h for h in result.health}
    assert health_by_source["greenhouse"].ok
    assert health_by_source["lever"].ok
    assert health_by_source["greenhouse"].count == 1


async def test_one_failing_source_degrades_gracefully() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json=_greenhouse_payload())
        )
        # Lever returns persistent 500 -> retries exhaust -> source marked failed, search survives
        mock.get(url__startswith="https://api.lever.co/v0/postings/spotify").mock(
            return_value=httpx.Response(500)
        )
        query = SearchQuery(companies=["stripe.com", "spotify.com"])
        async with AsyncFetcher(per_host_rate=100, retries=2) as fetcher:
            result = await run_search(query, fetcher)

    health = {h.source: h for h in result.health}
    assert health["greenhouse"].ok
    assert not health["lever"].ok
    assert health["lever"].error is not None
    # The good source's jobs still come through.
    assert any(j.source == "greenhouse" for j in result.jobs)


async def test_keyword_filter_applies_clientside() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json=_greenhouse_payload())
        )
        query = SearchQuery(keywords="backend", companies=["stripe.com"])
        async with AsyncFetcher(per_host_rate=100) as fetcher:
            result = await run_search(query, fetcher)
        assert len(result.jobs) == 1

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json=_greenhouse_payload())
        )
        query2 = SearchQuery(keywords="marketing", companies=["stripe.com"])
        async with AsyncFetcher(per_host_rate=100) as fetcher:
            result2 = await run_search(query2, fetcher)
        assert len(result2.jobs) == 0


def test_public_search_stays_callable_after_use() -> None:
    """Regression: the `search` function export must not be shadowed by the engine module
    after the first call (they used to collide as `ergon_tracker.search`)."""
    import ergon_tracker

    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json=_greenhouse_payload())
        )
        ergon_tracker.search(companies=["stripe.com"], limit=1)
        assert callable(ergon_tracker.search)
        # Second call would raise "'module' object is not callable" under the old bug.
        result = ergon_tracker.search(companies=["stripe.com"], limit=1)
    assert len(result) >= 0


async def test_sources_filter_restricts_providers() -> None:
    with respx.mock(assert_all_called=False) as mock:
        gh = mock.get(
            url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
        ).mock(return_value=httpx.Response(200, json=_greenhouse_payload()))
        lever = mock.get(url__startswith="https://api.lever.co/v0/postings/spotify").mock(
            return_value=httpx.Response(200, json=_lever_payload())
        )
        query = SearchQuery(companies=["stripe.com", "spotify.com"], sources=["greenhouse"])
        async with AsyncFetcher(per_host_rate=100) as fetcher:
            result = await run_search(query, fetcher)
        assert gh.called
        assert not lever.called
        assert {h.source for h in result.health} == {"greenhouse"}
