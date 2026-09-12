"""Guards for the apicapture field-map's metadata slots (``level``/``years_min``/``years_max``/
``degree_min``/``experience``).

The captured-API spec schema had no slot for these at all, so a captured company API that already
returns a seniority, years-of-experience or education field could not surface it. The slots are
OPT-IN: a spec that doesn't map them resolves the empty key, which ``_fget`` short-circuits to
``None`` — so every existing spec normalizes exactly as before (pinned below).
"""

from __future__ import annotations

from typing import Any

from ergon.models import JobLevel, RawJob
from ergon.providers.apicapture import ApiCaptureProvider, _load_specs


def _raw(fields: dict[str, str], record: dict[str, Any]) -> RawJob:
    return RawJob(
        source="apicapture",
        source_job_id="JID-1",
        company="Acme",
        token="acme",
        url=None,
        payload={**record, "_spec": fields},
    )


def test_slots_map_structured_metadata() -> None:
    fields = {
        "id": "id",
        "title": "jobTitle",
        "level": "seniority",
        "years_min": "minYears",
        "years_max": "maxYears",
        "degree_min": "education",
    }
    record = {
        "id": "JID-1",
        "jobTitle": "Widget Engineer",
        "seniority": "Senior",
        "minYears": 5,
        "maxYears": "8",
        "education": "Bachelor's Degree",
    }
    job = ApiCaptureProvider().normalize(_raw(fields, record))
    assert job.level is JobLevel.SENIOR
    assert (job.years_experience_min, job.years_experience_max) == (5, 8)
    assert job.degree_min == "bachelor"


def test_unknown_vocab_values_never_raise() -> None:
    fields = {"id": "id", "title": "jobTitle", "level": "seniority", "degree_min": "education"}
    record = {"id": "JID-1", "jobTitle": "Widget Engineer", "seniority": "Band 7", "education": "-"}
    job = ApiCaptureProvider().normalize(_raw(fields, record))
    assert job.level is JobLevel.UNKNOWN
    assert job.degree_min is None


def test_experience_slot_is_the_free_text_fallback() -> None:
    """The already-captured ``experience`` key (TCS) is prose, not a number."""
    fields = {"id": "id", "title": "jobTitle", "experience": "experience"}
    record = {"id": "JID-1", "jobTitle": "Widget Engineer", "experience": "4 to 6 Years"}
    job = ApiCaptureProvider().normalize(_raw(fields, record))
    assert (job.years_experience_min, job.years_experience_max) == (4, 6)

    # A numeric slot always wins over the prose one.
    fields = {**fields, "years_min": "minYears"}
    job = ApiCaptureProvider().normalize(_raw(fields, {**record, "minYears": 9}))
    assert (job.years_experience_min, job.years_experience_max) == (9, None)


def test_tatacs_spec_still_maps_experience() -> None:
    """The one shipped spec carrying the slot keeps it (the mapping is what makes it readable)."""
    spec = _load_specs().get("tatacs")
    assert spec is not None
    assert spec["fields"].get("experience") == "experience"


def test_specs_without_the_slots_are_unaffected() -> None:
    """No shipped spec maps the new keys yet, and normalize leaves those fields empty."""
    specs = _load_specs()
    assert not [
        t
        for t, s in specs.items()
        if {"level", "years_min", "years_max", "degree_min"} & set(s.get("fields", {}))
    ]
    spec = specs["uber"]
    record = {"id": "JID-1", "title": "Widget Engineer"}
    job = ApiCaptureProvider().normalize(_raw(spec["fields"], record))
    assert job.level is JobLevel.UNKNOWN
    assert (job.years_experience_min, job.years_experience_max) == (None, None)
    assert job.degree_min is None
