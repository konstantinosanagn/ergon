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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.build import build_index  # noqa: E402
from ergon_tracker.index.db import SCHEMA_VERSION  # noqa: E402

# Compression level 6 (not gzip's default 9): ~2x faster for ~5% larger output — the right trade for a
# ~1GB artifact rebuilt daily. Output stays standard gzip (.gz), so the SDK's gunzip is unchanged.
_GZIP_LEVEL = int(os.environ.get("ERGON_GZIP_LEVEL", "6"))


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


async def _crawl(limit_companies: int, network_pages: int = 0) -> list:
    """Bounded registry crawl: fetch N boards directly by their stored (ats, token).

    Bypasses resolve() (which is for arbitrary user domains/URLs) and reuses the providers +
    enrich + dedup, crash-isolated per board so one dead board never sinks the run. When
    ``network_pages`` > 0, also folds in the ``workable_network`` bulk feed before the final dedup.
    """
    import anyio

    from ergon_tracker.dedup import deduplicate
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.base import get_provider, load_builtins
    from ergon_tracker.registry.store import SeedRegistry

    load_builtins()
    items = [
        (k, e)
        for k, e in list(SeedRegistry().all().items())[:limit_companies]
        if e.get("ats") and e.get("token")
    ]
    jobs: list = []

    async def grab(key: str, entry: dict, fetcher: AsyncFetcher) -> None:
        provider = get_provider(entry["ats"])
        if provider is None:
            return
        try:
            raws = await provider.fetch(entry["token"], SearchQuery(), fetcher)
        except Exception:  # noqa: BLE001 - dead/blocked board, skip
            return
        for raw in raws:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            if entry.get("domain") and not job.company_domain:
                job.company_domain = entry["domain"]
            enrich_in_place(job, company_key=key, infer_level_from_experience=True)
            jobs.append(job)

    async with AsyncFetcher() as fetcher, anyio.create_task_group() as tg:
        for key, entry in items:
            tg.start_soon(grab, key, entry, fetcher)
    jobs.extend(await _crawl_network(network_pages))  # first-party Workable network coverage
    return deduplicate(jobs)


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


def build_and_publish_rich(
    db_path: Path, jobs: list, out: Path, *, build_id: str
) -> tuple[dict, int]:
    """Reconcile the vectors sidecar (pre-stored int8 embeddings) to the freshly-built main index,
    then gzip-publish it as ``index-vectors.sqlite.gz`` + ``manifest-vectors.json``.

    Uses the in-memory ``jobs`` (which still carry FULL descriptions — the main index truncates to a
    snippet) and the main index's live ids, so the cascade prunes anything the build dropped and
    re-embeds only new/changed postings. Needs the ``semantic`` extra (the embedding model)."""
    from ergon_tracker.index.rich import reconcile_rich_tier

    rich_db = out / "index-vectors.sqlite"
    stats = reconcile_rich_tier(rich_db, db_path, jobs, build_id=build_id)
    sha, nbytes = _gzip_file(rich_db, out / "index-vectors.sqlite.gz")
    _write_vectors_manifest(out, build_id=build_id, sha=sha, nbytes=nbytes)
    return stats, nbytes


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


_TIER3_DETAIL_SOURCES = ["smartrecruiters"]  # sources with a fetch_detail impl (Task 9: more ATS)


def _detail_max() -> int:
    """Per-run bound on Tier-3 detail fetches, ``ERGON_DETAIL_MAX`` (env), default 5000."""
    return int(os.environ.get("ERGON_DETAIL_MAX", "5000"))


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


async def _reconcile_detail(detail_db: Path, index_db: Path) -> dict:
    """Fetch JD detail for Tier-3 (no-description) postings via each source's registered provider
    (``fetch_detail``), then merge recovered fields into the index db (already-promoted at this
    point) in place. Returns the reconcile stats plus a ``merged`` row count.

    ``fetch_detail`` dispatch: look up the provider by ``ref.source`` in the registry and call its
    (possibly-unimplemented — defaults to returning ``None``) ``fetch_detail(ref, fetcher)``. One
    real ``AsyncFetcher`` is shared across the whole reconcile window (bounded concurrency handled
    inside ``reconcile_detail_tier``).
    """
    from datetime import datetime, timezone

    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.index.db import connect
    from ergon_tracker.index.detail import merge_detail_into_index, reconcile_detail_tier
    from ergon_tracker.providers.base import get_provider, load_builtins

    load_builtins()

    async with AsyncFetcher() as fetcher:

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
        )

    index_con = connect(index_db)
    try:
        stats["merged"] = merge_detail_into_index(index_con, str(detail_db))
    finally:
        index_con.close()
    return stats


def build_and_publish_detail(db_path: Path, out: Path, *, build_id: str) -> tuple[dict, int]:
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
    """
    import anyio

    detail_db = out / "index-detail.sqlite"
    stats = anyio.run(_reconcile_detail, detail_db, db_path)
    sha, nbytes = _gzip_file(detail_db, out / "index-detail.sqlite.gz")
    _write_detail_manifest(out, build_id=build_id, sha=sha, nbytes=nbytes)
    # Re-publish the core index so the fields merge_detail_into_index just wrote land in the
    # gzip/manifest a downloading user actually fetches (see ORDERING above).
    publish_artifacts(db_path, out, build_id=build_id)
    return stats, nbytes


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


def _registry_window(cursor: int, limit: int) -> tuple[list, int]:
    """Return (window, next_cursor): a rotating, backend-INTERLEAVED slice of crawlable boards.

    Each run takes `limit` boards starting at `cursor` (wrapping) from an ATS-interleaved ordering
    (see ``_interleave_by_ats``), then advances the cursor. Over ceil(total/limit) runs the whole
    registry is covered + seeded into board_state; interleaving keeps every window balanced across
    backends so no single ATS is throttled by a clustered burst.
    """
    from ergon_tracker.registry.store import SeedRegistry

    items = [(k, e) for k, e in SeedRegistry().all().items() if e.get("ats") and e.get("token")]
    items = _interleave_by_ats(items)
    total = len(items)
    if total == 0:
        return [], 0
    if limit >= total:
        return items, 0
    start = cursor % total
    window = [items[(start + i) % total] for i in range(limit)]
    return window, (start + limit) % total


async def _crawl_due(
    limit_companies: int,
    states: dict,
    fresh_db_path,
    build_id: str,
    cursor: int = 0,
    capture_rich: bool = False,
) -> tuple[dict, int]:
    """Crawl the due boards in this run's rotating window, streaming jobs into ``fresh_db_path``.

    Returns (per-board outcome, next_cursor). Jobs are written to the fresh DB as boards complete
    (memory O(in-flight boards), not O(all jobs)). The window bounds each run so it finishes within
    the CI timeout and durably seeds board_state; crash-isolated per board. When ``capture_rich`` is
    set, each board's FULL descriptions are also captured to the fresh DB's ``fresh_rich`` table (the
    index only keeps a snippet) so the incremental rich reconcile can index them — still O(window) on disk.
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
    detail = False  # opt-in: also run the Tier-3 detail reconcile + merge (manual-only; see workflow)
    network_pages = 0  # 0 disables the workable_network bulk feed; >0 = pages to pull
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
        else:
            print(f"unknown flag: {argv[i]}")
            return
    out.mkdir(parents=True, exist_ok=True)
    db = out / "index.sqlite"
    build_id = _build_id()

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
        outcome, next_cursor = anyio.run(
            _crawl_due, limit, states, fresh_path, build_id, cursor, rich
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
        n = build_index_from_fresh_db(
            fresh_path, db_tmp, build_id=build_id, prev_db=prev_db, crawled_keys=crawled_keys
        )
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
        if ok and rich:  # reconcile the rich sidecar from fresh_rich BEFORE fresh.sqlite is deleted
            # NON-FATAL: the rich tier is an optional enhancement. The main index is already gated +
            # promoted above, so an embedding OOM/timeout/model-download failure must NOT crash the
            # build and skip the publish step — log it and carry on (yesterday's rich gz stays live).
            try:
                rstats, rbytes = build_and_publish_rich_incremental(
                    db, fresh_path, out, build_id=build_id
                )
                print(
                    f"  + rich tier (pruned={rstats['pruned']} embedded={rstats['embedded']} "
                    f"missing={rstats['missing']}) -> index-vectors.sqlite.gz ({rbytes // 1024} KB)"
                )
            except Exception as exc:  # noqa: BLE001 - never let the rich tier break the core build
                print(f"  ! rich tier skipped (non-fatal): {type(exc).__name__}: {exc}")
        fresh_path.unlink(missing_ok=True)  # free disk before the shard VACUUMs
        if ok and detail:  # Tier-3 reconcile + merge against the already-published core index
            # NON-FATAL: the detail tier is an optional enhancement, same contract as rich above.
            # The main index is already gated + promoted, so a fetch-dispatcher/merge failure must
            # NOT crash the build or undo the core publish — log it and carry on.
            try:
                dstats, dbytes = build_and_publish_detail(db, out, build_id=build_id)
                print(
                    f"  + detail tier (fetched={dstats['fetched']} failed={dstats['failed']} "
                    f"missing={dstats['missing']} merged={dstats['merged']}) -> "
                    f"index-detail.sqlite.gz ({dbytes // 1024} KB)"
                )
            except Exception as exc:  # noqa: BLE001 - never let the detail tier break the core build
                print(f"  ! detail tier skipped (non-fatal): {type(exc).__name__}: {exc}")
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
        if not ok:
            raise SystemExit(1)
        return

    jobs = anyio.run(_crawl, limit, network_pages)
    db_tmp = out / "index.tmp.sqlite"
    n = build_index(jobs, db_tmp, build_id=build_id)
    full_prev_rows = _count_jobs(db) if db.exists() else None
    if not _gated_publish(
        db_tmp,
        db,
        out,
        build_id=build_id,
        prev_row_count=full_prev_rows,
        last_known_rows=_last_published_rows(out / "history.jsonl"),
    ):
        raise SystemExit(1)
    if sharded:
        ns = build_and_publish_shards(jobs, out, build_id=build_id)
        print(f"  + published {ns} sector shards")
        nslim = build_and_publish_slim(db, out, build_id=build_id)
        print(f"  + published slim tier ({nslim} rows) -> index-slim.sqlite.gz")
    if rich:
        stats, nbytes = build_and_publish_rich(db, jobs, out, build_id=build_id)
        print(
            f"  + published rich tier (pruned={stats['pruned']} embedded={stats['embedded']} "
            f"missing={stats['missing']}) -> index-vectors.sqlite.gz ({nbytes // 1024} KB)"
        )
    if detail:  # Tier-3 reconcile + merge against the already-published core index (non-fatal)
        try:
            dstats, dbytes = build_and_publish_detail(db, out, build_id=build_id)
            print(
                f"  + published detail tier (fetched={dstats['fetched']} failed={dstats['failed']} "
                f"missing={dstats['missing']} merged={dstats['merged']}) -> "
                f"index-detail.sqlite.gz ({dbytes // 1024} KB)"
            )
        except Exception as exc:  # noqa: BLE001 - never let the detail tier break the core build
            print(f"  ! detail tier skipped (non-fatal): {type(exc).__name__}: {exc}")
    print(f"built index: {n} jobs -> {out}/index.sqlite.gz (+manifest.json)")


if __name__ == "__main__":
    main(sys.argv[1:])
