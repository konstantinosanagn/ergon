"""Unit tests for the Oracle Taleo provider (respx-mocked, offline)."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.taleo import TaleoProvider

pytestmark = pytest.mark.anyio

HOST = "drhorton.taleo.net"
CS = "2"
PORTAL = "101430233"
TOKEN = f"{HOST}|{CS}|{PORTAL}"
SEARCH = f"https://{HOST}/careersection/rest/jobboard/searchjobs"


def _req(jid: str, contest: str, title: str, loc: str, date: str) -> dict:
    """One requisition with the self-describing column array (title, location, date)."""
    return {
        "jobId": jid,
        "contestNo": contest,
        "column": [title, loc, date],
        "linkedColumn": 0,
        "locationsColumns": [1],
    }


def _page(reqs: list[dict], total: int, page_no: int = 1) -> dict:
    return {
        "requisitionList": reqs,
        "pagingData": {"currentPageNo": page_no, "pageSize": 25, "totalCount": total},
        "careerSectionUnAvailable": False,
    }


def _mock(respx_mock: respx.MockRouter) -> None:
    """pageNo=1 -> 2 reqs (total=2); pageNo>=2 -> empty list (terminates)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if '"pageNo": 1' in body or '"pageNo":1' in body:
            return httpx.Response(
                200,
                json=_page(
                    [
                        _req(
                            "264858",
                            "2602892",
                            "Junior Sales Representative - Houston SW",
                            '["TX-Richmond"]',
                            "Jun 17, 2026",
                        ),
                        _req(
                            "264900",
                            "2602900",
                            "Remote Data Engineer",
                            '["Remote-US"]',
                            "Jun 16, 2026",
                        ),
                    ],
                    total=2,
                ),
            )
        return httpx.Response(200, json=_page([], total=2, page_no=2))

    respx_mock.post(url__startswith=SEARCH).mock(side_effect=handler)


def test_matches_career_urls() -> None:
    p = TaleoProvider
    assert (
        p.matches("https://drhorton.taleo.net/careersection/2/jobsearch.ftl?lang=en")
        == "drhorton.taleo.net|2|"
    )
    # alpha career-section code
    assert (
        p.matches("https://acme.taleo.net/careersection/ex/jobdetail.ftl?job=5")
        == "acme.taleo.net|ex|"
    )
    # bare host -> cs left empty (discovered at fetch time)
    assert p.matches("hyatt.taleo.net") == "hyatt.taleo.net||"
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_paginates_requisitionlist() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoProvider().fetch(TOKEN, SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "taleo"
    assert r0.source_job_id == "264858"
    assert r0.company == "drhorton"
    assert r0.url == f"https://{HOST}/careersection/{CS}/jobdetail.ftl?job=264858&lang=en"


async def test_normalize_fields_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoProvider().fetch(TOKEN, SearchQuery(), f)

    onsite = TaleoProvider().normalize(raws[0])
    assert onsite.id == make_job_id("taleo", "264858")
    assert onsite.title == "Junior Sales Representative - Houston SW"
    assert onsite.company == "drhorton"
    assert onsite.locations[0].raw == "TX-Richmond"
    assert onsite.remote is RemoteType.UNKNOWN
    assert onsite.salary is None
    assert onsite.description_text is None
    assert onsite.description_html is None
    posted = onsite.posted_at.astimezone(timezone.utc)
    assert (posted.year, posted.month, posted.day) == (2026, 6, 17)

    remote = TaleoProvider().normalize(raws[1])
    assert remote.locations[0].raw == "Remote-US"
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoProvider().fetch(TOKEN, SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_bare_host_discovers_cs_and_portal() -> None:
    with respx.mock as respx_mock:
        # cs=1 stub is small (skipped), cs=2 is large with a portal -> resolved.
        respx_mock.get(f"https://{HOST}/careersection/1/jobsearch.ftl").mock(
            return_value=httpx.Response(200, text="too small")
        )
        respx_mock.get(f"https://{HOST}/careersection/2/jobsearch.ftl").mock(
            return_value=httpx.Response(200, text="x" * 20_000 + f"portal={PORTAL}&foo")
        )
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoProvider().fetch(HOST, SearchQuery(), f)

    assert len(raws) == 2
    assert raws[0].url == f"https://{HOST}/careersection/2/jobdetail.ftl?job=264858&lang=en"


def test_legacy_ftl_stream_parser() -> None:
    """Legacy jobsearch.ajax sites embed page-1 jobs as a '!|!' stream; parse id/title/loc/date
    with type-classified columns (tenant-specific order) and dedupe."""
    from ergon_tracker.providers.taleo import TaleoProvider

    def job(jid, title, *cols):
        # signature: id‖title‖id‖title‖id‖id‖id‖id‖id‖ then tenant columns
        return [jid, title, jid, title, jid, jid, jid, jid, jid, *cols]

    fields = (
        ["<!DOCTYPE html>", "junk", "listRequisition"]
        + job("464078", "HR Director - Panels", "USA-WA-Seattle", "false", "", "Jun 19, 2026", "01024482", "Apply")
        + job("149092", "System Engineer", "INF00EO", "Kansas-Topeka, Missouri", "false", "Jun 21, 2026")  # contestNo-first tenant order
        + job("464078", "dup id dropped", "USA-WA-Tacoma")
    )
    html_text = "!|!".join(fields)
    raws = TaleoProvider()._raws_from_ftl(html_text, "weyerhaeuser.taleo.net", "10000", None)
    assert {r.source_job_id for r in raws} == {"464078", "149092"}  # deduped
    by = {r.source_job_id: TaleoProvider().normalize(r) for r in raws}
    assert by["464078"].title == "HR Director - Panels"
    assert by["464078"].locations[0].raw == "USA-WA-Seattle"
    assert by["464078"].posted_at is not None and by["464078"].posted_at.year == 2026
    # tenant with contestNo before location -> still classifies location by type
    assert by["149092"].locations[0].raw == "Kansas-Topeka, Missouri"
    assert by["149092"].apply_url.endswith("job=149092&lang=en")
