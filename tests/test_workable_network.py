"""Workable network-aggregator provider unit tests (offline, respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon import EmploymentType, RemoteType
from ergon.models import SearchQuery
from ergon.providers.workable_network import WorkableNetworkProvider

pytestmark = pytest.mark.anyio

API = "https://jobs.workable.com/api/v1/jobs"


def _job(jid: str, **over) -> dict:
    base = {
        "id": jid,
        "title": f"Engineer {jid}",
        "company": {"title": "Acme", "website": "https://www.acme.com/careers"},
        "workplace": "remote",
        "employmentType": "Full-time",
        "location": {"city": "Berlin", "subregion": None, "countryName": "Germany"},
        "created": "2026-06-19T16:39:52.663Z",
        "url": f"https://jobs.workable.com/view/{jid}/x",
        "description": "<p>Build things.</p>",
    }
    base.update(over)
    return base


def test_matches_always_none_aggregator() -> None:
    assert WorkableNetworkProvider.matches("jobs.workable.com") is None


async def test_fetch_paginates_with_cursor_only_after_first_page() -> None:
    page1 = {"jobs": [_job("1"), _job("2")], "nextPageToken": "TOK"}
    page2 = {"jobs": [_job("3")], "nextPageToken": None}
    calls: list[dict] = []

    def responder(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        return httpx.Response(200, json=page1 if "nextPageToken" not in params else page2)

    with respx.mock:
        respx.get(API).mock(side_effect=responder)
        async with httpx.AsyncClient() as _c:
            from ergon.http import AsyncFetcher

            async with AsyncFetcher(per_host_rate=100) as f:
                raws = await WorkableNetworkProvider().fetch(
                    "", SearchQuery(keywords="python", location="remote"), f
                )

    assert [r.source_job_id for r in raws] == ["1", "2", "3"]
    # First call carries the query; second call carries ONLY the cursor (no query reset).
    assert calls[0].get("query") == "python" and calls[0].get("location") == "remote"
    assert calls[1].get("nextPageToken") == "TOK" and "query" not in calls[1]


async def test_fetch_respects_limit() -> None:
    page1 = {"jobs": [_job("1"), _job("2"), _job("3")], "nextPageToken": "TOK"}
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=page1))
        from ergon.http import AsyncFetcher

        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await WorkableNetworkProvider().fetch("", SearchQuery(limit=2), f)
    assert len(raws) == 2


async def test_fetch_stops_when_page_adds_no_new_ids() -> None:
    # A non-advancing cursor returns the same ids forever; we must not loop past it.
    page = {"jobs": [_job("1")], "nextPageToken": "SAME"}
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=page))
        from ergon.http import AsyncFetcher

        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await WorkableNetworkProvider().fetch("", SearchQuery(), f)
    assert len(raws) == 1
    assert route.call_count == 2  # page 1 (new) + page 2 (all-dupes -> stop)


async def test_normalize_maps_fields() -> None:
    prov = WorkableNetworkProvider()
    raw = prov.fetch  # noqa: F841 - keep ref to ensure provider import side effects
    rj_list_page = {"jobs": [_job("42")], "nextPageToken": None}
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=rj_list_page))
        from ergon.http import AsyncFetcher

        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await prov.fetch("", SearchQuery(), f)
    job = prov.normalize(raws[0])
    assert job.source == "workable_network"
    assert job.company == "Acme"
    assert job.company_domain == "acme.com"
    assert job.title == "Engineer 42"
    assert job.remote is RemoteType.REMOTE
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.locations[0].country == "Germany"
    assert job.locations[0].city == "Berlin"
    assert job.apply_url == "https://jobs.workable.com/view/42/x"
