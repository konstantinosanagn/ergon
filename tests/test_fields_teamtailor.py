"""Unit tests for Teamtailor's embedded schema.org ``baseSalary`` (``MonetaryAmount``) ->
``JobPosting.salary`` mapping (offline, no network).

The field-inventory found ``baseSalary`` under ``_jobposting`` at ~12% fill across captured
boards, but it is ABSENT from ``tests/fixtures/teamtailor_jobs.json`` (confirmed: no
"baseSalary" string anywhere in that fixture) -- neither sampled item states a salary. So this
test builds a synthetic-but-schema.org-accurate payload for the present case (following the
same ``{"currency": ..., "value": {"minValue", "maxValue", "unitText"}}`` shape used elsewhere
in this codebase, e.g. ``tests/test_schemaorg.py``) and uses the real fixture-shaped payload
(no ``baseSalary`` key at all) for the absent case.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import RawJob, SalaryInterval
from ergon_tracker.providers.teamtailor import TeamtailorProvider


def _raw(jobposting: dict[str, Any] | None = None) -> RawJob:
    payload: dict[str, Any] = {
        "id": "abc-123",
        "title": "Software Engineer",
        "url": "https://acme.teamtailor.com/jobs/1-software-engineer",
        "content_html": "<p>About the role</p>",
    }
    if jobposting is not None:
        payload["_jobposting"] = jobposting
    return RawJob(
        source="teamtailor",
        source_job_id="abc-123",
        company="Acme",
        token="acme",
        url=payload["url"],
        payload=payload,
    )


def _jobposting(**base_salary_overrides: Any) -> dict[str, Any]:
    jp: dict[str, Any] = {
        "@context": "http://schema.org/",
        "@type": "JobPosting",
        "title": "Software Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
    }
    if base_salary_overrides:
        jp["baseSalary"] = base_salary_overrides
    return jp


def test_normalize_maps_base_salary_min_max_and_yearly_interval() -> None:
    jp = _jobposting(
        **{
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {
                "@type": "QuantitativeValue",
                "minValue": 90000,
                "maxValue": 120000,
                "unitText": "YEAR",
            },
        }
    )
    job = TeamtailorProvider().normalize(_raw(jp))

    assert job.salary is not None
    assert job.salary.min_amount == 90000.0
    assert job.salary.max_amount == 120000.0
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR


def test_normalize_maps_hourly_interval() -> None:
    jp = _jobposting(
        **{
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {"@type": "QuantitativeValue", "minValue": 25, "maxValue": 40, "unitText": "HOUR"},
        }
    )
    job = TeamtailorProvider().normalize(_raw(jp))

    assert job.salary is not None
    assert job.salary.interval is SalaryInterval.HOUR


def test_normalize_single_value_no_min_max() -> None:
    jp = _jobposting(
        **{
            "@type": "MonetaryAmount",
            "currency": "EUR",
            "value": {"@type": "QuantitativeValue", "value": 55000, "unitText": "YEAR"},
        }
    )
    job = TeamtailorProvider().normalize(_raw(jp))

    assert job.salary is not None
    assert job.salary.min_amount == 55000.0
    assert job.salary.max_amount == 55000.0
    assert job.salary.currency == "EUR"


def test_normalize_no_base_salary_key_leaves_salary_none() -> None:
    # Matches the real fixture shape: _jobposting present, but no "baseSalary" key at all.
    jp = _jobposting()
    assert "baseSalary" not in jp
    job = TeamtailorProvider().normalize(_raw(jp))

    assert job.salary is None


def test_normalize_no_jobposting_block_leaves_salary_none() -> None:
    job = TeamtailorProvider().normalize(_raw(None))
    assert job.salary is None


def test_normalize_base_salary_present_but_no_amount_leaves_salary_none() -> None:
    jp = _jobposting(**{"@type": "MonetaryAmount", "currency": "USD"})
    job = TeamtailorProvider().normalize(_raw(jp))
    assert job.salary is None


def test_normalize_unknown_unit_text_leaves_interval_none() -> None:
    jp = _jobposting(
        **{
            "@type": "MonetaryAmount",
            "currency": "USD",
            "value": {"@type": "QuantitativeValue", "minValue": 1000, "unitText": "FORTNIGHT"},
        }
    )
    job = TeamtailorProvider().normalize(_raw(jp))

    assert job.salary is not None
    assert job.salary.interval is None
