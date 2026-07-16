"""Rich index tier — vectors-only sidecar: pre-stored quantized embeddings for vector search at scale.

The default index stores only a 300-char ``snippet`` and embeds at query time. This **sidecar tier**,
keyed by job id and built alongside the main index, pre-stores **one embedding per job**, computed once
at build time and **int8-quantized** (cosine is scale-invariant, so int8 holds fidelity within ~1e-3
while cutting a 384-dim vector from 1536 B float32 → ~389 B). Query time embeds only the query and does
a single numpy mat-vec over stored vectors — no per-result model inference.

**Heavy + opt-in**: published as ``index-rich.sqlite.gz``, downloaded only when a query needs semantic
depth, joined to the main index by ``id``. This never bloats the default/slim index and needs no
migration of the main ``jobs`` schema.

(Previously this tier also stored full descriptions in an FTS5 table for full-text search. That was
dropped — the FTS5 ``'rebuild'`` command is O(total rows) *every run*, which grew CI build time
201→243→304 min and would have exceeded the timeout. ``sig`` moved onto ``job_vectors`` so
change-detection survives without it.)
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..models import JobPosting
    from ..semantic import SemanticReranker

__all__ = [
    "RICH_SCHEMA",
    "build_rich_tier",
    "migrate_legacy_rich",
    "reconcile_rich_tier",
    "reconcile_rich_tier_from_fresh",
    "write_fresh_rich",
    "vector_search",
    "VectorIndex",
    "quantize_int8",
    "dequantize_int8",
    "open_rich",
    "rich_meta",
]

RICH_SCHEMA_VERSION = 3  # 3 = vectors-only (job_text/FTS removed); bump to reject stale assets
RICH_SCHEMA = """
CREATE TABLE job_vectors (id TEXT PRIMARY KEY, sig TEXT, scale REAL NOT NULL, vec BLOB NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _sig(job: JobPosting) -> str:
    """Change signal for the RICH tier: content_hash (title/level/location/salary) PLUS the description.

    The main index's content_hash deliberately ignores the description, so a description-only edit isn't
    a "change" there. But the description IS this tier's payload, so we fold it in — the cascade then
    re-embeds whenever anything it stores actually changed."""
    import hashlib

    from .mapping import content_hash

    return hashlib.sha1(f"{content_hash(job)}|{job.description_text or ''}".encode()).hexdigest()[
        :16
    ]


# --- int8 quantization (pure; numpy local so importing this module never requires numpy) ----------
def quantize_int8(vec: list[float]) -> tuple[float, bytes]:
    """Symmetric per-vector int8 quantization → (scale, bytes). ``v ≈ scale * int8``. Cosine is
    scale-invariant, so the per-vector scale never affects ranking — it's kept only for exact dequant."""
    import numpy as np

    a = np.asarray(vec, dtype=np.float32)
    m = float(np.max(np.abs(a))) if a.size else 0.0
    scale = (m / 127.0) or 1.0  # avoid /0 for an all-zero vector
    q = np.clip(np.rint(a / scale), -127, 127).astype(np.int8)
    return scale, q.tobytes()


def dequantize_int8(scale: float, blob: bytes) -> list[float]:
    import numpy as np

    return [float(x) for x in np.frombuffer(blob, dtype=np.int8).astype(np.float32) * scale]


# --- build -----------------------------------------------------------------------------------------
def build_rich_tier(
    jobs: list[JobPosting],
    path: Path | str,
    *,
    build_id: str,
    reranker: SemanticReranker | None = None,
    batch: int = 256,
) -> int:
    """Build the rich sidecar: pre-stored int8 embeddings, keyed by id with ``sig``. Returns row count.

    ``reranker`` is injectable (tests pass a fake to avoid loading the ONNX model); production uses the
    memoized :func:`ergon_tracker.semantic.get_semantic_reranker`. Embedding is batched to bound memory."""
    from ..semantic import _job_text

    p = Path(path)
    p.unlink(missing_ok=True)
    con = sqlite3.connect(str(p))
    try:
        _ensure_schema(con)
        dim, model = _embed_rows_into(
            con,
            [(j.id, _sig(j), _job_text(j)) for j in jobs],
            reranker=reranker,
            batch=batch,
        )
        for k, v in (
            ("build_id", build_id),
            ("dim", str(dim)),
            ("model", model),
            ("quant", "int8"),
        ):
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (k, v))
        con.commit()
        return len(jobs)
    finally:
        con.close()


def reconcile_rich_tier(
    rich_path: Path | str,
    main_index_path: Path | str,
    fresh_jobs: list[JobPosting],
    *,
    build_id: str,
    reranker: SemanticReranker | None = None,
    batch: int = 256,
) -> dict[str, int]:
    """Cascade the rich sidecar to the main index after a build — incremental + efficient.

    The main index is the source of truth for live ids (it already freshness-filters, >5yr-purges, and
    delta-deletes). This keeps the sidecar in lockstep WITHOUT re-embedding the whole corpus daily:
      • **prune** rich rows whose id is gone from main (orphan cleanup — the cascade), and
      • **re-embed only NEW or CHANGED jobs** (``sig`` differs — sig folds content_hash + the full
        description, so a description-only edit re-embeds too), reusing every carried-forward vector.
    ``fresh_jobs`` = this build's crawled postings (the only ids that can be new/changed; carried-forward
    ids are unchanged by definition, so their stored vector stays valid). Returns
    ``{pruned, embedded, missing}`` (``missing`` = live ids in the main index that this tier still can't
    represent because they weren't in ``fresh_jobs`` — a coverage gap worth alerting on)."""
    main = sqlite3.connect(f"file:{main_index_path}?mode=ro", uri=True)
    try:
        live_ids = {
            r[0] for r in main.execute("SELECT id FROM jobs")
        }  # source of truth for what exists
    finally:
        main.close()

    if not Path(rich_path).exists():  # first run: full build (only the ids the main index kept)
        keep = [j for j in fresh_jobs if j.id in live_ids]
        build_rich_tier(keep, rich_path, build_id=build_id, reranker=reranker, batch=batch)
        return {"pruned": 0, "embedded": len(keep), "missing": len(live_ids - {j.id for j in keep})}

    from ..semantic import _job_text

    con = sqlite3.connect(str(rich_path))
    try:
        _ensure_schema(con)  # migrates a legacy (sig-less) sidecar in place before the SELECT below
        have = dict(con.execute("SELECT id, sig FROM job_vectors"))
        orphans = [i for i in have if i not in live_ids]
        _delete_ids(con, orphans)  # the cascade: drop everything the main index dropped

        # re-embed crawled jobs that are live AND new-or-changed (sig folds content_hash + description,
        # so a description-only edit re-embeds too); carried-forward ids keep their stored vector.
        rebuild = [j for j in fresh_jobs if j.id in live_ids and have.get(j.id) != _sig(j)]
        _delete_ids(con, [j.id for j in rebuild])  # clear stale rows before re-inserting
        _embed_rows_into(
            con,
            [(j.id, _sig(j), _job_text(j)) for j in rebuild],
            reranker=reranker,
            batch=batch,
        )
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('build_id', ?)", (build_id,))
        con.commit()

        final_ids = (set(have) - set(orphans)) | {j.id for j in rebuild}
        return {
            "pruned": len(orphans),
            "embedded": len(rebuild),
            "missing": len(
                live_ids - final_ids
            ),  # live in main but not represented here (never crawled)
        }
    finally:
        con.close()


_PARALLEL_MIN = 2000  # below this, multiprocessing spawn overhead outweighs the parallelism


def _auto_parallel(n: int) -> int | None:
    """fastembed ``parallel`` for a batch of ``n``: all-cores (0) only on a dedicated runner (CI=true,
    set by GitHub Actions) for a sizable batch; single-process (None) locally so a laptop build doesn't
    saturate every core. ONNX intra-op threads still apply either way."""
    import os

    if n < _PARALLEL_MIN:
        return None
    return 0 if os.environ.get("CI") else None


# --- incremental (streaming cron) path: capture fresh embed text on disk, reconcile from it --------
FRESH_RICH_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fresh_rich (id TEXT PRIMARY KEY, sig TEXT, embed_text TEXT)"
)


def write_fresh_rich(con: sqlite3.Connection, jobs: list[JobPosting]) -> None:
    """Capture ``(id, sig, embed_text)`` for freshly-crawled jobs into the streaming fresh DB — on
    disk, bounded to the crawl window, never holding jobs in memory. ``embed_text`` is the exact
    representation :func:`semantic._job_text` embeds, so a stored vector matches a query-time rerank.
    (``description`` was only ever FTS input; dropping it shrinks the fresh DB's I/O per run.)"""
    from ..semantic import _job_text

    con.execute(FRESH_RICH_SCHEMA)
    con.executemany(
        "INSERT OR REPLACE INTO fresh_rich(id, sig, embed_text) VALUES(?, ?, ?)",
        [(j.id, _sig(j), _job_text(j)) for j in jobs],
    )


def _embed_rows_into(
    con: sqlite3.Connection,
    rows: list[tuple[str, str, str]],
    *,
    reranker: SemanticReranker | None,
    batch: int,
    single_process: bool = False,
) -> tuple[int, str]:
    """Embed ``(id, sig, embed_text)`` rows (streamed) → upsert int8 vectors. Returns (dim, model).

    ``single_process=True`` hardwires ``parallel=None`` — no fastembed worker processes, ever (each
    worker loads its own ~67MB ONNX model). The reconcile path always sets it."""
    if not rows:
        return 0, getattr(reranker, "model_name", "?")
    if reranker is None:
        from ..semantic import get_semantic_reranker

        reranker = get_semantic_reranker()
    par = None if single_process else _auto_parallel(len(rows))
    texts = [r[2] for r in rows]
    dim = 0
    buf: list[tuple[str, str, float, bytes]] = []
    for (jid, sig, _), vec in zip(
        rows, reranker.embed_texts_iter(texts, batch_size=batch, parallel=par), strict=True
    ):
        scale, blob = quantize_int8(vec)
        dim = dim or len(vec)
        buf.append((jid, sig, scale, blob))
        if len(buf) >= 1000:
            con.executemany(
                "INSERT OR REPLACE INTO job_vectors(id, sig, scale, vec) VALUES(?, ?, ?, ?)", buf
            )
            buf = []
    if buf:
        con.executemany(
            "INSERT OR REPLACE INTO job_vectors(id, sig, scale, vec) VALUES(?, ?, ?, ?)", buf
        )
    return dim, getattr(reranker, "model_name", "?")


_RAMP_DEFAULT_CI = 120_000  # cold-start bound on a CI runner when ERGON_RICH_MAX_EMBED is unset


def _resolve_max_embed() -> int | None:
    """Embed budget per reconcile run: ``ERGON_RICH_MAX_EMBED`` if set, else a bounded default on CI
    (a cold start over a full crawl window must ramp across runs, not embed ~1M rows in one job),
    else unlimited locally."""
    import os

    env = os.environ.get("ERGON_RICH_MAX_EMBED", "").strip()
    if env:
        return int(env)
    return _RAMP_DEFAULT_CI if os.environ.get("CI") else None


def _resolve_backfill_from_index() -> bool:
    """``ERGON_RICH_BACKFILL_FROM_INDEX=1`` enables the coverage accelerator (below). Default off so
    normal cron keeps the full-quality crawl-window path; set it on a few 'catch-up' runs to embed
    the un-vectored backlog directly and reach ~100% coverage in 1-2 runs instead of ~7."""
    import os

    return os.environ.get("ERGON_RICH_BACKFILL_FROM_INDEX", "").strip() == "1"


def _embed_text_from_row(title: str | None, department: str | None, snippet: str | None) -> str:
    """Backfill embed-text, mirroring semantic._job_text but sourced from the main index (snippet,
    ~300 chars) instead of the fresh crawl's 600-char description. Same title — dept — text shape."""
    parts = [title or ""]
    if department:
        parts.append(department)
    if snippet:
        parts.append(snippet)
    return " — ".join(p for p in parts if p)


def _backfill_from_index(
    con: sqlite3.Connection,
    main_index_path: Path | str,
    *,
    skip_ids: set[str],
    budget: int | None,
    reranker: SemanticReranker | None,
    batch: int,
    chunk_size: int,
) -> tuple[int, set[str]]:
    """COVERAGE ACCELERATOR: embed live index rows that have NO vector yet, sourced DIRECTLY from the
    main index (title — department — snippet), instead of waiting for their board to rotate into the
    crawl window (the crawl-coupled path drains the tail only ~30k/run). ``sig`` is
    ``sha1(content_hash | snippet)`` — STABLE (idempotent across catch-up runs) yet DIFFERENT from the
    crawl path's ``sha1(content_hash | full_description)`` sig, so each row self-UPGRADES to a
    full-description vector the next time its board is crawled. Streams the main index with
    ``fetchmany`` (peak memory O(chunk), never the corpus). Returns ``(embedded, ids)``.
    """
    if budget is not None and budget <= 0:
        return 0, set()
    import hashlib

    embedded = 0
    done: set[str] = set()
    main = sqlite3.connect(f"file:{main_index_path}?mode=ro", uri=True)
    try:
        cur = main.execute(
            "SELECT id, title, department, snippet, content_hash FROM jobs WHERE status = 'active'"
        )
        while budget is None or embedded < budget:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            eligible: list[tuple[str, str, str]] = []
            for jid, title, dept, snippet, chash in rows:
                if jid in skip_ids:
                    continue
                text = _embed_text_from_row(title, dept, snippet)
                if not text:
                    continue  # no title AND no snippet -> nothing to embed
                sig = hashlib.sha1(f"{chash or ''}|{snippet or ''}".encode()).hexdigest()[:16]
                eligible.append((jid, sig, text))
            if budget is not None and len(eligible) > budget - embedded:
                eligible = eligible[: budget - embedded]
            if eligible:
                _embed_rows_into(con, eligible, reranker=reranker, batch=batch, single_process=True)
                con.commit()
                embedded += len(eligible)
                done.update(r[0] for r in eligible)
    finally:
        main.close()
    return embedded, done


def _shard_of(id_: str, num_shards: int) -> int:
    """Deterministic shard for a posting id: ``sha1(id) mod num_shards``. Pure function of the id, so
    every id belongs to exactly ONE shard across every run/runner -- the invariant that makes the
    sharded embed's 20 partials DISJOINT (clean union at merge) AND makes the sharded result provably
    identical to a single-runner run (sharding only changes WHICH runner embeds a row, never the
    row's embedding, which is deterministic given the model + text). No quality loss, by construction."""
    return int(hashlib.sha1(id_.encode("utf-8")).hexdigest(), 16) % num_shards


def reconcile_rich_tier_from_fresh(
    rich_path: Path | str,
    main_index_path: Path | str,
    fresh_db_path: Path | str,
    *,
    build_id: str,
    reranker: SemanticReranker | None = None,
    batch: int = 256,
    chunk_size: int = 10_000,
    max_embed_per_run: int | None | Literal["auto"] = "auto",
    backfill_from_index: bool | Literal["auto"] = "auto",
    shard: int | None = None,
    num_shards: int | None = None,
) -> dict[str, int]:
    """Incremental (cron) cascade — same contract as :func:`reconcile_rich_tier` but reads the freshly-
    crawled ``(id, sig, embed_text)`` from the streaming fresh DB (disk, memory-safe via
    :func:`write_fresh_rich`) instead of an in-memory job list. The main index is the source of truth
    for live ids; orphans are pruned; only new/changed fresh rows (``sig`` differs) re-embed; every
    carried-forward id keeps the vector already in the persisted sidecar. Returns
    ``{pruned, embedded, missing}`` (``missing`` = live ids not yet represented — they fill in as the
    rotating crawl window reaches them).

    **Memory model (the OOM fix for run 28070765535):** ``fresh_rich`` is never ``fetchall()``'d —
    at ~1.2M rows that would be several GB resident. Instead the cursor is drained with
    ``fetchmany(chunk_size)`` and each chunk is fully processed (filter → delete stale → embed via
    :func:`_embed_rows_into` with ``single_process=True``, so no per-worker model copies) before the
    next fetch: peak memory is O(chunk), not O(corpus). The ``have`` ``{id: sig}`` map *is* held in
    memory deliberately — two short strings per row is ~150MB at 1.4M ids, a fine trade for O(1)
    change detection versus a per-row SQL lookup.

    **Bounded cold-start ramp:** ``max_embed_per_run`` caps total embedded rows across chunks
    (``"auto"`` → ``ERGON_RICH_MAX_EMBED`` env, else 120k on CI, else unlimited). Once the cap hits,
    remaining new/changed rows are skipped — they count into ``missing`` and are picked up on the
    next run via the same sig comparison, so a cold start converges over a few runs instead of
    OOMing one."""
    max_embed = _resolve_max_embed() if max_embed_per_run == "auto" else max_embed_per_run
    do_backfill = (
        _resolve_backfill_from_index() if backfill_from_index == "auto" else backfill_from_index
    )
    if (shard is None) != (num_shards is None):
        raise ValueError("shard and num_shards must be given together (or neither)")
    sharded = num_shards is not None
    if sharded:
        if not (0 <= shard < num_shards):  # type: ignore[operator]
            raise ValueError(f"shard must be in [0, num_shards); got {shard}/{num_shards}")
        if do_backfill:
            # The backfill accelerator streams the WHOLE index; unsharded it would embed the full
            # backlog on EVERY shard (20x duplicate work + non-disjoint partials that break the merge
            # union). It's an opt-in unsharded-only path -- fail loud rather than silently corrupt.
            raise ValueError("backfill_from_index is not supported with sharding; run it unsharded")

    main = sqlite3.connect(f"file:{main_index_path}?mode=ro", uri=True)
    try:
        live_ids = {r[0] for r in main.execute("SELECT id FROM jobs")}
    finally:
        main.close()
    if sharded:
        live_ids = {i for i in live_ids if _shard_of(i, num_shards) == shard}  # type: ignore[arg-type]

    con = sqlite3.connect(str(rich_path))
    try:
        _ensure_schema(con)  # fresh DB -> create; legacy (sig-less) sidecar -> migrate in place
        # id→sig for every row already in the sidecar. Held in memory by design: ~150MB at 1.4M rows
        # (two short strings each) buys O(1) new/changed detection per fresh row (see docstring).
        have = dict(con.execute("SELECT id, sig FROM job_vectors"))
        if sharded:
            # The shard workflow seeds this partial by copying the FULL prev sidecar (carry-forward).
            # Drop every row NOT in this shard's slice so the output partial is scoped to shard
            # `shard` alone -- disjoint from every other shard, so the merge is a clean union. With
            # `live_ids` already slice-filtered above, the whole embed pipeline below then operates on
            # this slice only (the `r[0] in live_ids` chunk filter admits slice ids exclusively).
            _delete_ids(con, [i for i in have if _shard_of(i, num_shards) != shard])  # type: ignore[arg-type]
            have = {i: s for i, s in have.items() if _shard_of(i, num_shards) == shard}  # type: ignore[arg-type]
        orphans = [i for i in have if i not in live_ids]
        _delete_ids(con, orphans)

        embedded = 0
        rebuilt_ids: set[str] = set()
        deferred_ids: set[str] = set()
        dim, model = 0, ""
        fresh = sqlite3.connect(f"file:{fresh_db_path}?mode=ro", uri=True)
        try:
            try:
                cur = fresh.execute("SELECT id, sig, embed_text FROM fresh_rich")
            except sqlite3.OperationalError:
                cur = None  # capture was off this run → prune-only reconcile
            _tag = f"embed shard {shard}/{num_shards}" if sharded else "rich embed"
            _t_start = time.monotonic()
            _t_last = _t_start
            while cur is not None:
                rows = cur.fetchmany(chunk_size)
                if not rows:
                    break
                # new-or-changed live rows only (fresh_rich ids are unique, so no cross-chunk dupes)
                chunk = [r for r in rows if r[0] in live_ids and have.get(r[0]) != r[1]]
                if max_embed is not None:
                    budget = max_embed - embedded
                    if budget <= 0:
                        deferred_ids.update(r[0] for r in chunk)
                        continue
                    if len(chunk) > budget:
                        deferred_ids.update(r[0] for r in chunk[budget:])
                        chunk = chunk[:budget]
                if not chunk:
                    continue
                _delete_ids(con, [r[0] for r in chunk])  # clear stale rows before re-inserting
                d, m = _embed_rows_into(
                    con,
                    [(r[0], r[1], r[2]) for r in chunk],
                    reranker=reranker,
                    batch=batch,
                    single_process=True,  # never fork per-chunk model copies on the reconcile path
                )
                dim, model = dim or d, m or model
                rebuilt_ids.update(r[0] for r in chunk)
                embedded += len(chunk)
                # Live progress on the long embed -- the workflow step log streams this, so throttle
                # to ~every 20s: readable, and shows RATE + running total instead of a silent hang
                # (the observability gap that made build ETAs a guess).
                _now = time.monotonic()
                if _now - _t_last >= 20.0:
                    _el = _now - _t_start
                    print(
                        f"[{_tag}] embedded {embedded} rows | {embedded / _el if _el else 0:.0f}/s "
                        f"| {_el:.0f}s elapsed",
                        flush=True,
                    )
                    _t_last = _now
        finally:
            fresh.close()
        if embedded:
            print(
                f"[{_tag}] embed complete: {embedded} rows in {time.monotonic() - _t_start:.0f}s",
                flush=True,
            )

        # COVERAGE ACCELERATOR (opt-in): after the crawl-window path, spend any remaining embed
        # budget on live rows that STILL have no vector, sourced straight from the main index —
        # so the tail reaches ~100% in 1-2 runs instead of waiting ~7 for boards to rotate in.
        if do_backfill:
            remaining = None if max_embed is None else max(0, max_embed - embedded)
            bf_embedded, bf_ids = _backfill_from_index(
                con,
                main_index_path,
                skip_ids=set(have) | rebuilt_ids,  # already-vectored + this run's rebuilds
                budget=remaining,
                reranker=reranker,
                batch=batch,
                chunk_size=chunk_size,
            )
            embedded += bf_embedded
            rebuilt_ids.update(bf_ids)
            if bf_embedded:
                print(f"rich backfill-from-index: embedded {bf_embedded} un-vectored backlog rows")

        meta = [("build_id", build_id), ("quant", "int8")]
        if dim:
            meta += [("dim", str(dim)), ("model", model)]
        con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", meta)
        con.commit()
        if deferred_ids:
            print(f"rich ramp: embedded {embedded}, deferred {len(deferred_ids)} to next run")
        # Represented = carried-forward rows minus anything deferred-with-stale-content, plus this
        # run's rebuilds. A deferred id already in `have` keeps its stale row but is NOT represented
        # (counts into missing, like a deferred brand-new id); the sig mismatch re-selects it next run.
        final_ids = ((set(have) - set(orphans)) - deferred_ids) | rebuilt_ids
        return {
            "pruned": len(orphans),
            "embedded": embedded,
            "missing": len(live_ids - final_ids),
        }
    finally:
        con.close()


def _has_schema(con: sqlite3.Connection) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_vectors'"
        ).fetchone()
    )


def migrate_legacy_rich(con: sqlite3.Connection) -> int:
    """Upgrade a legacy sidecar (job_text + FTS + sig-less job_vectors) to the vectors-only schema.

    Carries every already-computed embedding forward — sigs come from ``job_text`` when present — so a
    ramp that cost hours of CI embedding is never recomputed. The ``sig`` column is added whenever it's
    missing from ``job_vectors``, independent of whether ``job_text`` exists (``_has_schema`` matches on
    the table name alone, so a legacy DB with no ``sig`` column would otherwise slip past the schema
    guard and blow up on ``SELECT id, sig FROM job_vectors``). Idempotent: returns 0 once migrated.
    Runs on the CI runner as part of the reconcile; never on a developer machine."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "job_vectors" not in tables:
        return 0
    has_sig = "sig" in {r[1] for r in con.execute("PRAGMA table_info(job_vectors)")}
    if not has_sig:
        con.execute("ALTER TABLE job_vectors ADD COLUMN sig TEXT")
    moved = 0
    if "job_text" in tables:
        moved = con.execute(
            "UPDATE job_vectors SET sig = (SELECT t.sig FROM job_text t WHERE t.id = job_vectors.id) "
            "WHERE sig IS NULL"
        ).rowcount
        con.execute("DROP TABLE IF EXISTS job_text_fts")
        con.execute("DROP TABLE IF EXISTS job_text")
        con.execute("DROP INDEX IF EXISTS idx_job_text_sig")
        con.commit()
        con.execute("VACUUM")  # reclaim the ~87% the text+FTS occupied
    elif not has_sig:
        con.commit()  # bare ALTER TABLE on a schema with no job_text to backfill from
    return moved


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the vectors-only schema if absent, else migrate a legacy sidecar in place.

    ``_has_schema`` only checks that a ``job_vectors`` table exists by name — it can't tell a fresh
    vectors-only DB from a legacy sig-less one — so every builder must route through here rather than
    trusting that check alone."""
    if not _has_schema(con):
        con.executescript(RICH_SCHEMA)
        return
    migrate_legacy_rich(con)


class VectorIndex:
    """Preloaded, pre-normalized vector matrix for fast REPEATED cosine search (the serving path).

    Loads ``job_vectors`` once into an in-memory float32 matrix with rows L2-normalized, so each search
    is a single BLAS matmul (multi-threaded) + argsort — no per-query SQL read or re-decode. Pass
    ``candidate_ids`` (e.g. the ids surviving the main index's SQL filters) to matmul only that subset,
    so a query rarely touches the whole corpus."""

    def __init__(self, con: sqlite3.Connection) -> None:
        import numpy as np

        rows = con.execute("SELECT id, vec FROM job_vectors").fetchall()
        self.ids = [r[0] for r in rows]
        self._pos = {i: k for k, i in enumerate(self.ids)}
        if rows:
            dim = len(rows[0][1])
            mat = (
                np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8)
                .reshape(len(rows), dim)
                .astype(np.float32)
            )
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matn = mat / norms  # pre-normalized once → each search is a bare matmul
        else:
            self._matn = np.zeros((0, 0), dtype=np.float32)

    def search(
        self, query_vec: list[float], *, limit: int = 50, candidate_ids: list[str] | None = None
    ) -> list[tuple[str, float]]:
        import numpy as np

        if self._matn.shape[0] == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        q = q / (float(np.linalg.norm(q)) or 1.0)
        if candidate_ids is not None:
            idx = [self._pos[i] for i in candidate_ids if i in self._pos]
            if not idx:
                return []
            scores = self._matn[idx] @ q
            order = np.argsort(-scores)[:limit]
            return [(self.ids[idx[int(k)]], float(scores[int(k)])) for k in order]
        scores = self._matn @ q
        order = np.argsort(-scores)[:limit]
        return [(self.ids[int(k)], float(scores[int(k)])) for k in order]


def _delete_ids(con: sqlite3.Connection, ids: list[str]) -> None:
    for i in range(0, len(ids), 500):  # chunk to stay under SQLite's variable limit
        chunk = ids[i : i + 500]
        ph = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM job_vectors WHERE id IN ({ph})", chunk)  # noqa: S608 - ph is placeholders


# --- query ----------------------------------------------------------------------------------------
def open_rich(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rich_meta(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key, value FROM meta").fetchall())


def vector_search(
    con: sqlite3.Connection,
    query_vec: list[float],
    *,
    limit: int = 50,
    candidate_ids: list[str] | None = None,
) -> list[tuple[str, float]]:
    """Cosine-rank stored job vectors against ``query_vec`` → [(id, score)] desc. Single numpy mat-vec.

    ``candidate_ids`` restricts the search to a pre-filtered set (e.g. after level/sector/geo SQL filters
    on the main index), so vector ranking composes with structured filters instead of replacing them."""
    import numpy as np

    if candidate_ids is not None:
        cand = list(candidate_ids)
        if not cand:
            return []
        rows = con.execute(
            f"SELECT id, vec FROM job_vectors WHERE id IN ({','.join('?' * len(cand))})", cand
        ).fetchall()
    else:
        rows = con.execute("SELECT id, vec FROM job_vectors").fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    dim = len(rows[0][1])  # int8 → 1 byte per dim
    mat = (
        np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8)
        .reshape(len(rows), dim)
        .astype(np.float32)
    )
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q)) or 1.0
    scores = (
        mat @ (q / qn)
    ) / norms  # = cosine(doc, query); per-vector int8 scale cancels in cosine
    order = np.argsort(-scores)[:limit]
    return [(ids[int(i)], float(scores[int(i)])) for i in order]
