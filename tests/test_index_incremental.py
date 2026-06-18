from ergon_tracker.index.build import (
    build_index,
    build_index_incremental,
    merge_incremental,
    read_index_jobs,
)
from ergon_tracker.index.db import connect
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType


def _job(sid, company, title, **kw):
    return JobPosting.create(
        source=kw.pop("source", "greenhouse"),
        source_job_id=sid,
        company=company,
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def test_read_index_jobs_round_trips(tmp_path):
    p = tmp_path / "i.sqlite"
    build_index([_job("1", "Stripe", "Backend Engineer", level=JobLevel.SENIOR)], p, build_id="b1")
    jobs = read_index_jobs(p)
    assert len(jobs) == 1 and jobs[0].company == "Stripe" and jobs[0].level is JobLevel.SENIOR


def test_merge_carries_uncrawled_and_replaces_crawled():
    prev = [_job("1", "Stripe", "Old Backend Role"), _job("2", "Ramp", "Ramp Engineer")]
    fresh = [_job("9", "Stripe", "New Backend Role")]  # Stripe re-crawled
    merged = merge_incremental(prev, fresh, crawled_keys={"stripe"})
    titles = {j.title for j in merged}
    assert "New Backend Role" in titles  # fresh Stripe kept
    assert "Old Backend Role" not in titles  # expired off the crawled board
    assert "Ramp Engineer" in titles  # Ramp not crawled -> carried forward


def test_build_incremental_end_to_end(tmp_path):
    prev_db = tmp_path / "prev.sqlite"
    build_index(
        [_job("1", "Stripe", "Old Role"), _job("2", "Ramp", "Ramp Engineer")],
        prev_db,
        build_id="b1",
    )
    out = tmp_path / "new.sqlite"
    n = build_index_incremental(
        prev_db,
        fresh_jobs=[_job("9", "Stripe", "New Role")],
        crawled_keys={"stripe"},
        path=out,
        build_id="b2",
    )
    assert n == 2  # New Stripe role + carried Ramp role
    con = connect(out, read_only=True)
    companies = {r[0] for r in con.execute("SELECT company FROM jobs")}
    titles = {r[0] for r in con.execute("SELECT title FROM jobs")}
    assert companies == {"Stripe", "Ramp"}
    assert "New Role" in titles and "Old Role" not in titles


def test_build_incremental_cold_start_no_prev(tmp_path):
    out = tmp_path / "new.sqlite"
    n = build_index_incremental(
        None, fresh_jobs=[_job("1", "Stripe", "Role")], crawled_keys={"stripe"},
        path=out, build_id="b1",
    )
    assert n == 1
