"""Unit tests for Eightfold's ``standardizedLocations`` -> structured ``Location`` mapping
(offline, no network).

PCSX-mode tenants expose ``standardizedLocations``: a list of comma-joined "City, Region, CC"
strings (e.g. ``"Azusa, CA, US"`` — the real shape observed in ``tests/test_eightfold.py``'s
``_PCSX_POSITIONS`` fixture data). The base fixture (``tests/fixtures/eightfold_jobs.json``,
an apply/v2-mode payload) does NOT carry this field at all, confirming it's endpoint/tenant
dependent — so ``normalize()`` must parse it when present and fall back to the existing
free-text ``locations``/``location`` handling when absent, without regressing tenants that
never see it.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import RawJob
from ergon_tracker.providers.eightfold import EightfoldProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="eightfold",
        source_job_id=str(payload.get("id", "")),
        company="starbucks",
        token="starbucks",
        url=payload.get("canonicalPositionUrl"),
        payload=payload,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 481077513632,
        "display_job_id": "260026021",
        "name": "barista - Store# 06447",
        "locations": ["890 E Alosta Ave, Azusa, California, United States"],
        "t_create": 1774929600,
        "department": "Barista",
        "work_location_option": "onsite",
        "canonicalPositionUrl": "https://starbucks.eightfold.ai/careers/job/481077513632",
    }
    base.update(overrides)
    return base


def test_normalize_maps_standardized_locations_to_structured_fields() -> None:
    """The real observed shape: "City, Region, CC" -> city/region/country populated."""
    payload = _payload(standardizedLocations=["Azusa, CA, US"])
    job = EightfoldProvider().normalize(_raw(payload))

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Azusa"
    assert loc.region == "CA"
    assert loc.country == "US"
    assert loc.raw == "Azusa, CA, US"
    assert loc.is_remote is False


def test_normalize_without_standardized_locations_falls_back_to_raw() -> None:
    """No regression: tenants without the field keep the existing raw-string Location."""
    payload = _payload()  # no standardizedLocations key at all
    job = EightfoldProvider().normalize(_raw(payload))

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city is None
    assert loc.region is None
    assert loc.country is None
    assert loc.raw == "890 E Alosta Ave, Azusa, California, United States"


def test_normalize_empty_standardized_locations_falls_back_to_raw() -> None:
    """An empty (present-but-empty) list must not shadow the real locations list."""
    payload = _payload(standardizedLocations=[])
    job = EightfoldProvider().normalize(_raw(payload))

    assert len(job.locations) == 1
    assert job.locations[0].raw == "890 E Alosta Ave, Azusa, California, United States"
    assert job.locations[0].city is None


def test_normalize_multiple_standardized_locations() -> None:
    payload = _payload(standardizedLocations=["Azusa, CA, US", "Seattle, WA, US"])
    job = EightfoldProvider().normalize(_raw(payload))

    assert len(job.locations) == 2
    assert [loc.city for loc in job.locations] == ["Azusa", "Seattle"]
    assert [loc.region for loc in job.locations] == ["CA", "WA"]
    assert all(loc.country == "US" for loc in job.locations)


def test_normalize_two_part_standardized_location_has_no_region() -> None:
    """Two-part "City, Country" entries (no region token) map city+country, region=None."""
    payload = _payload(standardizedLocations=["Berlin, Germany"])
    job = EightfoldProvider().normalize(_raw(payload))

    loc = job.locations[0]
    assert loc.city == "Berlin"
    assert loc.region is None
    assert loc.country == "Germany"


def test_normalize_single_token_standardized_location_is_not_guessed() -> None:
    """A single unparseable token (e.g. "Remote") must not be mislabeled as a city/country."""
    payload = _payload(standardizedLocations=["Remote"])
    job = EightfoldProvider().normalize(_raw(payload))

    loc = job.locations[0]
    assert loc.city is None
    assert loc.region is None
    assert loc.country is None
    assert loc.raw == "Remote"
    assert loc.is_remote is True


def test_normalize_malformed_standardized_locations_type_falls_back_to_raw() -> None:
    """A non-list value (defensive guard against a schema surprise) is ignored, not crashed on."""
    payload = _payload(standardizedLocations={"unexpected": "shape"})
    job = EightfoldProvider().normalize(_raw(payload))

    assert len(job.locations) == 1
    assert job.locations[0].raw == "890 E Alosta Ave, Azusa, California, United States"
