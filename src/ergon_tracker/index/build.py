"""Build a SQLite/FTS5 index file from canonical JobPostings (deterministic, integrity-checked)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..canonicalize import aggregate_companies
from ..dedup import deduplicate, normalize_company
from ..models import JobPosting
from .db import SCHEMA_VERSION, connect, fresh_db
from .mapping import from_row, to_row

_JOB_COLS = (  # noqa: SIM905 - space-delimited string is far more readable than a 40-item list
    "id content_hash company_key source company company_domain title department role_family "
    "location city country remote level employment_type sector salary_min salary_max "
    "salary_currency salary_interval salary_annual years_min years_max degree_min "
    "degree_required visa_sponsor "
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


def changed_companies(prev_jobs: list[JobPosting], fresh_jobs: list[JobPosting]) -> set[str]:
    """Company keys whose content set differs between prior and fresh crawls (for tiering).

    Compares the set of content hashes per normalized company. A company present in fresh with a
    different hash-set than before (added/removed/edited postings) is "changed" -> stays hot.
    """
    from .mapping import content_hash

    def by_company(jobs: list[JobPosting]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for j in jobs:
            out.setdefault(normalize_company(j.company), set()).add(content_hash(j))
        return out

    prev_by, fresh_by = by_company(prev_jobs), by_company(fresh_jobs)
    return {k for k, hashes in fresh_by.items() if prev_by.get(k) != hashes}


def changed_companies_sql(fresh_db: Path | str, prev_db: Path | str | None) -> set[str]:
    """SQL equivalent of changed_companies: companies in the fresh index whose content-hash set
    differs from the previous index — computed by comparing two index DBs, no jobs in memory.

    A company present in fresh but absent (or with a different hash-set) in prev is "changed".
    """
    import sqlite3

    con = sqlite3.connect(":memory:")
    try:
        con.execute("ATTACH DATABASE ? AS f", (str(fresh_db),))
        if not prev_db or not Path(prev_db).exists():
            # no prior index -> every fresh company is new == changed
            rows = con.execute(
                "SELECT DISTINCT company_key FROM f.jobs WHERE company_key IS NOT NULL"
            ).fetchall()
            return {r[0] for r in rows}
        con.execute("ATTACH DATABASE ? AS p", (str(prev_db),))
        rows = con.execute(
            "SELECT DISTINCT company_key FROM ("
            "  SELECT company_key, content_hash FROM f.jobs"
            "  EXCEPT SELECT company_key, content_hash FROM p.jobs"
            ") "
            "UNION "
            "SELECT DISTINCT company_key FROM ("
            "  SELECT company_key, content_hash FROM p.jobs"
            "  WHERE company_key IN (SELECT DISTINCT company_key FROM f.jobs)"
            "  EXCEPT SELECT company_key, content_hash FROM f.jobs"
            ")"
        ).fetchall()
        return {r[0] for r in rows if r[0]}
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


# --- Streaming / SQL-merge build (memory-bounded: O(batch) + O(#companies), not O(#jobs)) ----
#
# The in-memory build_index above materializes every job (and several derived lists) at once,
# which OOMs at ~1M rows. The streaming path inserts jobs batch-by-batch, carries forward the
# previous index via SQL ATTACH (no Python job objects), and aggregates companies + builds FTS
# from the DB. Dedup here is exact-id only (INSERT OR IGNORE on the unique `id`); deduplicate()'s
# fuzzy within-company merge is not reproduced (acceptable for a broad-discovery index at scale —
# callers may deduplicate() each batch first to catch intra-batch fuzzy dupes).


def append_jobs(con: object, jobs: Iterable[JobPosting], *, build_id: str) -> int:
    """Insert a batch of JobPostings (+ provenance) into an open index connection.

    Exact-id dedup via INSERT OR IGNORE on the unique ``id``. Returns the number of *new* job
    rows inserted. Memory is O(batch), so the caller can stream arbitrarily many batches.
    """
    import sqlite3
    from collections.abc import Iterable

    assert isinstance(con, sqlite3.Connection)
    batch: list[JobPosting] = list(jobs) if isinstance(jobs, Iterable) else []
    if not batch:
        return 0
    before = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    placeholders = ",".join(":" + c for c in _JOB_COLS)
    con.executemany(
        f"INSERT OR IGNORE INTO jobs({','.join(_JOB_COLS)}) VALUES({placeholders})",
        [to_row(j, build_id=build_id) for j in batch],
    )
    con.executemany(
        "INSERT OR IGNORE INTO job_sources(job_id,source,source_job_id,apply_url,fetched_at) "
        "VALUES(?,?,?,?,?)",
        [
            (j.id, p.source, p.source_job_id, p.apply_url, p.fetched_at.isoformat())
            for j in batch
            for p in j.provenance
        ],
    )
    after = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    return after - before


def _shared_cols(con: object, table: str, wanted: tuple[str, ...] | list[str]) -> str:
    """Comma-joined subset of ``wanted`` that also exists in the ATTACHed ``prev.<table>``.

    Makes carry-forward schema-tolerant: columns added since the prev snapshot was built are
    dropped from the copy (they default to NULL for carried rows) instead of raising and aborting
    the whole merge. ``con`` must already have the prev DB attached as ``prev``.
    """
    import sqlite3

    assert isinstance(con, sqlite3.Connection)
    prev_cols = {r[1] for r in con.execute(f"PRAGMA prev.table_info({table})").fetchall()}
    return ",".join(c for c in wanted if c in prev_cols)


def carry_forward(con: object, prev_db_path: Path | str, crawled_keys: set[str]) -> int:
    """Copy prior-index rows for companies we did NOT crawl into ``con`` (SQL ATTACH, no objects).

    Companies in ``crawled_keys`` were refreshed this run (their fresh rows are already inserted),
    so we skip them; everything else carries forward. Returns rows carried.
    """
    import logging
    import sqlite3

    assert isinstance(con, sqlite3.Connection)
    if not Path(prev_db_path).exists():
        return 0
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _crawled(k TEXT PRIMARY KEY)")
    con.execute("DELETE FROM _crawled")
    con.executemany("INSERT OR IGNORE INTO _crawled(k) VALUES(?)", [(k,) for k in crawled_keys])
    try:
        con.execute("ATTACH DATABASE ? AS prev", (str(prev_db_path),))
    except sqlite3.Error as exc:
        # A truncated/corrupt prev index (e.g. from a past OOM) must not crash the whole build —
        # degrade to a fresh-only build; the row_floor gate then decides whether to publish.
        logging.getLogger("ergon_tracker.index").warning(
            "carry_forward: cannot ATTACH prev index (%s); building fresh-only", exc
        )
        return 0
    try:
        # Carry-forward must tolerate an OLDER-schema prev snapshot: columns added since it was
        # built (e.g. degree_min/degree_required in schema v2) do not exist in prev.jobs. Copy only
        # the columns the two schemas SHARE; newer columns default to NULL for carried rows (they
        # get populated when the board is next re-crawled). Without this, the fixed-column SELECT
        # raises "no such column", the whole ~1.4M backlog is dropped, the row_floor gate rejects
        # the fresh-only build and keeps the stale snapshot -> a permanent deadlock the index can
        # never self-heal from (exactly what froze the daily build on the 2026-07-05 schema-v2 bump).
        jobs_cols = _shared_cols(con, "jobs", _JOB_COLS)
        before = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        con.execute(
            f"INSERT OR IGNORE INTO jobs({jobs_cols}) SELECT {jobs_cols} FROM prev.jobs "  # noqa: S608 - introspected cols
            "WHERE company_key IS NULL OR company_key NOT IN (SELECT k FROM _crawled)"
        )
        # Same intersection for job_sources so a future source-schema change can't silently drop
        # sources (replaces the positional `SELECT s.*`, which breaks the instant the schema drifts).
        src_names = [r[1] for r in con.execute("PRAGMA table_info(job_sources)").fetchall()]
        src_cols = _shared_cols(con, "job_sources", src_names)
        con.execute(
            f"INSERT OR IGNORE INTO job_sources({src_cols}) SELECT {src_cols} FROM prev.job_sources "  # noqa: S608 - introspected cols
            "WHERE job_id IN (SELECT id FROM jobs)"
        )
        after = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        con.commit()
        return after - before
    except sqlite3.DatabaseError as exc:
        logging.getLogger("ergon_tracker.index").warning(
            "carry_forward: prev index read failed mid-copy (%s); fresh-only", exc
        )
        con.rollback()
        return 0
    finally:
        con.execute("DETACH DATABASE prev")


def _aggregate_companies_streamed(con: object) -> list[Any]:
    """Aggregate Company rows by streaming the jobs cursor (memory O(#companies), not O(#jobs)).

    Mirrors canonicalize.aggregate_companies: keyed by normalize_company, open_roles counted,
    first non-null domain/sector kept, H-1B flags from the gazetteer.
    """
    import sqlite3

    from ..extract.visa import h1b_last_filed, is_h1b_sponsor
    from ..models import Company

    assert isinstance(con, sqlite3.Connection)
    out: dict[str, Company] = {}
    cur = con.execute("SELECT company, company_domain, source, sector FROM jobs")
    while True:
        rows = cur.fetchmany(10000)
        if not rows:
            break
        for company, domain, source, sector in rows:
            key = normalize_company(company)
            if not key:
                continue
            c = out.get(key)
            if c is None:
                out[key] = Company(
                    company_key=key,
                    display_name=company,
                    domain=domain,
                    primary_ats=source,
                    sector=sector,
                    h1b_sponsor=True if is_h1b_sponsor(company) else None,
                    h1b_last_filed=h1b_last_filed(company),
                    open_roles=1,
                )
            else:
                c.open_roles += 1
                if not c.domain and domain:
                    c.domain = domain
                if not c.sector and sector:
                    c.sector = sector
    return list(out.values())


def finalize_index(con: object, *, build_id: str, vacuum: bool = False) -> int:
    """Insert companies (streamed), build FTS, write meta, ANALYZE, integrity-check.

    VACUUM is OFF by default: the index is built write-once into a fresh DB (no updates/deletes),
    so there's no free space to reclaim — VACUUM would just rewrite the whole file, needing ~2x
    disk (ENOSPC risk at ~1GB × 30 shard builds) and minutes of time for ~no benefit (the gz
    handles size). Pass vacuum=True only if a build path actually churns rows.
    """
    import sqlite3

    assert isinstance(con, sqlite3.Connection)
    companies = _aggregate_companies_streamed(con)
    con.executemany(
        "INSERT OR REPLACE INTO companies(company_key,display_name,domain,primary_ats,board_token,"
        "sector,h1b_sponsor,h1b_last_filed,open_roles,first_seen,last_seen) "
        "VALUES(:company_key,:display_name,:domain,:primary_ats,:board_token,:sector,"
        ":h1b_sponsor,:h1b_last_filed,:open_roles,:first_seen,:last_seen)",
        [{**c.model_dump(), "h1b_sponsor": 1 if c.h1b_sponsor else None} for c in companies],
    )
    n = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('build_id',?)", (build_id,))
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('row_count',?)", (str(n),))
    con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
    con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('optimize')")
    con.commit()
    con.execute("ANALYZE")
    if vacuum:
        con.execute("VACUUM")
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        raise IndexBuildError(f"integrity_check failed: {ok}")
    return n


def _relevel_from_years(con: object) -> int:
    """Reclassify level='unknown' rows from already-stored years-of-experience — no re-crawl.

    Propagates the years->level inference to carried-forward jobs built BEFORE inference was
    enabled (fixes the whole index on the next build, not just newly-crawled boards). Description-
    phrase cues need JD text (not stored), so this covers the years path only — the bigger lever
    for the backlog. Reuses level_from_years so SQL/Python never drift; memory-bounded (only the
    unknown-with-years slice).
    """
    import sqlite3

    from ..extract.level import level_from_years
    from ..models import JobLevel

    assert isinstance(con, sqlite3.Connection)
    rows = con.execute(
        "SELECT id, years_min, years_max FROM jobs WHERE level = 'unknown' "
        "AND (years_min IS NOT NULL OR years_max IS NOT NULL)"
    ).fetchall()
    updates = [
        (lvl.value, jid)
        for jid, ymin, ymax in rows
        if (lvl := level_from_years(ymin, ymax)) is not JobLevel.UNKNOWN
    ]
    if updates:
        con.executemany("UPDATE jobs SET level = ? WHERE id = ?", updates)
        con.commit()
    return len(updates)


PURGE_YEARS = (
    5  # postings older than this (by most-recent activity) are unambiguously dead -> dropped
)


def _purge_ancient(con: object, *, max_years: int = PURGE_YEARS) -> int:
    """Physically drop postings whose most-recent activity (max of posted_at/updated_at) is older than
    ``max_years`` — the unambiguous-dead tail. ATS boards leave filled reqs open for years; a 5+ year-
    old posting is never live, so we DELETE it (shrinks the published index, and it stays gone even
    when a query opts into stale results) rather than merely hide it via the query-time filter.

    DATED rows only: undated rows are left alone (their age is unknown, so they stay recoverable and
    are handled by the query freshness filter). Run BEFORE finalize_index, which rebuilds FTS +
    companies + integrity-checks from the survivors. Returns the number of postings dropped.
    """
    import sqlite3
    from datetime import date, timedelta

    assert isinstance(con, sqlite3.Connection)
    cutoff = (date.today() - timedelta(days=365 * max_years)).isoformat()
    fresh = "MAX(COALESCE(posted_at, ''), COALESCE(updated_at, ''))"
    where = f"{fresh} != '' AND {fresh} < ?"
    ids = f"SELECT id FROM jobs WHERE {where}"  # noqa: S608 - fresh/where are constants, cutoff is bound
    # FK is OFF during build so ON DELETE CASCADE won't fire; clean child tables explicitly (mirrors
    # the delta-delete path). job_events/job_tags may be empty — the deletes are then no-ops.
    for child in ("job_sources", "job_events", "job_tags"):
        con.execute(f"DELETE FROM {child} WHERE job_id IN ({ids})", (cutoff,))  # noqa: S608
    cur = con.execute(f"DELETE FROM jobs WHERE {where}", (cutoff,))  # noqa: S608
    con.commit()
    return cur.rowcount


def build_index_streaming(
    job_batches: Iterable[Iterable[JobPosting]],
    path: Path | str,
    *,
    build_id: str,
    prev_db: Path | str | None = None,
    crawled_keys: set[str] | None = None,
) -> int:
    """Memory-bounded build: stream job batches in, carry forward prev via SQL, finalize.

    ``job_batches`` is an iterable of JobPosting iterables (e.g. one per board). When ``prev_db``
    + ``crawled_keys`` are given, prior rows for un-crawled companies are carried forward in SQL.
    """
    fresh_db(path)
    con = connect(path)
    try:
        # Jobs are inserted before companies exist (companies are aggregated from jobs in
        # finalize_index), so defer FK enforcement; finalize's integrity_check + the
        # company_fk_intact gate confirm referential integrity once companies are written.
        con.execute("PRAGMA foreign_keys = OFF")
        for batch in job_batches:
            append_jobs(con, batch, build_id=build_id)
        if prev_db is not None and crawled_keys is not None and Path(prev_db).exists():
            carry_forward(con, prev_db, crawled_keys)
        _relevel_from_years(con)  # re-level carried-forward backlog from stored years (no re-crawl)
        _purge_ancient(con)  # drop the unambiguous-dead >5yr tail before finalize
        return finalize_index(con, build_id=build_id)
    finally:
        con.close()


def build_index_from_fresh_db(
    fresh_db_path: Path | str,
    path: Path | str,
    *,
    build_id: str,
    prev_db: Path | str | None = None,
    crawled_keys: set[str] | None = None,
) -> int:
    """Build the final index from a crawl's fresh-jobs DB + carry-forward, entirely in SQL.

    The streaming crawl writes each board's jobs into ``fresh_db_path``; here we copy those into
    a clean index, carry forward un-crawled companies from ``prev_db``, and finalize — never
    loading job objects into memory. Memory is O(#companies) at finalize.
    """
    fresh_db(path)
    con = connect(path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")  # companies aggregated in finalize_index
        con.execute("ATTACH DATABASE ? AS fr", (str(fresh_db_path),))
        cols = ",".join(_JOB_COLS)
        con.execute(f"INSERT OR IGNORE INTO jobs({cols}) SELECT {cols} FROM fr.jobs")  # noqa: S608
        con.execute("INSERT OR IGNORE INTO job_sources SELECT * FROM fr.job_sources")
        con.commit()
        con.execute("DETACH DATABASE fr")
        if prev_db is not None and crawled_keys is not None and Path(prev_db).exists():
            carry_forward(con, prev_db, crawled_keys)
        _relevel_from_years(con)  # re-level carried-forward backlog from stored years (no re-crawl)
        _purge_ancient(con)  # drop the unambiguous-dead >5yr tail before finalize
        return finalize_index(con, build_id=build_id)
    finally:
        con.close()


# Columns nulled in the slim broad-query tier: heavy/free-text fields a BROAD keyword search
# doesn't need to match or display. Kept: id, company, title, level, sector, city/country/location,
# remote, salary*, degree_min/degree_required (tiny + filterable, so max_degree serves from slim),
# visa*, sponsorship, apply_url, posted_at, source, status, dates (schema NOT NULL cols must stay).
# The FTS over title+company+department+snippet auto-shrinks since department + snippet become
# NULL. content_hash stays (NOT NULL + cheap, 16 hex).
_SLIM_NULL_COLS = frozenset(
    {
        "snippet",
        "department",
        "role_family",
        "company_domain",
        "listing_url",
        "board_token",
        "salary_annual",
        "years_min",
        "years_max",
        "visa_last_filed",
        "updated_at",
        "closes_at",
        "expired_at",
        "expiry_reason",
    }
)


def build_slim_index(full_db: Path | str, slim_path: Path | str, *, build_id: str) -> int:
    """Build the compact broad-query tier from a full index (SQL copy, memory-bounded).

    Same schema as the full index (so SqliteIndexBackend/search_rows work unchanged), but heavy
    free-text columns are nulled and provenance (job_sources) is skipped, so a BROAD keyword query
    downloads a much smaller file. Keyword matching still works on title+company (snippet/department
    nulled -> the FTS shrinks). A query needing the description falls back to the full index.
    """
    fresh_db(slim_path)
    con = connect(slim_path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("ATTACH DATABASE ? AS full", (str(full_db),))
        select_cols = ",".join(f"NULL AS {c}" if c in _SLIM_NULL_COLS else c for c in _JOB_COLS)
        insert_cols = ",".join(_JOB_COLS)
        con.execute(
            f"INSERT INTO jobs({insert_cols}) SELECT {select_cols} FROM full.jobs"  # noqa: S608
        )
        con.execute(
            "INSERT INTO companies SELECT * FROM full.companies"  # companies needed for FK + nav
        )
        con.commit()
        con.execute("DETACH DATABASE full")
        n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('build_id',?)", (build_id,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('row_count',?)", (str(n),))
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('optimize')")
        con.commit()
        con.execute("ANALYZE")
        con.execute("VACUUM")  # reclaim the nulled-column slack — being small IS the point here
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise IndexBuildError(f"slim integrity_check failed: {ok}")
        return int(n)
    finally:
        con.close()


# Per-build bookkeeping that advances for EVERY row each build (even unchanged postings). Excluded
# from delta change-detection so a delta carries only genuinely changed rows, not the whole index.
_DELTA_VOLATILE_COLS = frozenset({"build_id", "fetched_at", "last_seen"})


def build_delta(
    prev_db: Path | str,
    curr_db: Path | str,
    out_path: Path | str,
    *,
    from_build_id: str,
    to_build_id: str,
) -> dict[str, Any]:
    """Emit a compact row-level delta from ``prev_db`` to ``curr_db`` (v2.1 incremental download).

    The delta carries only what changed: ``delta_upserts`` = jobs new in curr or whose
    content_hash differs (+ their job_sources), and ``delta_deletes`` = ids present in prev but
    gone in curr. A returning user one build behind downloads this instead of the whole index.
    Returns ``{upserts, deletes, from_build_id, to_build_id}``.
    """
    import sqlite3

    con = sqlite3.connect(str(out_path))
    try:
        cols = ",".join(_JOB_COLS)
        con.execute(f"CREATE TABLE delta_upserts({cols})")  # noqa: S608 - fixed col list
        con.execute(
            "CREATE TABLE delta_upserts_sources(job_id,source,source_job_id,apply_url,fetched_at)"
        )
        con.execute("CREATE TABLE delta_deletes(id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("ATTACH DATABASE ? AS prev", (str(prev_db),))
        con.execute("ATTACH DATABASE ? AS curr", (str(curr_db),))
        # upserts: row is new (id not in prev) OR any content-bearing column differs. Per-build
        # bookkeeping (_DELTA_VOLATILE_COLS) is excluded so an unchanged posting isn't re-sent every
        # build just because its build_id/fetched_at advanced. NULL-safe via IS.
        # Schema-tolerant: only compare columns that EXIST in prev — a column added since the prev
        # snapshot was built (e.g. degree_min in schema v2) isn't there to compare, so referencing
        # p.<newcol> would raise "no such column" and crash the whole delta (the build's second
        # schema-v2 regression). New columns are simply not part of the change test for one build.
        prev_cols = {r[1] for r in con.execute("PRAGMA prev.table_info(jobs)").fetchall()}
        same = " AND ".join(
            f"p.{c} IS c.{c}"
            for c in _JOB_COLS
            if c not in _DELTA_VOLATILE_COLS and c in prev_cols
        )
        con.execute(
            f"INSERT INTO delta_upserts({cols}) SELECT {cols} FROM curr.jobs c "  # noqa: S608
            f"WHERE NOT EXISTS (SELECT 1 FROM prev.jobs p WHERE p.id = c.id AND {same})"
        )
        con.execute(
            "INSERT INTO delta_upserts_sources SELECT s.* FROM curr.job_sources s "
            "WHERE s.job_id IN (SELECT id FROM delta_upserts)"
        )
        # deletes: in prev, absent from curr
        con.execute(
            "INSERT INTO delta_deletes(id) SELECT p.id FROM prev.jobs p "
            "WHERE NOT EXISTS (SELECT 1 FROM curr.jobs c WHERE c.id = p.id)"
        )
        ups = int(con.execute("SELECT COUNT(*) FROM delta_upserts").fetchone()[0])
        dels = int(con.execute("SELECT COUNT(*) FROM delta_deletes").fetchone()[0])
        con.commit()  # release the attached DBs before detaching
        con.execute("DETACH DATABASE prev")
        con.execute("DETACH DATABASE curr")
        meta = {
            "from_build_id": from_build_id,
            "to_build_id": to_build_id,
            "schema_version": str(SCHEMA_VERSION),
        }
        con.executemany("INSERT INTO meta(key,value) VALUES(?,?)", list(meta.items()))
        con.commit()
        con.execute("VACUUM")  # keep the delta as small as possible — the entire point
        return {
            "upserts": ups,
            "deletes": dels,
            "from_build_id": from_build_id,
            "to_build_id": to_build_id,
        }
    finally:
        con.close()


def apply_delta(base_db: Path | str, delta_db: Path | str) -> int:
    """Apply a row-level delta IN PLACE onto a cached base index, advancing it to the delta target.

    Refuses unless the base's build_id equals the delta's ``from_build_id`` (a delta is only valid
    against the exact build it was diffed from). Deletes gone rows, upserts changed/new rows,
    re-aggregates companies, rebuilds FTS, advances build_id, and integrity-checks. Returns the
    resulting job count.
    """
    con = connect(base_db)
    try:
        con.execute("ATTACH DATABASE ? AS d", (str(delta_db),))
        dmeta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM d.meta").fetchall()}
        base_build = con.execute("SELECT value FROM meta WHERE key='build_id'").fetchone()
        base_build_id = base_build[0] if base_build else None
        if dmeta.get("from_build_id") != base_build_id:
            raise IndexBuildError(
                f"delta from_build_id={dmeta.get('from_build_id')!r} != base build_id="
                f"{base_build_id!r}; cannot apply"
            )
        # FK off during mutation: upserted jobs may reference new company_keys not aggregated yet
        # (companies are rebuilt below). job_sources for removed ids are cleaned manually since the
        # ON DELETE CASCADE only fires with FK on. integrity_check in finalize validates the result.
        con.execute("PRAGMA foreign_keys = OFF")
        # ids leaving or being replaced -> drop their stale job_sources first
        con.execute(
            "DELETE FROM job_sources WHERE job_id IN (SELECT id FROM d.delta_deletes) "
            "OR job_id IN (SELECT id FROM d.delta_upserts)"
        )
        # 1. deletes
        con.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM d.delta_deletes)")
        # 2. upserts: drop any existing row with the same id, then insert the new version
        con.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM d.delta_upserts)")
        cols = ",".join(_JOB_COLS)
        con.execute(f"INSERT INTO jobs({cols}) SELECT {cols} FROM d.delta_upserts")  # noqa: S608
        con.execute(
            "INSERT OR IGNORE INTO job_sources(job_id,source,source_job_id,apply_url,fetched_at) "
            "SELECT job_id,source,source_job_id,apply_url,fetched_at FROM d.delta_upserts_sources"
        )
        con.commit()
        con.execute("DETACH DATABASE d")
        # 3. re-aggregate companies from the mutated jobs table (open_roles etc. shift)
        con.execute("DELETE FROM companies")
        to_build_id = dmeta["to_build_id"]
        n = finalize_index(con, build_id=to_build_id, vacuum=True)
        return n
    finally:
        con.close()


def sector_slug(sector: str | None) -> str:
    """Filesystem-safe shard key for a sector ('AI/ML' -> 'ai-ml'); None/empty -> 'unknown'."""
    if not sector:
        return "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", sector.lower()).strip("-")
    return slug or "unknown"


def build_sharded_index(
    jobs: list[JobPosting], out_dir: Path | str, *, build_id: str
) -> dict[str, Any]:
    """Build one SQLite shard per sector (+ 'unknown') + a shards.json manifest. Returns manifest.

    Dedup once over the whole set, then partition by sector so a sector-scoped query later opens
    only its shard. Each shard is a normal index DB (same schema), so ShardedIndexBackend can use
    the same query path.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    deduped = deduplicate(jobs)
    by_sector: dict[str, list[JobPosting]] = {}
    for j in deduped:
        by_sector.setdefault(sector_slug(j.sector), []).append(j)

    shards: dict[str, dict[str, Any]] = {}
    for slug, sjobs in sorted(by_sector.items()):
        fname = f"shard-{slug}.sqlite"
        n = build_index(sjobs, out / fname, build_id=build_id)
        raw = (out / fname).read_bytes()
        shards[slug] = {"file": fname, "rows": n, "sha256": hashlib.sha256(raw).hexdigest()}

    manifest = {"build_id": build_id, "schema_version": SCHEMA_VERSION, "shards": shards}
    (out / "shards.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _build_shard_from_db(
    src_db: Path | str, shard_path: Path, sectors: list[Any], *, build_id: str
) -> int:
    """Copy one sector's rows from a built index into a shard DB via SQL (memory-bounded)."""
    fresh_db(shard_path)
    con = connect(shard_path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")  # companies aggregated in finalize_index
        con.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        cols = ",".join(_JOB_COLS)
        non_null = [s for s in sectors if s not in (None, "")]
        clauses, params = [], []
        if non_null:
            clauses.append(f"sector IN ({','.join('?' for _ in non_null)})")
            params.extend(non_null)
        if any(s in (None, "") for s in sectors):
            clauses.append("sector IS NULL OR sector = ''")
        where = " OR ".join(f"({c})" for c in clauses) or "0"
        con.execute(
            f"INSERT INTO jobs({cols}) SELECT {cols} FROM src.jobs WHERE {where}",  # noqa: S608
            params,
        )
        con.execute(
            "INSERT OR IGNORE INTO job_sources SELECT s.* FROM src.job_sources s "
            "WHERE s.job_id IN (SELECT id FROM jobs)"
        )
        con.commit()  # close the txn so DETACH (and finalize's VACUUM) can run
        con.execute("DETACH DATABASE src")
        return finalize_index(con, build_id=build_id)
    finally:
        con.close()


def _shard_meta(out: Path, slug: str, n: int) -> dict[str, Any]:
    fname = f"shard-{slug}.sqlite"
    raw = (out / fname).read_bytes()
    return {"file": fname, "rows": n, "sha256": hashlib.sha256(raw).hexdigest()}


def build_sharded_index_from_db(
    db_path: Path | str, out_dir: Path | str, *, build_id: str, max_workers: int | None = None
) -> dict[str, Any]:
    """Build per-sector shards from an already-built index via SQL — no jobs loaded into memory.

    Partitions the index by sector slug using SQL ATTACH + finalize_index per shard. Shards are
    INDEPENDENT (each is a read-only ATTACH of the immutable finalized index writing to its own file),
    so they build CONCURRENTLY across a process pool — the per-sector loop was the single biggest serial
    cost in the publish phase. ``_build_shard_from_db`` takes only paths, so it pickles cleanly. Falls
    back to sequential for a single shard / single worker (tests, tiny builds) to skip pool overhead.
    ``ERGON_SHARD_WORKERS`` overrides the worker count (default = cpu_count-2).
    """
    import os
    from concurrent.futures import ProcessPoolExecutor, as_completed

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    src = connect(db_path, read_only=True)
    try:
        raw_sectors = [r[0] for r in src.execute("SELECT DISTINCT sector FROM jobs")]
    finally:
        src.close()
    slug_to_sectors: dict[str, list[Any]] = {}
    for s in raw_sectors:
        slug_to_sectors.setdefault(sector_slug(s), []).append(s)
    items = sorted(slug_to_sectors.items())

    # Laptop-safe by default: parallel shard processes only on a dedicated runner (CI=true, set by
    # GitHub Actions). Locally a build runs shards sequentially so it never oversubscribes a laptop —
    # ERGON_SHARD_WORKERS overrides either way.
    env_workers = os.environ.get("ERGON_SHARD_WORKERS")
    if env_workers:
        workers = int(env_workers)
    elif os.environ.get("CI"):
        workers = max(2, (os.cpu_count() or 4) - 2)
    else:
        workers = 1  # local: sequential, no worker processes
    workers = min(workers, len(items)) if items else 1

    shards: dict[str, dict[str, Any]] = {}
    if workers <= 1:  # sequential fallback: tiny builds / tests — no process-pool spawn cost
        for slug, sectors in items:
            n = _build_shard_from_db(
                db_path, out / f"shard-{slug}.sqlite", sectors, build_id=build_id
            )
            shards[slug] = _shard_meta(out, slug, n)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _build_shard_from_db,
                    db_path,
                    out / f"shard-{slug}.sqlite",
                    sectors,
                    build_id=build_id,
                ): slug
                for slug, sectors in items
            }
            for fut in as_completed(futs):
                slug = futs[fut]
                shards[slug] = _shard_meta(
                    out, slug, fut.result()
                )  # .result() re-raises worker errors

    manifest = {
        "build_id": build_id,
        "schema_version": SCHEMA_VERSION,
        "shards": dict(sorted(shards.items())),
    }
    (out / "shards.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
