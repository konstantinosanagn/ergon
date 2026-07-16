"""Unit tests for Recruitee's ``salary`` -> ``JobPosting.salary`` mapping (offline, no
network). Recruitee reports salary as a dict with STRING amounts on some boards (e.g.
``{"min": "5200", "max": "6400", "currency": "EUR", "period": "month"}``) and, on others,
numeric amounts. ``normalize()`` must coerce either shape into a :class:`Salary`, and must
never build an empty-shell ``Salary`` when both min and max are missing/blank.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import RawJob, SalaryInterval
from ergon_tracker.providers.recruitee import RecruiteeProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="recruitee",
        source_job_id=str(payload.get("id", "")),
        company="Acme",
        token="acme",
        url=payload.get("careers_apply_url") or payload.get("careers_url"),
        payload=payload,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 42,
        "title": "Software Engineer",
        "careers_url": "https://jobs.acme.com/o/software-engineer",
    }
    base.update(overrides)
    return base


def test_normalize_maps_string_amounts_month() -> None:
    payload = _payload(salary={"min": "5200", "max": "6400", "currency": "EUR", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is not None
    assert job.salary.min_amount == 5200.0
    assert job.salary.max_amount == 6400.0
    assert job.salary.currency == "EUR"
    assert job.salary.interval is SalaryInterval.MONTH


def test_normalize_maps_numeric_amounts() -> None:
    payload = _payload(salary={"min": 80000, "max": 100000, "currency": "USD", "period": "year"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is not None
    assert job.salary.min_amount == 80000.0
    assert job.salary.max_amount == 100000.0
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR


def test_normalize_maps_all_known_periods() -> None:
    for period, expected in (
        ("year", SalaryInterval.YEAR),
        ("month", SalaryInterval.MONTH),
        ("week", SalaryInterval.WEEK),
        ("day", SalaryInterval.DAY),
        ("hour", SalaryInterval.HOUR),
    ):
        payload = _payload(salary={"min": "10", "max": "20", "period": period})
        job = RecruiteeProvider().normalize(_raw(payload))
        assert job.salary is not None
        assert job.salary.interval is expected


def test_normalize_min_only() -> None:
    payload = _payload(salary={"min": "5200", "max": None, "currency": "EUR", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is not None
    assert job.salary.min_amount == 5200.0
    assert job.salary.max_amount is None


def test_normalize_max_only() -> None:
    payload = _payload(salary={"min": "", "max": "6400", "currency": "EUR", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is not None
    assert job.salary.min_amount is None
    assert job.salary.max_amount == 6400.0


def test_normalize_salary_present_but_min_and_max_null_yields_no_salary() -> None:
    payload = _payload(salary={"min": None, "max": None, "currency": "EUR", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is None


def test_normalize_salary_present_but_min_and_max_empty_string_yields_no_salary() -> None:
    payload = _payload(salary={"min": "", "max": "", "currency": "EUR", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is None


def test_normalize_missing_salary_key_yields_none() -> None:
    payload = _payload()
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is None


def test_normalize_non_numeric_amounts_are_ignored() -> None:
    payload = _payload(salary={"min": "n/a", "max": "n/a", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is None


def test_normalize_unknown_period_leaves_interval_none() -> None:
    payload = _payload(salary={"min": "5200", "max": "6400", "period": "quarterly"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is not None
    assert job.salary.interval is None


def test_normalize_missing_currency_leaves_currency_none() -> None:
    payload = _payload(salary={"min": "5200", "max": "6400", "period": "month"})
    job = RecruiteeProvider().normalize(_raw(payload))

    assert job.salary is not None
    assert job.salary.currency is None
