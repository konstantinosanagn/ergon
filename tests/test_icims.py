"""Unit tests for the iCIMS provider (respx-mocked, offline).

Covers both generations: the new "Career Sites" JSON API and the classic HTML + JSON-LD
path, plus auto-detection between them.
"""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.icims import ICIMSProvider

pytestmark = pytest.mark.anyio

NEW_HOST = "careers.amd.com"
CLASSIC_HOST = "careers-winco.icims.com"


# --- new JSON-API fixtures -------------------------------------------------


def _job(req_id: str, title: str, **over: object) -> dict:
    data = {
        "req_id": req_id,
        "slug": req_id,
        "title": title,
        "description": "<p>Build chips.</p>",
        "apply_url": f"https://careers-amd.icims.com/jobs/{req_id}/login",
        "posted_date": "2026-06-17T17:50:00+0000",
        "update_date": "2026-06-17T18:00:00+0000",
        "city": "Austin",
        "state": "Texas",
        "country": "United States",
        "location_name": "US,TX,Austin",
        "employment_type": "FULL_TIME",
        "department": "",
        "categories": [{"name": "Engineering"}],
        "salary_min_value": 0,
        "salary_max_value": 0,
        "hiring_organization": "Advanced Micro Devices, Inc",
    }
    data.update(over)
    return {"data": data}


def _api_body(jobs: list[dict], total: int) -> dict:
    return {"jobs": jobs, "locations": [], "totalCount": total, "count": len(jobs)}


def _mock_new(respx_mock: respx.MockRouter) -> None:
    """page=1 -> 2 jobs (totalCount=2 -> a single page)."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(
                200,
                json=_api_body(
                    [
                        _job("87222", "Lead Formal Verification Engineer"),
                        _job(
                            "87223",
                            "Remote Data Engineer",
                            location_name="Remote - US",
                            employment_type="PART_TIME",
                            salary_min_value=120000,
                            salary_max_value=160000,
                        ),
                    ],
                    total=2,
                ),
            )
        return httpx.Response(200, json=_api_body([], total=2))

    respx_mock.get(url__startswith=f"https://{NEW_HOST}/api/jobs").mock(side_effect=handler)


# --- classic HTML + JSON-LD fixtures ---------------------------------------


def _listing(ids: list[tuple[str, str]], page_of: tuple[int, int]) -> str:
    links = "".join(f'<a href="/jobs/{jid}/{slug}/job" class="title">x</a>' for jid, slug in ids)
    return f"<html><body>{links}<span>Page {page_of[0]} of {page_of[1]}</span></body></html>"


def _detail(title: str, jid: str, employment: str = "OTHER", remote: bool = False) -> str:
    locality = "Remote" if remote else "Orangevale"
    ld = f"""
    {{
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "{title}",
      "datePosted": "2026-06-17T04:00:00.000Z",
      "validThrough": "2026-06-22T04:00:00.000Z",
      "employmentType": "{employment}",
      "hiringOrganization": {{"@type": "Organization", "name": "WinCo Foods"}},
      "jobLocation": [{{"@type": "Place", "address": {{
          "@type": "PostalAddress", "addressLocality": "{locality}",
          "addressRegion": "CA", "addressCountry": "US"}}}}],
      "description": "<p>Join WinCo.</p>",
      "url": "https://{CLASSIC_HOST}/jobs/{jid}/slug/job"
    }}
    """
    return (
        f'<html><head><script type="application/ld+json">{ld}</script></head>'
        f"<body>{title}</body></html>"
    )


def _mock_classic(respx_mock: respx.MockRouter) -> None:
    """/api/jobs -> HTML (detection -> classic); listing pr=0 -> 2 jobs (Page 1 of 1)."""
    respx_mock.get(url__startswith=f"https://{CLASSIC_HOST}/api/jobs").mock(
        return_value=httpx.Response(200, html="<!DOCTYPE html><html>not json</html>")
    )

    def listing_handler(request: httpx.Request) -> httpx.Response:
        pr = request.url.params.get("pr")
        if pr == "0":
            return httpx.Response(
                200,
                html=_listing([("152502", "cashier"), ("152480", "us-or-eugene")], (1, 1)),
            )
        return httpx.Response(200, html=_listing([], (1, 1)))

    respx_mock.get(url__startswith=f"https://{CLASSIC_HOST}/jobs/search").mock(
        side_effect=listing_handler
    )
    respx_mock.get(url__regex=rf"https://{CLASSIC_HOST}/jobs/152502/").mock(
        return_value=httpx.Response(200, html=_detail("Cashier", "152502"))
    )
    respx_mock.get(url__regex=rf"https://{CLASSIC_HOST}/jobs/152480/").mock(
        return_value=httpx.Response(
            200, html=_detail("Remote Clerk", "152480", employment="PART_TIME", remote=True)
        )
    )


# --- matches ---------------------------------------------------------------


def test_matches_hosts_and_urls() -> None:
    p = ICIMSProvider
    assert p.matches("https://careers-winco.icims.com/jobs/search") == "careers-winco.icims.com"
    assert p.matches("careers.icims.com") == "careers.icims.com"
    assert p.matches("https://careers.amd.com/jobs/search?ss=1") == "careers.amd.com"
    assert p.matches("https://careers.amd.com/careers-home/jobs?page=1") == "careers.amd.com"
    assert p.matches("https://x.icims.com/jobs/123/title/job") == "x.icims.com"
    # vanity domain without an iCIMS path shape must NOT match
    assert p.matches("https://careers.amd.com/") is None
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


# --- new JSON-API path -----------------------------------------------------


async def test_fetch_new_json_api() -> None:
    with respx.mock as respx_mock:
        _mock_new(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(NEW_HOST, SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "icims"
    assert r0.source_job_id == "87222"
    assert r0.company == "Advanced Micro Devices, Inc"
    assert r0.url == "https://careers-amd.icims.com/jobs/87222/login"


async def test_normalize_new_fields() -> None:
    with respx.mock as respx_mock:
        _mock_new(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(NEW_HOST, SearchQuery(), f)

    job = ICIMSProvider().normalize(raws[0])
    assert job.id == make_job_id("icims", "87222")
    assert job.title == "Lead Formal Verification Engineer"
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Engineering"  # falls back to categories[0].name
    assert job.locations[0].city == "Austin"
    assert job.locations[0].region == "Texas"
    assert job.remote is RemoteType.UNKNOWN
    assert job.salary is None  # 0/0 -> not present
    assert job.description_html == "<p>Build chips.</p>"
    assert job.description_text is None
    posted = job.posted_at.astimezone(timezone.utc)
    assert (posted.year, posted.month, posted.day) == (2026, 6, 17)

    remote = ICIMSProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE
    assert remote.employment_type is EmploymentType.PART_TIME
    assert remote.salary is not None
    assert remote.salary.min_amount == 120000.0
    assert remote.salary.max_amount == 160000.0


async def test_fetch_new_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock_new(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(NEW_HOST, SearchQuery(limit=1), f)
    assert len(raws) == 1


# --- classic HTML + JSON-LD path -------------------------------------------


async def test_fetch_classic_detects_and_parses() -> None:
    with respx.mock as respx_mock:
        _mock_classic(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(CLASSIC_HOST, SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "icims"
    assert r0.source_job_id == "152502"
    assert r0.company == "WinCo Foods"


async def test_normalize_classic_fields() -> None:
    with respx.mock as respx_mock:
        _mock_classic(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(CLASSIC_HOST, SearchQuery(), f)

    job = ICIMSProvider().normalize(raws[0])
    assert job.title == "Cashier"
    assert job.company == "WinCo Foods"
    assert job.employment_type is EmploymentType.OTHER
    assert job.locations[0].city == "Orangevale"
    assert job.locations[0].region == "CA"
    assert job.locations[0].country == "US"
    assert job.remote is RemoteType.UNKNOWN
    assert job.salary is None
    assert job.description_html == "<p>Join WinCo.</p>"
    assert job.posted_at is not None

    remote = ICIMSProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE
    assert remote.employment_type is EmploymentType.PART_TIME


async def test_fetch_classic_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock_classic(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(CLASSIC_HOST, SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_pinned_classic_skips_api_probe() -> None:
    """A ``{host}|classic`` token goes straight to the HTML path (no /api/jobs probe)."""
    with respx.mock as respx_mock:
        _mock_classic(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await ICIMSProvider().fetch(f"{CLASSIC_HOST}|classic", SearchQuery(), f)
    assert len(raws) == 2


# --- fetch_detail: 404-vs-transient hardening contract ----------------------

DETAIL_APPLY_URL = f"https://{CLASSIC_HOST}/jobs/152502/cashier/login"
DETAIL_URL = f"https://{CLASSIC_HOST}/jobs/152502/job?in_iframe=1"


class _FakeFetcher:
    """Stands in for AsyncFetcher: returns a fixed body, or raises a fixed exception."""

    def __init__(self, *, html: str | None = None, exc: BaseException | None = None) -> None:
        self._html = html
        self._exc = exc
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        assert self._html is not None
        return self._html


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", DETAIL_URL)
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


def _detail_ref(apply_url: str | None = DETAIL_APPLY_URL) -> DetailRef:
    return DetailRef(
        id="152502",
        source="icims",
        token=None,
        apply_url=apply_url,
        listing_url=None,
        content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await ICIMSProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await ICIMSProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await ICIMSProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await ICIMSProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_description() -> None:
    fetcher = _FakeFetcher(html=_detail("Cashier", "152502"))
    res = await ICIMSProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert res == "<p>Join WinCo.</p>"


async def test_fetch_detail_missing_url_raises() -> None:
    fetcher = _FakeFetcher(html="<html></html>")
    with pytest.raises(RuntimeError):
        await ICIMSProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)


async def test_fetch_detail_no_jsonld_raises() -> None:
    # A 200 with no JobPosting JSON-LD is indeterminate (iCIMS has no verified soft-404 body).
    fetcher = _FakeFetcher(html="<html><body>not found or something else</body></html>")
    with pytest.raises(RuntimeError):
        await ICIMSProvider().fetch_detail(_detail_ref(), fetcher)
