"""Tier-3 detail sidecar: recovered structured fields + snippet from per-posting JD detail fetches,
keyed by posting id with a sig for re-crawl-safe carry-forward. The JD text itself is never stored."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio

from ..enrich import enrich_in_place
from ..extract.base import html_to_text
from ..http import _rate_key
from ..models import DetailFetch, JobPosting, Location, Salary

DETAIL_SCHEMA_VERSION = 2  # v2: added city/country (structured location recovery)
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
  sponsorship_offered INTEGER,
  city TEXT, country TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
# Columns added after v1; ADDed to any pre-existing (carry-forward) sidecar so the merge can read
# them. Additive only -- old rows get NULLs, which the merge simply skips.
_DETAIL_ADDED_COLUMNS = {"city": "TEXT", "country": "TEXT"}


def ensure_detail_schema(con: sqlite3.Connection) -> None:
    con.executescript(DETAIL_SCHEMA)
    existing = {r[1] for r in con.execute("PRAGMA table_info(job_detail)")}
    for col, coltype in _DETAIL_ADDED_COLUMNS.items():
        if col not in existing:
            con.execute(f"ALTER TABLE job_detail ADD COLUMN {col} {coltype}")
    con.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(DETAIL_SCHEMA_VERSION),),
    )
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


# --- sharded drain -----------------------------------------------------------------------------
#
# The drain is fanned across a GitHub Actions matrix (20 shards, see .github/workflows/
# drain-detail.yml) so ~990k Tier-3 candidates drain in ~1 run instead of ~40 daily-cron runs.
# THE CORRECTNESS INVARIANT: politeness (AsyncFetcher's per-host token-bucket + circuit breaker,
# see http.py) is enforced per ``_rate_key(host)`` — subdomains collapse to the registrable domain
# EXCEPT the per-tenant hosts (Workday). If two shards independently rate-limited the same
# collapsed backend, each shard's own token-bucket would think it owns the full request budget,
# so the backend would see up to N shards' worth of traffic simultaneously — silently N-xing the
# real load against it. So the shard key MUST be ``_rate_key(host)``, never a raw hostname: every
# rate-bucket lives on exactly ONE shard, by construction (a pure function of the bucket string).


def rate_key_for_host(host: str) -> str:
    """Thin public wrapper around ``http._rate_key`` (private) — reused, not duplicated, so the
    shard boundary and AsyncFetcher's actual politeness boundary can never drift apart."""
    return _rate_key(host)


def _host_from_urls(apply_url: str | None, listing_url: str | None) -> str | None:
    url = apply_url or listing_url
    if not url:
        return None
    host = urlsplit(url).netloc
    return host or None


def _host_for_ref(ref: DetailRef) -> str | None:
    """Best-effort FETCH host a provider's ``fetch_detail`` would hit for this ref.

    Every current Tier-3 provider (see ``providers/*.py``) derives its detail URL from
    ``ref.apply_url`` (falling back to ``ref.listing_url``) — e.g. workday/oracle/icims/
    successfactors/radancy/smartrecruiters/workable/join/rippling/eightfold/phenom all follow this
    ``ref.apply_url or ref.listing_url`` pattern. Taking the netloc of that URL generically is
    therefore correct for every registered source without needing per-provider knowledge here. A
    future provider whose ``fetch_detail`` hits a wholly different host would need this updated.
    """
    return _host_from_urls(ref.apply_url, ref.listing_url)


def _rate_bucket_from_fields(source: str, apply_url: str | None, listing_url: str | None) -> str:
    host = _host_from_urls(apply_url, listing_url)
    if host:
        return rate_key_for_host(host)
    return f"source:{source}"  # no derivable URL -- bucket by source, still deterministic


def _rate_bucket_for_ref(ref: DetailRef) -> str:
    """The politeness bucket this ref's fetch will contend on — ``_rate_key`` of its fetch host,
    or (when no host is derivable) a per-source fallback bucket. Either way this is the SAME
    string used both to shard candidates and, transitively, to key AsyncFetcher's own rate
    limiter, so a ref's shard assignment always matches the backend it will actually contend on.
    """
    return _rate_bucket_from_fields(ref.source, ref.apply_url, ref.listing_url)


# Single shared-backend megahosts pinned to a FIXED shard number each, rather than left to the
# hash, so their (very large, single-bucket) request stream is never split across two matrix jobs
# purely by hash luck -- keeping them pinned makes the "one bucket, one shard" invariant obvious
# by inspection instead of by trusting the hash distribution. ``oraclecloud.com`` is included
# defensively: it is currently one shared bucket build-wide, but is expected to become per-tenant
# once the Oracle quickwins land (another workstream) -- pinning it now is a safe default either
# way (a per-tenant key that happens not to be in this dict just falls through to the hash below).
MEGAHOST_SHARDS: dict[str, int] = {
    "smartrecruiters.com": 0,
    "oraclecloud.com": 1,
    "workable.com": 2,  # apply.workable.com / workable.com both collapse here via _rate_key
    "join.com": 3,
    "icims.com": 4,
}


def _shard_of(rate_key: str, num_shards: int) -> int:
    """Deterministic shard assignment for a politeness bucket.

    Pinned megahosts (``MEGAHOST_SHARDS``) go to their fixed shard (mod ``num_shards``, so this
    never indexes out of range even if ``num_shards`` is smaller than the pin table). Everything
    else hashes via ``hashlib.sha1`` -- NOT Python's built-in ``hash()``, which is salted per
    process (``PYTHONHASHSEED``) and would make shard assignment nondeterministic across runs/
    processes, breaking the "every bucket lives on exactly one shard" invariant across the matrix.
    """
    pinned = MEGAHOST_SHARDS.get(rate_key)
    if pinned is not None:
        return pinned % num_shards
    digest = hashlib.sha1(rate_key.encode("utf-8")).hexdigest()
    return int(digest, 16) % num_shards


def _ref_in_shard(ref: DetailRef, shard: int, num_shards: int) -> bool:
    return _shard_of(_rate_bucket_for_ref(ref), num_shards) == shard


# --- reconcile pass ----------------------------------------------------------------------------

RETRY_CAP = 3  # bounded retries for a ref whose fetch keeps failing (never re-fetched forever)
# In-flight Tier-3 fetches. Raised 8 -> 24 (env-tunable via ERGON_DETAIL_CONCURRENCY) to drain the
# highest-volume sources (Workday 37% of the index across ~2,228 independent tenant hosts,
# SmartRecruiters 10.5%, Greenhouse 9%) in weeks not months. Politeness is enforced BELOW this by
# the injected AsyncFetcher's own per-host token-bucket (default 5 req/s per registrable domain,
# Workday keyed per-tenant-host, see http.py::_PER_TENANT_HOSTS) -- raising this global figure only
# lets more distinct hosts be in flight at once, it never raises the request rate against any one
# host. The caller (scripts/build_index.py::_reconcile_detail) must construct its AsyncFetcher with
# a matching-or-higher concurrency so this limiter -- not AsyncFetcher's own global cap -- is the
# one governing throughput.
_DETAIL_CONCURRENCY = int(os.environ.get("ERGON_DETAIL_CONCURRENCY", "24"))  # was a flat 8

_JOBS_COLUMNS = "id, source, board_token, apply_url, listing_url, content_hash"


def _tier3_rows(
    idx_con: sqlite3.Connection,
    sources: Sequence[str] | None,
    shard: int | None = None,
    num_shards: int | None = None,
) -> list[dict[str, Any]]:
    """Index rows lacking a recovered JD (Tier-3 candidates), optionally restricted to ``sources``.

    The real ``jobs`` schema never stores the full description (discard-after-extract), so ``snippet``
    is the real-column signal for "no JD captured yet": list-only sources have an empty snippet until a
    detail fetch + ``merge_detail_into_index`` populates one, which is what makes the drain converge
    (a fetched+merged row gains a snippet and drops out of this candidate set).

    When ``shard``/``num_shards`` are given, the shard predicate is pushed DOWN INTO SQL (via a
    registered ``_shard_of_row`` function that reuses the exact ``_rate_bucket_from_fields`` +
    ``_shard_of`` logic) so each of the 20 matrix jobs materializes ONLY its ~1/20th of candidates
    instead of the full ~1M-row Tier-3 set. Previously every shard did ``fetchall()`` on all ~1M
    rows and filtered in Python -- ~1GB of dicts held for the shard's whole (multi-hour) lifetime,
    the likely trigger of the SR megahost shard's late OOM. The caller still re-checks
    ``_ref_in_shard`` as a cheap belt-and-suspenders on the (now small) result."""
    sql = f"SELECT {_JOBS_COLUMNS} FROM jobs WHERE (snippet IS NULL OR TRIM(snippet) = '')"
    params: list[Any] = []
    if sources:
        placeholders = ",".join("?" for _ in sources)
        sql += f" AND source IN ({placeholders})"
        params.extend(sources)
    if num_shards is not None:
        n = num_shards

        def _shard_of_row(source: Any, apply_url: Any, listing_url: Any) -> int:
            return _shard_of(_rate_bucket_from_fields(source or "", apply_url, listing_url), n)

        idx_con.create_function("_shard_of_row", 3, _shard_of_row, deterministic=True)
        sql += " AND _shard_of_row(source, apply_url, listing_url) = ?"
        params.append(shard)
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


def _detail_parts(
    result: str | DetailFetch | None,
) -> tuple[str | None, Salary | None, list[Location] | None]:
    """Normalize a ``fetch_detail`` return to ``(text, structured_salary, structured_locations)``.
    A bare ``str`` (the historical contract) carries neither; a ``DetailFetch`` may carry both."""
    if isinstance(result, DetailFetch):
        return (result.text or None), result.salary, result.locations
    return result, None, None


async def _run_fetches(
    window: list[DetailRef],
    fetch_detail: Callable[[DetailRef], Awaitable[str | DetailFetch | None]],
    concurrency: int,
) -> list[tuple[DetailRef, str | DetailFetch | None]]:
    """Fetch the whole window concurrently, bounded by ``concurrency``. A failing fetch (exception
    or ``None``) is captured per-ref, never raised — one bad host must not abort the others."""
    limiter = anyio.CapacityLimiter(concurrency)
    results: list[tuple[DetailRef, str | DetailFetch | None]] = []

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


def _prune_sidecar_to_shard(
    det_con: sqlite3.Connection, refs: Sequence[DetailRef], shard: int, num_shards: int
) -> None:
    """Restrict the sidecar's OUTPUT to rows whose id belongs to ``shard`` (of ``num_shards``),
    among the given ``refs`` (the current Tier-3 candidate set, i.e. every row still lacking a
    recovered JD in the index — a row that's already been merged elsewhere has dropped out of
    Tier-3 entirely and doesn't need to be carried in this sidecar at all).

    THE BUG THIS CLOSES: the drain matrix seeds each shard's sidecar from the FULL prior combined
    ``index-detail.sqlite`` (see ``.github/workflows/drain-detail.yml``) so a shard can skip rows
    it already recovered — but that means the sidecar walks in carrying every OTHER shard's rows
    too. Without this prune, each shard's OUTPUT artifact would contain the whole backlog (mostly
    untouched carry-forward): ``merge_detail_shards.py``'s union across (say) 20 shards would then
    be ~20x the real unique row count, AND a later shard's STALE carried-forward copy of a row
    could ``INSERT OR REPLACE`` over an earlier shard's FRESHLY-fetched copy of that SAME row on
    merge — silently discarding real work.

    Keeps every row belonging to THIS shard (whether freshly (re-)fetched this pass or an
    untouched, still-valid carry-forward) and drops every row belonging to a DIFFERENT shard.
    Uses a temp table rather than a giant parameterized ``IN (...)`` so this scales past SQLite's
    bound-variable ceiling for large backlogs.
    """
    det_con.execute("CREATE TEMP TABLE _shard_ids (id TEXT PRIMARY KEY)")
    try:
        det_con.executemany(
            "INSERT OR IGNORE INTO _shard_ids(id) VALUES (?)",
            ((ref.id,) for ref in refs if _ref_in_shard(ref, shard, num_shards)),
        )
        det_con.execute("DELETE FROM job_detail WHERE id NOT IN (SELECT id FROM _shard_ids)")
    finally:
        det_con.execute("DROP TABLE _shard_ids")
    det_con.commit()


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
    # Recovered structured location: the first geo-normalized location that carries a city/country
    # (enrich_in_place already ran normalize_geo). Fills the index row's NULL city/country on merge.
    city = country = None
    for loc in job.locations:
        if city is None and loc.city:
            city = loc.city
        if country is None and loc.country:
            country = loc.country
        if city and country:
            break
    det_con.execute(
        """
        INSERT INTO job_detail (id, sig, fetched_at, attempts, snippet, salary_min, salary_max,
            salary_currency, salary_interval, years_min, years_max, degree_min, degree_required,
            sponsorship_offered, city, country)
        VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            sig = excluded.sig, fetched_at = excluded.fetched_at, attempts = 0,
            snippet = excluded.snippet, salary_min = excluded.salary_min,
            salary_max = excluded.salary_max, salary_currency = excluded.salary_currency,
            salary_interval = excluded.salary_interval, years_min = excluded.years_min,
            years_max = excluded.years_max, degree_min = excluded.degree_min,
            degree_required = excluded.degree_required,
            sponsorship_offered = excluded.sponsorship_offered,
            city = excluded.city, country = excluded.country
        """,
        (
            ref.id,
            ref.content_sig,
            fetched_at,
            snippet,
            salary_min,
            salary_max,
            salary_currency,
            salary_interval,
            job.years_experience_min,
            job.years_experience_max,
            job.degree_min,
            degree_required,
            sponsorship_offered,
            city,
            country,
        ),
    )


# --- build merge ------------------------------------------------------------------------------

# Recovered columns, named identically in `job_detail` and the real index `jobs` table (see
# index/schema.sql) -- no name mapping needed between the sidecar and the index.
_MERGE_COLUMNS: tuple[str, ...] = (
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_interval",
    "years_min",
    "years_max",
    "degree_min",
    "degree_required",
    "sponsorship_offered",
    "city",
    "country",
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

    Scoped to O(sidecar rows), NOT O(all jobs): the sidecar is bounded (<= ERGON_DETAIL_MAX) but
    the index is not (~1.48M+ rows), so this ATTACHes the sidecar db and INNER JOINs it onto
    `jobs` -- only rows present in `job_detail` are ever read into memory, never a whole-table
    `SELECT ... FROM jobs`.
    """
    open_detail(detail_path).close()  # idempotent: ensure the sidecar schema exists before ATTACH

    jobs_cols = ", ".join(f"j.{c} AS jobs_{c}" for c in _MERGE_COLUMNS)
    det_cols = ", ".join(f"d.{c} AS det_{c}" for c in _MERGE_COLUMNS)
    prev_factory = index_con.row_factory
    index_con.row_factory = sqlite3.Row
    index_con.execute("ATTACH DATABASE ? AS det", (detail_path,))
    try:
        # Exhaust the read cursor (fetchall, bounded by sidecar size) BEFORE any writes below --
        # never mutate `jobs` while a read cursor from this JOIN is still open on it.
        rows = index_con.execute(
            "SELECT j.id AS id, j.content_hash AS content_hash, j.title AS title, "
            "j.level AS level, j.snippet AS jobs_snippet, " + jobs_cols + ", "
            "d.sig AS sig, d.snippet AS det_snippet, " + det_cols + " "
            "FROM jobs j JOIN det.job_detail d ON j.id = d.id"
        ).fetchall()
    finally:
        index_con.execute("DETACH DATABASE det")
        index_con.row_factory = prev_factory

    updated = 0
    for row in rows:
        current_sig = detail_sig(
            {"content_hash": row["content_hash"], "title": row["title"], "level": row["level"]}
        )
        if row["sig"] != current_sig:
            continue  # posting changed since the detail fetch -- stale sidecar, don't merge

        sets: list[str] = []
        params: list[Any] = []
        for col in _MERGE_COLUMNS:
            if row[f"jobs_{col}"] is not None:
                continue  # list-crawl already provided this -- never clobber
            value = row[f"det_{col}"]
            if value is None:
                continue
            if col in _INT_COLUMNS:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            sets.append(f"{col} = ?")
            params.append(value)

        if not row["jobs_snippet"] and row["det_snippet"]:
            sets.append("snippet = ?")
            params.append(row["det_snippet"])

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
    fetch_detail: Callable[[DetailRef], Awaitable[str | DetailFetch | None]],
    max_details: int | None = None,
    sources: Sequence[str] | None = None,
    now: Callable[[], str],
    shard: int | None = None,
    num_shards: int | None = None,
) -> dict[str, int]:
    """Tier-3 reconcile pass: select index rows lacking a recovered JD (empty snippet), fetch their JD
    via the
    injected ``fetch_detail``, run the extractors over it, and write recovered fields + a snippet
    into the detail sidecar. The JD text itself is discarded immediately after extraction.

    Bounded (``max_details``), sig-gated (skips a ref whose posting hasn't changed since it was
    last successfully fetched), retry-budgeted (``RETRY_CAP`` attempts on failure, then left
    alone), and non-fatal (one ref's exception never aborts the others). ``fetch_detail`` and
    ``now`` are both injected for determinism/testability — no network or wall-clock reads here.

    ``shard``/``num_shards`` (both-or-neither) restrict this pass to the candidates whose
    politeness bucket (``_rate_bucket_for_ref``, keyed via ``_rate_key`` -- see the "sharded
    drain" section above) hashes to ``shard`` of ``num_shards`` -- the fan-out primitive behind
    the drain matrix (``.github/workflows/drain-detail.yml``). Leaving both ``None`` (the default)
    is the ORIGINAL, byte-identical non-sharded path: no filtering happens at all.

    When sharded, the OUTPUT sidecar (``detail_path``) is pruned at the end of the pass to contain
    ONLY rows belonging to ``shard`` (see ``_prune_sidecar_to_shard``) -- even though the caller
    may have SEEDED ``detail_path`` from a full prior combined sidecar (the drain matrix's
    carry-forward, so this shard can skip rows it already recovered). This keeps every shard's
    output disjoint by id from every other shard's, so ``merge_detail_shards.py``'s union is a
    clean, ~1x-sized combine with no possibility of a stale carry-forward clobbering a fresher
    fetch from another shard.
    """
    if (shard is None) != (num_shards is None):
        raise ValueError("shard and num_shards must be given together (or neither)")
    if num_shards is not None and (shard is None or not (0 <= shard < num_shards)):
        raise ValueError(
            f"shard must be in [0, num_shards); got shard={shard}, num_shards={num_shards}"
        )
    # Plain-int locals (never Optional) once validated above, so the filtering below reads clean
    # regardless of whether sharding is active -- `sharded=False` makes them dead/unused.
    sharded = num_shards is not None
    shard_i: int = shard if shard is not None else 0
    num_shards_i: int = num_shards if num_shards is not None else 0

    det_con = open_detail(detail_path)
    try:
        idx_con = sqlite3.connect(index_path)
        try:
            # Push the shard predicate into SQL so a matrix job loads only ITS ~1/20th of the ~1M
            # Tier-3 candidates (not all of them) -- the memory fix. `None,None` keeps the original
            # unsharded path byte-identical.
            tier3_rows = _tier3_rows(
                idx_con,
                sources,
                shard=shard_i if sharded else None,
                num_shards=num_shards_i if sharded else None,
            )
        finally:
            idx_con.close()

        existing = _load_existing(det_con)
        all_refs = [DetailRef.from_row(row) for row in tier3_rows]
        candidates = [ref for ref in all_refs if _eligible(ref.id, ref.content_sig, existing)]

        if sharded:
            # Belt-and-suspenders: the SQL filter above already restricted to this shard; re-checking
            # in Python guards against any SQL/Python drift and is cheap now (operates on ~50k, not 1M).
            candidates = [c for c in candidates if _ref_in_shard(c, shard_i, num_shards_i)]

        interleaved = _interleave_by_source(candidates)
        window, next_cursor = _select_window(det_con, interleaved, max_details)
        _save_cursor(det_con, next_cursor)
        det_con.commit()

        results = await _run_fetches(window, fetch_detail, _DETAIL_CONCURRENCY)

        fetched = 0
        failed = 0
        for ref, result in results:
            try:
                # fetch_detail may return a bare str (JD text) or a DetailFetch carrying a
                # STRUCTURED salary alongside the text (e.g. rippling's payRangeDetails). Seed the
                # posting with that salary BEFORE enrich so the text extractors — which only fill a
                # still-empty field — prefer the structured range over re-parsing it from prose.
                text, pre_salary, pre_locations = _detail_parts(result)
                if not text:
                    raise ValueError("fetch_detail returned no description")
                job = JobPosting.create(
                    source=ref.source,
                    source_job_id=ref.id,
                    company="",
                    title="",
                    description_html=text,
                    salary=pre_salary,
                    locations=pre_locations or [],
                )
                enrich_in_place(job)  # geo-normalizes the seeded locations -> resolves country
                snippet = (html_to_text(text) or "")[:300]
                _record_success(det_con, ref, job, snippet, now())
                fetched += 1
            except Exception:
                _record_attempt(det_con, ref.id)
                failed += 1
        det_con.commit()

        if sharded:
            # Scope the OUTPUT sidecar down to just this shard's own rows (see
            # `_prune_sidecar_to_shard`'s docstring) -- must happen AFTER the fetch/record loop
            # above (so this shard's own fresh writes are in the db to be kept) and BEFORE the
            # `missing` count below (which re-reads the sidecar's post-prune state).
            _prune_sidecar_to_shard(det_con, all_refs, shard_i, num_shards_i)

        # Remaining drainable backlog AFTER this pass: tier-3 rows still eligible (not recovered,
        # not retry-exhausted). Decreases as the sidecar fills; reaches 0 when every posting is
        # recovered-or-dead — the drain loop's stop condition (Task 8's "until missing == 0").
        # When sharded, this is scoped to just THIS shard's own candidates (mirroring the
        # filtering above) so a shard's ``missing`` reflects its own backlog, not the whole index's.
        existing_after = _load_existing(det_con)
        missing = sum(
            1
            for ref in all_refs
            if (not sharded or _ref_in_shard(ref, shard_i, num_shards_i))
            and _eligible(ref.id, ref.content_sig, existing_after)
        )

        return {"fetched": fetched, "failed": failed, "missing": missing}
    finally:
        det_con.close()
