"""Unit tests for the PeopleAdmin provider (offline: respx-mocked Atom feed + parse/normalize).

PeopleAdmin serves a tenant's whole posting list as one Atom feed at ``/postings/search.atom``;
these tests cover host/company token expansion, feed parsing + dedup, and normalize (location is
never invented — it isn't in the feed).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.peopleadmin import PeopleAdminProvider

pytestmark = pytest.mark.anyio

FEED_URL = "https://unmc.peopleadmin.com/postings/search.atom"

# Two distinct postings + a third entry repeating id 2959345 (to exercise dedup). Entities in the
# title (&amp;) and an HTML-escaped <content> body verify the unescape path.
ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://unmc.peopleadmin.com/postings/60051</id>
    <title>Nurse Practitioner &amp; Educator</title>
    <link rel="alternate" href="https://unmc.peopleadmin.com/postings/60051"/>
    <author><name>College of Nursing</name></author>
    <content type="html">&lt;p&gt;Join our team.&lt;/p&gt;</content>
    <published>2026-05-30T00:00:00Z</published>
  </entry>
  <entry>
    <id>https://unmc.peopleadmin.com/postings/2959345</id>
    <title>Academic Advisor</title>
    <link rel="alternate" href="https://unmc.peopleadmin.com/postings/2959345"/>
    <author><name>Student Affairs</name></author>
    <content type="html">Advise students.</content>
    <published>2026-06-01T00:00:00Z</published>
  </entry>
  <entry>
    <id>https://unmc.peopleadmin.com/postings/2959345</id>
    <title>Academic Advisor (duplicate id)</title>
  </entry>
</feed>"""


# --- matches / token expansion ---------------------------------------------


def test_matches_only_peopleadmin_hosts() -> None:
    p = PeopleAdminProvider
    assert p.matches("https://unmc.peopleadmin.com/postings/search") == "unmc.peopleadmin.com"
    assert p.matches("unmc.peopleadmin.com") == "unmc.peopleadmin.com"
    assert p.matches("https://jobs.rutgers.edu/postings") is None  # white-label host isn't resolved
    assert p.matches("https://boards.greenhouse.io/acme") is None


def test_host_expands_bare_subdomain() -> None:
    assert PeopleAdminProvider._host("unmc") == "unmc.peopleadmin.com"
    assert PeopleAdminProvider._host("unmc.peopleadmin.com") == "unmc.peopleadmin.com"
    assert PeopleAdminProvider._host("jobs.rutgers.edu") == "jobs.rutgers.edu"  # custom host as-is


def test_company_label() -> None:
    assert PeopleAdminProvider._company("unmc.peopleadmin.com") == "unmc"
    assert PeopleAdminProvider._company("jobs.rutgers.edu") == "rutgers"  # registrable label


# --- fetch ------------------------------------------------------------------


async def test_fetch_parses_feed_and_dedupes() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=ATOM))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleAdminProvider().fetch("unmc", SearchQuery(), f)

    assert [r.source_job_id for r in raws] == ["60051", "2959345"]  # dedup drops the repeat
    r0 = raws[0]
    assert r0.source == "peopleadmin" and r0.company == "unmc"
    assert r0.payload["title"] == "Nurse Practitioner & Educator"  # entity decoded
    assert r0.url == "https://unmc.peopleadmin.com/postings/60051"


async def test_fetch_honors_limit() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=ATOM))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleAdminProvider().fetch("unmc", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_network_error_returns_empty() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("boom"))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleAdminProvider().fetch("unmc", SearchQuery(), f)
    assert raws == []


# --- normalize --------------------------------------------------------------


async def test_normalize_fields_and_no_invented_location() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=ATOM))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PeopleAdminProvider().fetch("unmc", SearchQuery(), f)

    job = PeopleAdminProvider().normalize(raws[0])
    assert job.id == make_job_id("peopleadmin", "60051")
    assert job.company == "unmc"
    assert job.title == "Nurse Practitioner & Educator"
    assert job.department == "College of Nursing"
    assert job.description_html == "<p>Join our team.</p>"
    assert job.locations == []  # feed carries no location -> never invented
    assert job.remote == RemoteType.UNKNOWN
    assert job.posted_at is not None and job.posted_at.year == 2026


def test_date_parses_z_suffix() -> None:
    assert PeopleAdminProvider._date("2026-05-30T00:00:00Z") is not None
    assert PeopleAdminProvider._date("") is None
    assert PeopleAdminProvider._date(None) is None
