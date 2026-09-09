"""Unit tests for the Dayforce provider — matches() + normalize() (pure; fetch is browser-backed)."""

from __future__ import annotations

from ergon.models import RawJob
from ergon.providers.dayforce import DayforceProvider


def test_matches_unified_and_legacy_hosts() -> None:
    m = DayforceProvider.matches
    assert m("https://jobs.dayforcehcm.com/bassett/CANDIDATEPORTAL") == "bassett"
    assert m("https://jobs.dayforcehcm.com/en-US/acv/CANDIDATEPORTAL/jobs") == "acv"
    assert m("https://www.dayforcehcm.com/CandidatePortal/en-US/angio/Site/AngioCareers") == "angio"
    assert m("https://us251.dayforcehcm.com/CandidatePortal/en-US/bassett") == "bassett"
    # case-insensitive namespace
    assert m("https://jobs.dayforcehcm.com/ALTG/CANDIDATEPORTAL") == "altg"


def test_matches_rejects_non_dayforce() -> None:
    assert DayforceProvider.matches("https://boards.greenhouse.io/acme") is None
    assert DayforceProvider.matches("https://acme.wd1.myworkdayjobs.com/Acme") is None


def test_parse_carries_board_and_company() -> None:
    assert DayforceProvider._parse("bassett") == ("bassett", "CANDIDATEPORTAL", None)
    assert DayforceProvider._parse("acv|CANDIDATEPORTAL|ACV Auctions") == (
        "acv",
        "CANDIDATEPORTAL",
        "ACV Auctions",
    )


def test_normalize_fields() -> None:
    raw = RawJob(
        source="dayforce",
        source_job_id="1737",
        company="Bassett Furniture",
        token="bassett|CANDIDATEPORTAL|Bassett Furniture",
        url="https://jobs.dayforcehcm.com/bassett/CANDIDATEPORTAL/jobs/1737",
        payload={
            "jobPostingId": 1737,
            "jobTitle": "DESIGN CONSULTANT",
            "jobDescription": "<p>Design sales</p>",
            "postingStartTimestampUTC": "2026-06-17T13:40:00+00:00",
            "hasVirtualLocation": False,
            "postingLocations": [
                {"cityName": "York", "stateCode": "PA", "formattedAddress": "York, PA, USA"}
            ],
        },
    )
    job = DayforceProvider().normalize(raw)
    assert job.title == "DESIGN CONSULTANT"
    assert job.company == "Bassett Furniture"
    assert job.locations[0].city == "York"
    assert job.locations[0].region == "PA"
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert "jobs/1737" in job.apply_url
