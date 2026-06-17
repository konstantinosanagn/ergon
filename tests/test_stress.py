"""Stress + resilience: high concurrency fan-out, malformed payloads, dedup at scale."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker import JobPosting, Location, RemoteType, Salary, SearchQuery
from ergon_tracker.dedup import deduplicate
from ergon_tracker.engine import run_search
from ergon_tracker.http import AsyncFetcher

pytestmark = pytest.mark.anyio


async def test_concurrent_fanout_over_many_boards() -> None:
    """100 greenhouse boards fetched in one search; all complete, all jobs collected."""
    n = 100
    with respx.mock(assert_all_called=False) as mock:
        for i in range(n):
            token = f"co{i}"
            payload = {
                "jobs": [
                    {
                        "id": i,
                        "title": f"Engineer {i}",
                        "absolute_url": f"https://boards.greenhouse.io/{token}/jobs/{i}",
                        "updated_at": "2026-06-01T00:00:00Z",
                        "location": {"name": "Remote"},
                        "content": "x",
                        "departments": [],
                        "offices": [],
                        "metadata": [],
                    }
                ]
            }
            mock.get(
                url__startswith=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
            ).mock(return_value=httpx.Response(200, json=payload))

        companies = [f"https://boards.greenhouse.io/co{i}" for i in range(n)]
        query = SearchQuery(companies=companies)
        async with AsyncFetcher(concurrency=16, per_host_rate=1000) as fetcher:
            result = await run_search(query, fetcher)

    assert len(result.jobs) == n
    gh = next(h for h in result.health if h.source == "greenhouse")
    assert gh.ok
    assert gh.count == n


async def test_malformed_payload_marks_source_failed_not_crash() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, text="<html>not json</html>")
        )
        query = SearchQuery(companies=["stripe.com"])
        async with AsyncFetcher(per_host_rate=100, retries=2) as fetcher:
            result = await run_search(query, fetcher)
    health = {h.source: h for h in result.health}
    assert not health["greenhouse"].ok
    assert health["greenhouse"].error is not None
    assert result.jobs == []


async def test_empty_board_is_ok_with_zero_count() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )
        query = SearchQuery(companies=["stripe.com"])
        async with AsyncFetcher(per_host_rate=100) as fetcher:
            result = await run_search(query, fetcher)
    health = {h.source: h for h in result.health}
    assert health["greenhouse"].ok
    assert health["greenhouse"].count == 0


def _job(source: str, sid: str, title: str, *, salary: Salary | None = None) -> JobPosting:
    return JobPosting.create(
        source=source,
        source_job_id=sid,
        company="Acme Inc",
        title=title,
        locations=[Location(city="Berlin")],
        remote=RemoteType.REMOTE,
        salary=salary,
    )


def test_dedup_merges_same_role_across_sources_ats_wins() -> None:
    ats = _job("greenhouse", "1", "Senior Backend Engineer")
    agg = _job(
        "remoteok", "x", "Sr. Backend Engineer", salary=Salary(min_amount=120000, currency="USD")
    )
    merged = deduplicate([ats, agg])
    assert len(merged) == 1
    primary = merged[0]
    assert primary.source == "greenhouse"  # ATS outranks aggregator
    sources = {p.source for p in primary.provenance}
    assert sources == {"greenhouse", "remoteok"}
    # Missing salary on the ATS record is backfilled from the aggregator.
    assert primary.salary is not None
    assert primary.salary.min_amount == 120000


def test_dedup_keeps_distinct_roles() -> None:
    a = _job("greenhouse", "1", "Backend Engineer")
    b = _job("greenhouse", "2", "Frontend Designer")
    assert len(deduplicate([a, b])) == 2


async def test_dedup_runs_in_search_pipeline() -> None:
    """Same role on a greenhouse board and remoteok collapses to one in a real search."""
    gh_payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Backend Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "updated_at": "2026-06-01T00:00:00Z",
                "location": {"name": "Berlin"},
                "content": "x",
                "departments": [],
                "offices": [{"name": "Berlin", "location": "Berlin"}],
                "metadata": [],
            }
        ]
    }
    remoteok_payload = [
        {"legal": "metadata element to skip"},
        {
            "id": "9",
            "company": "Acme",
            "position": "Sr Backend Engineer",
            "description": "x",
            "location": "Berlin",
            "date": "2026-06-01T00:00:00+00:00",
            "url": "https://remoteok.com/remote-jobs/9",
            "apply_url": "https://remoteok.com/remote-jobs/9",
            "tags": [],
        },
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=gh_payload)
        )
        mock.get(url__startswith="https://remoteok.com/api").mock(
            return_value=httpx.Response(200, json=remoteok_payload)
        )
        # 'acme' isn't in the seed registry; reference it via its greenhouse host so resolve()
        # matches it by URL pattern. remoteok is an aggregator included via sources=.
        query = SearchQuery(
            keywords="backend engineer",
            sources=["greenhouse", "remoteok"],
            companies=["boards.greenhouse.io/acme"],
        )
        async with AsyncFetcher(per_host_rate=100) as fetcher:
            result = await run_search(query, fetcher)

    backend = [j for j in result.jobs if "backend" in j.title.lower()]
    assert len(backend) == 1
    assert {p.source for p in backend[0].provenance} == {"greenhouse", "remoteok"}
