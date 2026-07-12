"""Unit tests for Jobicy's ``jobLevel``/``jobIndustry`` -> ``JobPosting`` mapping (offline).

Jobicy's remote-jobs API reports seniority as a plain string under ``jobLevel`` (observed live
values include "Midweight", "Senior", "Entry Level", ...) and an industry/sector string under
``jobIndustry``. ``normalize()`` must run ``jobLevel`` through ``level_from_ats_vocab`` (which
already resolves "Midweight" -> MID via its substring match on "mid" — verified directly, no
provider-local alias needed) and pass ``jobIndustry`` straight through to ``JobPosting.sector``.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import JobLevel, RawJob
from ergon_tracker.providers.jobicy import JobicyProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="jobicy",
        source_job_id=str(payload.get("id", "")),
        company=payload.get("companyName") or "",
        token=None,
        url=payload.get("url"),
        payload=payload,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "123",
        "jobTitle": "Software Engineer",
        "companyName": "Acme",
        "jobGeo": "Worldwide",
        "jobType": ["Full-Time"],
        "jobLevel": "Midweight",
        "jobIndustry": "Fintech",
        "url": "https://jobicy.com/jobs/123",
        "pubDate": "2024-01-01T00:00:00Z",
        "jobExcerpt": "Job description.",
    }
    base.update(overrides)
    return base


def test_normalize_maps_midweight_to_mid() -> None:
    job = JobicyProvider().normalize(_raw(_payload(jobLevel="Midweight")))

    assert job.level is JobLevel.MID


def test_normalize_maps_senior() -> None:
    job = JobicyProvider().normalize(_raw(_payload(jobLevel="Senior")))

    assert job.level is JobLevel.SENIOR


def test_normalize_maps_junior() -> None:
    job = JobicyProvider().normalize(_raw(_payload(jobLevel="Junior")))

    assert job.level is JobLevel.JUNIOR


def test_normalize_missing_job_level_is_unknown() -> None:
    payload = _payload()
    del payload["jobLevel"]
    job = JobicyProvider().normalize(_raw(payload))

    assert job.level is JobLevel.UNKNOWN


def test_normalize_maps_job_industry_to_sector() -> None:
    job = JobicyProvider().normalize(_raw(_payload(jobIndustry="Fintech")))

    assert job.sector == "Fintech"


def test_normalize_missing_job_industry_leaves_sector_none() -> None:
    payload = _payload()
    del payload["jobIndustry"]
    job = JobicyProvider().normalize(_raw(payload))

    assert job.sector is None


def test_normalize_job_industry_as_list() -> None:
    # The real jobicy API returns jobIndustry as a LIST (e.g. ["Marketing"]); take the first entry.
    payload = _payload()
    payload["jobIndustry"] = ["Marketing", "Advertising"]
    job = JobicyProvider().normalize(_raw(payload))

    assert job.sector == "Marketing"
