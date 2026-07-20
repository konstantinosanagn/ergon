"""M1 build entry: crawl a bounded slice of the registry -> build index -> publish artifacts.

Usage:
  .venv/bin/python scripts/build_index.py --limit-companies 300 --out dist/
  # also fold in the first-party Workable network feed (N pages, ~20 jobs/page):
  .venv/bin/python scripts/build_index.py --limit-companies 300 --network-pages 200 --out dist/
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.db import SCHEMA_VERSION  # noqa: E402

# Compression level 6 (not gzip's default 9): ~2x faster for ~5% larger output — the right trade for a
# ~1GB artifact rebuilt daily. Output stays standard gzip (.gz), so the SDK's gunzip is unchanged.
_GZIP_LEVEL = int(os.environ.get("ERGON_GZIP_LEVEL", "6"))


# Per-run accumulator of phase durations, populated by `_phase`. Persisted into history.jsonl at the
# end of the build so ETAs are queryable from history (data-driven) instead of read off each live run.
_PHASE_TIMINGS: dict[str, float] = {}


@contextmanager
def _phase(label: str):
    """Time a build phase and make it OBSERVABLE: log ``[phase] start/done in Ns`` to stdout (streams
    into the workflow step log live), append a ``- label — Ns`` line to the GitHub run-page step
    summary (``$GITHUB_STEP_SUMMARY``) when running in Actions, AND record the duration in
    ``_PHASE_TIMINGS`` for the history.jsonl timing record. Turns the build from a silent black box
    into a timed, RECORDED timeline -- so the crawl/build/embed split and the next ETA are grounded in
    measured history, not guessed. Never raises from the observability itself (writes are swallowed)."""
    print(f"[phase] {label} ...", flush=True)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = time.perf_counter() - t0
        _PHASE_TIMINGS[label] = round(dt, 1)
        print(f"[phase] {label}: done in {dt:.0f}s", flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            try:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(f"- **{label}** — {dt:.0f}s\n")
            except OSError:
                pass


def _gzip_file(src: Path, dst: Path) -> tuple[str, int]:
    """Compress ``src`` -> ``dst`` (gzip format); return (sha256 of RAW bytes, raw byte count).

    Uses ``pigz`` (parallel gzip — saturates all cores, identical .gz format) when available, else
    streams through Python's single-threaded gzip. Either way we stream src in 1MB chunks (no ~1GB RAM
    spike) and hash the RAW bytes. ``pigz -n`` / ``mtime=0`` keep the .gz byte-stable for unchanged input.
    """
    pigz = shutil.which("pigz")
    h = hashlib.sha256()
    total = 0
    if pigz:
        # Stream src through pigz's stdin while hashing raw bytes; pigz fans compression across cores.
        with open(dst, "wb") as raw_out:
            proc = subprocess.Popen(
                [pigz, "-n", f"-{_GZIP_LEVEL}"], stdin=subprocess.PIPE, stdout=raw_out
            )
            assert proc.stdin is not None
            with open(src, "rb") as f_in:
                while chunk := f_in.read(1 << 20):
                    h.update(chunk)
                    total += len(chunk)
                    proc.stdin.write(chunk)
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"pigz failed compressing {src} (exit {proc.returncode})")
        return h.hexdigest(), total
    with (
        open(src, "rb") as f_in,
        open(dst, "wb") as raw_out,
        gzip.GzipFile(fileobj=raw_out, mode="wb", mtime=0, compresslevel=_GZIP_LEVEL) as f_out,
    ):
        while chunk := f_in.read(1 << 20):
            h.update(chunk)
            total += len(chunk)
            f_out.write(chunk)
    return h.hexdigest(), total


def publish_artifacts(db_path: Path, out_dir: Path, *, build_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    sha, nbytes = _gzip_file(db_path, out_dir / "index.sqlite.gz")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {"build_id": build_id, "schema_version": SCHEMA_VERSION, "sha256": sha, "bytes": nbytes}
        )
    )


async def _crawl_network(cap_pages: int) -> list:
    """Bulk-fetch the ``workable_network`` aggregator feed and return normalized + enriched jobs
    (NOT yet deduped — the caller folds these into its own list and dedups once).

    This is the one ATS that exposes its whole active customer base first-party
    (``jobs.workable.com/api/v1/jobs``, ~172k jobs), so a build can reach Workable companies that
    were never in the per-board registry. ``cap_pages`` bounds the paged pull (0 disables it).
    """
    if cap_pages <= 0:
        return []
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.base import get_provider, load_builtins

    load_builtins()
    provider = get_provider("workable_network")
    if provider is None:
        return []
    provider.MAX_PAGES = cap_pages  # raise the live cap for a bulk build pull
    jobs: list = []
    async with AsyncFetcher() as fetcher:
        try:
            raws = await provider.fetch("", SearchQuery(), fetcher)
        except Exception:  # noqa: BLE001 - network feed down: build proceeds without it
            return []
    for raw in raws:
        try:
            job = provider.normalize(raw)
        except Exception:  # noqa: BLE001
            continue
        enrich_in_place(job, infer_level_from_experience=True)
        jobs.append(job)
    return jobs


async def _fold_network_into_fresh(fresh_path, network_pages: int, build_id: str) -> set[str]:
    """Append the workable_network bulk feed into a streaming crawl's ``fresh.sqlite`` and return
    the set of normalized company keys it added.

    Used by the incremental build: the fresh rows flow into the final index via
    ``build_index_from_fresh_db`` (INSERT ... FROM fr.jobs), and returning the company keys lets
    the caller add them to ``crawled_keys`` so ``carry_forward`` treats those companies as
    refreshed — otherwise a network company that also had a prior per-board row would be carried
    forward as a stale duplicate.
    """
    from ergon_tracker.dedup import deduplicate, normalize_company
    from ergon_tracker.index.build import append_jobs
    from ergon_tracker.index.db import connect

    net = deduplicate(await _crawl_network(network_pages))
    if not net:
        return set()
    con = connect(fresh_path)
    try:
        con.execute("PRAGMA foreign_keys = OFF")  # companies are aggregated later, at finalize
        append_jobs(con, net, build_id=build_id)
        con.commit()
    finally:
        con.close()
    return {normalize_company(j.company) for j in net}


def _apply_freshness(db_path: Path, out: Path) -> int:
    """Carry forward a prior daily freshness-sweep's expiries onto the just-built (not-yet-
    published) index db -- Phase 2 of the freshness sweep (docs/superpowers/specs/2026-07-18-daily-
    freshness-sweep-design.md). Called BEFORE the gated publish (and, transitively, before
    shards/slim/delta are derived) so every downstream artifact inherits the expiries, mirroring how
    the detail/liveness sidecars are consumed.

    The sweep itself is a SEPARATE daily workflow (a later phase) that publishes
    ``index-freshness.sqlite.gz`` to the ``index-latest`` release; the build workflow downloads +
    gunzips it to ``dist/index-freshness.sqlite`` before the build (same pattern as the detail/
    liveness sidecar downloads). NON-FATAL: an absent/malformed sidecar (first run, or every run
    before the sweep workflow ships) must never break the core build.
    """
    from ergon_tracker.index.build import apply_freshness_expiries
    from ergon_tracker.index.db import connect

    freshness_db = out / "index-freshness.sqlite"
    try:
        con = connect(db_path)
        try:
            return apply_freshness_expiries(con, freshness_db)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - never let a sidecar hiccup break the core build
        print(f"  ! freshness carry-forward skipped (non-fatal): {type(exc).__name__}: {exc}")
        return 0


def _backfill_board_tokens(db_path: Path) -> int:
    """Populate jobs.board_token for carried-forward active rows (NULL until re-crawled) from the
    seed registry + verified apply-URL derivation, so the freshness sweep covers ~all boards
    immediately instead of ramping over the ~5-day crawl cycle. NON-FATAL: a hiccup here must never
    break the core build; the crawl still fills board_token the normal way."""
    from ergon_tracker.index.build import backfill_board_tokens
    from ergon_tracker.index.db import connect

    try:
        con = connect(db_path)
        try:
            n = backfill_board_tokens(con)
            con.commit()
            return n
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - never let the backfill break the core build
        print(f"  ! board_token backfill skipped (non-fatal): {type(exc).__name__}: {exc}")
        return 0


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def _build_id() -> str:
    """Unique id per build: ``build-<date>-<suffix>``.

    Date-only ids repeat across same-day builds, which makes row-level delta chains ambiguous
    (v2.2). The suffix is the CI run number when available (monotonic, unique per workflow run),
    else a UTC HHMMSS stamp — so every build is distinctly addressable for from->to delta links.
    """
    import os
    from datetime import datetime, timezone

    suffix = os.environ.get("GITHUB_RUN_NUMBER") or datetime.now(timezone.utc).strftime("%H%M%S")
    return f"build-{_today()}-{suffix}"


def build_and_publish_shards(jobs: list, out: Path, *, build_id: str) -> int:
    """Build per-sector shards from jobs and gzip each for release upload. Returns shard count."""
    from ergon_tracker.index.build import build_sharded_index

    manifest = build_sharded_index(jobs, out, build_id=build_id)
    for info in manifest["shards"].values():
        _gzip_file(out / info["file"], out / (info["file"] + ".gz"))
    return len(manifest["shards"])


def build_and_publish_shards_from_db(db_path: Path, out: Path, *, build_id: str) -> int:
    """Memory-bounded shard publish: partition the built index by sector via SQL, gzip each."""
    from ergon_tracker.index.build import build_sharded_index_from_db

    manifest = build_sharded_index_from_db(db_path, out, build_id=build_id)
    for info in manifest["shards"].values():
        _gzip_file(out / info["file"], out / (info["file"] + ".gz"))
    return len(manifest["shards"])


def build_and_publish_delta(prev_db: Path, curr_db: Path, out: Path, *, build_id: str) -> dict:
    """Diff the prior published index against the new one and publish a compact row-level delta.

    A returning user one build behind downloads ``index-delta.sqlite.gz`` (only changed/deleted
    rows — typically a few % of the file) and applies it locally, instead of the whole index.
    Returns the delta info (or {} when there's no usable prior build).
    """
    from ergon_tracker.index.build import build_delta
    from ergon_tracker.index.db import connect

    try:
        con = connect(prev_db, read_only=True)
        row = con.execute("SELECT value FROM meta WHERE key='build_id'").fetchone()
        con.close()
        from_build_id = row[0] if row else None
    except Exception as exc:  # noqa: BLE001 - corrupt/missing prior -> skip delta, full still works
        print(f"  (skip delta: cannot read prev build_id: {exc})")
        return {}
    if not from_build_id or from_build_id == build_id:
        return {}
    delta = out / "index-delta.sqlite"
    info = build_delta(prev_db, curr_db, delta, from_build_id=from_build_id, to_build_id=build_id)
    sha, nbytes = _gzip_file(
        delta, out / "index-delta.sqlite.gz"
    )  # 1-behind fast path (stable name)
    # Per-build copy (unique name) so a user N>1 builds behind can chain consecutive deltas (v2.2).
    chain_file = f"index-delta-{build_id}.sqlite.gz"
    import shutil

    shutil.copyfile(out / "index-delta.sqlite.gz", out / chain_file)
    delta.unlink(missing_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "from_build_id": from_build_id,
        "to_build_id": build_id,
        "sha256": sha,
        "bytes": nbytes,
        **info,
    }
    (out / "manifest-delta.json").write_text(json.dumps(manifest))
    _update_deltas_window(
        out,
        {
            "from_build_id": from_build_id,
            "to_build_id": build_id,
            "file": chain_file,
            "sha256": sha,
            "bytes": nbytes,
        },
    )
    return manifest


_DELTA_WINDOW = 10  # how many recent per-build deltas to keep for chaining


def _update_deltas_window(out: Path, entry: dict) -> list[str]:
    """Append a delta to the rolling deltas.json window (last _DELTA_WINDOW), drop stale ones.

    Returns the filenames pruned out of the window so the publish step can delete those release
    assets. The window must form a contiguous from->to chain ending at the newest build.
    """
    path = out / "deltas.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    deltas = [d for d in data.get("deltas", []) if d.get("to_build_id") != entry["to_build_id"]]
    deltas.append(entry)
    deltas = deltas[-_DELTA_WINDOW:]
    kept = {d["file"] for d in deltas}
    pruned = [d["file"] for d in data.get("deltas", []) if d["file"] not in kept]
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "deltas": deltas}))
    return pruned


def _write_vectors_manifest(out: Path, *, build_id: str, sha: str, nbytes: int) -> None:
    """Write ``manifest-vectors.json`` alongside the gz — the exact fields ``RichCache.ensure_fresh``
    reads (schema_version gate, build_id freshness key, sha256 of the RAW bytes). Mirrors the slim
    manifest producer; a field-name drift here silently disables the sidecar for every user."""
    from ergon_tracker.index.rich import RICH_SCHEMA_VERSION

    (out / "manifest-vectors.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema_version": RICH_SCHEMA_VERSION,
                "sha256": sha,
                "bytes": nbytes,
            },
            indent=2,
        )
    )


def build_and_publish_rich_incremental(
    db_path: Path, fresh_db_path: Path, out: Path, *, build_id: str
) -> tuple[dict, int]:
    """Incremental (cron) vectors publish: reconcile the persisted sidecar against the freshly-built
    main index using the crawl window's ``fresh_rich`` rows (full descriptions captured on disk), then
    gzip-publish ``index-vectors.sqlite.gz`` + ``manifest-vectors.json``. Carried-forward ids keep the
    vectors already in the sidecar, so only new/changed postings re-embed. Needs the ``semantic`` extra."""
    from ergon_tracker.index.rich import reconcile_rich_tier_from_fresh

    rich_db = out / "index-vectors.sqlite"
    stats = reconcile_rich_tier_from_fresh(rich_db, db_path, fresh_db_path, build_id=build_id)
    sha, nbytes = _gzip_file(rich_db, out / "index-vectors.sqlite.gz")
    _write_vectors_manifest(out, build_id=build_id, sha=sha, nbytes=nbytes)
    return stats, nbytes


_TIER3_DETAIL_SOURCES = [  # sources with a fetch_detail impl
    "smartrecruiters",
    "workday",
    "oracle",
    "icims",
    "successfactors",
    "eightfold",
    "rippling",
    "radancy",
    "workable",
    "join",
    "phenom",  # phenom re-routes to workday/successfactors by apply_url host
    "bamboohr",  # /careers/{id}/detail -> description + structured `compensation` string
    "ukg",  # OpportunityDetail page -> full JD body (structured pay is gated, but ~40% state it in prose)
    "jobvite",  # per-job JSON-LD JobPosting.description -> full JD body (no salary, but yoe/degree/skills)
]


def _detail_max() -> int:
    """Per-run bound on Tier-3 detail fetches, ``ERGON_DETAIL_MAX`` (env), default 5000."""
    return int(os.environ.get("ERGON_DETAIL_MAX", "5000"))


def _location_backfill() -> bool:
    """Opt-in location-backfill drain, ``ERGON_DETAIL_LOCATION_BACKFILL`` (env), default off.

    When ``"1"``, the sharded reconcile also re-fetches already-drained rows on the location-capable
    sources that still lack a city/country (see ``reconcile_detail_tier``). Never set by the daily
    crawl -- only the drain workflow's dedicated dispatch input turns it on -- so ordinary builds are
    byte-for-byte unaffected."""
    return os.environ.get("ERGON_DETAIL_LOCATION_BACKFILL", "") == "1"


def _sharded_embed() -> bool:
    """When ``ERGON_SHARDED_EMBED == "1"``, SKIP the inline (single-runner) rich embed. The crawl still
    captures ``fresh_rich`` (``--rich`` stays on, so the full-description embed-text DB is written +
    published) -- but the embedding itself is owned by the sharded ``embed-vectors.yml`` matrix
    (~10x faster across 20 runners). Default off keeps the inline embed for local/manual builds and
    any environment that doesn't run the matrix. This is the daily-build cutover lever."""
    return os.environ.get("ERGON_SHARDED_EMBED", "") == "1"


def _liveness_max_boards() -> int | None:
    """Per-run bound on the number of DUE boards the liveness pass re-fetches, ``ERGON_LIVENESS_
    MAX_BOARDS`` (env), default 2000. A board re-fetch here costs the same as one normal crawl
    fetch (``provider.fetch(token, ...)``) -- left unbounded, a single run would re-fetch every
    active board whose recheck window elapsed, doubling that run's crawl cost. The bounded,
    rotating-cursor window (``liveness.py::_select_board_window``) still reaches every due board
    within a few runs, same shape as the Tier-3 detail drain."""
    v = os.environ.get("ERGON_LIVENESS_MAX_BOARDS")
    return int(v) if v else 2000


def _liveness_recheck_days() -> int:
    """``ERGON_LIVENESS_RECHECK_DAYS`` (env), default 7 -- see ``liveness.RECHECK_DAYS``."""
    from ergon_tracker.index.liveness import RECHECK_DAYS

    return RECHECK_DAYS


def _write_liveness_manifest(out: Path, *, build_id: str, sha: str, nbytes: int) -> None:
    """Write ``manifest-liveness.json`` alongside the gz — mirrors ``_write_detail_manifest``: the
    sidecar (``checked_at``/``dead_streak``/``verdict`` per posting id) must persist build-to-build
    for the recheck-cadence + dead-streak logic to mean anything, so it's published as a release
    asset the same way ``index-detail.sqlite.gz`` is, for the next build to download and carry
    forward as its starting sidecar."""
    from ergon_tracker.index.liveness import LIVENESS_SCHEMA_VERSION

    (out / "manifest-liveness.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema_version": LIVENESS_SCHEMA_VERSION,
                "sha256": sha,
                "bytes": nbytes,
            },
            indent=2,
        )
    )


async def _reconcile_liveness(liveness_db: Path, index_db: Path) -> dict:
    """Dispatch glue for the liveness pass: a real ``AsyncFetcher`` + ``get_provider`` lookup for
    both the Stage-1 board re-fetch and the Stage-2 detail confirm, injected into
    ``reconcile_liveness_tier`` (which itself never touches the network stack -- see
    ``index/liveness.py``'s module docstring). Mirrors ``_reconcile_detail``'s shape exactly.
    """
    from datetime import datetime, timezone

    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.index.liveness import _LIVENESS_CONCURRENCY, reconcile_liveness_tier
    from ergon_tracker.models import SearchQuery, make_job_id
    from ergon_tracker.providers.base import get_provider, load_builtins

    load_builtins()

    # Match AsyncFetcher's own global cap to reconcile_liveness_tier's own concurrency bound
    # (ERGON_LIVENESS_CONCURRENCY) -- same reasoning as _reconcile_detail: AsyncFetcher's default
    # (16) would otherwise become the binding limiter instead of the liveness-tuned figure.
    async with AsyncFetcher(concurrency=_LIVENESS_CONCURRENCY) as fetcher:

        async def _fetch_board(source: str, token: str) -> set[str] | None:
            prov = get_provider(source)
            if prov is None:
                return None
            try:
                raws = await prov.fetch(token, SearchQuery(), fetcher)
            except Exception:  # noqa: BLE001 - a dead/blocked/erroring board -> skip it this run
                return None
            ids: set[str] = set()
            for raw in raws:
                try:
                    ids.add(make_job_id(raw.source, str(raw.source_job_id)))
                except Exception:  # noqa: BLE001 - one malformed raw must not drop the whole board
                    continue
            return ids

        async def _fetch_detail(ref):
            prov = get_provider(ref.source)
            if prov is None:
                return None
            return await prov.fetch_detail(ref, fetcher)

        return await reconcile_liveness_tier(
            str(liveness_db),
            str(index_db),
            fetch_board=_fetch_board,
            fetch_detail=_fetch_detail,
            now=lambda: datetime.now(timezone.utc).isoformat(),
            recheck_days=_liveness_recheck_days(),
            max_boards=_liveness_max_boards(),
        )


def build_and_publish_liveness(db_path: Path, out: Path, *, build_id: str) -> dict:
    """Liveness reconcile against the ALREADY-PUBLISHED core index, then re-publish
    ``index.sqlite.gz``/``manifest.json`` so any status flips actually reach a downloading user
    (mirrors ``build_and_publish_detail``'s ORDERING exactly -- see its docstring for the full
    rationale). Also publishes the liveness sidecar itself (``index-liveness.sqlite.gz`` +
    ``manifest-liveness.json``) so the NEXT build can download + carry it forward, preserving the
    recheck cadence across days instead of re-checking every active row on every single build.

    Unlike the detail tier, there is no separate merge step: ``reconcile_liveness_tier`` writes
    the ``status='expired'`` flip directly onto ``jobs`` as part of the reconcile pass itself.
    """
    import anyio

    liveness_db = out / "index-liveness.sqlite"
    stats = anyio.run(lambda: _reconcile_liveness(liveness_db, db_path))
    sha, nbytes = _gzip_file(liveness_db, out / "index-liveness.sqlite.gz")
    _write_liveness_manifest(out, build_id=build_id, sha=sha, nbytes=nbytes)
    # Re-publish the core index so any status='expired' flips just written land in the gzip/
    # manifest a downloading user actually fetches (see ORDERING note above).
    publish_artifacts(db_path, out, build_id=build_id)
    return stats


def _write_detail_manifest(out: Path, *, build_id: str, sha: str, nbytes: int) -> None:
    """Write ``manifest-detail.json`` alongside the gz — the exact fields ``DetailCache.ensure_fresh``
    reads (schema_version gate, build_id freshness key, sha256 of the RAW bytes). Mirrors the vectors
    manifest producer; a field-name drift here silently disables the sidecar for every user."""
    from ergon_tracker.index.detail import DETAIL_SCHEMA_VERSION

    (out / "manifest-detail.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema_version": DETAIL_SCHEMA_VERSION,
                "sha256": sha,
                "bytes": nbytes,
            },
            indent=2,
        )
    )


def _rebuild_jobs_fts(db_path: Path) -> None:
    """Rebuild the external-content ``jobs_fts`` index over the merged ``jobs`` table.

    ``merge_detail_into_index`` writes recovered fields (including ``snippet``) straight into
    ``jobs`` via plain ``UPDATE`` statements. An external-content FTS5 table is NOT kept in sync
    by those updates (only triggers on the *content* table would do that, and this schema has
    none) -- so without an explicit rebuild, a recovered snippet is visible in ``jobs.snippet``
    but invisible to ``jobs_fts MATCH`` keyword queries. Reuses the exact
    ``INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')`` idiom the core build uses
    (``index/build.py``: ``build_index``/``finalize_index``/``build_slim_index``).
    """
    from ergon_tracker.index.db import connect

    con = connect(db_path)
    try:
        con.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")
        con.commit()
    finally:
        con.close()


def _detail_shard() -> tuple[int | None, int | None]:
    """(shard, num_shards) from env (``ERGON_DETAIL_SHARD``/``ERGON_DETAIL_NUM_SHARDS``), used by
    the drain matrix (``.github/workflows/drain-detail.yml``) when ``--shard``/``--num-shards``
    aren't passed explicitly on the command line. ``(None, None)`` (the default) is the original
    non-sharded path."""
    shard = os.environ.get("ERGON_DETAIL_SHARD")
    num_shards = os.environ.get("ERGON_DETAIL_NUM_SHARDS")
    if shard is None or num_shards is None:
        return None, None
    return int(shard), int(num_shards)


async def _reconcile_detail(
    detail_db: Path,
    index_db: Path,
    *,
    shard: int | None = None,
    num_shards: int | None = None,
    merge: bool = True,
) -> dict:
    """Fetch JD detail for Tier-3 (no-description) postings via each source's registered provider
    (``fetch_detail``), then (unless ``merge=False``) merge recovered fields into the index db
    (already-promoted at this point) in place. Returns the reconcile stats plus, when merged, a
    ``merged`` row count.

    ``fetch_detail`` dispatch: look up the provider by ``ref.source`` in the registry and call its
    (possibly-unimplemented — defaults to returning ``None``) ``fetch_detail(ref, fetcher)``. One
    real ``AsyncFetcher`` is shared across the whole reconcile window (bounded concurrency handled
    inside ``reconcile_detail_tier``).

    ``shard``/``num_shards`` (both-or-neither) restrict this pass to one shard of the drain matrix
    (see ``reconcile_detail_tier``'s docstring in ``index/detail.py`` for the shard-key design).
    ``merge=False`` is used by ``--detail-shard-only`` (the drain matrix's per-shard job): it must
    NOT touch/merge/republish the core index -- the drain workflow's separate merge job combines
    every shard's sidecar first, and the next daily ``build-index.yml`` run merges the combined
    sidecar into the core index via the existing carry-forward path.
    """
    from datetime import datetime, timezone

    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.index.db import connect
    from ergon_tracker.index.detail import (
        _DETAIL_CONCURRENCY,
        merge_detail_into_index,
        reconcile_detail_tier,
    )
    from ergon_tracker.providers.base import get_provider, load_builtins

    load_builtins()

    # Match AsyncFetcher's own global cap to reconcile_detail_tier's fetch concurrency
    # (ERGON_DETAIL_CONCURRENCY, default 24) -- AsyncFetcher's default (16) would otherwise become
    # the binding limiter instead of _DETAIL_CONCURRENCY. Politeness is unaffected: it's enforced
    # per-host by AsyncFetcher's token-bucket regardless of this global figure (see detail.py).
    async with AsyncFetcher(concurrency=_DETAIL_CONCURRENCY) as fetcher:

        async def _dispatch(ref):
            prov = get_provider(ref.source)
            if prov is None:
                return None
            return await prov.fetch_detail(ref, fetcher)

        stats = await reconcile_detail_tier(
            str(detail_db),
            str(index_db),
            fetch_detail=_dispatch,
            max_details=_detail_max(),
            sources=_TIER3_DETAIL_SOURCES,
            now=lambda: datetime.now(timezone.utc).isoformat(),
            shard=shard,
            num_shards=num_shards,
            location_backfill=_location_backfill(),
        )

    if not merge:
        return stats

    index_con = connect(index_db)
    try:
        stats["merged"] = merge_detail_into_index(index_con, str(detail_db))
    finally:
        index_con.close()
    return stats


def build_and_publish_detail(
    db_path: Path,
    out: Path,
    *,
    build_id: str,
    shard: int | None = None,
    num_shards: int | None = None,
) -> tuple[dict, int]:
    """Tier-3 reconcile + merge against the ALREADY-PUBLISHED core index, then re-publish
    ``index.sqlite.gz``/``manifest.json`` so the recovered fields actually reach users, and
    publish the detail sidecar itself (``index-detail.sqlite.gz`` + ``manifest-detail.json``).

    ORDERING (the one subtle wiring decision, see Task-5 brief): callers only invoke this AFTER
    ``_gated_publish`` has already gzipped+promoted the core index — the reconcile pass needs the
    final ``jobs`` table (with its real ids/content_hash) to select Tier-3 candidates against, and
    that table only exists once ``_gated_publish`` has promoted ``db_tmp`` -> ``db``. But
    ``merge_detail_into_index`` then mutates that SAME on-disk db in place, which means the
    ``index.sqlite.gz``/``manifest.json`` that ``_gated_publish`` already wrote are now stale — the
    recovered fields are on disk in ``db`` but not yet in the gzip a downloading user fetches. So
    this function re-runs ``publish_artifacts`` at the end, re-gzipping + rewriting ``manifest.json``
    (same ``build_id``, refreshed ``sha256``) with the merged fields included. Skipping that step
    would silently strand every recovered field in the sidecar and never reach a user. This whole
    call is wrapped in try/except by the caller (non-fatal: a detail failure must never undo or
    block the already-succeeded core publish).

    ``shard``/``num_shards`` are plumbed through to ``_reconcile_detail`` for completeness (both
    default ``None`` -- the ordinary daily/manual ``--detail`` path here always reconciles the
    WHOLE backlog, unsharded). The drain matrix uses the separate ``--detail-shard-only`` path
    (``build_detail_shard_only`` below) instead, which skips the merge/republish entirely.
    """
    import anyio

    detail_db = out / "index-detail.sqlite"
    stats = anyio.run(
        lambda: _reconcile_detail(detail_db, db_path, shard=shard, num_shards=num_shards)
    )
    if stats.get("merged", 0) > 0:
        # Recovered snippets just landed in `jobs.snippet` via plain UPDATEs, which do NOT
        # propagate into the external-content `jobs_fts` table -- rebuild it so those postings
        # become MATCHable before the re-publish below ships them.
        _rebuild_jobs_fts(db_path)
    sha, nbytes = _gzip_file(detail_db, out / "index-detail.sqlite.gz")
    _write_detail_manifest(out, build_id=build_id, sha=sha, nbytes=nbytes)
    # Re-publish the core index so the fields merge_detail_into_index just wrote land in the
    # gzip/manifest a downloading user actually fetches (see ORDERING above).
    publish_artifacts(db_path, out, build_id=build_id)
    return stats, nbytes


def build_detail_shard_only(index_db: Path, out: Path, *, shard: int, num_shards: int) -> dict:
    """Drain-matrix entry point (``--detail-shard-only``): run ONLY the sharded Tier-3 reconcile
    for shard ``shard`` of ``num_shards``, writing its sidecar to
    ``out / f"index-detail-shard-{shard}.sqlite"``. Never touches, merges into, or republishes the
    core index -- that decoupling is deliberate:

    - The drain workflow's separate ``merge`` job combines every shard's sidecar
      (``scripts/merge_detail_shards.py``) into one ``index-detail.sqlite`` and publishes it
      alongside its manifest, but publishes NOTHING for ``index.sqlite`` itself.
    - The next daily ``build-index.yml`` run downloads that combined sidecar as its carry-forward
      ``index-detail.sqlite.gz`` and merges it into the core index via the EXISTING
      ``build_and_publish_detail`` path -- its reconcile finds every already-drained row's sig
      still current (0 refetch needed) and just applies ``merge_detail_into_index``.

    This means the drain run never races the daily build's core-index publish, at the cost of a
    day's latency before recovered fields actually reach the published ``index.sqlite.gz``.
    ``index_db`` must already exist (the drain workflow downloads+gunzips the prior
    ``index.sqlite.gz`` first, purely for Tier-3 candidate selection -- it is opened read-only in
    spirit, never written to here).

    NOTE on the sidecar at ``detail_db``: the drain workflow (``.github/workflows/drain-detail.yml``)
    pre-seeds this exact path by ``cp``-ing the FULL prior combined ``index-detail.sqlite`` into it
    before this function runs, so this shard's reconcile can skip rows it already recovered without
    re-fetching them. ``reconcile_detail_tier`` (see its docstring / ``_prune_sidecar_to_shard`` in
    ``index/detail.py``) prunes that seed back down to ONLY this shard's own rows before returning,
    so the sidecar this function leaves on disk -- and thus the artifact uploaded for the drain
    workflow's ``merge`` job -- is scoped to this shard alone (disjoint from every other shard's).
    """
    import anyio

    out.mkdir(parents=True, exist_ok=True)
    detail_db = out / f"index-detail-shard-{shard}.sqlite"
    stats = anyio.run(
        lambda: _reconcile_detail(
            detail_db, index_db, shard=shard, num_shards=num_shards, merge=False
        )
    )
    return stats


def build_embed_shard_only(index_db: Path, out: Path, *, shard: int, num_shards: int) -> dict:
    """Embed-matrix entry point (``--embed-shard-only``): run ONLY the sharded vector embed for shard
    ``shard`` of ``num_shards`` against the already-built core index (``index_db``) + the crawl's
    published fresh DB (``out / "fresh-rich.sqlite"``), writing its partial to
    ``out / f"index-vectors-shard-{shard}.sqlite"``. Never crawls, builds, or republishes the core
    index -- the same decoupling as ``build_detail_shard_only``:

    - ``.github/workflows/embed-vectors.yml`` pre-seeds the partial by ``cp``-ing the prior combined
      ``index-vectors.sqlite`` into it (carry-forward); ``reconcile_rich_tier_from_fresh(shard=...)``
      prunes that seed down to ONLY this shard's slice (``rich._shard_of``), so the 20 partials stay
      DISJOINT and ``scripts/merge_vectors_shards.py`` unions them cleanly.
    - Sharding only changes WHICH runner embeds WHICH row; the embedding is deterministic, so the
      merged result is byte-identical to a single unsharded run (proven in test_rich_index.py)."""
    from ergon_tracker.index.rich import reconcile_rich_tier_from_fresh

    fresh = out / "fresh-rich.sqlite"  # published by build-index.yml from the crawl's fresh DB
    part = out / f"index-vectors-shard-{shard}.sqlite"
    return reconcile_rich_tier_from_fresh(
        part,
        index_db,
        fresh,
        build_id=_build_id(),
        shard=shard,
        num_shards=num_shards,
    )


def build_and_publish_slim(db_path: Path, out: Path, *, build_id: str) -> int:
    """Build the slim broad-query tier (no snippet, FTS over title+company) and gzip it.

    Broad keyword/filter queries that need no description hit this (~half the full-file bytes)
    instead of the full single file. Returns the row count.
    """
    from ergon_tracker.index.build import build_slim_index

    slim = out / "index-slim.sqlite"
    n = build_slim_index(db_path, slim, build_id=build_id)
    sha, nbytes = _gzip_file(slim, out / "index-slim.sqlite.gz")
    slim.unlink(missing_ok=True)
    (out / "manifest-slim.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema_version": SCHEMA_VERSION,
                "sha256": sha,
                "bytes": nbytes,
                "rows": n,
            }
        )
    )
    return n


def _count_jobs(db_path: Path) -> int:
    """Row count of an index DB (cheap; avoids loading jobs into memory)."""
    from ergon_tracker.index.db import connect

    con = connect(db_path, read_only=True)
    try:
        return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        con.close()


def publish_coverage(db_path: Path, out_dir: Path, *, build_id: str) -> dict:
    """Write coverage.json + INDEX_STATUS.md so users/forkers can see index coverage."""
    from ergon_tracker.index.coverage import compute_coverage, render_status_md
    from ergon_tracker.index.db import connect

    con = connect(db_path, read_only=True)
    try:
        cov = compute_coverage(con)
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_status_md(cov, build_id=build_id)
    (out_dir / "coverage.json").write_text(json.dumps(cov, indent=2))
    (out_dir / "INDEX_STATUS.md").write_text(md)  # published as a release asset (always current)
    # NB: do NOT write ROOT/INDEX_STATUS.md here — that polluted the repo on every local/test
    # build. The repo-root copy is a periodic snapshot; the release asset is the live one.
    return cov


def append_history(history_path: Path, row: dict) -> None:
    """Append one build-summary row to the history JSONL time series (for drift detection)."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _last_published_rows(history_path: Path) -> int | None:
    """Row count of the most recent SUCCESSFULLY published build in history.jsonl, else None.

    A durable row-floor basis for when the live prev snapshot is absent: history.jsonl is restored
    from the release every CI run, so a failed ``index.sqlite.gz`` download can't fool the gate into
    a cold-start pass. Filters ``published`` so a prior *failed* build's tiny count is never the basis
    (failed builds append a ``published: false`` row). Last matching row wins (append-chronological).
    """
    if not history_path.exists():
        return None
    best: int | None = None
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows = rec.get("total_jobs")
        if rec.get("published") and isinstance(rows, int) and rows > 0:
            best = rows
    return best


def _gated_publish(
    tmp_db: Path,
    final_db: Path,
    out: Path,
    *,
    build_id: str,
    prev_row_count: int | None = None,
    last_known_rows: int | None = None,
) -> bool:
    """Good-or-nothing publish: gate the temp build, promote+publish only if it passes.

    Writes gates.json always. On failure the previous snapshot (final_db) is left untouched.
    ``ERGON_ALLOW_COLD_START`` (env) permits publishing below the historical floor for a genuine
    first build / intentional reset.
    """
    from ergon_tracker.index.gates import evaluate_gates

    allow_cold_start = os.environ.get("ERGON_ALLOW_COLD_START", "").lower() in ("1", "true", "yes")
    rep = evaluate_gates(
        tmp_db,
        prev_row_count=prev_row_count,
        last_known_rows=last_known_rows,
        allow_cold_start=allow_cold_start,
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "gates.json").write_text(json.dumps(rep.to_dict(), indent=2))
    if not rep.passed:
        print(f"GATES FAILED — keeping previous snapshot. {rep.summary()}")
        tmp_db.unlink(missing_ok=True)
        return False
    tmp_db.replace(final_db)  # atomic promote
    publish_artifacts(final_db, out, build_id=build_id)
    cov = publish_coverage(final_db, out, build_id=build_id)
    print(
        f"gates passed: {rep.summary()} | coverage: {cov['total_jobs']} jobs, "
        f"{len(cov['by_source'])} providers, {len(cov['by_sector'])} sectors"
    )
    return True


def _new_boards(registry_items, states: dict, cap: int = 2000) -> list:
    """Registry boards with no board_state entry yet (added since the last build), capped.

    These get crawled in the next build regardless of the rotating cursor, so freshly-captured ATS
    boards become queryable immediately instead of waiting for the window to reach them. The cap
    keeps a cold start (everything unseen) bounded to the window size.
    """
    from ergon_tracker.index.scheduler import BoardState

    out: list = []
    for key, e in registry_items:
        if len(out) >= cap:
            break
        if not (e.get("ats") and e.get("token")):
            continue
        if BoardState(provider=e["ats"], token=e["token"]).key not in states:
            out.append((key, e))
    return out


def _interleave_by_ats(items: list) -> list:
    """Reorder registry boards round-robin across their ATS so any contiguous window is balanced
    across backends.

    The registry is in insertion order, which CLUSTERS same-ATS boards (we append by ATS during
    ingest). A contiguous slice of that order can therefore be dominated by one backend — e.g. a
    window that landed on ~8k freshly-added Workable boards hammered apply.workable.com into a
    2,181x-429 storm (build-2026-06-21-18). Round-robin interleaving caps any window's share of a
    backend at roughly that backend's share of the whole registry, so no single ATS gets a
    sustained burst. Deterministic (stable buckets in first-seen order) so the rotating cursor
    stays meaningful build-to-build; minor drift when the registry grows is absorbed by the
    rotation + ``_new_boards``.
    """
    from collections import OrderedDict

    buckets: OrderedDict[str, list] = OrderedDict()
    for k, e in items:
        buckets.setdefault(e["ats"], []).append((k, e))
    # Stratified placement: give each board a fractional position (rank+0.5)/bucket_size in [0,1)
    # and sort by it, so every backend is spread EVENLY across the whole order. (A naive
    # round-robin balances the head but lets the largest bucket's overflow cluster in the tail —
    # so a tail window would still be one-backend-dominated.) Now any contiguous window holds
    # roughly each backend's share of the registry.
    keyed: list[tuple[float, int, tuple]] = []
    for order, blist in enumerate(buckets.values()):
        m = len(blist)
        for i, item in enumerate(blist):
            keyed.append(((i + 0.5) / m, order, item))
    keyed.sort(key=lambda t: (t[0], t[1]))  # order as deterministic tiebreaker
    return [item for _, _, item in keyed]


# Hard cap on how many boards a SINGLE crawl run may take, regardless of how large
# --limit-companies is. Bounds each run so it finishes within the CI timeout AND makes a killed run
# resumable: the cursor advances by (at most) one window, so successive runs continue instead of
# re-crawling one giant 58k window forever. 12000 == the proven daily window, so the scheduled
# `--incremental --limit-companies 12000` run is unaffected (its window is already <= this cap).
_DEFAULT_MAX_WINDOW = 12000


def _registry_window(cursor: int, limit: int, max_window: int | None = None) -> tuple[list, int]:
    """Return (window, next_cursor): a rotating, backend-INTERLEAVED slice of crawlable boards.

    Each run takes up to `limit` boards starting at `cursor` (wrapping) from an ATS-interleaved
    ordering (see ``_interleave_by_ats``), then advances the cursor. The per-run window is CAPPED at
    ``max_window`` (env ``ERGON_CRAWL_MAX_WINDOW``, default 12000) so a single job never crawls one
    giant window: even ``--limit-companies 58078`` is served as rotating ~12k slices whose cursor
    advances, so a killed/timed-out run is resumable (the next run continues from next_cursor). Over
    ceil(total/window) runs the whole registry is covered + seeded into board_state; interleaving
    keeps every window balanced across backends so no single ATS is throttled by a clustered burst.
    """
    from ergon_tracker.registry.store import SeedRegistry

    if max_window is None:
        max_window = int(os.environ.get("ERGON_CRAWL_MAX_WINDOW") or _DEFAULT_MAX_WINDOW)
    items = [(k, e) for k, e in SeedRegistry().all().items() if e.get("ats") and e.get("token")]
    items = _interleave_by_ats(items)
    total = len(items)
    if total == 0:
        return [], 0
    eff = min(limit, max_window) if max_window > 0 else limit  # <=0 disables the cap (tests/manual)
    if eff >= total:
        return items, 0
    start = cursor % total
    window = [items[(start + i) % total] for i in range(eff)]
    return window, (start + eff) % total


async def _crawl_due(
    limit_companies: int,
    states: dict,
    fresh_db_path,
    build_id: str,
    cursor: int = 0,
    capture_rich: bool = False,
    prev_db: Path | str | None = None,
) -> tuple[dict, int]:
    """Crawl the due boards in this run's rotating window, streaming jobs into ``fresh_db_path``.

    Returns (per-board outcome, next_cursor). Jobs are written to the fresh DB as boards complete
    (memory O(in-flight boards), not O(all jobs)). The window bounds each run so it finishes within
    the CI timeout and durably seeds board_state; crash-isolated per board. When ``capture_rich`` is
    set, each board's FULL descriptions are also captured to the fresh DB's ``fresh_rich`` table (the
    index only keeps a snippet) so the incremental rich reconcile can index them — still O(window) on disk.

    ``prev_db`` (optional): the prior published index, used ONLY to resolve a zero-result board's
    real prior ``company_key`` by ``(source, board_token)`` -- see the zero-results branch below.
    """
    import anyio

    from ergon_tracker.dedup import deduplicate, normalize_company
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.exceptions import RateLimitError
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.index.build import append_jobs
    from ergon_tracker.index.db import connect, fresh_db
    from ergon_tracker.index.scheduler import BoardState, due_boards
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.base import get_provider, load_builtins

    load_builtins()

    # (source, board_token) -> the set of company_key values the PRIOR index actually stored for
    # that board. `to_row` computes `company_key = normalize_company(job.company)` -- the
    # ATS-returned name -- which commonly differs from the seed registry's OWN key (`regkey`,
    # below) for the same board (e.g. a legal-name vs. brand-name mismatch). A zero-result board
    # has no fresh job to derive that name from, so when it departs we resolve its real prior
    # company_key(s) here instead of guessing with `regkey` (see the zero-results branch).
    prior_company_keys_by_board: dict[tuple[str, str], set[str]] = {}
    if prev_db is not None and Path(prev_db).exists():
        pcon = connect(prev_db, read_only=True)
        try:
            for source, token, ckey in pcon.execute(
                "SELECT DISTINCT source, board_token, company_key FROM jobs "
                "WHERE board_token IS NOT NULL AND company_key IS NOT NULL"
            ):
                prior_company_keys_by_board.setdefault((source, token), set()).add(ckey)
        finally:
            pcon.close()

    window, next_cursor = _registry_window(cursor, limit_companies)
    boards = {}
    for key, e in window:
        bs = BoardState(provider=e["ats"], token=e["token"])
        boards[bs.key] = (key, e)
        states.setdefault(bs.key, bs)
    # Also crawl NEVER-SEEN boards (added to the registry since the last build) regardless of the
    # cursor, so fresh captures appear in the very next build instead of waiting for the window to
    # rotate to them. Bounded so a cold start (everything unseen) still respects the window size.
    if len(states) > limit_companies:  # past the initial cold-start rotation
        from ergon_tracker.registry.store import SeedRegistry

        new = _new_boards(SeedRegistry().all().items(), states)
        for key, e in new:
            bs = BoardState(provider=e["ats"], token=e["token"])
            boards[bs.key] = (key, e)
            states[bs.key] = bs
        if new:
            print(f"  + {len(new)} never-seen board(s) pulled in ahead of the cursor")
    due = set(due_boards(list(states.values()), _today())) & set(boards)

    outcome: dict[str, dict] = {
        b: {"error": False, "http_429": 0, "companies": set(), "not_modified": False} for b in due
    }
    fresh_db(fresh_db_path)
    con = connect(fresh_db_path)
    con.execute(
        "PRAGMA foreign_keys = OFF"
    )  # companies aggregated later (build_index_from_fresh_db)
    write_lock = anyio.Lock()
    pending = {"rows": 0}  # uncommitted row count; mutated only while holding write_lock

    async def grab(bkey: str, fetcher: AsyncFetcher) -> None:
        regkey, e = boards[bkey]
        provider = get_provider(e["ats"])
        state = states[bkey]
        # Cross-build conditional request: if this provider exposes a whole-board validator URL,
        # present the stored ETag/Last-Modified. A 304 means unchanged -> carry forward without
        # re-downloading (the big throttle/bandwidth win). A 200 refreshes the validator and we
        # parse that same body (no refetch) via raws_from_body.
        curl = provider.conditional_url(e["token"])
        try:
            if curl:
                res = await fetcher.conditional_get(
                    curl, etag=state.etag, last_modified=state.last_modified
                )
                if res.not_modified:
                    outcome[bkey]["not_modified"] = True
                    return  # unchanged -> prev jobs carry forward (company set stays empty)
                state.etag, state.last_modified = res.etag, res.last_modified
                # Reuse the body we just downloaded (200) instead of refetching the same board.
                raws = provider.raws_from_body(e["token"], res.body) if res.body else None
                if raws is None:
                    raws = await provider.fetch(e["token"], SearchQuery(), fetcher)
            else:
                raws = await provider.fetch(e["token"], SearchQuery(), fetcher)
        except RateLimitError:
            outcome[bkey].update(error=True, http_429=1)
            return
        except Exception:  # noqa: BLE001
            outcome[bkey]["error"] = True
            return
        # Crash isolation: normalize/enrich/dedup/insert for ONE board must never propagate to
        # the task group (that would cancel every other in-flight board and lose the whole crawl).
        try:
            board_jobs: list = []
            for raw in raws:
                try:
                    job = provider.normalize(raw)
                except Exception:  # noqa: BLE001
                    continue
                if e.get("domain") and not job.company_domain:
                    job.company_domain = e["domain"]
                job.board_token = e["token"]  # registry token this board was crawled with
                enrich_in_place(job, company_key=regkey, infer_level_from_experience=True)
                board_jobs.append(job)
                outcome[bkey]["companies"].add(normalize_company(job.company))
            if board_jobs:
                # Per-board fuzzy dedup (cheap, memory-safe) recovers most of the old in-memory
                # deduplicate() quality; cross-board exact-id dedup is handled by append_jobs' UNIQUE.
                board_jobs = deduplicate(board_jobs)
                # one shared connection; the lock serializes the (sync, fast) batch insert and
                # the commit-batching counter. Periodic commit bounds the open transaction so a
                # crash/timeout doesn't roll back the entire crawl.
                async with write_lock:
                    append_jobs(con, board_jobs, build_id=build_id)
                    if (
                        capture_rich
                    ):  # full descriptions for the rich tier (index keeps only a snippet)
                        from ergon_tracker.index.rich import write_fresh_rich

                        write_fresh_rich(con, board_jobs)
                    pending["rows"] += len(board_jobs)
                    if pending["rows"] >= 20000:
                        con.commit()
                        pending["rows"] = 0
            if not outcome[bkey]["companies"]:
                # The fetch SUCCEEDED (no exception, we got this far) but yielded no usable
                # postings this run -- either raws was genuinely empty (the board emptied out) or
                # every raw failed to normalize. Either way this board WAS crawled, just with zero
                # results, so it must still register as crawled: carry_forward only carries
                # forward companies NOT in crawled_keys, and a company key that never appears here
                # (because no job survived to be normalize_company()'d) would silently keep its
                # stale prior-index jobs forever.
                #
                # regkey (the seed registry's OWN key for this board) is only a FALLBACK: the
                # prior index's stored `company_key` is `normalize_company(job.company)` -- the
                # ATS-returned name -- which commonly differs from regkey. Resolve the board's
                # REAL prior company_key(s) by (source, board_token) so the drop reliably fires;
                # only fall back to regkey when there's no prior row for this board (e.g. a
                # brand-new board that happened to crawl empty on its first run -- nothing to
                # drop either way).
                prior_keys = prior_company_keys_by_board.get((e["ats"], e["token"]))
                if prior_keys:
                    outcome[bkey]["companies"].update(prior_keys)
                else:
                    outcome[bkey]["companies"].add(regkey)
        except Exception:  # noqa: BLE001 - one bad board never sinks the crawl
            outcome[bkey]["error"] = True
            outcome[bkey]["companies"].clear()  # not "crawled" -> prev jobs carry forward

    import os

    # Crawl-tuned fetcher: fail fast on dead/slow boards (a big fraction of a 46k-board cold
    # crawl). Defaults (25s timeout, 3 retries + backoff) can burn ~88s per dead board; 12s +
    # 1 retry caps that at ~24s. Per-host rate limiting + circuit breaker still apply, and
    # transiently-missed boards stay 'hot' and are retried next build (tiering).
    # Concurrency 64 (was 16): the build crawls thousands of boards, and the global limiter — not the
    # network — was the cap. Per-host AsyncLimiters + the circuit breaker remain the safety valve, so
    # no single ATS is hit faster. Env-tunable for CI/runner sizing. (The SDK live-fetch path keeps the
    # polite default of 16 — this higher concurrency is scoped to the bulk build only.)
    crawl_concurrency = int(
        os.environ.get("ERGON_CRAWL_CONCURRENCY") or ("64" if os.environ.get("CI") else "12")
    )
    try:
        async with (
            AsyncFetcher(timeout=12.0, retries=2, concurrency=crawl_concurrency) as fetcher,
            anyio.create_task_group() as tg,
        ):
            for bkey in due:
                tg.start_soon(grab, bkey, fetcher)
        con.commit()
    finally:
        con.close()
    return outcome, next_cursor


def _load_cursor(path: Path) -> int:
    """Read the rotating-crawl cursor (registry offset) from a small JSON file; 0 if absent."""
    try:
        return int(json.loads(Path(path).read_text()).get("cursor", 0))
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _save_cursor(path: Path, cursor: int) -> None:
    Path(path).write_text(json.dumps({"cursor": cursor}))


def main(argv: list[str]) -> None:
    import anyio

    limit = 300
    out = ROOT / "dist"
    incremental = False
    sharded = False
    rich = (
        False  # opt-in: also build/reconcile the rich sidecar (full-JD FTS + pre-stored embeddings)
    )
    detail = (
        False  # opt-in: also run the Tier-3 detail reconcile + merge (manual-only; see workflow)
    )
    liveness = False  # opt-in: also run the apply-URL liveness pass (dead-link detection)
    network_pages = 0  # 0 disables the workable_network bulk feed; >0 = pages to pull
    detail_shard_only = False  # drain-matrix mode: sharded reconcile only, no crawl/build/merge
    embed_shard_only = False  # embed-matrix mode: sharded vector embed only, no crawl/build
    shard: int | None = None
    num_shards: int | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--limit-companies":
            limit = int(argv[i + 1])
            i += 2
        elif argv[i] == "--out":
            out = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--network-pages":
            network_pages = int(argv[i + 1])
            i += 2
        elif argv[i] == "--incremental":
            incremental = True
            i += 1
        elif argv[i] == "--sharded":
            sharded = True
            i += 1
        elif argv[i] == "--rich":
            rich = True
            i += 1
        elif argv[i] == "--detail":
            detail = True
            i += 1
        elif argv[i] == "--liveness":
            liveness = True
            i += 1
        elif argv[i] == "--detail-shard-only":
            detail_shard_only = True
            i += 1
        elif argv[i] == "--embed-shard-only":
            embed_shard_only = True
            i += 1
        elif argv[i] == "--shard":
            shard = int(argv[i + 1])
            i += 2
        elif argv[i] == "--num-shards":
            num_shards = int(argv[i + 1])
            i += 2
        else:
            print(f"unknown flag: {argv[i]}")
            return
    if shard is None and num_shards is None:
        shard, num_shards = (
            _detail_shard()
        )  # fall back to ERGON_DETAIL_SHARD/ERGON_DETAIL_NUM_SHARDS
    out.mkdir(parents=True, exist_ok=True)
    db = out / "index.sqlite"
    build_id = _build_id()

    if detail_shard_only:
        # Drain-matrix mode (see .github/workflows/drain-detail.yml): ONLY the sharded Tier-3
        # reconcile against the already-downloaded prior index db -- no crawl, no core build, no
        # merge/republish. See build_detail_shard_only's docstring for the decoupling rationale.
        if shard is None or num_shards is None:
            print(
                "--detail-shard-only requires --shard/--num-shards (or ERGON_DETAIL_SHARD/ERGON_DETAIL_NUM_SHARDS)"
            )
            raise SystemExit(2)
        if not db.exists():
            print(
                f"--detail-shard-only requires an existing index db at {db} (download+gunzip the prior index.sqlite.gz first)"
            )
            raise SystemExit(2)
        stats = build_detail_shard_only(db, out, shard=shard, num_shards=num_shards)
        print(
            f"detail shard {shard}/{num_shards}: fetched={stats['fetched']} "
            f"failed={stats['failed']} missing={stats['missing']} -> "
            f"{out / f'index-detail-shard-{shard}.sqlite'}"
        )
        return

    if embed_shard_only:
        # Embed-matrix mode (see .github/workflows/embed-vectors.yml): ONLY the sharded vector embed
        # for shard `shard` of `num_shards`, against the already-downloaded core index + the crawl's
        # fresh DB. Never crawls, builds, or republishes the core index -- the merge job
        # (merge_vectors_shards.py) unions the 20 partials into index-vectors.sqlite.
        if shard is None or num_shards is None:
            print("--embed-shard-only requires --shard/--num-shards")
            raise SystemExit(2)
        if not db.exists():
            print(
                f"--embed-shard-only requires an existing index db at {db} "
                "(download+gunzip the prior index.sqlite.gz first)"
            )
            raise SystemExit(2)
        stats = build_embed_shard_only(db, out, shard=shard, num_shards=num_shards)
        print(
            f"embed shard {shard}/{num_shards}: embedded={stats['embedded']} "
            f"pruned={stats['pruned']} missing={stats['missing']} -> "
            f"{out / f'index-vectors-shard-{shard}.sqlite'}"
        )
        return

    # A full/large manual crawl (no --incremental) used to take a legacy in-memory path (`_crawl`)
    # with NO windowing and NO cursor, so a CI-timeout kill lost the entire run (this is exactly what
    # happened to `--limit-companies 58078 --sharded` — ~4.5h, killed at the 330-min timeout, all
    # work lost). Route EVERY crawl through the streaming/incremental path instead: it windows the
    # registry (see _registry_window's cap), streams jobs to fresh.sqlite as boards complete, and
    # persists the cursor + board_state so a re-run resumes. The daily `--incremental` run is
    # unchanged; this only redirects the previously-dangerous non-incremental invocation.
    if not incremental:
        print(
            "[route] non-incremental crawl -> streaming/incremental path "
            "(bounded window, resumable cursor). A large --limit-companies is served as rotating "
            "windows; re-run to advance the cursor until the registry is fully covered.",
            flush=True,
        )
        incremental = True

    if incremental:
        from ergon_tracker.index.build import (
            build_index_from_fresh_db,
            changed_companies_sql,
        )
        from ergon_tracker.index.scheduler import apply_outcome, load_state, save_state

        state_path = out / "board_state.json"
        cursor_path = out / "crawl_cursor.json"
        states = load_state(state_path)
        cursor = _load_cursor(cursor_path)
        prev_db = db if db.exists() else None
        prev_row_count = _count_jobs(db) if prev_db else None
        # Durable floor basis: even if the live prev snapshot failed to download, history.jsonl
        # (restored from the release) still records the last published size — so a collapse can't
        # sneak past the row_floor gate as a cold start.
        last_known_rows = _last_published_rows(out / "history.jsonl")
        # Streaming crawl over a rotating window: jobs stream to fresh.sqlite as boards complete.
        fresh_path = out / "fresh.sqlite"
        with _phase("crawl"):
            outcome, next_cursor = anyio.run(
                _crawl_due, limit, states, fresh_path, build_id, cursor, rich, prev_db
            )
        # Fold the first-party Workable network feed into the same fresh.sqlite (its rows flow into
        # the index alongside the crawled boards). Done before changed_companies_sql so new network
        # companies register as changed.
        net_keys = anyio.run(_fold_network_into_fresh, fresh_path, network_pages, build_id)
        changed = changed_companies_sql(fresh_path, prev_db)  # SQL diff, no jobs in memory
        crawled_keys: set = (
            set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
        )
        crawled_keys |= net_keys  # network companies are refreshed -> no stale carry-forward dupes
        # fold each board's outcome back into its state (tiering + throttle back-pressure)
        for bkey, o in outcome.items():
            board_changed = bool(o["companies"] & changed)
            apply_outcome(
                states[bkey],
                today=_today(),
                changed=board_changed and not o["error"],
                error=o["error"],
                http_429=o["http_429"],
                requests=1,
            )
        # Persist crawl progress (tiering + cursor) IMMEDIATELY — durable even if the build/publish
        # below fails or times out, so the next run advances instead of re-crawling this window.
        save_state(states, state_path)
        _save_cursor(cursor_path, next_cursor)
        fresh_jobs_count = _count_jobs(fresh_path)
        db_tmp = out / "index.tmp.sqlite"
        with _phase("build index"):
            n = build_index_from_fresh_db(
                fresh_path, db_tmp, build_id=build_id, prev_db=prev_db, crawled_keys=crawled_keys
            )
        # Backfill board_token for carried-forward rows (registry + verified URL derivation) so the
        # freshness sweep covers ~all boards immediately, not just the crawl-visited subset.
        bf = _backfill_board_tokens(db_tmp)
        if bf:
            print(f"  + backfilled board_token on {bf} rows (freshness-sweep coverage)")
        # Carry forward a prior daily freshness-sweep's expiries onto db_tmp BEFORE the gated
        # publish, so the promoted index (and every downstream shard/slim/delta derived from it)
        # never resurrects a posting the sweep already confirmed departed its board. Non-fatal;
        # never touches row count (row_floor gate unaffected).
        freshness_expired = _apply_freshness(db_tmp, out)
        if freshness_expired:
            print(f"  + carried forward {freshness_expired} freshness-sweep expiries")
        # Preserve the prior index (move aside, instant on same fs) so we can diff it for the delta
        # AFTER the gated promote overwrites `db`. Build_index_from_fresh_db has already read it.
        prev_snap = None
        if prev_db is not None and db.exists():
            prev_snap = out / "index.prev.sqlite"
            db.replace(prev_snap)
        ok = _gated_publish(
            db_tmp,
            db,
            out,
            build_id=build_id,
            prev_row_count=prev_row_count,
            last_known_rows=last_known_rows,
        )
        if not ok and prev_snap is not None:
            prev_snap.replace(db)  # gates failed -> restore the previous snapshot
            prev_snap = None
        append_history(
            out / "history.jsonl",
            {
                "build_id": build_id,
                "date": _today(),
                "due_boards": len(outcome),
                "fresh_jobs": fresh_jobs_count,
                "total_jobs": n,
                "changed_companies": len(changed),
                "throttled_boards": sum(1 for o in outcome.values() if o["http_429"]),
                "errored_boards": sum(1 for o in outcome.values() if o["error"]),
                "not_modified_boards": sum(1 for o in outcome.values() if o.get("not_modified")),
                "cursor": cursor,
                "next_cursor": next_cursor,
                "window": limit,
                "published": ok,
            },
        )
        if ok and rich and not _sharded_embed():  # inline embed UNLESS the sharded matrix owns it
            # NON-FATAL: the rich tier is an optional enhancement. The main index is already gated +
            # promoted above, so an embedding OOM/timeout/model-download failure must NOT crash the
            # build and skip the publish step — log it and carry on (yesterday's rich gz stays live).
            try:
                with _phase("embed (inline)"):
                    rstats, rbytes = build_and_publish_rich_incremental(
                        db, fresh_path, out, build_id=build_id
                    )
                print(
                    f"  + rich tier (pruned={rstats['pruned']} embedded={rstats['embedded']} "
                    f"missing={rstats['missing']}) -> index-vectors.sqlite.gz ({rbytes // 1024} KB)"
                )
            except Exception as exc:  # noqa: BLE001 - never let the rich tier break the core build
                print(f"  ! rich tier skipped (non-fatal): {type(exc).__name__}: {exc}")
        # Publish the crawl's fresh_rich DB (full-description embed_text) so the sharded
        # embed-vectors.yml matrix can consume it -- gzip it to a persistent artifact BEFORE the
        # unlink below frees the raw file. The embed shards download + gunzip `fresh-rich.sqlite.gz`
        # to `fresh-rich.sqlite`, which build_embed_shard_only reads. Gated on `ok` so a gate-failed
        # build never ships a fresh DB out of sync with the (unpublished) index.
        if ok and fresh_path.exists():
            _gzip_file(fresh_path, out / "fresh-rich.sqlite.gz")
        fresh_path.unlink(missing_ok=True)  # free disk before the shard VACUUMs
        if ok and detail:  # Tier-3 reconcile + merge against the already-published core index
            # NON-FATAL: the detail tier is an optional enhancement, same contract as rich above.
            # The main index is already gated + promoted, so a fetch-dispatcher/merge failure must
            # NOT crash the build or undo the core publish — log it and carry on.
            try:
                dstats, dbytes = build_and_publish_detail(
                    db, out, build_id=build_id, shard=shard, num_shards=num_shards
                )
                print(
                    f"  + detail tier (fetched={dstats['fetched']} failed={dstats['failed']} "
                    f"missing={dstats['missing']} merged={dstats['merged']}) -> "
                    f"index-detail.sqlite.gz ({dbytes // 1024} KB)"
                )
            except Exception as exc:  # noqa: BLE001 - never let the detail tier break the core build
                print(f"  ! detail tier skipped (non-fatal): {type(exc).__name__}: {exc}")
        if ok and liveness:  # apply-URL liveness pass against the already-published core index
            # NON-FATAL: same contract as rich/detail above. The main index is already gated +
            # promoted, so a board-fetch/classify failure here must NOT crash the build or undo
            # the core publish — log it and carry on (yesterday's statuses stay live).
            try:
                lstats = build_and_publish_liveness(db, out, build_id=build_id)
                print(
                    f"  + liveness tier (checked={lstats['checked']} "
                    f"flipped_dead={lstats['flipped_dead']} "
                    f"confirmed_alive={lstats['confirmed_alive']} "
                    f"boards_fetched={lstats['boards_fetched']} "
                    f"boards_failed={lstats['boards_failed']}) -> index-liveness.sqlite.gz"
                )
            except Exception as exc:  # noqa: BLE001 - never let the liveness tier break the build
                print(f"  ! liveness tier skipped (non-fatal): {type(exc).__name__}: {exc}")
        if ok and sharded:
            ns = build_and_publish_shards_from_db(db, out, build_id=build_id)
            print(f"  + published {ns} sector shards")
            nslim = build_and_publish_slim(db, out, build_id=build_id)
            print(f"  + published slim tier ({nslim} rows) -> index-slim.sqlite.gz")
        if ok and prev_snap is not None and prev_snap.exists():
            try:
                di = build_and_publish_delta(prev_snap, db, out, build_id=build_id)
                if di:
                    print(
                        f"  + published delta {di['from_build_id']}->{di['to_build_id']} "
                        f"({di.get('upserts', 0)} upserts, {di.get('deletes', 0)} deletes, "
                        f"{di.get('bytes', 0) / 1e6:.1f}MB)"
                    )
            finally:
                prev_snap.unlink(missing_ok=True)  # reclaim the ~500MB snapshot
        print(
            f"incremental build: crawled {len(outcome)} due boards, {fresh_jobs_count} fresh jobs, "
            f"{n} total{' -> published' if ok else ' (gates FAILED, kept previous)'}"
        )
        # Persist the measured phase breakdown so future ETAs are queryable from history.jsonl
        # (grep kind=timing) rather than read off a live run. Separate JSONL line from the main
        # build record above (which is written before the embed phase completes).
        append_history(
            out / "history.jsonl",
            {
                "build_id": build_id,
                "date": _today(),
                "kind": "timing",
                "phase_seconds": dict(_PHASE_TIMINGS),
                "total_seconds": round(sum(_PHASE_TIMINGS.values()), 1),
                "published": ok,
            },
        )
        if not ok:
            raise SystemExit(1)
        return


if __name__ == "__main__":
    main(sys.argv[1:])
