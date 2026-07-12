"""Unit tests for Lever's top-level ``country`` -> ``Location.country`` mapping (offline, no
network). Lever's postings API reports a posting-level ISO country code (``payload["country"]``)
separately from the free-text location segments under ``categories`` — ``normalize()`` must carry
that code onto each parsed ``Location`` so geo filtering doesn't depend solely on parsing the raw
string.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import RawJob
from ergon_tracker.providers.lever import LeverProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="lever",
        source_job_id=str(payload.get("id", "")),
        company="Acme",
        token="acme",
        url=payload.get("hostedUrl"),
        payload=payload,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "abc-123",
        "text": "Software Engineer",
        "categories": {"location": "New York, NY"},
        "workplaceType": "onsite",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
        "country": "US",
    }
    base.update(overrides)
    return base


def test_normalize_maps_top_level_country_onto_location() -> None:
    job = LeverProvider().normalize(_raw(_payload()))

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.raw == "New York, NY"
    assert loc.country == "US"


def test_normalize_maps_country_across_all_locations() -> None:
    payload = _payload(
        categories={"allLocations": ["London", "Manchester"]},
        country="GB",
    )
    job = LeverProvider().normalize(_raw(payload))

    assert [loc.raw for loc in job.locations] == ["London", "Manchester"]
    assert all(loc.country == "GB" for loc in job.locations)


def test_normalize_no_country_field_leaves_location_country_none() -> None:
    payload = _payload()
    del payload["country"]
    job = LeverProvider().normalize(_raw(payload))

    assert len(job.locations) == 1
    assert job.locations[0].country is None


def test_normalize_no_locations_is_unaffected_by_country() -> None:
    payload = _payload(categories={}, country="US")
    job = LeverProvider().normalize(_raw(payload))

    assert job.locations == []
