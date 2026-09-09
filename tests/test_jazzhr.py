"""Unit tests for the JazzHR provider (respx-mocked, offline)."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest
import respx

from ergon.exceptions import ProviderError
from ergon.http import AsyncFetcher
from ergon.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon.providers.jazzhr import JazzHRProvider

pytestmark = pytest.mark.anyio

SUB = "firstadvantage"
FEED = f"https://app.jazz.co/feeds/export/jobs/{SUB}"


def _job(
    jid: str,
    title: str,
    *,
    city: str = "",
    state: str = "",
    country: str = "",
    jtype: str = "Full Time",
    status: str = "Open",
    desc: str = "<p>Build things.</p>",
) -> str:
    return f"""
    <job>
      <id><![CDATA[{jid}]]></id>
      <status><![CDATA[{status}]]></status>
      <title><![CDATA[{title}]]></title>
      <department><![CDATA[Engineering]]></department>
      <url><![CDATA[http://{SUB}.applytojob.com/apply/AbCd123/slug]]></url>
      <city><![CDATA[{city}]]></city>
      <state><![CDATA[{state}]]></state>
      <country><![CDATA[{country}]]></country>
      <postalcode><![CDATA[]]></postalcode>
      <description><![CDATA[{desc}]]></description>
      <type><![CDATA[{jtype}]]></type>
      <experience><![CDATA[Experienced]]></experience>
    </job>"""


def _feed(jobs: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<jobs>'
        "<publisher><![CDATA[JazzHR]]></publisher>"
        "<publisherurl>http://app.jazz.co</publisherurl>"
        "<company><![CDATA[First Advantage]]></company>" + "".join(jobs) + "</jobs>"
    )


def _mock(respx_mock: respx.MockRouter) -> None:
    body = _feed(
        [
            _job(
                "job_20260106122430_CR9WJRTY9BDUBAKM",
                "Software Engineer",
                city="Austin",
                state="TX",
                country="United States",
            ),
            _job(
                "job_20260518211639_VFKY5OL56EKU1EHF",
                "Associate Engineer (US Remote)",
                country="United States",
                jtype="Contractor",
            ),
            _job("job_20250101000000_CLOSEDONE", "Old Closed Role", status="Filled"),
        ]
    )
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, text=body))


def test_matches_host_feed_and_bare() -> None:
    p = JazzHRProvider
    assert (
        p.matches("https://firstadvantage.applytojob.com/apply/hWkx/Software") == "firstadvantage"
    )
    assert p.matches("firstadvantage.applytojob.com") == "firstadvantage"
    assert p.matches("https://app.jazz.co/feeds/export/jobs/talentwwinc") == "talentwwinc"
    # non-JazzHR shapes don't match
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None
    # JazzHR's own marketing subdomains are not tenants
    assert p.matches("https://www.applytojob.com/") is None


async def test_fetch_parses_open_jobs_only() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JazzHRProvider().fetch(SUB, SearchQuery(), f)

    assert len(raws) == 2  # the "Filled" job is dropped
    r0 = raws[0]
    assert r0.source == "jazzhr"
    assert r0.source_job_id == "job_20260106122430_CR9WJRTY9BDUBAKM"
    assert r0.company == SUB
    assert r0.url == f"http://{SUB}.applytojob.com/apply/AbCd123/slug"


async def test_normalize_fields_posted_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JazzHRProvider().fetch(SUB, SearchQuery(), f)

    onsite = JazzHRProvider().normalize(raws[0])
    assert onsite.id == make_job_id("jazzhr", "job_20260106122430_CR9WJRTY9BDUBAKM")
    assert onsite.title == "Software Engineer"
    assert onsite.department == "Engineering"
    assert onsite.employment_type is EmploymentType.FULL_TIME
    assert onsite.remote is RemoteType.UNKNOWN
    assert onsite.locations[0].city == "Austin"
    assert onsite.locations[0].region == "TX"
    assert onsite.locations[0].raw == "Austin, TX, United States"
    assert onsite.description_html == "<p>Build things.</p>"
    assert onsite.description_text is None
    assert onsite.salary is None
    posted = onsite.posted_at.astimezone(timezone.utc)
    assert (posted.year, posted.month, posted.day) == (2026, 1, 6)

    remote = JazzHRProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE  # "Remote" in title
    assert remote.locations[0].is_remote is True
    assert remote.employment_type is EmploymentType.CONTRACT


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JazzHRProvider().fetch(SUB, SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_raises_on_error() -> None:
    """A failed fetch must RAISE, not return []. An empty list reads as "board is empty"
    to the crawl and the liveness/freshness passes, which expires every row on it."""
    with respx.mock as respx_mock:
        respx_mock.get(FEED).mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(ProviderError):
                await JazzHRProvider().fetch(SUB, SearchQuery(), f)
