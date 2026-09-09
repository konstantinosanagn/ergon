"""Unit tests for the BrassRing provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from datetime import timezone

import httpx
import pytest
import respx

from ergon.exceptions import ProviderError
from ergon.http import AsyncFetcher
from ergon.models import RemoteType, SearchQuery, make_job_id
from ergon.providers.brassring import BrassRingProvider

pytestmark = pytest.mark.anyio

HOST = "sjobs.brassring.com"
PID = "25416"
SID = "5429"
TOKEN = f"{HOST}|{PID}|{SID}"
HOME = f"https://{HOST}/TGnewUI/Search/Home/Home"
LIST = f"https://{HOST}/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"

# Bootstrap HTML: anti-forgery token, session value, tenant field map, company name.
_HOME_HTML = """<!DOCTYPE html><html><body>
<input name="__RequestVerificationToken" type="hidden" value="RFT-TOKEN-123" />
<input id="CookieValue" type="hidden" value="^ENCRYPTED-SESSION" />
<script>var cfg = {"PartnerName":"Archer Daniels Midland",
"JobFieldsToDisplay":{"Position1":null,"JobTitle":"jobtitle",
"Position3":["formtext8","formtext10","department"],"Summary":"formtext3"}};</script>
</body></html>"""


def _job(reqid: str, title: str, city: str, country: str, remote: bool = False) -> dict:
    questions = [
        {"QuestionName": "reqid", "Value": reqid},
        {"QuestionName": "jobtitle", "Value": title},
        {"QuestionName": "department", "Value": "Finance"},
        {"QuestionName": "formtext3", "Value": "<p>Do finance things.</p>"},
        {"QuestionName": "formtext8", "Value": "Remote" if remote else city},
        {"QuestionName": "formtext10", "Value": country},
        {"QuestionName": "lastupdated", "Value": "18-Jun-2026"},
    ]
    link = (
        f"https://{HOST}/TGnewUI/Search/home/HomeWithPreLoad"
        f"?partnerid={PID}&siteid={SID}&PageType=JobDetails&jobid={reqid}"
    )
    return {"Questions": questions, "Link": link}


def _mock(respx_mock: respx.MockRouter, *, jobs: list[dict] | None = None) -> None:
    """Home GET -> bootstrap HTML; list POST -> page1 = jobs (JobsCount set), page2 = empty."""
    page1 = (
        jobs
        if jobs is not None
        else [
            _job("3374337", "Manager Credit EMEA", "Chelmsford", "United Kingdom"),
            _job("3371653", "Remote Data Engineer", "Anywhere", "United States", remote=True),
        ]
    )
    respx_mock.get(url__startswith=HOME).mock(return_value=httpx.Response(200, html=_HOME_HTML))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        page = body.get("pageNumber")
        if page == 1:
            return httpx.Response(200, json={"Jobs": {"Job": page1}, "JobsCount": len(page1)})
        return httpx.Response(200, json={"Jobs": None, "JobsCount": len(page1)})

    respx_mock.post(LIST).mock(side_effect=handler)


def test_matches_brassring_urls() -> None:
    p = BrassRingProvider
    assert (
        p.matches(f"https://{HOST}/TGnewUI/Search/Home/Home?partnerid={PID}&siteid={SID}") == TOKEN
    )
    # case-insensitive param names, vanity host
    assert (
        p.matches("https://krb-sjobs.brassring.com/x?partnerId=26059&siteId=5016")
        == "krb-sjobs.brassring.com|26059|5016"
    )
    # missing siteid -> not a usable tenant
    assert p.matches(f"https://{HOST}/x?partnerid={PID}") is None
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_lists_jobs() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BrassRingProvider().fetch(TOKEN, SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "brassring"
    assert r0.source_job_id == "3374337"
    assert r0.company == "Archer Daniels Midland"
    assert r0.url == (
        f"https://{HOST}/TGnewUI/Search/home/HomeWithPreLoad"
        f"?partnerid={PID}&siteid={SID}&PageType=JobDetails&jobid=3374337"
    )


async def test_fetch_sends_rft_header_and_session() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            await BrassRingProvider().fetch(TOKEN, SearchQuery(), f)
        post_call = next(c for c in respx_mock.calls if c.request.method == "POST")
    assert post_call.request.headers.get("RFT") == "RFT-TOKEN-123"
    body = json.loads(post_call.request.content)
    assert body["encryptedSessionValue"] == "^ENCRYPTED-SESSION"
    assert body["partnerId"] == PID and body["siteId"] == SID


async def test_normalize_fields_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BrassRingProvider().fetch(TOKEN, SearchQuery(), f)

    onsite = BrassRingProvider().normalize(raws[0])
    assert onsite.id == make_job_id("brassring", "3374337")
    assert onsite.title == "Manager Credit EMEA"
    assert onsite.department == "Finance"
    assert onsite.description_html == "<p>Do finance things.</p>"
    assert onsite.description_text is None
    assert onsite.salary is None
    assert onsite.remote is RemoteType.UNKNOWN
    # Position3 (formtext8, formtext10) -> location; department excluded.
    assert onsite.locations[0].raw == "Chelmsford, United Kingdom"
    assert onsite.posted_at is None
    updated = onsite.updated_at.astimezone(timezone.utc)
    assert (updated.year, updated.month, updated.day) == (2026, 6, 18)

    remote = BrassRingProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE
    assert remote.locations[0].is_remote is True


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BrassRingProvider().fetch(TOKEN, SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_raises_without_csrf_token() -> None:
    """A failure to bootstrap must RAISE, not return []. An empty list reads as "board is
    empty" to the crawl and the liveness/freshness passes, which expires every row."""
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=HOME).mock(
            return_value=httpx.Response(200, html="<html><body>no token</body></html>")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(ProviderError):
                await BrassRingProvider().fetch(TOKEN, SearchQuery(), f)


async def test_fetch_raises_on_home_error() -> None:
    """A failed fetch must RAISE, not return []. An empty list reads as "board is empty"
    to the crawl and the liveness/freshness passes, which expires every row on it."""
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=HOME).mock(return_value=httpx.Response(500))
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(ProviderError):
                await BrassRingProvider().fetch(TOKEN, SearchQuery(), f)
