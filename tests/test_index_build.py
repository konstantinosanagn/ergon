from ergon_tracker.index.build import build_index
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


def test_build_writes_rows_companies_fts_and_passes_integrity(tmp_path):
    p = tmp_path / "i.sqlite"
    jobs = [
        _job("1", "Stripe", "Senior Backend Engineer", level=JobLevel.SENIOR, sector="Fintech"),
        _job("2", "Stripe", "Frontend Engineer"),
    ]
    n = build_index(jobs, p, build_id="b1")
    assert n == 2
    con = connect(p, read_only=True)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert (
        con.execute("SELECT open_roles FROM companies WHERE company_key='stripe'").fetchone()[0]
        == 2
    )
    hits = con.execute(
        "SELECT j.title FROM jobs j JOIN jobs_fts f ON j.rowid=f.rowid WHERE jobs_fts MATCH 'backend'"
    ).fetchall()
    assert any("Backend" in h[0] for h in hits)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_build_dedups_same_job_from_two_sources(tmp_path):
    p = tmp_path / "i.sqlite"
    jobs = [
        _job("1", "Stripe", "Senior Backend Engineer", source="greenhouse"),
        _job("x", "Stripe", "Sr. Backend Engineer", source="remoteok"),
    ]
    build_index(jobs, p, build_id="b1")
    con = connect(p, read_only=True)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] >= 2
