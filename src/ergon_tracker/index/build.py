"""Build a SQLite/FTS5 index file from canonical JobPostings (deterministic, integrity-checked)."""

from __future__ import annotations

from pathlib import Path

from ..canonicalize import aggregate_companies
from ..dedup import deduplicate, normalize_company
from ..models import JobPosting
from .db import connect, fresh_db
from .mapping import from_row, to_row

_JOB_COLS = (  # noqa: SIM905 - space-delimited string is far more readable than a 40-item list
    "id content_hash company_key source company company_domain title department role_family "
    "location city country remote level employment_type sector salary_min salary_max "
    "salary_currency salary_interval salary_annual years_min years_max visa_sponsor "
    "visa_last_filed sponsorship_offered apply_url listing_url board_token posted_at updated_at "
    "closes_at status first_seen last_seen expired_at expiry_reason fetched_at build_id snippet"
).split()


class IndexBuildError(RuntimeError):
    pass


def read_index_jobs(path: Path | str) -> list[JobPosting]:
    """Load all postings from an existing index (for carry-forward in an incremental build)."""
    con = connect(path, read_only=True)
    try:
        return [from_row(r) for r in con.execute("SELECT * FROM jobs")]
    finally:
        con.close()


def merge_incremental(
    prev_jobs: list[JobPosting], fresh_jobs: list[JobPosting], crawled_keys: set[str]
) -> list[JobPosting]:
    """Carry forward prior jobs from boards we did NOT crawl; replace crawled boards with fresh.

    A prior job whose company is in ``crawled_keys`` but absent from ``fresh_jobs`` is dropped
    (it expired off its board). Boards we didn't crawl keep their prior jobs unchanged. The union
    is deduped by :func:`build_index` (idempotent), so we just concatenate here.
    """
    carried = [j for j in prev_jobs if normalize_company(j.company) not in crawled_keys]
    return carried + fresh_jobs


def build_index_incremental(
    prev_index: Path | str | None,
    fresh_jobs: list[JobPosting],
    crawled_keys: set[str],
    path: Path | str,
    *,
    build_id: str,
) -> int:
    """Incremental build: prior index (if any) carried forward + freshly-crawled boards."""
    prev = read_index_jobs(prev_index) if prev_index and Path(prev_index).exists() else []
    merged = merge_incremental(prev, fresh_jobs, crawled_keys)
    return build_index(merged, path, build_id=build_id)


def build_index(jobs: list[JobPosting], path: Path | str, *, build_id: str) -> int:
    """Dedup -> write companies + jobs + provenance + FTS + meta. Returns row count."""
    deduped = deduplicate(jobs)
    deduped.sort(key=lambda j: j.id)  # deterministic order
    fresh_db(path)
    con = connect(path)
    try:
        companies = aggregate_companies(deduped)
        con.executemany(
            "INSERT INTO companies(company_key,display_name,domain,primary_ats,board_token,"
            "sector,h1b_sponsor,h1b_last_filed,open_roles,first_seen,last_seen) "
            "VALUES(:company_key,:display_name,:domain,:primary_ats,:board_token,:sector,"
            ":h1b_sponsor,:h1b_last_filed,:open_roles,:first_seen,:last_seen)",
            [{**c.model_dump(), "h1b_sponsor": 1 if c.h1b_sponsor else None} for c in companies],
        )
        placeholders = ",".join(":" + c for c in _JOB_COLS)
        con.executemany(
            f"INSERT INTO jobs({','.join(_JOB_COLS)}) VALUES({placeholders})",
            [to_row(j, build_id=build_id) for j in deduped],
        )
        con.executemany(
            "INSERT OR IGNORE INTO job_sources(job_id,source,source_job_id,apply_url,fetched_at) "
            "VALUES(?,?,?,?,?)",
            [
                (j.id, p.source, p.source_job_id, p.apply_url, p.fetched_at.isoformat())
                for j in deduped
                for p in j.provenance
            ],
        )
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('build_id',?)", (build_id,))
        con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('row_count',?)", (str(len(deduped)),)
        )
        # External-content FTS5 isn't auto-populated by inserts into `jobs`; rebuild from content,
        # then optimize (merge b-trees) for faster/smaller queries.
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('optimize')")
        con.commit()
        con.execute("ANALYZE")
        con.execute("VACUUM")
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise IndexBuildError(f"integrity_check failed: {ok}")
        return len(deduped)
    finally:
        con.close()
