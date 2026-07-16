"""Field-recovery tests for the Paycom provider (offline).

Covers two evidence-based fixes (see scratchpad inventory-C.md paycom section):

1. ``positionType`` -> ``employment_type`` was collected by Paycom's search API but never
   mapped; ``normalize()`` always produced ``EmploymentType.UNKNOWN``.
2. Paycom's ``job-posting-previews/search`` endpoint's ``description`` is a hard-truncated
   ~153-char teaser, not the full JD (confirmed by sampling: every non-empty value observed
   across 74 postings was exactly 153 chars, cut off mid-word). We pin the deliberate decision
   to keep it in ``description_html`` (short plain text still aids keyword search and won't
   false-positive against the precision-oriented extractors) rather than nulling it out.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import EmploymentType, RawJob
from ergon_tracker.providers.paycom import PaycomProvider

KEY = "7C5AC05D8D2EC046AE4FAF26F5F9712E"


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="paycom",
        source_job_id=str(payload.get("jobId") or "1"),
        company="Acme Corp",
        token=KEY,
        url=f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={KEY}&jobId=1",
        payload=payload,
    )


def test_position_type_maps_to_employment_type_full_time() -> None:
    raw = _raw(
        {
            "jobId": 1,
            "jobTitle": "Software Engineer",
            "positionType": "Full Time",
        }
    )
    job = PaycomProvider().normalize(raw)
    assert job.employment_type == EmploymentType.FULL_TIME


def test_position_type_maps_case_and_hyphen_insensitively() -> None:
    raw = _raw(
        {
            "jobId": 2,
            "jobTitle": "Support Rep",
            "positionType": "part-time",
        }
    )
    job = PaycomProvider().normalize(raw)
    assert job.employment_type == EmploymentType.PART_TIME


def test_position_type_missing_falls_back_to_unknown() -> None:
    raw = _raw({"jobId": 3, "jobTitle": "No Type Given"})
    job = PaycomProvider().normalize(raw)
    assert job.employment_type == EmploymentType.UNKNOWN


def test_position_type_unrecognised_value_falls_back_to_unknown() -> None:
    raw = _raw({"jobId": 4, "jobTitle": "Weird Type", "positionType": "Freelance Consulting"})
    job = PaycomProvider().normalize(raw)
    assert job.employment_type == EmploymentType.UNKNOWN


def test_truncated_teaser_description_is_kept_not_nulled() -> None:
    """Pin the deliberate decision: a hard-truncated ~153-char teaser is still stored in
    description_html (not discarded) — it aids keyword search and is too short to make
    precision-oriented extractors (degree/comp/yoe) false-positive."""
    teaser = (
        "Overview- At Angel Oak Mortgage Solutions, we achieve success through our "
        "people. The Sr Software Engineer III Principal Engine"
    )[:153]
    raw = _raw({"jobId": 5, "jobTitle": "Sr Software Engineer III", "description": teaser})
    job = PaycomProvider().normalize(raw)
    assert job.description_html == teaser


def test_blank_description_stays_none() -> None:
    raw = _raw({"jobId": 6, "jobTitle": "No Description", "description": "   "})
    job = PaycomProvider().normalize(raw)
    assert job.description_html is None
