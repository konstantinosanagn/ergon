"""Unit tests for the generic schema.org JobPosting provider (respx-mocked, offline).

Fixtures mirror the real server-rendered Phenom JSON-LD shape live-confirmed on
``jobs.cvshealth.com`` and ``talent.lowes.com`` (2026-06-18): ``employmentType`` is a list,
``identifier`` is a ``PropertyValue`` carrying the req id, ``jobLocation`` is a single ``Place``
with a ``PostalAddress``, and ``validThrough``/``baseSalary``/``url`` are often null.
"""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.schemaorg import SchemaOrgProvider

pytestmark = pytest.mark.anyio

HOST = "jobs.cvshealth.com"
INDEX = f"https://{HOST}/us/en/sitemap_index.xml"


# --- fixtures --------------------------------------------------------------


def _sitemap_index(children: list[str]) -> str:
    locs = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</sitemapindex>"
    )


def _urlset(urls: list[str]) -> str:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    )


def _detail(
    *,
    title: str,
    req_id: str,
    url: str,
    employment: str = "FULL_TIME",
    city: str = "Lenexa",
    region: str = "Kansas",
    country: str = "United States",
    salary: tuple[int, int] | None = None,
) -> str:
    base = ""
    if salary is not None:
        base = (
            ',"baseSalary":{"@type":"MonetaryAmount","currency":"USD",'
            f'"value":{{"@type":"QuantitativeValue","minValue":{salary[0]},'
            f'"maxValue":{salary[1]},"unitText":"YEAR"}}}}'
        )
    ld = f"""
    {{
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "{title}",
      "datePosted": "2026-04-05",
      "validThrough": null,
      "employmentType": ["{employment}"],
      "occupationalCategory": "Store Operations",
      "identifier": {{"@type": "PropertyValue", "name": "CVS Health", "value": "{req_id}"}},
      "hiringOrganization": {{"@type": "Organization", "name": "CVS Health",
          "url": "{url}"}},
      "jobLocation": {{"@type": "Place", "geo": {{"@type": "GeoCoordinates",
          "latitude": "38.9", "longitude": "-94.7"}}, "address": {{
          "@type": "PostalAddress", "postalCode": "66219", "addressCountry": "{country}",
          "addressLocality": "{city}", "addressRegion": "{region}"}}}},
      "description": "<p>Join CVS Health.</p>"{base}
    }}
    """
    return (
        f'<html><head><script type="application/ld+json">{ld}</script>'
        '<script type="application/ld+json">{"@type":"WebPage"}</script></head>'
        f"<body>{title}</body></html>"
    )


JOB1 = f"https://{HOST}/us/en/job/R0870953/Inventory-Control-Coordinator"
JOB2 = f"https://{HOST}/us/en/job/R0870999/Remote-Care-Coordinator"
# A non-job URL (category listing) that must be ignored by the collector.
CAT = f"https://{HOST}/us/en/search-results"
# A job URL whose detail page carries no server-rendered JobPosting -> skipped gracefully.
JOB_NO_LD = f"https://{HOST}/us/en/job/R0000000/Client-Rendered"


def _mock(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"https://{HOST}/robots.txt").mock(
        return_value=httpx.Response(200, text=f"User-agent: *\nSitemap: {INDEX}\n")
    )
    respx_mock.get(INDEX).mock(
        return_value=httpx.Response(
            200, text=_sitemap_index([f"https://{HOST}/us/en/sitemap1.xml"])
        )
    )
    respx_mock.get(f"https://{HOST}/us/en/sitemap1.xml").mock(
        return_value=httpx.Response(200, text=_urlset([JOB1, CAT, JOB2, JOB_NO_LD]))
    )
    respx_mock.get(JOB1).mock(
        return_value=httpx.Response(
            200,
            html=_detail(title="Inventory Control Coordinator", req_id="R0870953", url=JOB1),
        )
    )
    respx_mock.get(JOB2).mock(
        return_value=httpx.Response(
            200,
            html=_detail(
                title="Remote Care Coordinator",
                req_id="R0870999",
                url=JOB2,
                employment="PART_TIME",
                city="Remote",
                region="",
                country="United States",
                salary=(45000, 60000),
            ),
        )
    )
    respx_mock.get(JOB_NO_LD).mock(
        return_value=httpx.Response(200, html="<html><body>no json-ld here</body></html>")
    )


# --- matches ---------------------------------------------------------------


def test_matches_requires_scheme_prefix() -> None:
    p = SchemaOrgProvider
    assert p.matches("schemaorg:jobs.cvshealth.com") == "jobs.cvshealth.com"
    assert p.matches("schema:https://talent.lowes.com/us/en/sitemap_index.xml") == (
        "https://talent.lowes.com/us/en/sitemap_index.xml"
    )
    # Must NOT auto-claim bare hosts/URLs (would collide with every other provider).
    assert p.matches("jobs.cvshealth.com") is None
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("schemaorg:") is None


# --- fetch via host -> robots -> index -> urlset -> detail -----------------


async def test_fetch_resolves_sitemap_and_parses_jsonld() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SchemaOrgProvider().fetch(HOST, SearchQuery(), f)

    # Two job pages carry JobPosting JSON-LD; the category URL is filtered out and the
    # no-JSON-LD job page is skipped.
    assert len(raws) == 2
    ids = {r.source_job_id for r in raws}
    assert ids == {"R0870953", "R0870999"}
    r0 = next(r for r in raws if r.source_job_id == "R0870953")
    assert r0.source == "schemaorg"
    assert r0.company == "CVS Health"
    assert r0.url == JOB1


async def test_fetch_accepts_schemeprefixed_token() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SchemaOrgProvider().fetch(f"schemaorg:{HOST}", SearchQuery(), f)
    assert len(raws) == 2


async def test_fetch_direct_sitemap_url_token() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SchemaOrgProvider().fetch(INDEX, SearchQuery(), f)
    assert len(raws) == 2


# --- normalize -------------------------------------------------------------


async def test_normalize_fields() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SchemaOrgProvider().fetch(HOST, SearchQuery(), f)

    by_id = {r.source_job_id: r for r in raws}
    job = SchemaOrgProvider().normalize(by_id["R0870953"])
    assert job.id == make_job_id("schemaorg", "R0870953")
    assert job.title == "Inventory Control Coordinator"
    assert job.company == "CVS Health"
    assert job.employment_type is EmploymentType.FULL_TIME  # parsed from a list
    assert job.department == "Store Operations"
    assert job.locations[0].city == "Lenexa"
    assert job.locations[0].region == "Kansas"
    assert job.locations[0].country == "United States"
    assert job.remote is RemoteType.UNKNOWN
    assert job.salary is None  # baseSalary null
    assert job.description_html == "<p>Join CVS Health.</p>"
    assert job.description_text is None
    posted = job.posted_at
    assert posted is not None
    posted = posted.astimezone(timezone.utc)
    assert (posted.year, posted.month, posted.day) == (2026, 4, 5)

    remote = SchemaOrgProvider().normalize(by_id["R0870999"])
    assert remote.remote is RemoteType.REMOTE
    assert remote.employment_type is EmploymentType.PART_TIME
    assert remote.salary is not None
    assert remote.salary.min_amount == 45000.0
    assert remote.salary.max_amount == 60000.0
    assert remote.salary.currency == "USD"


# --- bounds ----------------------------------------------------------------


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SchemaOrgProvider().fetch(HOST, SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_degrades_to_empty_on_missing_sitemap() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(f"https://{HOST}/robots.txt").mock(return_value=httpx.Response(404))
        # All candidate sitemap paths 404 -> no roots -> [].
        respx_mock.get(url__startswith=f"https://{HOST}/").mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SchemaOrgProvider().fetch(HOST, SearchQuery(), f)
    assert raws == []
