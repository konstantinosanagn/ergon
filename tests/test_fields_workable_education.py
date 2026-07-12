"""Unit tests for Workable's ``education`` -> ``degree_min`` mapping (offline, no network).

Workable's widget endpoint exposes a free-text ``education`` vocabulary on each job (sample
values seen live: "Bachelor's Degree", "Associate Degree", "Master's Degree", "Doctorate",
"High School", "Professional", "Vocational"). ``degree_from_ats_vocab`` maps the closed set of
recognised terms to the canonical ``DEGREE_LEVELS`` values and returns ``None`` for anything
ambiguous ("Professional", "Vocational", "Certification") or empty, so the description-based
``DegreeExtractor`` (or nothing) fills the gap rather than guessing.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.extract.degree import degree_from_ats_vocab
from ergon_tracker.models import RawJob
from ergon_tracker.providers.workable import WorkableProvider


def _raw(payload: dict[str, Any]) -> RawJob:
    return RawJob(
        source="workable",
        source_job_id=str(payload.get("shortcode", "")),
        company="Acme",
        token="acme",
        url=payload.get("url"),
        payload=payload,
    )


# --- degree_from_ats_vocab ----------------------------------------------------


def test_high_school() -> None:
    assert degree_from_ats_vocab("High School") == "highschool"


def test_associate_degree() -> None:
    assert degree_from_ats_vocab("Associate Degree") == "associate"


def test_associates_degree_apostrophe() -> None:
    assert degree_from_ats_vocab("Associate's Degree") == "associate"


def test_bachelors_degree() -> None:
    assert degree_from_ats_vocab("Bachelor's Degree") == "bachelor"


def test_masters_degree() -> None:
    assert degree_from_ats_vocab("Master's Degree") == "master"


def test_doctorate() -> None:
    assert degree_from_ats_vocab("Doctorate") == "phd_md"


def test_phd_spelled_out() -> None:
    assert degree_from_ats_vocab("PhD") == "phd_md"


def test_case_and_punctuation_insensitive() -> None:
    assert degree_from_ats_vocab("  bachelor's   degree  ") == "bachelor"
    assert degree_from_ats_vocab("BACHELORS DEGREE") == "bachelor"


def test_professional_is_ambiguous_none() -> None:
    assert degree_from_ats_vocab("Professional") is None


def test_vocational_is_ambiguous_none() -> None:
    assert degree_from_ats_vocab("Vocational") is None


def test_certification_is_ambiguous_none() -> None:
    assert degree_from_ats_vocab("Certification") is None


def test_empty_string_is_none() -> None:
    assert degree_from_ats_vocab("") is None


def test_none_is_none() -> None:
    assert degree_from_ats_vocab(None) is None


def test_unrecognized_value_is_none() -> None:
    assert degree_from_ats_vocab("Something Else Entirely") is None


# --- WorkableProvider.normalize wiring -----------------------------------------


def test_normalize_maps_education_to_degree_min() -> None:
    payload = {"title": "x", "education": "Master's Degree"}
    job = WorkableProvider().normalize(_raw(payload))

    assert job.degree_min == "master"
    # Workable's "education" is the ATS's own structured minimum-education setting for the
    # requisition (not free text), so a recognised value IS a stated requirement.
    assert job.degree_required is True


def test_normalize_no_education_field_leaves_degree_min_none() -> None:
    payload = {"title": "x"}
    job = WorkableProvider().normalize(_raw(payload))

    assert job.degree_min is None
    # No mapped degree_min -> degree_required stays None too, so the description-based
    # DegreeExtractor still gets a chance to run (enrich's guard checks both fields).
    assert job.degree_required is None


def test_normalize_ambiguous_education_leaves_degree_min_none() -> None:
    payload = {"title": "x", "education": "Professional"}
    job = WorkableProvider().normalize(_raw(payload))

    assert job.degree_min is None
    assert job.degree_required is None
