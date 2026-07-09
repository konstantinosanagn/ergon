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

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..models import JobPosting
    from ..semantic import SemanticReranker

__all__ = [
    "RICH_SCHEMA",
    "build_rich_tier",
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
        con.executescript(RICH_SCHEMA)
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

    main = sqlite3.connect(f"file:{main_index_path}?mode=ro", uri=True)
    try:
        live_ids = {r[0] for r in main.execute("SELECT id FROM jobs")}
    finally:
        main.close()

    con = sqlite3.connect(str(rich_path))
    try:
        if not Path(rich_path).exists() or not _has_schema(con):
            con.executescript(RICH_SCHEMA)
        # id→sig for every row already in the sidecar. Held in memory by design: ~150MB at 1.4M rows
        # (two short strings each) buys O(1) new/changed detection per fresh row (see docstring).
        have = dict(con.execute("SELECT id, sig FROM job_vectors"))
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
        finally:
            fresh.close()

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
