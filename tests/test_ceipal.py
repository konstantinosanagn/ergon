"""Unit tests for the Ceipal provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import SearchQuery, make_job_id
from ergon.providers.ceipal import CeipalProvider

pytestmark = pytest.mark.anyio

AK = "APIKEY123"
CP = "CPID456"
URL = f"https://careerapi.ceipal.com/{AK}/CareerPortalJobPostings/"


def _job(
    jid: int, title: str, state: str, country: str = "United States", remote: str = "2"
) -> dict:
    return {
        "job_id": jid,
        "id": f"enc-{jid}",
        "position_title": title,
        "public_job_title": f"public {title}",
        "city": "",
        "state": state,
        "country": country,
        "job_code": f"JPC-{jid}",
        "client": "Acme Staffing",
        "remote_opportunities": remote,
    }


def _mock(respx_mock: respx.MockRouter, pages: list[list[dict]]) -> None:
    n = len(pages)

    def _resp(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        results = pages[page - 1] if 1 <= page <= n else []
        return httpx.Response(
            200,
            json={
                "count": sum(len(p) for p in pages),
                "num_pages": n,
                "host": "https://talenthire.ceipal.com",
                "results": results,
            },
        )

    respx_mock.post(URL).mock(side_effect=_resp)


def test_parse_token() -> None:
    assert CeipalProvider._parse("AK|CP|Acme") == ("AK", "CP", "Acme")
    assert CeipalProvider._parse("AK|CP") == ("AK", "CP", None)
    assert CeipalProvider._parse("AK") == ("AK", "", None)


def test_form_has_required_fields() -> None:
    form = CeipalProvider()._form(AK, CP, 3)
    assert form["method"] == (None, "CareerPortalJobPostings")
    assert form["from_career_portal"] == (None, "1")
    assert form["api_key"] == (None, AK)
    assert form["cp_id"] == (None, CP)
    assert form["page"] == (None, "3")


async def test_fetch_paginates_and_normalizes() -> None:
    pages = [
        [_job(1, "Security Engineer", "District of Columbia"), _job(2, "SDET", "Texas")],
        [_job(3, "Remote DevOps", "California", remote="1")],
    ]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await CeipalProvider().fetch(f"{AK}|{CP}|Acme Staffing", SearchQuery(), f)

    assert len(raws) == 3
    assert {r.company for r in raws} == {"Acme Staffing"}
    j0 = CeipalProvider().normalize(raws[0])
    assert j0.id == make_job_id("ceipal", "1")
    assert j0.title == "Security Engineer"
    assert j0.locations[0].raw == "District of Columbia, United States"
    assert "/job/1" in j0.apply_url
    jr = CeipalProvider().normalize(raws[2])
    assert jr.remote.value == "remote"  # remote_opportunities == "1"


async def test_fetch_respects_limit() -> None:
    pages = [[_job(i, f"Role {i}", "Texas") for i in range(20)]]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await CeipalProvider().fetch(f"{AK}|{CP}|Acme", SearchQuery(limit=5), f)
    assert len(raws) == 5


async def test_missing_keys_degrades() -> None:
    async with AsyncFetcher(per_host_rate=100) as f:
        assert await CeipalProvider().fetch("AK", SearchQuery(), f) == []
