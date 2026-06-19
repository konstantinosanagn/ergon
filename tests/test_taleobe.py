"""Unit tests for the Taleo Business Edition (TBE/CwsV2) provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.taleobe import TaleoBEProvider

pytestmark = pytest.mark.anyio

HOST = "phf.tbe.taleo.net/phf03"
BASE = f"https://{HOST}/ats/careers/v2/searchResults"


def _row(rid: str, title: str, loc: str) -> str:
    return (
        f'<h4 class="oracletaleocwsv2-head-title"><a href="https://{HOST}/ats/careers/v2/'
        f'viewRequisition?org=CALTECH&cws=37&rid={rid}" class="viewJobLink">{title}</a></h4>'
        f'<div tabindex="0">{loc}</div>'
    )


def _mock(respx_mock: respx.MockRouter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rf = request.url.params.get("rowFrom")
        if not rf:  # page 0
            body = _row("101", "Administrative Assistant", "Pasadena, CA") + _row(
                "102", "Research Scientist (Remote)", "Remote - US"
            )
            return httpx.Response(200, text=f"<html>23Jobs{body}</html>")
        return httpx.Response(200, text="<html>no more</html>")

    respx_mock.get(url__startswith=BASE).mock(side_effect=handler)


def test_matches_host() -> None:
    p = TaleoBEProvider
    assert (
        p.matches("https://phf.tbe.taleo.net/phf03/ats/careers/v2/searchResults")
        == "phf.tbe.taleo.net"
    )
    assert p.matches("https://boards.greenhouse.io/x") is None


def test_parse_token() -> None:
    assert TaleoBEProvider._parse("phf.tbe.taleo.net/phf03|CALTECH|37") == (
        "phf.tbe.taleo.net/phf03",
        "CALTECH",
        "37",
        None,
    )
    assert TaleoBEProvider._parse("phf.tbe.taleo.net/phf03|CALTECH|37|Caltech")[3] == "Caltech"


async def test_fetch_and_normalize() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoBEProvider().fetch(f"{HOST}|CALTECH|37|Caltech", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"Caltech"}
    j0 = TaleoBEProvider().normalize(raws[0])
    assert j0.id == make_job_id("taleobe", "101")
    assert j0.title == "Administrative Assistant"
    assert j0.locations[0].raw == "Pasadena, CA"
    assert "rid=101" in j0.apply_url

    remote = TaleoBEProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoBEProvider().fetch(f"{HOST}|CALTECH|37", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_degrades_on_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=BASE).mock(return_value=httpx.Response(500))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoBEProvider().fetch(f"{HOST}|CALTECH|37", SearchQuery(), f)
    assert raws == []
