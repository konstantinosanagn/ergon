"""Unit tests for the RippleHire provider (respx-mocked, offline)."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.ripplehire import RippleHireProvider

pytestmark = pytest.mark.anyio

URL = "https://mphasis.ripplehire.com/candidate/candidatejobsearch"


def _job(seq: str, title: str, loc: str) -> dict:
    return {
        "jobSeq": seq,
        "jobTitle": title,
        "locations": loc,
        "jobReqExp": "3 - 6 Years",
        "jobCode": "C-1",
    }


def _mock(respx_mock: respx.MockRouter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # body carries careerSiteUrlParams={...page:N...}; page 0 -> 2 jobs, page >=1 -> empty
        m = re.search(r'"page":\s*(\d+)', request.content.decode())
        if m and m.group(1) == "0":
            return httpx.Response(
                200,
                json={
                    "totalJobCount": 2,
                    "jobVoList": [
                        _job("884519", "Technical Lead", "Barcelona"),
                        _job("885450", "Senior Software Engineer (Remote)", "Remote - India"),
                    ],
                },
            )
        return httpx.Response(200, json={"totalJobCount": 2, "jobVoList": []})

    respx_mock.post(URL).mock(side_effect=handler)


def test_matches_host() -> None:
    p = RippleHireProvider
    assert (
        p.matches("https://mphasis.ripplehire.com/candidate/careerpage") == "mphasis.ripplehire.com"
    )
    assert p.matches("citiustech.ripplehire.com") == "citiustech.ripplehire.com"
    assert p.matches("https://boards.greenhouse.io/x") is None


def test_parse_token() -> None:
    assert RippleHireProvider._parse("mphasis|tok123|Mphasis") == ("mphasis", "tok123", "Mphasis")
    assert RippleHireProvider._parse("mphasis.ripplehire.com|tok") == ("mphasis", "tok", None)


async def test_fetch_and_normalize() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RippleHireProvider().fetch("mphasis|tok123|Mphasis", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"Mphasis"}  # not the per-job jobCode client
    j0 = RippleHireProvider().normalize(raws[0])
    assert j0.id == make_job_id("ripplehire", "884519")
    assert j0.title == "Technical Lead"
    assert j0.locations[0].raw == "Barcelona"
    assert j0.remote is RemoteType.UNKNOWN
    assert "884519" in j0.apply_url

    remote = RippleHireProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE  # "Remote" in location


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RippleHireProvider().fetch(
                "mphasis|tok123|Mphasis", SearchQuery(limit=1), f
            )
    assert len(raws) == 1


async def test_fetch_degrades_on_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.post(URL).mock(return_value=httpx.Response(500))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RippleHireProvider().fetch("mphasis|tok123|Mphasis", SearchQuery(), f)
    assert raws == []
