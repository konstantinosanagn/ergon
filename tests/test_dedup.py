"""Tests for the job-aware dedup/merge engine."""

from __future__ import annotations

from ergon import JobLevel, JobPosting, Location, RemoteType, Salary
from ergon.dedup import deduplicate, normalize_company, normalize_title


def test_exact_id_duplicate_collapses() -> None:
    a = JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="Engineer")
    b = JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="Engineer")
    out = deduplicate([a, b])
    assert len(out) == 1
    assert out[0].id == a.id


def test_cross_source_near_duplicate_merges_ats_wins() -> None:
    ats = JobPosting.create(
        source="greenhouse",
        source_job_id="g1",
        company="Acme, Inc.",
        title="Sr. Backend Engineer",
        apply_url="https://boards.greenhouse.io/acme/g1",
        description_text="Build backend systems.",
        locations=[Location(city="Berlin", country="DE")],
    )
    agg = JobPosting.create(
        source="remoteok",
        source_job_id="r9",
        company="Acme",
        title="Senior Backend Engineer",
        apply_url="https://remoteok.com/r9",
        salary=Salary(min_amount=120_000, currency="EUR"),
        locations=[Location(is_remote=True)],
        remote=RemoteType.REMOTE,
    )
    out = deduplicate([ats, agg])

    assert len(out) == 1
    merged = out[0]
    # Primary is the ATS (greenhouse) record, not the aggregator.
    assert merged.source == "greenhouse"
    assert merged.source_job_id == "g1"
    # Provenance unions BOTH sources.
    prov_sources = {p.source for p in merged.provenance}
    assert prov_sources == {"greenhouse", "remoteok"}


def test_salary_from_aggregator_fills_missing_ats_salary() -> None:
    ats = JobPosting.create(
        source="lever",
        source_job_id="l1",
        company="Globex",
        title="Data Engineer",
    )
    agg = JobPosting.create(
        source="remoteok",
        source_job_id="r1",
        company="Globex",
        title="Data Engineer",
        salary=Salary(min_amount=90_000, max_amount=110_000, currency="USD"),
    )
    out = deduplicate([ats, agg])
    assert len(out) == 1
    merged = out[0]
    assert merged.source == "lever"  # ATS primary
    assert merged.salary is not None
    assert merged.salary.min_amount == 90_000  # filled from aggregator


def test_different_roles_do_not_merge() -> None:
    a = JobPosting.create(
        source="greenhouse", source_job_id="1", company="Acme", title="Backend Engineer"
    )
    b = JobPosting.create(
        source="greenhouse", source_job_id="2", company="Acme", title="Product Designer"
    )
    out = deduplicate([a, b])
    assert len(out) == 2


def test_different_companies_same_title_do_not_merge() -> None:
    a = JobPosting.create(
        source="greenhouse", source_job_id="1", company="Acme", title="Backend Engineer"
    )
    b = JobPosting.create(
        source="greenhouse", source_job_id="2", company="Globex", title="Backend Engineer"
    )
    out = deduplicate([a, b])
    assert len(out) == 2


def test_incompatible_cities_do_not_merge() -> None:
    a = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Backend Engineer",
        locations=[Location(city="Berlin")],
    )
    b = JobPosting.create(
        source="lever",
        source_job_id="2",
        company="Acme",
        title="Backend Engineer",
        locations=[Location(city="Tokyo")],
    )
    out = deduplicate([a, b])
    assert len(out) == 2


def test_different_levels_do_not_merge() -> None:
    # Same role + same city, but distinct seniorities are distinct openings (users filter on
    # `level`). A title-only fuzzy match would collapse these; the level gate must keep them apart.
    senior = JobPosting.create(
        source="lever",
        source_job_id="1",
        company="Palantir",
        title="Senior Backend Software Engineer - Application Development",
        locations=[Location(city="New York")],
        level=JobLevel.SENIOR,
    )
    plain = JobPosting.create(
        source="lever",
        source_job_id="2",
        company="Palantir",
        title="Backend Software Engineer - Application Development",
        locations=[Location(city="New York")],
        level=JobLevel.UNKNOWN,
    )
    out = deduplicate([senior, plain])
    assert len(out) == 2


def test_same_level_cross_source_still_merges() -> None:
    # The legitimate cross-source case: same job, same seniority, one source drops the city.
    ats = JobPosting.create(
        source="greenhouse",
        source_job_id="g1",
        company="Acme",
        title="Senior Backend Engineer",
        locations=[Location(city="Berlin")],
        level=JobLevel.SENIOR,
    )
    agg = JobPosting.create(
        source="remoteok",
        source_job_id="r1",
        company="Acme",
        title="Sr. Backend Engineer",
        locations=[Location(is_remote=True)],
        remote=RemoteType.REMOTE,
        level=JobLevel.SENIOR,
    )
    out = deduplicate([ats, agg])
    assert len(out) == 1
    assert {p.source for p in out[0].provenance} == {"greenhouse", "remoteok"}


def test_remote_postings_in_different_cities_do_not_merge() -> None:
    # Hybrid/remote must NOT be a wildcard that collapses distinct cities — `city` is a filter.
    ny = JobPosting.create(
        source="lever",
        source_job_id="1",
        company="Palantir",
        title="Deployment Strategist",
        locations=[Location(city="New York")],
        remote=RemoteType.HYBRID,
    )
    london = JobPosting.create(
        source="lever",
        source_job_id="2",
        company="Palantir",
        title="Deployment Strategist",
        locations=[Location(city="London")],
        remote=RemoteType.HYBRID,
    )
    out = deduplicate([ny, london])
    assert len(out) == 2


def test_order_stable_by_first_occurrence() -> None:
    z = JobPosting.create(source="greenhouse", source_job_id="z", company="Zeta", title="QA")
    a1 = JobPosting.create(source="greenhouse", source_job_id="a", company="Acme", title="Eng")
    a2 = JobPosting.create(source="remoteok", source_job_id="a2", company="Acme", title="Eng")
    out = deduplicate([z, a1, a2])
    assert [j.company for j in out] == ["Zeta", "Acme"]


def test_normalize_title_strips_seniority_and_punctuation() -> None:
    assert normalize_title("Sr. Backend Engineer") == "backend engineer"
    assert normalize_title("Senior  Backend   Engineer") == "backend engineer"
    assert normalize_title("Backend Engineer II") == "backend engineer"
    assert normalize_title("Junior Data Scientist") == "data scientist"


def test_normalize_company_collapses_aliases() -> None:
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("Acme GmbH") == "acme"
    assert normalize_company("Acme Ltd") == "acme"
    assert normalize_company("Foo & Bar LLC") == "foo and bar"
