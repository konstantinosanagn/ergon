"""Unit tests for the Personio provider (XML; respx-mocked, offline).

Fixture ``personio_sample.xml`` mirrors the live
``https://personio.jobs.personio.de/xml`` structure (token "personio"), with a populated
``jobDescriptions`` block and a second remote/working-student position added to exercise the
description, additional-office, and employment-type paths.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from jobspine.http import AsyncFetcher
from jobspine.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from jobspine.providers.personio import PersonioProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
FEED_URL = "https://personio.jobs.personio.de/xml"


def _xml() -> str:
    return (FIXTURES / "personio_sample.xml").read_text(encoding="utf-8")


def test_matches_recognizes_hosts() -> None:
    p = PersonioProvider
    assert p.matches("https://personio.jobs.personio.de/xml") == "personio"
    assert p.matches("https://acme.jobs.personio.de") == "acme"
    assert p.matches("acme.jobs.personio.com/job/123") == "acme"
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_parses_xml_to_rawjobs() -> None:
    with respx.mock:
        route = respx.get(FEED_URL).mock(
            return_value=httpx.Response(
                200, text=_xml(), headers={"Content-Type": "application/xml"}
            )
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PersonioProvider().fetch("personio", SearchQuery(), f)

        assert str(route.calls.last.request.url) == FEED_URL

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "personio"
    assert r0.source_job_id == "1834171"
    assert r0.company == "personio"
    assert r0.token == "personio"
    assert r0.url == "https://personio.jobs.personio.de/job/1834171"
    assert r0.payload["name"] == "Staff Software Engineer, Data Platform"
    assert r0.payload["additionalOffices"] == ["Berlin"]
    # jobDescriptions flattened to a list of {name, value} sections
    assert isinstance(r0.payload["jobDescriptions"], list)
    assert r0.payload["jobDescriptions"][0]["name"] == "Your responsibilities"


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=_xml()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PersonioProvider().fetch("personio", SearchQuery(), f)

    job = PersonioProvider().normalize(raws[0])

    assert job.id == make_job_id("personio", "1834171")
    assert job.source == "personio"
    assert job.source_job_id == "1834171"
    assert job.title == "Staff Software Engineer, Data Platform"
    assert job.company == "personio"
    assert job.apply_url == "https://personio.jobs.personio.de/job/1834171"

    # primary office + additionalOffices
    assert [loc.raw for loc in job.locations] == ["Munich", "Berlin"]
    assert all(loc.is_remote is False for loc in job.locations)
    assert job.remote is RemoteType.UNKNOWN

    # employmentType "permanent" -> FULL_TIME
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Product and Tech"
    assert job.salary is None

    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.posted_at.year == 2024

    assert job.description_html is not None
    assert "<h3>Your responsibilities</h3>" in job.description_html
    assert job.description_text is not None and "data platform" in job.description_text
    assert job.raw == raws[0].payload


async def test_normalize_remote_office_and_intern() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text=_xml()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PersonioProvider().fetch("personio", SearchQuery(), f)

    job = PersonioProvider().normalize(raws[1])
    assert job.source_job_id == "2099001"
    assert job.title == "Working Student, Customer Support (m/f/d)"
    # office "Remote" -> is_remote True -> REMOTE
    assert job.locations[0].raw == "Remote"
    assert job.locations[0].is_remote is True
    assert job.remote is RemoteType.REMOTE
    # employmentType "intern" -> INTERNSHIP
    assert job.employment_type is EmploymentType.INTERNSHIP
    # empty jobDescriptions -> no description
    assert job.description_html is None
    assert job.description_text is None


async def test_fetch_malformed_xml_returns_empty() -> None:
    with respx.mock:
        respx.get(FEED_URL).mock(return_value=httpx.Response(200, text="<not-xml"))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PersonioProvider().fetch("personio", SearchQuery(), f)
    assert raws == []
