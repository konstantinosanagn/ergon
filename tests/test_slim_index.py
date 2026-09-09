"""Slim broad-query tier: same schema, snippet dropped, parity with full for broad queries."""

from __future__ import annotations

from ergon.index.backend import SqliteIndexBackend
from ergon.index.build import build_index, build_slim_index
from ergon.index.db import connect
from ergon.models import JobLevel, JobPosting, Location, RemoteType, SearchQuery


def _job(sid, company, title, **kw):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=company,
        title=title,
        description_text=kw.pop("desc", "a long job description " * 40),
        locations=[Location(raw=kw.pop("loc", "Remote"), is_remote=True)],
        remote=RemoteType.REMOTE,
        **kw,
    )


def test_slim_parity_and_smaller(tmp_path):
    # Enough rows with big descriptions that the dropped snippet text spans many pages, so the
    # slim file is visibly smaller after VACUUM (page-granular at tiny sizes otherwise).
    jobs = [
        _job(
            str(i),
            f"Co{i}",
            f"Backend Engineer {i}",
            desc="detailed responsibilities and requirements " * 60,
            level=JobLevel.SENIOR if i % 2 else JobLevel.MID,
            sector="Fintech" if i % 2 else "AI/ML",
        )
        for i in range(120)
    ]
    full = tmp_path / "full.sqlite"
    slim = tmp_path / "slim.sqlite"
    build_index(jobs, full, build_id="b1")
    n = build_slim_index(full, slim, build_id="b1")
    assert n == 120

    # broad keyword query returns the SAME jobs from slim as from full (matches on title/company)
    q = SearchQuery(keywords="backend", limit=200)
    full_ids = {j.id for j in SqliteIndexBackend(full).search(q)}
    slim_ids = {j.id for j in SqliteIndexBackend(slim).search(q)}
    assert slim_ids == full_ids and len(slim_ids) == 120

    # filters still work on the slim tier (level/sector kept)
    fin = SqliteIndexBackend(slim).search(
        SearchQuery(keywords="backend", sector="Fintech", limit=200)
    )
    assert fin and all(j.sector == "Fintech" for j in fin)

    # snippet dropped in slim, present in full
    assert (
        connect(slim, read_only=True)
        .execute("SELECT COUNT(*) FROM jobs WHERE snippet IS NOT NULL")
        .fetchone()[0]
        == 0
    )

    # slim file is materially smaller (snippet text + its FTS dropped, then VACUUMed)
    assert slim.stat().st_size < full.stat().st_size

    # slim is integrity-clean
    assert connect(slim, read_only=True).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
