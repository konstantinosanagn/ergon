"""MTS / Member of Technical Staff is an IC ladder, not staff-level."""

from __future__ import annotations

from ergon_tracker.extract.level import infer_level
from ergon_tracker.models import JobLevel


def test_mts_is_ic_level_not_staff() -> None:
    assert infer_level("Member of Technical Staff") is JobLevel.MID
    assert infer_level("MTS") is JobLevel.MID
    assert infer_level("Senior Member of Technical Staff") is JobLevel.SENIOR
    # plain "Staff Engineer" must still be staff-level
    assert infer_level("Staff Software Engineer") is JobLevel.STAFF
