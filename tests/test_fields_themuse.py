"""Unit tests for The Muse's ``levels`` -> ``JobPosting.level`` mapping (offline, no network).

The Muse's postings API reports seniority as a list of ``{"name": ...}`` objects under
``levels``. ``normalize()`` must take the FIRST level's ``name`` and pass it through
``level_from_ats_vocab`` so the seniority ladder is populated without relying on
title/description enrichment (which only fills in when ``job.level is JobLevel.UNKNOWN``).
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import JobLevel, RawJob
from ergon_tracker.providers.themuse import TheMuseProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="themuse",
        source_job_id=str(payload.get("id", "")),
        company="Acme",
        token=None,
        url=None,
        payload=payload,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "123",
        "name": "Software Engineer",
        "company": {"name": "Acme"},
        "locations": [{"name": "New York, NY"}],
        "levels": [{"name": "Senior Level"}],
        "type": "Full Time",
        "refs": {"landing_page": "https://www.themuse.com/jobs/acme/software-engineer"},
        "publication_date": "2024-01-01T00:00:00Z",
        "contents": "<p>Job description.</p>",
        "categories": [{"name": "Engineering"}],
    }
    base.update(overrides)
    return base


def test_normalize_maps_first_level_name_to_senior() -> None:
    job = TheMuseProvider().normalize(_raw(_payload()))

    assert job.level is JobLevel.SENIOR


def test_normalize_maps_entry_level_name() -> None:
    job = TheMuseProvider().normalize(_raw(_payload(levels=[{"name": "Entry Level"}])))

    assert job.level is JobLevel.ENTRY


def test_normalize_empty_levels_list_is_unknown() -> None:
    job = TheMuseProvider().normalize(_raw(_payload(levels=[])))

    assert job.level is JobLevel.UNKNOWN


def test_normalize_missing_levels_key_is_unknown() -> None:
    payload = _payload()
    del payload["levels"]
    job = TheMuseProvider().normalize(_raw(payload))

    assert job.level is JobLevel.UNKNOWN


def test_normalize_takes_first_level_when_multiple() -> None:
    job = TheMuseProvider().normalize(
        _raw(_payload(levels=[{"name": "Senior Level"}, {"name": "Entry Level"}]))
    )

    assert job.level is JobLevel.SENIOR
