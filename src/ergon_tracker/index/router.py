"""Decide whether a query should be served from the index, and do it safely (never raise)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..models import JobPosting, SearchQuery
from ..semantic import get_semantic_reranker
from .backend import ShardedIndexBackend, SqliteIndexBackend
from .cache import IndexCache, RichCache, ShardCache, SlimCache

log = logging.getLogger("ergon_tracker.index")


def _load_sharded(query: SearchQuery) -> ShardedIndexBackend | None:
    """v2 path: download only the shard(s) this query needs, return a sharded backend."""
    shard_dir = ShardCache().ensure(query)
    return ShardedIndexBackend(shard_dir) if shard_dir else None


def _slim_serves(query: SearchQuery) -> bool:
    """True when the slim tier returns IDENTICAL results to the full index for this query.

    The slim tier nulls snippet/description (so keyword matches in the description would be lost)
    and years (so year filters can't apply) and skips semantic embeddings. It is therefore exactly
    equivalent to the full index only for broad STRUCTURED-FILTER queries: no keywords, no year
    filter, no semantic rerank. Those download ~half the bytes with zero recall loss.
    """
    return (
        not query.keywords
        and not query.semantic
        and query.min_years is None
        and query.max_years is None
    )


def _load_slim() -> SqliteIndexBackend | None:
    """v2 path: the compact slim broad-query tier (~half the full-file download)."""
    path = SlimCache().ensure_fresh()
    return SqliteIndexBackend(path) if path else None


def _load_backend() -> SqliteIndexBackend | None:
    """v1 path: the single-file index."""
    path = IndexCache().ensure_fresh()
    return SqliteIndexBackend(path) if path else None


def try_index(query: SearchQuery) -> list[JobPosting] | None:
    """Return index results for a broad query, or None to signal 'fall back to live'.

    Preference order: sector-sharded index (v2, sector queries only) -> single-file index (v1)
    -> live (None).
    """
    if os.environ.get("ERGON_INDEX", "").lower() == "off":
        return None
    if query.companies or query.sources:  # targeted => live (fresher, already fast)
        return None
    try:
        # The sharded path only wins for SECTOR-scoped queries (download one small shard). A
        # broad/cross-sector query would have to pull every shard — slower than the single-file
        # index's one download + single global FTS rank — so skip straight to single-file.
        if query.sector:
            sharded = _load_sharded(query)
            if sharded is not None and sharded.available():
                return sharded.search(query)
        # Broad structured-filter query (no keywords/years/semantic): the slim tier is an exact,
        # smaller-download equivalent of the full index — prefer it, fall through to full if absent.
        if _slim_serves(query):
            slim = _load_slim()
            if slim is not None and slim.available():
                return slim.search(query)
        backend = _load_backend()
        if backend is None or not backend.available():
            return None
        return backend.search(query)
    except Exception as exc:  # noqa: BLE001 - index is a fast path, never a hard dependency
        log.warning("index query failed (%s); live fallback", exc)
        return None


def _rich_path() -> Path | None:
    """Path to the cached vectors sidecar, or None (caller reranks at query time)."""
    return RichCache().ensure_fresh()


def _vector_rerank(
    query: SearchQuery, pool: list[JobPosting], want: int
) -> list[JobPosting] | None:
    """Rank ``pool`` by cosine against PRE-STORED vectors; embed only the uncovered remainder.

    One query embedding instead of ~200 document embeddings. ``vector_search`` silently drops ids the
    sidecar doesn't cover, so during the ramp the uncovered remainder is embedded at query time and
    scored by cosine against the SAME query vector — both score sets share one cosine scale and sort
    together, so an uncovered job is interleaved by its true similarity (never dropped, never forced
    to an arbitrary end). Returns None when no sidecar is available (caller falls back to query-time
    rerank / lexical order). Never builds ``VectorIndex`` — the candidate-restricted ``vector_search``
    reads only the pool's rows, whereas ``VectorIndex`` would materialize every vector as float32
    (~2.26 GB at 1.47M jobs).
    """
    path = _rich_path()
    if path is None:
        return None
    from ..semantic import _cosine
    from .rich import open_rich, vector_search

    reranker = get_semantic_reranker()
    qvec = reranker.embed_query(query.keywords or "")
    con = open_rich(path)
    try:
        scored = dict(vector_search(con, qvec, limit=len(pool), candidate_ids=[j.id for j in pool]))
    finally:
        con.close()
    uncovered = [j for j in pool if j.id not in scored]
    if uncovered:  # ramp not finished: embed the remainder at query time (same cosine scale)
        for j, dv in zip(uncovered, reranker.embed_jobs(uncovered), strict=True):
            scored[j.id] = _cosine(qvec, dv)
    for j in pool:
        j.score = scored.get(j.id, 0.0)
    return sorted(pool, key=lambda j: j.score if j.score is not None else 0.0, reverse=True)[:want]


def try_index_ranked(query: SearchQuery) -> list[JobPosting] | None:
    """try_index + semantic rerank when query.semantic — the full index serving path.

    Shared by BOTH the live engine and the MCP server so a broad ``semantic=True`` query is
    embedding-reranked no matter which surface issues it (the index itself only ranks lexically via
    BM25). On any rerank failure (e.g. the optional fastembed extra is absent) it degrades to the
    index's lexical order. Returns None to signal 'fall back to live' exactly like try_index.
    """
    indexed = try_index(query)
    if indexed is None:
        return None
    if query.semantic and query.keywords and len(indexed) > 1:
        # Rerank a WIDER candidate pool. Prefer PRE-STORED vectors (one query embedding); fall back
        # to query-time document embedding, then to the index's lexical order.
        try:
            from ..ranking import rank
            from ..semantic import get_semantic_reranker  # local re-lookup: the fallback stays
            # patchable via the semantic module (see test_try_index_ranked_semantic_degrades_gracefully)

            want = query.limit or 20
            pool = try_index(query.model_copy(update={"limit": max(want * 10, 200)})) or indexed
            ranked = _vector_rerank(query, pool, want)
            indexed = (
                ranked
                if ranked is not None
                else rank(pool, query.keywords, reranker=get_semantic_reranker())[:want]
            )
        except Exception as exc:  # noqa: BLE001 - reranker optional; lexical order is fine
            log.warning("semantic rerank on index unavailable (%s); lexical order", exc)
    return indexed
