"""include_unknown_level / include_unknown_sector: narrow without dropping unlabeled jobs."""

from __future__ import annotations

from ergon.models import JobLevel, JobPosting, SearchQuery


def _job(level: JobLevel = JobLevel.UNKNOWN, sector: str | None = None) -> JobPosting:
    return JobPosting.create(
        source="greenhouse",
        source_job_id=f"{level.value}-{sector}",
        company="Acme",
        title="Engineer",
        level=level,
        sector=sector,
    )


# --- level ------------------------------------------------------------------
def test_level_filter_strict_by_default_drops_unknown() -> None:
    q = SearchQuery(level=JobLevel.SENIOR)
    assert q.matches(_job(level=JobLevel.SENIOR)) is True
    assert q.matches(_job(level=JobLevel.UNKNOWN)) is False  # dropped (strict)
    assert q.matches(_job(level=JobLevel.MID)) is False


def test_level_filter_include_unknown_keeps_unlabeled() -> None:
    q = SearchQuery(level=JobLevel.SENIOR, include_unknown_level=True)
    assert q.matches(_job(level=JobLevel.SENIOR)) is True
    assert q.matches(_job(level=JobLevel.UNKNOWN)) is True  # kept
    assert q.matches(_job(level=JobLevel.MID)) is False  # a *known* mismatch is still dropped


# --- sector -----------------------------------------------------------------
def test_sector_filter_strict_by_default_drops_unknown() -> None:
    q = SearchQuery(sector="Fintech")
    assert q.matches(_job(sector="Fintech")) is True
    assert q.matches(_job(sector=None)) is False  # dropped (strict)
    assert q.matches(_job(sector="Healthcare")) is False


def test_sector_filter_include_unknown_keeps_unlabeled() -> None:
    q = SearchQuery(sector="Fintech", include_unknown_sector=True)
    assert q.matches(_job(sector="Fintech")) is True
    assert q.matches(_job(sector=None)) is True  # kept
    assert q.matches(_job(sector="Healthcare")) is False  # known mismatch still dropped
