"""Unit tests for the Paylocity Recruiting provider (offline — parses the public feed envelope)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import EmploymentType, RawJob, SearchQuery
from ergon.providers.paylocity import PaylocityProvider

pytestmark = pytest.mark.anyio

GUID = "b181f77f-0432-453f-b229-869d786bb46c"
V2 = f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{GUID}"

_FEED = {
    "displayName": "Acme Corp",
    "showVideo": False,
    "jobs": [
        {
            "jobId": "101",
            "title": "Staff Engineer",
            "companyName": "Acme Corp",
            "applyUrl": "https://recruiting.paylocity.com/recruiting/jobs/Details/101",
            "publishedDate": "2026-06-18T00:00:00Z",
            "description": "<p>Build things.</p>",
            "employmentType": "Full Time",
            "jobLocation": {"city": "Austin", "state": "TX", "country": "USA"},
        },
        {  # flattened location + duplicate id (must dedupe) + remote
            "jobId": "102",
            "title": "Remote Designer",
            "city": "Remote",
            "jobType": "Part-Time",
        },
        {"jobId": "101", "title": "dup — dropped"},
        {"title": "no id — dropped"},
    ],
}


def test_matches() -> None:
    assert (
        PaylocityProvider.matches(
            f"https://recruiting.paylocity.com/recruiting/jobs/All/{GUID}/Careers"
        )
        == GUID
    )
    assert PaylocityProvider.matches("https://recruiting.paylocity.com/") is None
    assert PaylocityProvider.matches("https://example.com/" + GUID) is None


async def test_fetch_parses_and_normalizes() -> None:
    with respx.mock as m:
        m.get(V2).mock(return_value=httpx.Response(200, json=_FEED))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PaylocityProvider().fetch(GUID, SearchQuery(), f)
    assert {r.source_job_id for r in raws} == {"101", "102"}  # deduped, empty-id dropped
    j = PaylocityProvider().normalize(raws[0])
    assert j.title == "Staff Engineer"
    assert j.company == "Acme Corp"
    assert j.locations[0].raw == "Austin, TX, USA"
    assert j.employment_type.value == "full_time"
    assert j.posted_at is not None and j.posted_at.year == 2026
    assert j.apply_url.endswith("/Details/101")


async def test_flattened_location_and_remote() -> None:
    with respx.mock as m:
        m.get(V2).mock(return_value=httpx.Response(200, json=_FEED))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await PaylocityProvider().fetch(GUID, SearchQuery(), f)
    j = PaylocityProvider().normalize(next(r for r in raws if r.source_job_id == "102"))
    assert j.locations[0].raw == "Remote"
    assert j.remote.value == "remote"
    assert j.employment_type.value == "part_time"


def _raw(payload: dict) -> RawJob:
    return RawJob(source="paylocity", source_job_id="201", company="Acme Corp", payload=payload)


def test_documented_feed_keys_for_type_department_and_pay() -> None:
    """The feed's real keys: jobTypesArray/jobTypes, hiringDepartment, salaryDescription."""
    job = PaylocityProvider().normalize(
        _raw(
            {
                "jobId": "201",
                "title": "Staff Engineer",
                "jobTypesArray": ["Full-Time", "Temporary"],  # first entry wins
                "hiringDepartment": "Engineering",
                "salaryDescription": "$75,000 - $95,000 per year",
            }
        )
    )
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Engineering"
    assert job.salary is not None
    assert (job.salary.min_amount, job.salary.max_amount) == (75000.0, 95000.0)
    assert job.salary.currency == "USD"

    comma = PaylocityProvider().normalize(
        _raw({"jobId": "202", "title": "Intern", "jobTypes": "Internship,Part-Time"})
    )
    assert comma.employment_type is EmploymentType.INTERNSHIP


def test_unmapped_type_and_unparseable_pay_stay_empty() -> None:
    job = PaylocityProvider().normalize(
        _raw(
            {
                "jobId": "203",
                "title": "Contractor",
                "jobTypesArray": [],
                "salaryDescription": "Competitive",
            }
        )
    )
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.salary is None  # free text with no parseable range is never guessed at
    assert job.department is None


async def test_limit_and_empty() -> None:
    with respx.mock as m:
        m.get(V2).mock(return_value=httpx.Response(200, json=_FEED))
        async with AsyncFetcher(per_host_rate=100) as f:
            assert len(await PaylocityProvider().fetch(GUID, SearchQuery(limit=1), f)) == 1
    with respx.mock as m:
        m.get(V2).mock(return_value=httpx.Response(200, json={"jobs": []}))
        async with AsyncFetcher(per_host_rate=100) as f:
            assert await PaylocityProvider().fetch(GUID, SearchQuery(), f) == []
