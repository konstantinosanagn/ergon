"""Unit tests for the SuccessFactors provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.successfactors import SuccessFactorsProvider

pytestmark = pytest.mark.anyio

HOST = "careers.ey.com"
SITEID = "ey"


def _page(rows: list[tuple[str, str, str]]) -> str:
    """Build a minimal SF search page from (job_id, title, location) tuples."""
    cards = "".join(
        f"""
        <tr class="data-row">
          <td class="colTitle">
            <span class="jobTitle hidden-phone">
              <a href="/{SITEID}/job/Some-Slug-{jid}/{jid}/" class="jobTitle-link">{title}</a>
            </span>
          </td>
          <td class="colLocation hidden-phone"><span class="jobLocation">{loc}</span></td>
        </tr>"""
        for jid, title, loc in rows
    )
    return f"<html><body><table><tbody>{cards}</tbody></table></body></html>"


def _mock(respx_mock: respx.MockRouter) -> None:
    """startrow=0 -> 2 jobs; startrow=25 -> empty (terminates pagination)."""
    base = f"https://{HOST}/{SITEID}/search/"

    def handler(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("startrow")
        if start == "0":
            return httpx.Response(
                200,
                html=_page(
                    [
                        (
                            "1395167233",
                            "Analyst - Business Consulting &amp; Risk",
                            "Mumbai, MH, IN, 400028",
                        ),
                        ("1399453633", "Remote Quantum Associate", "Remote - United States"),
                    ]
                ),
            )
        return httpx.Response(200, html=_page([]))

    respx_mock.get(url__startswith=base).mock(side_effect=handler)


def test_matches_job_and_search_urls() -> None:
    p = SuccessFactorsProvider
    assert (
        p.matches("https://careers.ey.com/ey/job/Mumbai-Analyst/1395167233/") == "careers.ey.com|ey"
    )
    assert p.matches("https://careers.ey.com/ey/search/?q=audit") == "careers.ey.com|ey"
    assert p.matches("https://jobs.sap.com/sap/job/Berlin-Dev/1381730633/") == "jobs.sap.com|sap"
    # non-SF shapes don't match
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://careers.ey.com/ey/about") is None
    assert p.matches("https://example.com") is None
    # generic locale/section first-segments must NOT be mistaken for a siteid
    assert p.matches("https://jobs.apple.com/en-us/search?...") is None
    assert p.matches("https://www.amazon.jobs/en/search") is None
    assert p.matches("https://www.ibm.com/careers/search") is None
    # a /job/ URL with a short (non-SF) id is rejected; only long numeric ids match
    assert p.matches("https://x.com/foo/job/bar/42/") is None


async def test_fetch_paginates_and_parses() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SuccessFactorsProvider().fetch("careers.ey.com|ey", SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "successfactors"
    assert r0.source_job_id == "1395167233"
    assert r0.company == "ey"
    assert r0.url == "https://careers.ey.com/ey/job/Some-Slug-1395167233/1395167233/"


async def test_normalize_location_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SuccessFactorsProvider().fetch("careers.ey.com|ey", SearchQuery(), f)

    onsite = SuccessFactorsProvider().normalize(raws[0])
    assert onsite.id == make_job_id("successfactors", "1395167233")
    assert onsite.title == "Analyst - Business Consulting & Risk"  # entity unescaped
    assert onsite.company == "ey"
    assert onsite.locations[0].raw == "Mumbai, MH, IN, 400028"
    assert onsite.locations[0].is_remote is False
    assert onsite.remote is RemoteType.UNKNOWN
    assert onsite.posted_at is None
    assert onsite.salary is None
    assert onsite.description_text is None

    remote = SuccessFactorsProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE
    assert remote.locations[0].is_remote is True


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SuccessFactorsProvider().fetch(
                "careers.ey.com|ey", SearchQuery(limit=1), f
            )
    assert len(raws) == 1


async def test_siteid_discovery_from_host_only_token() -> None:
    """A bare host token discovers the siteid from the landing page."""
    with respx.mock as respx_mock:
        respx_mock.get("https://careers.ey.com/").mock(
            return_value=httpx.Response(200, html='<a href="/ey/search/?q=">Search jobs</a>')
        )
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SuccessFactorsProvider().fetch("careers.ey.com", SearchQuery(), f)
    assert len(raws) == 2
    assert raws[0].company == "ey"


def _root_page(rows: list[tuple[str, str, str]]) -> str:
    """A siteid-less CSB page: jobs at /job/{slug}/{id}/ (no /{siteid}/ prefix)."""
    cards = "".join(
        f"""
        <tr class="data-row">
          <td class="colTitle"><span class="jobTitle">
            <a href="/job/Some-Slug-{jid}/{jid}/" class="jobTitle-link">{title}</a>
          </span></td>
          <td class="colLocation"><span class="jobLocation">{loc}</span></td>
        </tr>"""
        for jid, title, loc in rows
    )
    return f"<html><body><table><tbody>{cards}</tbody></table></body></html>"


async def test_root_csb_no_siteid_with_company_label() -> None:
    """White-labeled CSB without a siteid path: token '{host}|*|{Company}' searches /search/."""
    host = "careers.ltimindtree.com"
    base = f"https://{host}/search/"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("startrow") == "0":
            return httpx.Response(
                200, html=_root_page([("1406195133", "Senior Software Engineer", "BR")])
            )
        return httpx.Response(200, html=_root_page([]))

    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=base).mock(side_effect=handler)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SuccessFactorsProvider().fetch(f"{host}|*|LTIMindtree", SearchQuery(), f)
    assert len(raws) == 1
    assert raws[0].source_job_id == "1406195133"
    assert raws[0].company == "LTIMindtree"  # explicit label, not the siteid sentinel
