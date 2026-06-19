"""Unit tests for the Radancy/TalentBrew provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import SearchQuery, make_job_id
from ergon_tracker.providers.radancy import RadancyProvider

pytestmark = pytest.mark.anyio

RESULTS = "https://jobs.acme.com/search-jobs/results"


def _card(jid: str, title: str, city: str, loc: str, cat: str) -> str:
    return (
        f'<li><a href="/job/{city}/{title.lower().replace(" ", "-")}/9/{jid}" data-job-id="{jid}">'
        f"<h2>{title}</h2>"
        f'<span class="job-location">{loc}</span>'
        f'<span class="job-category">{cat}</span></a></li>'
    )


def _page(cards: list[str]) -> dict:
    return {"results": "<ul>" + "".join(cards) + "</ul>" if cards else "", "hasJobs": bool(cards)}


def _mock(respx_mock: respx.MockRouter, pages: list[list[str]]) -> None:
    def _resp(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("CurrentPage", "1"))
        cards = pages[page - 1] if 1 <= page <= len(pages) else []
        return httpx.Response(200, json=_page(cards))

    respx_mock.get(RESULTS).mock(side_effect=_resp)


def test_parse_token() -> None:
    assert RadancyProvider._parse("jobs.acme.com|Acme") == ("jobs.acme.com", "Acme")
    assert RadancyProvider._parse("https://jobs.acme.com/|Acme") == ("jobs.acme.com", "Acme")
    assert RadancyProvider._parse("jobs.acme.com") == ("jobs.acme.com", None)


async def test_fetch_paginates_and_normalizes() -> None:
    pages = [
        [
            _card("111", "Senior Data Engineer", "phoenix", "Phoenix, AZ", "Engineering"),
            _card("222", "Remote Marketing Lead", "remote", "Remote", "Marketing"),
        ],
        [_card("333", "Analyst, Finance", "miami", "Miami, FL", "Finance")],
    ]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch("jobs.acme.com|Acme", SearchQuery(), f)

    assert len(raws) == 3
    assert {r.company for r in raws} == {"Acme"}
    j0 = RadancyProvider().normalize(raws[0])
    assert j0.id == make_job_id("radancy", "111")
    assert j0.title == "Senior Data Engineer"
    assert j0.locations[0].raw == "Phoenix, AZ"
    assert j0.department == "Engineering"
    assert j0.apply_url == "https://jobs.acme.com/job/phoenix/senior-data-engineer/9/111"
    jr = RadancyProvider().normalize(raws[1])
    assert jr.remote.value == "remote"


async def test_fetch_respects_limit() -> None:
    pages = [[_card(str(i), f"Role {i}", "nyc", "New York, NY", "Ops") for i in range(100)]]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch("jobs.acme.com|Acme", SearchQuery(limit=4), f)
    assert len(raws) == 4


async def test_empty_page_stops() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock, [[]])  # first page already empty
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch("jobs.acme.com|Acme", SearchQuery(), f)
    assert raws == []
