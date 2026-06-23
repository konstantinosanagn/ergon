"""Unit tests for the Avature provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.avature import AvatureProvider

pytestmark = pytest.mark.anyio

HOST = "bloomberg.avature.net"
PORTAL = "careers"


def _card(jid: str, title: str, location: str) -> str:
    """A search card: an h3 title link plus an 'Apply' button sharing the same id, and a
    location span carrying a tenant-specific class (substring 'location')."""
    href = f"https://{HOST}/{PORTAL}/JobDetail/Some-Slug/{jid}"
    return f"""
    <article class="article article--result">
      <div class="article__header__text">
        <h3 class="title"><a class="link" href="{href}">{title}</a></h3>
        <div class="article__header__text__subtitle">
          <span class="list-item-location">{location}</span>
        </div>
      </div>
      <div class="article__footer">
        <a class="button button--primary" href="{href}">Apply</a>
      </div>
    </article>"""


def _page(cards: list[tuple[str, str, str]]) -> str:
    body = "".join(_card(jid, title, loc) for jid, title, loc in cards)
    return f"<html><body><div class='results'>{body}</div></body></html>"


def _mock(respx_mock: respx.MockRouter) -> None:
    """offset=0 -> 2 jobs; any other offset -> empty (terminates pagination)."""
    base = f"https://{HOST}/{PORTAL}/SearchJobs"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("jobOffset") == "0":
            return httpx.Response(
                200,
                html=_page(
                    [
                        ("20316", "Product Manager &amp; Loans", "New York, United States"),
                        ("20337", "Remote Machine Learning Engineer", "Remote - United States"),
                    ]
                ),
            )
        return httpx.Response(200, html=_page([]))

    respx_mock.get(url__startswith=base).mock(side_effect=handler)


def test_matches_search_and_jobdetail_urls() -> None:
    p = AvatureProvider
    assert (
        p.matches("https://bloomberg.avature.net/careers/SearchJobs?jobOffset=0")
        == "bloomberg.avature.net|careers"
    )
    assert (
        p.matches("https://bloomberg.avature.net/careers/JobDetail/Some-Job/20316")
        == "bloomberg.avature.net|careers"
    )
    # locale prefix is skipped: portalPath is the segment before the page name
    assert (
        p.matches("https://careers.avature.net/en_US/main/SearchJobs") == "careers.avature.net|main"
    )
    # a bare Avature host (no recognizable page) -> bare-host token
    assert p.matches("koch.avature.net") == "koch.avature.net"
    assert p.matches("https://bloomberg.avature.net/") == "bloomberg.avature.net"
    # non-Avature hosts never match
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://careers.ey.com/ey/search/") is None
    assert p.matches("example.com") is None


async def test_fetch_paginates_and_parses() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch(f"{HOST}|{PORTAL}", SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "avature"
    assert r0.source_job_id == "20316"
    assert r0.company == "bloomberg"
    assert r0.url == f"https://{HOST}/{PORTAL}/JobDetail/Some-Slug/20316"


async def test_normalize_location_and_remote() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch(f"{HOST}|{PORTAL}", SearchQuery(), f)

    onsite = AvatureProvider().normalize(raws[0])
    assert onsite.id == make_job_id("avature", "20316")
    assert onsite.title == "Product Manager & Loans"  # entity unescaped, not "Apply"
    assert onsite.company == "bloomberg"
    assert onsite.locations[0].raw == "New York, United States"
    assert onsite.locations[0].is_remote is False
    assert onsite.remote is RemoteType.UNKNOWN
    assert onsite.posted_at is None
    assert onsite.salary is None
    assert onsite.description_text is None
    assert onsite.apply_url == f"https://{HOST}/{PORTAL}/JobDetail/Some-Slug/20316"

    remote = AvatureProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE
    assert remote.locations[0].is_remote is True


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch(f"{HOST}|{PORTAL}", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_bare_host_token_tries_default_portals() -> None:
    """A bare host token tries 'careers' first, then 'main'; here 'careers' 404s and 'main'
    serves the jobs."""
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=f"https://{HOST}/careers/SearchJobs").mock(
            return_value=httpx.Response(404, html="<html>not found</html>")
        )

        def main_handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("jobOffset") == "0":
                return httpx.Response(
                    200,
                    html=_page([("20316", "Backend Engineer", "Spain")]).replace(
                        f"/{PORTAL}/JobDetail", "/main/JobDetail"
                    ),
                )
            return httpx.Response(200, html=_page([]))

        respx_mock.get(url__startswith=f"https://{HOST}/main/SearchJobs").mock(
            side_effect=main_handler
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch(HOST, SearchQuery(), f)

    assert len(raws) == 1
    assert raws[0].source_job_id == "20316"
    assert raws[0].token == f"{HOST}|main"


async def test_blocked_tenant_degrades_to_empty() -> None:
    """A 202 + empty body (anti-bot, e.g. koch) degrades to []."""
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith="https://koch.avature.net/careers/SearchJobs").mock(
            return_value=httpx.Response(202, content=b"")
        )
        respx_mock.get(url__startswith="https://koch.avature.net/main/SearchJobs").mock(
            return_value=httpx.Response(202, content=b"")
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch("koch.avature.net", SearchQuery(), f)
    assert raws == []


def test_job_re_matches_custom_jobdetail_suffix() -> None:
    """Some tenants (Ralph Lauren) use /JobDetailRetail/ etc. — the id regex must still match."""
    from ergon_tracker.providers.avature import _JOB_RE

    assert (
        _JOB_RE.search("/CareersCorporate/JobDetailRetail/Cloud-Architect/46386").group(1)
        == "46386"
    )
    assert _JOB_RE.search("/main/JobDetail/Some-Role/12345").group(1) == "12345"


async def test_rss_uses_custom_page_name() -> None:
    """RSS fallback must build the feed at the tenant's custom search page (SearchJobsCorporate),
    not the hardcoded SearchJobs."""
    import httpx
    import respx

    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.avature import AvatureProvider

    feed = (
        "<rss><channel><item><title><![CDATA[Analyst]]></title>"
        "<link>https://careers.ralphlauren.com/CareersCorporate/JobDetailRetail/Analyst/46386</link>"
        "</item></channel></rss>"
    )
    url = "https://careers.ralphlauren.com/careerscorporate/SearchJobsCorporate/feed/?jobRecordsPerPage=20"
    with respx.mock as m:
        # portal HTML yields nothing -> RSS fallback at the custom page
        m.get(
            url__startswith="https://careers.ralphlauren.com/careerscorporate/SearchJobsCorporate?"
        ).mock(return_value=httpx.Response(200, text="<html></html>"))
        m.get(url).mock(return_value=httpx.Response(200, text=feed))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch(
                "careers.ralphlauren.com|CareersCorporate|SearchJobsCorporate", SearchQuery(), f
            )
    assert [r.source_job_id for r in raws] == ["46386"]


async def test_data_endpoint_full_board() -> None:
    """SPA tenants expose the FULL board at /{portal}/{page}Data/ as location-grouped JSON;
    _fetch_data flattens & dedupes it (vs the 20-capped RSS)."""
    import httpx
    import respx

    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.avature import AvatureProvider

    data = {
        "totalCount": 2,
        "locations": {
            "geoA": {
                "title": "New York, NY",
                "jobs": [
                    {
                        "id": "57886",
                        "title": "Analyst, Planning",
                        "url": "JobDetailCorporate?jobId=57886",
                    },
                    {
                        "id": "64715",
                        "title": "Assistant Buyer",
                        "url": "JobDetailCorporate?jobId=64715",
                    },
                ],
            },
            "geoB": {
                "title": "Nutley, NJ",
                "jobs": [
                    {"id": "64715", "title": "dup id dropped", "url": "x"},  # cross-location dup
                    {
                        "id": "59272",
                        "title": "Sales Audit Mgr",
                        "url": "JobDetailCorporate?jobId=59272",
                    },
                ],
            },
        },
    }
    base = "https://careers.ralphlauren.com/careerscorporate"
    with respx.mock as m:
        m.get(url__startswith=f"{base}/SearchJobsCorporate?").mock(
            return_value=httpx.Response(200, text="<html></html>")
        )
        m.get(f"{base}/SearchJobsCorporateData/").mock(return_value=httpx.Response(200, json=data))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await AvatureProvider().fetch(
                "careers.ralphlauren.com|CareersCorporate|SearchJobsCorporate", SearchQuery(), f
            )
    assert {r.source_job_id for r in raws} == {
        "57886",
        "64715",
        "59272",
    }  # deduped across locations
    j = next(r for r in raws if r.source_job_id == "57886")
    nj = AvatureProvider().normalize(j)
    assert nj.title == "Analyst, Planning" and nj.locations[0].raw == "New York, NY"
    assert nj.apply_url.endswith("/careerscorporate/JobDetailCorporate?jobId=57886")
