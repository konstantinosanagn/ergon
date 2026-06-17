"""Cross-source dedup: the same job posted on many sites collapses to one, ATS wins, provenance
unions all sources."""

from __future__ import annotations

from ergon_tracker import JobPosting, Location, RemoteType, Salary
from ergon_tracker.dedup import deduplicate


def _job(source: str, sid: str, title: str, company: str, **kw: object) -> JobPosting:
    return JobPosting.create(source=source, source_job_id=sid, title=title, company=company, **kw)


def test_same_job_on_three_sites_merges_to_one() -> None:
    jobs = [
        _job(
            "themuse",
            "m1",
            "Backend Engineer",
            "Acme Inc",
            locations=[Location(city="Berlin", country="Germany")],
        ),
        _job(
            "greenhouse",
            "g1",
            "Senior Backend Engineer",
            "Acme",
            locations=[Location(city="Berlin", country="Germany")],
            salary=Salary(min_amount=90000, currency="EUR"),
        ),
        _job("remoteok", "r1", "Sr. Backend Engineer", "Acme", remote=RemoteType.REMOTE),
    ]
    out = deduplicate(jobs)
    assert len(out) == 1
    merged = out[0]
    assert merged.source == "greenhouse"  # ATS beats aggregators
    assert {p.source for p in merged.provenance} == {"greenhouse", "themuse", "remoteok"}


def test_aggregator_only_crosspost_merges() -> None:
    jobs = [
        _job("themuse", "m2", "Product Designer", "Globex"),
        _job("jobicy", "j2", "Product Designer", "Globex", remote=RemoteType.REMOTE),
        _job("remotive", "rv2", "Product  Designer", "Globex", remote=RemoteType.REMOTE),
    ]
    out = deduplicate(jobs)
    assert len(out) == 1
    assert {p.source for p in out[0].provenance} == {"themuse", "jobicy", "remotive"}


def test_different_companies_same_title_do_not_merge() -> None:
    jobs = [
        _job("greenhouse", "a", "Software Engineer", "Google"),
        _job("greenhouse", "b", "Software Engineer", "Meta"),
    ]
    assert len(deduplicate(jobs)) == 2


def test_same_company_distinct_roles_do_not_merge() -> None:
    jobs = [
        _job("lever", "1", "Backend Engineer", "Acme"),
        _job("lever", "2", "Frontend Designer", "Acme"),
    ]
    assert len(deduplicate(jobs)) == 2


def test_ats_beats_aggregator_even_when_aggregator_first() -> None:
    jobs = [
        _job("remotive", "rv", "Data Scientist", "Acme", remote=RemoteType.REMOTE),
        _job(
            "workday",
            "wd",
            "Data Scientist",
            "Acme",
            locations=[Location(raw="Remote", is_remote=True)],
        ),
    ]
    out = deduplicate(jobs)
    assert len(out) == 1
    assert out[0].source == "workday"
