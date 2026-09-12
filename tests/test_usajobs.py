"""USAJOBS provider unit tests (offline, respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon.http import AsyncFetcher
from ergon.models import (
    EmploymentType,
    JobLevel,
    RawJob,
    RemoteType,
    SalaryInterval,
    SearchQuery,
)
from ergon.providers.usajobs import USAJobsProvider, _parse_dt

pytestmark = pytest.mark.anyio

API = "https://data.usajobs.gov/api/search"

_PAYLOAD = {
    "SearchResult": {
        "SearchResultCount": 1,
        "SearchResultItems": [
            {
                "MatchedObjectId": "987654",
                "MatchedObjectDescriptor": {
                    "PositionID": "DOE-ABC-123",
                    "PositionTitle": "Data Scientist",
                    "PositionURI": "https://www.usajobs.gov/job/987654",
                    "OrganizationName": "Department of Energy",
                    "DepartmentName": "Department of Energy",
                    "PositionLocation": [
                        {
                            "LocationName": "Washington, District of Columbia",
                            "CountryCode": "United States",
                            "CityName": "Washington",
                        }
                    ],
                    "PositionRemuneration": [
                        {
                            "MinimumRange": "120000",
                            "MaximumRange": "150000",
                            "RateIntervalCode": "PA",  # real USAJOBS code = Per Annum
                            "Description": "Per Year",
                        }
                    ],
                    "PositionSchedule": [{"Name": "Full-time"}],
                    # Realistic 7-digit fractional seconds (rejected by raw fromisoformat).
                    "PublicationStartDate": "2026-06-01T00:00:00.0000000",
                    "UserArea": {
                        "Details": {"JobSummary": "Analyze data.", "RemoteIndicator": True}
                    },
                },
            }
        ],
    }
}


def _provider() -> USAJobsProvider:
    return USAJobsProvider()


def _configured(monkeypatch) -> None:
    creds = {"USAJOBS_API_KEY": "test_key", "USAJOBS_EMAIL": "me@example.com"}
    monkeypatch.setattr("ergon.providers.usajobs.get_env", lambda k: creds.get(k))


def test_matches_always_none_aggregator() -> None:
    assert USAJobsProvider.matches("usajobs.gov") is None
    assert USAJobsProvider.is_aggregator is True


def test_parse_dt_handles_seven_digit_fraction() -> None:
    dt = _parse_dt("2026-06-01T00:00:00.0000000")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 6 and dt.day == 1
    assert _parse_dt(None) is None
    assert _parse_dt("not-a-date") is None


async def test_fetch_skips_without_keys(monkeypatch) -> None:
    monkeypatch.setattr("ergon.providers.usajobs.get_env", lambda k: None)
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=_PAYLOAD))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(keywords="data"), f)
    assert raws == []
    assert not route.called


async def test_fetch_sends_required_headers(monkeypatch) -> None:
    _configured(monkeypatch)
    with respx.mock:
        route = respx.get(API).mock(return_value=httpx.Response(200, json=_PAYLOAD))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch(
                "", SearchQuery(keywords="data scientist", location="Washington"), f
            )
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("Authorization-Key") == "test_key"
    assert req.headers.get("User-Agent") == "me@example.com"
    assert req.headers.get("Host") == "data.usajobs.gov"
    params = req.url.params
    assert params.get("Keyword") == "data scientist"
    assert params.get("LocationName") == "Washington"
    assert len(raws) == 1
    assert raws[0].company == "Department of Energy"


async def test_normalize_full_field_mapping(monkeypatch) -> None:
    _configured(monkeypatch)
    with respx.mock:
        respx.get(API).mock(return_value=httpx.Response(200, json=_PAYLOAD))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)
    job = _provider().normalize(raws[0])
    assert job.source == "usajobs"
    assert job.title == "Data Scientist"
    assert job.company == "Department of Energy"
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.remote is RemoteType.REMOTE  # RemoteIndicator true
    assert job.description_text == "Analyze data."
    assert job.locations[0].city == "Washington"
    assert job.locations[0].country == "United States"
    assert job.salary is not None
    assert job.salary.min_amount == 120000
    assert job.salary.max_amount == 150000
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR
    assert job.apply_url == "https://www.usajobs.gov/job/987654"
    assert job.posted_at is not None


def _descriptor(**over: object) -> RawJob:
    payload = {
        "PositionID": "DOE-ABC-123",
        "PositionTitle": "Data Scientist",
        "OrganizationName": "Department of Energy",
        **over,
    }
    return RawJob(
        source="usajobs", source_job_id="987654", company="Department of Energy", payload=payload
    )


def _graded(plan: str, low: str | None = None, high: str | None = None) -> RawJob:
    """A descriptor shaped like the live response: pay plan in JobGrade[].Code, numeric grade in
    UserArea.Details.LowGrade/HighGrade."""
    details: dict[str, object] = {"JobSummary": "Analyze data."}
    if low is not None:
        details["LowGrade"] = low
    if high is not None:
        details["HighGrade"] = high
    return _descriptor(JobGrade=[{"Code": plan}], UserArea={"Details": details})


def test_gs_grade_maps_to_level() -> None:
    """Pay plan GS + the LOW grade (what a ladder posting actually hires at) -> coarse level."""
    cases = {
        "07": JobLevel.ENTRY,
        "11": JobLevel.MID,
        "13": JobLevel.SENIOR,
        "15": JobLevel.PRINCIPAL,
    }
    for grade, expected in cases.items():
        assert _provider().normalize(_graded("GS", grade)).level is expected, grade
    # "GS-13/14" ladder: the low grade wins.
    assert _provider().normalize(_graded("GS", "13", "14")).level is JobLevel.SENIOR
    # HighGrade alone still works when only it is present.
    assert _provider().normalize(_graded("GS", None, "14")).level is JobLevel.STAFF
    # Some feeds put the whole grade in the code instead.
    assert _provider().normalize(_descriptor(JobGrade=[{"Code": "GS-14"}])).level is JobLevel.STAFF


def test_non_gs_pay_plans_and_missing_grades_are_never_guessed() -> None:
    """SES/Senior-Level/wage-grade plans have no GS equivalent -> UNKNOWN, never a guess."""
    for plan in ("ES", "SL", "ST", "AD", "WG"):
        assert _provider().normalize(_graded(plan, "13")).level is JobLevel.UNKNOWN, plan
    assert _provider().normalize(_graded("GS")).level is JobLevel.UNKNOWN  # no grade number
    assert _provider().normalize(_graded("GS", "n/a")).level is JobLevel.UNKNOWN
    assert _provider().normalize(_graded("GS", "3")).level is JobLevel.UNKNOWN  # outside the table
    assert _provider().normalize(_descriptor()).level is JobLevel.UNKNOWN  # no JobGrade at all


def test_qualification_summary_yields_years_and_degree() -> None:
    """No structured years/education field is documented -- the prose runs through the shared
    text extractors."""
    job = _provider().normalize(
        _descriptor(
            QualificationSummary=(
                "Applicants must have a Bachelor's degree in statistics and at least "
                "3 years of specialized experience equivalent to the GS-12 level."
            )
        )
    )
    assert job.years_experience_min == 3
    assert job.degree_min == "bachelor"

    bare = _provider().normalize(_descriptor(QualificationSummary="See the announcement."))
    assert (bare.years_experience_min, bare.years_experience_max) == (None, None)
    assert bare.degree_min is None
