"""Rich index tier — full descriptions (FTS) + pre-stored quantized embeddings (vector search).

The default index stores only a 300-char ``snippet`` and embeds at query time. This **sidecar tier**,
keyed by job id and built alongside the main index, closes two gaps:

1. **Full-text over whole job descriptions** — the complete ``description_text`` in its own FTS5 table,
   so a keyword search matches the entire posting, not just the first 300 chars.
2. **Pre-stored embeddings (vector search at scale)** — one embedding per job, computed once at build
   time and **int8-quantized** (cosine is scale-invariant, so int8 holds fidelity within ~1e-3 while
   cutting a 384-dim vector from 1536 B float32 → ~389 B). Query time embeds only the query and does a
   single numpy mat-vec over stored vectors — no per-result model inference.

Both are **heavy + opt-in**: published as ``index-rich.sqlite.gz``, downloaded only when a query needs
full-text/semantic depth, joined to the main index by ``id``. This never bloats the default/slim index
and needs no migration of the main ``jobs`` schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import JobPosting
    from ..semantic import SemanticReranker

__all__ = [
    "RICH_SCHEMA",
    "build_rich_tier",
    "reconcile_rich_tier",
    "fulltext_search",
    "vector_search",
    "VectorIndex",
    "quantize_int8",
    "dequantize_int8",
    "open_rich",
    "rich_meta",
]

RICH_SCHEMA = """
CREATE TABLE job_text (rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, sig TEXT, description TEXT);
CREATE VIRTUAL TABLE job_text_fts USING fts5(
  description, content='job_text', content_rowid='rowid',
  tokenize="porter unicode61 remove_diacritics 2"
);
CREATE TABLE job_vectors (id TEXT PRIMARY KEY, scale REAL NOT NULL, vec BLOB NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_job_text_sig ON job_text(sig);
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
    """Build the rich sidecar: full descriptions + FTS + pre-stored int8 embeddings. Returns row count.

    ``reranker`` is injectable (tests pass a fake to avoid loading the ONNX model); production uses the
    memoized :func:`ergon_tracker.semantic.get_semantic_reranker`. Embedding is batched to bound memory."""
    p = Path(path)
    p.unlink(missing_ok=True)
    con = sqlite3.connect(str(p))
    try:
        con.executescript(RICH_SCHEMA)
        _upsert_text(con, jobs)
        con.execute(
            "INSERT INTO job_text_fts(job_text_fts) VALUES('rebuild')"
        )  # external-content FTS
        dim, model = _embed_into(con, jobs, reranker=reranker, batch=batch)
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
    ids are unchanged by definition, so their stored text+vector stay valid). Returns
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

    con = sqlite3.connect(str(rich_path))
    try:
        have = dict(con.execute("SELECT id, sig FROM job_text"))
        orphans = [i for i in have if i not in live_ids]
        _delete_ids(con, orphans)  # the cascade: drop everything the main index dropped

        # re-embed crawled jobs that are live AND new-or-changed (sig folds content_hash + description,
        # so a description-only edit re-embeds too); carried-forward ids keep their stored text+vector.
        rebuild = [j for j in fresh_jobs if j.id in live_ids and have.get(j.id) != _sig(j)]
        _delete_ids(con, [j.id for j in rebuild])  # clear stale rows before re-inserting
        _upsert_text(con, rebuild)
        _embed_into(con, rebuild, reranker=reranker, batch=batch)
        con.execute("INSERT INTO job_text_fts(job_text_fts) VALUES('rebuild')")
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


def _upsert_text(con: sqlite3.Connection, jobs: list[JobPosting]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO job_text(id, sig, description) VALUES(?, ?, ?)",
        [(j.id, _sig(j), j.description_text or "") for j in jobs],
    )


_PARALLEL_MIN = 2000  # below this, multiprocessing spawn overhead outweighs the parallelism


def _embed_into(
    con: sqlite3.Connection,
    jobs: list[JobPosting],
    *,
    reranker: SemanticReranker | None,
    batch: int,
    parallel: int | None = None,
) -> tuple[int, str]:
    """Data-parallel embed + STREAM-quantize-store int8 vectors (memory-bounded). Returns (dim, model).

    Concurrency: embedding is the CPU-bound cost; we hand the whole corpus to fastembed with
    ``parallel`` (auto = all cores for a sizable build, single for small/test sets) so ONNX inference
    fans across worker processes, and we consume the result stream — never materializing all vectors."""
    if not jobs:
        return 0, getattr(reranker, "model_name", "?")
    if reranker is None:
        from ..semantic import get_semantic_reranker

        reranker = get_semantic_reranker()
    par = parallel if parallel is not None else (0 if len(jobs) >= _PARALLEL_MIN else None)
    dim = 0
    buf: list[tuple[str, float, bytes]] = []
    for job, vec in reranker.embed_jobs_iter(jobs, batch_size=batch, parallel=par):
        scale, blob = quantize_int8(vec)
        dim = dim or len(vec)
        buf.append((job.id, scale, blob))
        if len(buf) >= 1000:  # bounded write batches -> bounded memory regardless of corpus size
            con.executemany(
                "INSERT OR REPLACE INTO job_vectors(id, scale, vec) VALUES(?, ?, ?)", buf
            )
            buf = []
    if buf:
        con.executemany("INSERT OR REPLACE INTO job_vectors(id, scale, vec) VALUES(?, ?, ?)", buf)
    return dim, getattr(reranker, "model_name", "?")


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
        con.execute(f"DELETE FROM job_text WHERE id IN ({ph})", chunk)  # noqa: S608


# --- query ----------------------------------------------------------------------------------------
def open_rich(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def rich_meta(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key, value FROM meta").fetchall())


def fulltext_search(con: sqlite3.Connection, query: str, *, limit: int = 50) -> list[str]:
    """Job ids whose FULL description matches ``query`` (FTS5 bm25-ranked) — not just the snippet."""
    from .query import _match_expr

    match = _match_expr(query) if query else ""
    if not match:
        return []
    rows = con.execute(
        "SELECT t.id FROM job_text_fts f JOIN job_text t ON t.rowid = f.rowid "
        "WHERE job_text_fts MATCH ? ORDER BY bm25(job_text_fts) LIMIT ?",
        (match, limit),
    ).fetchall()
    return [r[0] for r in rows]


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
