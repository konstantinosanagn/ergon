"""Tier-3 detail sidecar: recovered structured fields + snippet from per-posting JD detail fetches,
keyed by posting id with a sig for re-crawl-safe carry-forward. The JD text itself is never stored."""
from __future__ import annotations

import hashlib
import sqlite3
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio

from ..enrich import enrich_in_place
from ..extract.base import html_to_text
from ..models import JobPosting

DETAIL_SCHEMA_VERSION = 1
DETAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_detail (
  id TEXT PRIMARY KEY,
  sig TEXT,
  fetched_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  snippet TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
  years_min INTEGER, years_max INTEGER,
  degree_min TEXT, degree_required INTEGER,
  sponsorship_offered INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ensure_detail_schema(con: sqlite3.Connection) -> None:
    con.executescript(DETAIL_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(DETAIL_SCHEMA_VERSION),))
    con.commit()


def open_detail(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    ensure_detail_schema(con)
    return con


def detail_sig(row: dict[str, Any]) -> str:
    """Change signal for a posting, INDEPENDENT of the (to-be-fetched) description — so we only
    re-fetch when the posting materially changed. Uses content_hash if present, else title+level."""
    basis = row.get("content_hash") or f"{row.get('title', '')}|{row.get('level', '')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DetailRef:
    id: str
    source: str
    token: str | None
    apply_url: str | None
    listing_url: str | None
    content_sig: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DetailRef:
        return cls(
            id=str(row["id"]),
            source=str(row.get("source") or ""),
            token=row.get("board_token"),
            apply_url=row.get("apply_url"),
            listing_url=row.get("listing_url"),
            content_sig=detail_sig(row),
        )


# --- reconcile pass ----------------------------------------------------------------------------

RETRY_CAP = 3  # bounded retries for a ref whose fetch keeps failing (never re-fetched forever)
_DEFAULT_CONCURRENCY = 8  # in-flight fetches; the injected AsyncFetcher bounds per-host rate

_JOBS_COLUMNS = "id, source, board_token, apply_url, listing_url, content_hash, description"


def _tier3_rows(idx_con: sqlite3.Connection, sources: Sequence[str] | None) -> list[dict[str, Any]]:
    """Index rows with no description (Tier-3 candidates), optionally restricted to ``sources``."""
    sql = f"SELECT {_JOBS_COLUMNS} FROM jobs WHERE (description IS NULL OR TRIM(description) = '')"
    params: list[Any] = []
    if sources:
        placeholders = ",".join("?" for _ in sources)
        sql += f" AND source IN ({placeholders})"
        params.extend(sources)
    idx_con.row_factory = sqlite3.Row
    return [dict(r) for r in idx_con.execute(sql, params).fetchall()]


def _load_existing(det_con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = det_con.execute("SELECT id, sig, fetched_at, attempts FROM job_detail").fetchall()
    return {r[0]: {"sig": r[1], "fetched_at": r[2], "attempts": r[3]} for r in rows}


def _eligible(id_: str, sig: str, existing: dict[str, dict[str, Any]]) -> bool:
    """A ref needs (re-)fetching when the sidecar has no row, a stale sig, or an unspent retry
    budget on a prior failure (``fetched_at`` never got set)."""
    d = existing.get(id_)
    if d is None:
        return True
    if d["sig"] != sig:
        return True
    return d["fetched_at"] is None and d["attempts"] < RETRY_CAP


def _interleave_by_source(refs: Sequence[DetailRef]) -> list[DetailRef]:
    """Round-robin refs across their source so a windowed slice isn't dominated by one board —
    mirrors ``scripts/build_index._interleave_by_ats`` (stratified placement within each bucket)."""
    buckets: OrderedDict[str, list[DetailRef]] = OrderedDict()
    for ref in refs:
        buckets.setdefault(ref.source, []).append(ref)
    keyed: list[tuple[float, int, DetailRef]] = []
    for order, blist in enumerate(buckets.values()):
        m = len(blist)
        for i, ref in enumerate(blist):
            keyed.append(((i + 0.5) / m, order, ref))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [ref for _, _, ref in keyed]


def _load_cursor(det_con: sqlite3.Connection) -> int:
    row = det_con.execute("SELECT value FROM meta WHERE key = 'detail_cursor'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _save_cursor(det_con: sqlite3.Connection, cursor: int) -> None:
    det_con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('detail_cursor', ?)", (str(cursor),)
    )


def _select_window(
    det_con: sqlite3.Connection, interleaved: list[DetailRef], max_details: int | None
) -> tuple[list[DetailRef], int]:
    """Rotating-cursor slice of ``interleaved`` (wrapping), so repeated bounded runs eventually
    reach every candidate rather than always the same head of the list."""
    total = len(interleaved)
    if total == 0:
        return [], 0
    if max_details is None or max_details >= total:
        return interleaved, 0
    cursor = _load_cursor(det_con) % total
    window = [interleaved[(cursor + i) % total] for i in range(max_details)]
    return window, (cursor + max_details) % total


async def _run_fetches(
    window: list[DetailRef],
    fetch_detail: Callable[[DetailRef], Awaitable[str | None]],
    concurrency: int,
) -> list[tuple[DetailRef, str | None]]:
    """Fetch the whole window concurrently, bounded by ``concurrency``. A failing fetch (exception
    or ``None``) is captured per-ref, never raised — one bad host must not abort the others."""
    limiter = anyio.CapacityLimiter(concurrency)
    results: list[tuple[DetailRef, str | None]] = []

    async def worker(ref: DetailRef) -> None:
        async with limiter:
            try:
                desc = await fetch_detail(ref)
            except Exception:
                desc = None
        results.append((ref, desc))

    async with anyio.create_task_group() as tg:
        for ref in window:
            tg.start_soon(worker, ref)
    return results


def _record_attempt(det_con: sqlite3.Connection, id_: str) -> None:
    det_con.execute(
        "INSERT INTO job_detail(id, attempts) VALUES (?, 1) "
        "ON CONFLICT(id) DO UPDATE SET attempts = attempts + 1",
        (id_,),
    )


def _record_success(
    det_con: sqlite3.Connection, ref: DetailRef, job: JobPosting, snippet: str, fetched_at: str
) -> None:
    """Persist recovered fields + snippet; the JD text (``job.description_html``) is discarded —
    only what's passed in below ever reaches the sidecar. Resets ``attempts`` (fresh retry budget
    should this sig later go stale again)."""
    salary = job.salary
    salary_min = salary.min_amount if salary else None
    salary_max = salary.max_amount if salary else None
    salary_currency = salary.currency if salary else None
    salary_interval = salary.interval.value if salary and salary.interval else None
    degree_required = None if job.degree_required is None else int(job.degree_required)
    sponsorship_offered = None if job.sponsorship_offered is None else int(job.sponsorship_offered)
    det_con.execute(
        """
        INSERT INTO job_detail (id, sig, fetched_at, attempts, snippet, salary_min, salary_max,
            salary_currency, salary_interval, years_min, years_max, degree_min, degree_required,
            sponsorship_offered)
        VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            sig = excluded.sig, fetched_at = excluded.fetched_at, attempts = 0,
            snippet = excluded.snippet, salary_min = excluded.salary_min,
            salary_max = excluded.salary_max, salary_currency = excluded.salary_currency,
            salary_interval = excluded.salary_interval, years_min = excluded.years_min,
            years_max = excluded.years_max, degree_min = excluded.degree_min,
            degree_required = excluded.degree_required,
            sponsorship_offered = excluded.sponsorship_offered
        """,
        (
            ref.id, ref.content_sig, fetched_at, snippet, salary_min, salary_max, salary_currency,
            salary_interval, job.years_experience_min, job.years_experience_max, job.degree_min,
            degree_required, sponsorship_offered,
        ),
    )


# --- build merge ------------------------------------------------------------------------------

# Recovered columns, named identically in `job_detail` and the real index `jobs` table (see
# index/schema.sql) -- no name mapping needed between the sidecar and the index.
_MERGE_COLUMNS: tuple[str, ...] = (
    "salary_min", "salary_max", "salary_currency", "salary_interval",
    "years_min", "years_max", "degree_min", "degree_required", "sponsorship_offered",
)
_INT_COLUMNS = {"degree_required", "sponsorship_offered"}


def merge_detail_into_index(index_con: sqlite3.Connection, detail_path: str) -> int:
    """Build-time merge: apply the Tier-3 detail sidecar's recovered fields onto the matching
    index `jobs` row, but ONLY when the sidecar's `sig` still matches the row's CURRENT sig
    (recomputed from the index row's own content_hash/title/level, not trusted from the sidecar).
    A posting that materially changed since its detail fetch is left untouched -- this is what
    stops a re-crawl from stale-merging a recovered field onto a since-changed posting.

    Within a sig-matched row, only columns that are currently NULL on the index row are filled
    (a value the list-crawl DID provide is never clobbered); `snippet` is filled the same way
    (NULL/empty only). Returns the count of index rows that were actually changed.
    """
    prev_factory = index_con.row_factory
    index_con.row_factory = sqlite3.Row
    try:
        idx_rows = index_con.execute(
            "SELECT id, content_hash, title, level, snippet, " + ", ".join(_MERGE_COLUMNS)
            + " FROM jobs"
        ).fetchall()
    finally:
        index_con.row_factory = prev_factory

    det_con = open_detail(detail_path)
    try:
        det_con.row_factory = sqlite3.Row
        det_rows = det_con.execute(
            "SELECT id, sig, snippet, " + ", ".join(_MERGE_COLUMNS) + " FROM job_detail"
        ).fetchall()
    finally:
        det_con.close()
    detail_by_id: dict[str, sqlite3.Row] = {r["id"]: r for r in det_rows}

    updated = 0
    for row in idx_rows:
        d = detail_by_id.get(row["id"])
        if d is None:
            continue
        current_sig = detail_sig(
            {"content_hash": row["content_hash"], "title": row["title"], "level": row["level"]}
        )
        if d["sig"] != current_sig:
            continue  # posting changed since the detail fetch -- stale sidecar, don't merge

        sets: list[str] = []
        params: list[Any] = []
        for col in _MERGE_COLUMNS:
            if row[col] is not None:
                continue  # list-crawl already provided this -- never clobber
            value = d[col]
            if value is None:
                continue
            if col in _INT_COLUMNS:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            sets.append(f"{col} = ?")
            params.append(value)

        if not row["snippet"] and d["snippet"]:
            sets.append("snippet = ?")
            params.append(d["snippet"])

        if not sets:
            continue

        params.append(row["id"])
        # Per-row SAVEPOINT: a single row whose merged values trip a DB-level CHECK constraint
        # (e.g. filling salary_max below an existing salary_min, or a bad enum value) must not
        # discard every other row's already-computed UPDATE in this same call -- only the one
        # bad row's partial write is rolled back; everything else keeps the single final commit.
        index_con.execute("SAVEPOINT detail_merge_row")
        try:
            index_con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        except sqlite3.IntegrityError:
            index_con.execute("ROLLBACK TO detail_merge_row")
            index_con.execute("RELEASE detail_merge_row")
            continue
        index_con.execute("RELEASE detail_merge_row")
        updated += 1

    index_con.commit()
    return updated


async def reconcile_detail_tier(
    detail_path: str,
    index_path: str,
    *,
    fetch_detail: Callable[[DetailRef], Awaitable[str | None]],
    max_details: int | None = None,
    sources: Sequence[str] | None = None,
    now: Callable[[], str],
) -> dict[str, int]:
    """Tier-3 reconcile pass: select index rows with no description, fetch their JD via the
    injected ``fetch_detail``, run the extractors over it, and write recovered fields + a snippet
    into the detail sidecar. The JD text itself is discarded immediately after extraction.

    Bounded (``max_details``), sig-gated (skips a ref whose posting hasn't changed since it was
    last successfully fetched), retry-budgeted (``RETRY_CAP`` attempts on failure, then left
    alone), and non-fatal (one ref's exception never aborts the others). ``fetch_detail`` and
    ``now`` are both injected for determinism/testability — no network or wall-clock reads here.
    """
    det_con = open_detail(detail_path)
    try:
        idx_con = sqlite3.connect(index_path)
        try:
            tier3_rows = _tier3_rows(idx_con, sources)
        finally:
            idx_con.close()

        existing = _load_existing(det_con)
        candidates: list[DetailRef] = []
        for row in tier3_rows:
            ref = DetailRef.from_row(row)
            if _eligible(ref.id, ref.content_sig, existing):
                candidates.append(ref)

        interleaved = _interleave_by_source(candidates)
        window, next_cursor = _select_window(det_con, interleaved, max_details)
        _save_cursor(det_con, next_cursor)
        det_con.commit()

        results = await _run_fetches(window, fetch_detail, _DEFAULT_CONCURRENCY)

        fetched = 0
        failed = 0
        for ref, desc in results:
            try:
                if desc is None:
                    raise ValueError("fetch_detail returned no description")
                job = JobPosting.create(
                    source=ref.source,
                    source_job_id=ref.id,
                    company="",
                    title="",
                    description_html=desc,
                )
                enrich_in_place(job)
                snippet = (html_to_text(desc) or "")[:300]
                _record_success(det_con, ref, job, snippet, now())
                fetched += 1
            except Exception:
                _record_attempt(det_con, ref.id)
                failed += 1
        det_con.commit()

        # Remaining drainable backlog AFTER this pass: tier-3 rows still eligible (not recovered,
        # not retry-exhausted). Decreases as the sidecar fills; reaches 0 when every posting is
        # recovered-or-dead — the drain loop's stop condition (Task 8's "until missing == 0").
        existing_after = _load_existing(det_con)
        missing = sum(
            1 for row in tier3_rows if _eligible(str(row["id"]), detail_sig(row), existing_after)
        )

        return {"fetched": fetched, "failed": failed, "missing": missing}
    finally:
        det_con.close()
