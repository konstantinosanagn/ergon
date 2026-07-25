"""Tests for the deterministic employment-type extractor (title -> label -> framed body)."""

from __future__ import annotations

import pytest

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.employment_type import EmploymentTypeExtractor
from ergon_tracker.models import EmploymentType

_ex = EmploymentTypeExtractor()


def _t(title: str, desc: str | None = None) -> EmploymentType:
    return _ex.extract(ExtractInput(title=title, description_text=desc))


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineering Intern", EmploymentType.INTERNSHIP),
        ("Summer 2026 Internship - Data", EmploymentType.INTERNSHIP),
        ("COOP Student - Engineering", EmploymentType.INTERNSHIP),
        ("Part Time Branch Ambassador", EmploymentType.PART_TIME),
        ("Warehouse Associate (Seasonal)", EmploymentType.TEMPORARY),
        ("Payroll Specialist (Fixed Term)", EmploymentType.TEMPORARY),
        ("Full-Time Registered Nurse", EmploymentType.FULL_TIME),
        # contract only when engagement-framed
        ("Senior Account Manager (Contractor)", EmploymentType.CONTRACT),
        ("HR Coordinator (Contract)", EmploymentType.CONTRACT),
        ("Liaison - 12 months, fixed-term contract", EmploymentType.CONTRACT),
        ("Recruiter - Contract Role", EmploymentType.CONTRACT),
    ],
)
def test_title_signal(title: str, expected: EmploymentType) -> None:
    assert _t(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer - Smart Contract, Bridge",  # blockchain domain, not an engagement
        "Production Operator - Permanent Contract",  # permanent role, not a contractor engagement
        "Contract Manager",  # job duty, not the employment type
        "Government Contract Analyst",
        "Software Engineer",  # no signal
        "International Sales Lead",  # 'intern' must not fire inside 'international'
    ],
)
def test_title_no_false_positive(title: str) -> None:
    assert _t(title) == EmploymentType.UNKNOWN


def test_explicit_label_wins_over_bare_default() -> None:
    # An explicit ATS label is unambiguous: bare "Contract"/"Permanent" count without framing.
    assert _t("Analyst", "Great role.\nEmployment Type: Contract\nApply now.") == (
        EmploymentType.CONTRACT
    )
    assert _t("Analyst", "Job Type - Part-time. Flexible hours.") == EmploymentType.PART_TIME
    assert _t("Analyst", "Position Type: Permanent") == EmploymentType.FULL_TIME


def test_framed_body_phrases() -> None:
    assert _t("Analyst", "This is a full-time position with benefits.") == (
        EmploymentType.FULL_TIME
    )
    assert _t("Analyst", "We are hiring a paid internship for the summer.") == (
        EmploymentType.INTERNSHIP
    )
    assert _t("Analyst", "Engagement is on a 6-month contract basis.") == EmploymentType.CONTRACT


def test_body_bare_mention_does_not_fire() -> None:
    # Job DUTIES that mention the type word without engagement framing must stay UNKNOWN.
    assert _t("Analyst", "Responsible for contract negotiation and vendor management.") == (
        EmploymentType.UNKNOWN
    )
    assert _t("Analyst", "You will manage a full range of duties.") == EmploymentType.UNKNOWN


def test_specific_beats_full_time_boilerplate() -> None:
    # A contract role whose JD also carries full-time benefits boilerplate must not become full_time.
    desc = "6-month contract role. Full-time employees are eligible for the 401k."
    assert _t("Analyst", desc) == EmploymentType.CONTRACT


def test_unknown_when_no_text() -> None:
    assert _t("Analyst") == EmploymentType.UNKNOWN
    assert _ex.extract(ExtractInput(title="Analyst", description_text=None)) == (
        EmploymentType.UNKNOWN
    )
