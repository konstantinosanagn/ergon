"""Unit tests for the Coveo provider (offline: respx-mocked proxy search + parse/normalize).

Covers the opt-in ``coveo:`` scheme, the proxy-mode search POST (source named in the token, so no
auto-detect), dedup + limit, and normalize's multi-value collapse / epoch-millis + ISO date handling.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RawJob, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.coveo import CoveoProvider

pytestmark = pytest.mark.anyio

SEARCH_URL = "https://careers.slb.com/coveo/rest/search/v2"

# Two job results. #0 exercises multi-value (list) city/country + epoch-millis date; #1 exercises
# scalar fields, a "Remote" label, and an ISO date string. 2026-01-01T00:00:00Z == 1767225600 s.
RESULTS = [
    {
        "title": "Reservoir Engineer",
        "clickUri": "https://careers.slb.com/job/1",
        "uniqueId": "u1",
        "raw": {
            "permanentid": "p1",
            "city": ["Houston"],
            "country": ["United States"],
            "category": "Engineering",
            "description": "<p>Drill wells.</p>",
            "date": 1767225600000,
        },
    },
    {
        "title": "Remote Data Scientist",
        "clickUri": "https://careers.slb.com/job/2",
        "uniqueId": "u2",
        "raw": {
            "permanentid": "p2",
            "city": "Remote",
            "country": "United States",
            "date": "2026-05-30T00:00:00Z",
        },
    },
]


# --- matches (opt-in scheme) ------------------------------------------------


def test_matches_requires_coveo_scheme() -> None:
    p = CoveoProvider
    assert p.matches("coveo:careers.slb.com") == "careers.slb.com"
    assert p.matches("coveo:careers.slb.com|ATS_Jobs") == "careers.slb.com|ATS_Jobs"
    assert p.matches("careers.slb.com") is None  # never auto-claims a bare host
    assert p.matches("https://boards.greenhouse.io/acme") is None


# --- fetch ------------------------------------------------------------------


async def test_fetch_builds_rawjobs_and_filters_by_source() -> None:
    with respx.mock:
        route = respx.post(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"results": RESULTS})
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await CoveoProvider().fetch("coveo:careers.slb.com|ATS_Jobs", SearchQuery(), f)

    body = json.loads(route.calls[0].request.content)
    assert body["aq"] == '@source=="ATS_Jobs"'  # named source used, no auto-detect probe
    assert [r.source_job_id for r in raws] == ["p1", "p2"]
    assert raws[0].source == "coveo" and raws[0].company == "slb"  # host label -> company
    assert raws[0].url == "https://careers.slb.com/job/1"
    assert raws[0].payload["_title"] == "Reservoir Engineer"


async def test_fetch_honors_limit() -> None:
    with respx.mock:
        respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": RESULTS}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await CoveoProvider().fetch(
                "coveo:careers.slb.com|ATS_Jobs", SearchQuery(limit=1), f
            )
    assert len(raws) == 1


async def test_fetch_empty_results_stops() -> None:
    with respx.mock:
        respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await CoveoProvider().fetch("coveo:careers.slb.com|ATS_Jobs", SearchQuery(), f)
    assert raws == []


# --- normalize --------------------------------------------------------------


def _normalize(result: dict) -> object:
    raw = result["raw"]
    rj = RawJob(
        source="coveo",
        source_job_id=raw["permanentid"],
        company="slb",
        token="coveo:careers.slb.com|ATS_Jobs",
        url=result["clickUri"],
        payload={**raw, "_title": result["title"]},
    )
    return CoveoProvider().normalize(rj)


def test_normalize_collapses_multivalue_and_epoch_date() -> None:
    job = _normalize(RESULTS[0])
    assert job.id == make_job_id("coveo", "p1")
    assert job.title == "Reservoir Engineer"
    assert job.locations[0].raw == "Houston, United States"  # list fields collapsed to scalars
    assert job.department == "Engineering"
    assert job.description_html == "<p>Drill wells.</p>"
    assert job.remote == RemoteType.UNKNOWN
    assert job.posted_at is not None and job.posted_at.year == 2026  # epoch millis -> datetime


def test_normalize_detects_remote_and_iso_date() -> None:
    job = _normalize(RESULTS[1])
    assert job.locations[0].raw == "Remote, United States"
    assert job.remote == RemoteType.REMOTE  # "remote" in the label
    assert job.posted_at is not None and job.posted_at.year == 2026  # ISO string parsed


def test_scalar_and_date_helpers() -> None:
    assert CoveoProvider._scalar(["Houston", "Dallas"]) == "Houston"
    assert CoveoProvider._scalar([]) == ""
    assert CoveoProvider._scalar(None) == ""
    assert CoveoProvider._date(1767225600000) is not None
    assert CoveoProvider._date(0) is None
    assert CoveoProvider._date("nonsense") is None
