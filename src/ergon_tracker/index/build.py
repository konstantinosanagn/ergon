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
        cols = ",".join(_JOB_COLS)
        before = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        con.execute(
            f"INSERT OR IGNORE INTO jobs({cols}) SELECT {cols} FROM prev.jobs "  # noqa: S608 - fixed cols
            "WHERE company_key IS NULL OR company_key NOT IN (SELECT k FROM _crawled)"
        )
        con.execute(
            "INSERT OR IGNORE INTO job_sources SELECT s.* FROM prev.job_sources s "
            "WHERE s.job_id IN (SELECT id FROM jobs)"
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
        return finalize_index(con, build_id=build_id)
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


def build_sharded_index_from_db(
    db_path: Path | str, out_dir: Path | str, *, build_id: str
) -> dict[str, Any]:
    """Build per-sector shards from an already-built index via SQL — no jobs loaded into memory.

    Memory-bounded equivalent of build_sharded_index: partitions the index by sector slug using
    SQL ATTACH + finalize_index per shard. Use for full-scale builds.
    """
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

    shards: dict[str, dict[str, Any]] = {}
    for slug, sectors in sorted(slug_to_sectors.items()):
        fname = f"shard-{slug}.sqlite"
        n = _build_shard_from_db(db_path, out / fname, sectors, build_id=build_id)
        raw = (out / fname).read_bytes()
        shards[slug] = {"file": fname, "rows": n, "sha256": hashlib.sha256(raw).hexdigest()}

    manifest = {"build_id": build_id, "schema_version": SCHEMA_VERSION, "shards": shards}
    (out / "shards.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
