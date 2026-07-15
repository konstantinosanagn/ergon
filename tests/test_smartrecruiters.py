"""Unit tests for the SmartRecruiters provider (offline, respx-mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.smartrecruiters import SmartRecruitersProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/Visa/postings"


def _fixture() -> dict:
    return json.loads((FIXTURES / "smartrecruiters_sample.json").read_text())


# --- matches ----------------------------------------------------------------


def test_matches_recognizes_all_hosts() -> None:
    p = SmartRecruitersProvider
    assert p.matches("https://careers.smartrecruiters.com/Visa") == "Visa"
    assert p.matches("https://jobs.smartrecruiters.com/Visa/12345") == "Visa"
    assert p.matches("https://api.smartrecruiters.com/v1/companies/Visa/postings") == "Visa"
    assert p.matches("careers.smartrecruiters.com/Acme?x=1#y") == "Acme"
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


# --- fetch / token + param construction ------------------------------------


async def test_fetch_builds_rawjobs_and_hits_postings_endpoint() -> None:
    with respx.mock:
        route = respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SmartRecruitersProvider().fetch("Visa", SearchQuery(), f)

        request = route.calls.last.request
        assert request.url.params["limit"] == "100"
        assert request.url.params["offset"] == "0"
        assert str(request.url).startswith(POSTINGS_URL)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "smartrecruiters"
    assert r0.source_job_id == "744000129971988"
    assert r0.company == "Visa"
    assert r0.token == "Visa"
    assert r0.url == "https://jobs.smartrecruiters.com/Visa/744000129971988"
    assert r0.payload["name"] == "Director"


async def test_fetch_forwards_server_side_filters() -> None:
    with respx.mock:
        route = respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        query = SearchQuery(keywords="engineer", country="us", city="Austin")
        async with AsyncFetcher(per_host_rate=100) as f:
            await SmartRecruitersProvider().fetch("Visa", query, f)

        params = route.calls.last.request.url.params
        assert params["q"] == "engineer"
        assert params["country"] == "us"
        assert params["city"] == "Austin"


# --- pagination -------------------------------------------------------------


async def test_fetch_paginates_by_offset_concurrently() -> None:
    total = 250  # -> 3 pages of 100 (offsets 0, 100, 200)
    offsets_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets_seen.append(offset)
        page = [
            {
                "id": str(1_000_000 + i),
                "name": f"Engineer {i}",
                "company": {"name": "Visa"},
                "location": {"city": "Austin", "country": "us", "remote": False},
                "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
                "department": {"label": "Eng"},
                "releasedDate": "2026-01-01T00:00:00.000Z",
            }
            for i in range(offset, min(offset + 100, total))
        ]
        return httpx.Response(
            200, json={"offset": offset, "limit": 100, "totalFound": total, "content": page}
        )

    with respx.mock:
        respx.get(POSTINGS_URL).mock(side_effect=handler)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SmartRecruitersProvider().fetch("Visa", SearchQuery(), f)

    # All three pages fetched (page 1 first, then 100/200 concurrently).
    assert sorted(offsets_seen) == [0, 100, 200]
    assert len(raws) == total
    # Order preserved: first page first, ascending offsets.
    assert raws[0].source_job_id == "1000000"
    assert raws[-1].source_job_id == str(1_000_000 + total - 1)


async def test_fetch_single_page_when_total_fits() -> None:
    offsets_seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets_seen.append(int(request.url.params["offset"]))
        return httpx.Response(200, json=_fixture())  # totalFound=10

    with respx.mock:
        respx.get(POSTINGS_URL).mock(side_effect=handler)
        async with AsyncFetcher(per_host_rate=100) as f:
            await SmartRecruitersProvider().fetch("Visa", SearchQuery(), f)

    # totalFound (10) < limit (100): exactly one request.
    assert offsets_seen == [0]


# --- normalize --------------------------------------------------------------


async def test_normalize_maps_every_field() -> None:
    with respx.mock:
        respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SmartRecruitersProvider().fetch("Visa", SearchQuery(), f)

    provider = SmartRecruitersProvider()
    job = provider.normalize(raws[0])

    assert job.id == make_job_id("smartrecruiters", "744000129971988")
    assert job.title == "Director"
    assert job.company == "Visa"
    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Bengaluru"
    assert loc.region == "KA"
    assert loc.country == "IN"  # uppercased
    assert loc.is_remote is False
    assert job.remote is RemoteType.ONSITE  # remote=False, hybrid=False, has location
    assert job.employment_type is EmploymentType.FULL_TIME  # "permanent"
    assert job.department == "Cyber Security"
    assert job.salary is None
    assert job.apply_url == "https://jobs.smartrecruiters.com/Visa/744000129971988"
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.posted_at.year == 2026


async def test_normalize_detects_hybrid() -> None:
    with respx.mock:
        respx.get(POSTINGS_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await SmartRecruitersProvider().fetch("Visa", SearchQuery(), f)

    # second sample posting is hybrid (Austin, TX, hybrid=True)
    job = SmartRecruitersProvider().normalize(raws[1])
    assert job.remote is RemoteType.HYBRID
    assert job.locations[0].city == "Austin"


def test_normalize_remote_flag() -> None:
    from ergon_tracker.models import RawJob

    provider = SmartRecruitersProvider()
    payload = {
        "id": "1",
        "name": "Remote Eng",
        "company": {"name": "Visa"},
        "location": {"remote": True},
        "typeOfEmployment": {"id": "contractor"},
    }
    job = provider.normalize(
        RawJob(source="smartrecruiters", source_job_id="1", company="Visa", payload=payload)
    )
    assert job.remote is RemoteType.REMOTE
    assert job.employment_type is EmploymentType.CONTRACT


async def test_fetch_detail_includes_additional_information_pay_section() -> None:
    # US pay-transparency salary ranges live in jobAd.sections.additionalInformation, NOT in
    # jobDescription/qualifications. fetch_detail must concatenate it so the enrich CompExtractor
    # can recover the range (SR was 5.7% salary because this section was dropped).
    import respx
    from ergon_tracker.index.detail import DetailRef

    posting = {
        "jobAd": {
            "sections": {
                "jobDescription": {"text": "Build reliable services."},
                "qualifications": {"text": "5+ years, BS in CS."},
                "additionalInformation": {
                    "text": "The U.S. base salary range for this role is $88,000 - $95,000."
                },
            }
        }
    }
    url = "https://api.smartrecruiters.com/v1/companies/boschgroup/postings/744000137669170"
    ref = DetailRef(
        id="x",
        source="smartrecruiters",
        token=None,
        apply_url="https://jobs.smartrecruiters.com/boschgroup/744000137669170",
        listing_url=None,
        content_sig="",
    )
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=posting))
        async with AsyncFetcher(per_host_rate=100) as f:
            body = await SmartRecruitersProvider().fetch_detail(ref, f)

    assert body is not None
    assert "$88,000 - $95,000" in body  # the pay section is present
    assert "5+ years" in body  # qualifications still included
    from ergon_tracker.extract.comp import parse_salary

    sal = parse_salary(body)
    assert sal is not None and sal.min_amount == 88_000 and sal.max_amount == 95_000


async def test_fetch_detail_recovers_when_job_description_empty() -> None:
    # Measured bug (fixed): ~40% of failed SR postings have an EMPTY jobDescription.text but real
    # content in qualifications/additionalInformation. The parser must NOT bail on an empty
    # jobDescription -- it must return whatever JD-relevant sections carry text.
    from ergon_tracker.index.detail import DetailRef

    posting = {
        "jobAd": {
            "sections": {
                "jobDescription": {"text": ""},  # empty -- the pre-fix bail trigger
                "qualifications": {"text": "5+ years, BS in CS."},
                "additionalInformation": {"text": "Salary $88,000 - $95,000."},
            }
        }
    }
    url = "https://api.smartrecruiters.com/v1/companies/acme/postings/12345"
    ref = DetailRef(id="x", source="smartrecruiters", token=None,
                    apply_url="https://jobs.smartrecruiters.com/acme/12345", listing_url=None,
                    content_sig="")
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=posting))
        async with AsyncFetcher(per_host_rate=100) as f:
            body = await SmartRecruitersProvider().fetch_detail(ref, f)
    assert body is not None and "5+ years" in body and "$88,000" in body


async def test_fetch_detail_none_when_only_company_boilerplate() -> None:
    # companyDescription is deliberately excluded (boilerplate, not the role) -- a posting with only
    # an empty jobDescription + companyDescription must still return None, not company marketing.
    from ergon_tracker.index.detail import DetailRef

    posting = {"jobAd": {"sections": {
        "jobDescription": {"text": ""},
        "companyDescription": {"text": "We are Acme, a great place to work."},
    }}}
    url = "https://api.smartrecruiters.com/v1/companies/acme/postings/999"
    ref = DetailRef(id="x", source="smartrecruiters", token=None,
                    apply_url="https://jobs.smartrecruiters.com/acme/999", listing_url=None,
                    content_sig="")
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=posting))
        async with AsyncFetcher(per_host_rate=100) as f:
            body = await SmartRecruitersProvider().fetch_detail(ref, f)
    assert body is None
