"""Remotive ``salary`` field mapping (offline, TDD).

Real fixture shapes (``tests/fixtures/remotive_sample.json``):
  - populated: ``"$175k - $225k"`` (id 2090881, Business Transformation Lead)
  - empty string: ``""`` (id 2090989, Assistant Account Payable)
"""

from __future__ import annotations

from datetime import datetime

from ergon_tracker.models import RawJob, SalaryInterval
from ergon_tracker.providers.remotive import RemotiveProvider


def _raw(payload: dict) -> RawJob:
    return RawJob(
        source="remotive",
        source_job_id=str(payload.get("id", "")),
        company=payload.get("company_name") or "",
        token=None,
        url=payload.get("url"),
        payload=payload,
        fetched_at=datetime(2026, 1, 1),
    )


def _provider() -> RemotiveProvider:
    return RemotiveProvider()


def test_populated_salary_parses_min_max() -> None:
    payload = {
        "id": 2090881,
        "title": "Business Transformation Lead",
        "company_name": "Expion Health",
        "salary": "$175k - $225k",
    }
    job = _provider().normalize(_raw(payload))
    assert job.salary is not None
    assert job.salary.min_amount == 175000.0
    assert job.salary.max_amount == 225000.0
    assert job.salary.currency == "USD"
    assert job.salary.interval is SalaryInterval.YEAR


def test_empty_salary_string_is_none() -> None:
    payload = {
        "id": 2090989,
        "title": "Assistant Account Payable",
        "company_name": "The Obesity Society",
        "salary": "",
    }
    job = _provider().normalize(_raw(payload))
    assert job.salary is None


def test_absent_salary_key_is_none() -> None:
    payload = {
        "id": 2090983,
        "title": "Head of Engineering",
        "company_name": "Lemon.io",
    }
    job = _provider().normalize(_raw(payload))
    assert job.salary is None


def test_non_string_salary_is_guarded() -> None:
    payload = {
        "id": 999999,
        "title": "Weird Payload",
        "company_name": "Acme",
        "salary": {"unexpected": "shape"},
    }
    job = _provider().normalize(_raw(payload))
    assert job.salary is None
