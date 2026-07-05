"""Opt-in yoe->level inference: off by default (title-based), on boosts coverage."""

from __future__ import annotations

from ergon_tracker import JobLevel, JobPosting, Location
from ergon_tracker.enrich import enrich_in_place, level_from_years


def test_level_from_years_mapping() -> None:
    assert level_from_years(0, None) is JobLevel.ENTRY
    assert level_from_years(1, 2) is JobLevel.ENTRY
    assert level_from_years(3, None) is JobLevel.MID
    assert level_from_years(5, 8) is JobLevel.SENIOR
    assert level_from_years(12, None) is JobLevel.SENIOR  # capped at senior
    assert level_from_years(None, None) is JobLevel.UNKNOWN


def _plain_job() -> JobPosting:
    # plain title (no seniority marker) + a description stating experience
    return JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        description_text="We need 6+ years of professional software engineering experience.",
        locations=[Location(raw="Remote")],
    )


def test_explicit_flag_off_keeps_level_unknown() -> None:
    job = _plain_job()
    enrich_in_place(job, infer_level_from_experience=False)  # explicit opt-out
    assert job.level is JobLevel.UNKNOWN
    assert job.years_experience_min == 6  # yoe still extracted


def test_default_infers_level_from_yoe() -> None:
    job = _plain_job()
    enrich_in_place(job)  # NEW default: yoe->level fallback is ON
    assert job.level is JobLevel.SENIOR  # 6 years -> senior


def test_flag_on_infers_level_from_yoe() -> None:
    job = _plain_job()
    enrich_in_place(job, infer_level_from_experience=True)
    assert job.years_experience_min == 6
    assert job.level is JobLevel.SENIOR  # 6 years -> senior


def test_flag_never_overrides_a_title_level() -> None:
    job = JobPosting.create(
        source="greenhouse",
        source_job_id="2",
        company="Acme",
        title="Staff Software Engineer",  # explicit title level
        description_text="2+ years experience.",
    )
    enrich_in_place(job, infer_level_from_experience=True)
    assert job.level is JobLevel.STAFF  # title wins over the yoe fallback
