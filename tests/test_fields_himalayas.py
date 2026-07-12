"""Unit tests for Himalayas' ``seniority``/``categories`` -> level/department mapping.

Offline, no network. Himalayas' feed API (``GET https://himalayas.app/jobs/api``) reports
seniority as a list of strings under ``seniority`` (e.g. ``["Senior"]``) and department-ish
tags as a list of strings under ``categories`` (e.g. ``["Java-Architect", ...]``); see
``tests/fixtures/himalayas_sample.json``. ``normalize()`` must take the FIRST ``seniority``
entry through ``level_from_ats_vocab`` and the FIRST ``categories`` entry verbatim as
``department``, without relying on title/description enrichment (which only fills in when
``job.level is JobLevel.UNKNOWN`` / ``job.department`` is falsy).
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import JobLevel, RawJob
from ergon_tracker.providers.himalayas import HimalayasProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="himalayas",
        source_job_id=str(payload.get("guid") or payload.get("applicationLink") or ""),
        company=payload.get("companyName") or "",
        token=None,
        url=None,
        payload=payload,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Software Engineer",
        "companyName": "Acme",
        "employmentType": "Full Time",
        "seniority": ["Mid-level"],
        "locationRestrictions": ["United States"],
        "categories": ["Java-Architect", "Software-Architecture"],
        "parentCategories": ["Developer"],
        "description": "<p>Job description.</p>",
        "pubDate": 1781589519,
        "applicationLink": "https://himalayas.app/companies/acme/jobs/software-engineer",
        "guid": "https://himalayas.app/companies/acme/jobs/software-engineer",
    }
    base.update(overrides)
    return base


def test_normalize_maps_mid_level_seniority_to_mid() -> None:
    job = HimalayasProvider().normalize(_raw(_payload()))

    assert job.level is JobLevel.MID


def test_normalize_maps_senior_seniority() -> None:
    job = HimalayasProvider().normalize(_raw(_payload(seniority=["Senior"])))

    assert job.level is JobLevel.SENIOR


def test_normalize_empty_seniority_list_is_unknown() -> None:
    job = HimalayasProvider().normalize(_raw(_payload(seniority=[])))

    assert job.level is JobLevel.UNKNOWN


def test_normalize_missing_seniority_key_is_unknown() -> None:
    payload = _payload()
    del payload["seniority"]
    job = HimalayasProvider().normalize(_raw(payload))

    assert job.level is JobLevel.UNKNOWN


def test_normalize_takes_first_seniority_when_multiple() -> None:
    job = HimalayasProvider().normalize(_raw(_payload(seniority=["Senior", "Mid-level"])))

    assert job.level is JobLevel.SENIOR


def test_normalize_maps_first_category_to_department() -> None:
    job = HimalayasProvider().normalize(_raw(_payload()))

    assert job.department == "Java-Architect"


def test_normalize_empty_categories_list_is_none_department() -> None:
    job = HimalayasProvider().normalize(_raw(_payload(categories=[])))

    assert job.department is None


def test_normalize_missing_categories_key_is_none_department() -> None:
    payload = _payload()
    del payload["categories"]
    job = HimalayasProvider().normalize(_raw(payload))

    assert job.department is None


def test_normalize_category_object_shape_uses_name() -> None:
    # Defensive: handle a category entry shaped as {"name": ...} like other aggregators use.
    job = HimalayasProvider().normalize(
        _raw(_payload(categories=[{"name": "Engineering"}]))
    )

    assert job.department == "Engineering"
