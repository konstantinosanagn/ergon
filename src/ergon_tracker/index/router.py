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
    """Whether to serve this query from the compact slim tier (~1/3 the cold-start download).

    The slim tier nulls snippet/department (keyword matches THERE are lost — it keeps title+company
    FTS) and years, and has no embeddings. So:
    - ``semantic`` / year-filtered queries can NEVER use slim (it lacks the data) -> always False.
    - broad STRUCTURED-FILTER queries (no keywords) are an EXACT equivalent of the full index -> slim
      always wins (same results, ~1/3 the bytes).
    - broad KEYWORD queries are served from slim ONLY under ``ERGON_INDEX=slim`` (opt-in fast mode):
      a ~3x faster first-query download, at the cost of matching keywords on title+company only
      (description-body/department matches need the full index). Default keeps full recall.
    """
    if query.semantic or query.min_years is not None or query.max_years is not None:
        return False
    if not query.keywords:
        return True
    return os.environ.get("ERGON_INDEX", "").lower() == "slim"


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


# Hard cap on QUERY-TIME document embeddings per rerank. The sidecar covers only ~24% of jobs today,
# so the uncovered remainder of the pool is embedded live during the ramp. Bounding that at 100 keeps
# the cost at or below what the pre-change path already paid (``ranking.rank`` embedded its lexical
# ``top_k = min(len(jobs), 100)``), so this is never a regression, and it falls monotonically toward
# zero as the sidecar fills. Uncovered jobs past the cap are NOT dropped and NOT given a fake cosine
# score: they rank after every cosine-scored job in their incoming lexical (BM25) order.
_UNCOVERED_EMBED_CAP = 100


def _vector_rerank(
    query: SearchQuery, pool: list[JobPosting], want: int
) -> list[JobPosting] | None:
    """Rank ``pool`` by cosine against PRE-STORED vectors; embed only a capped uncovered slice.

    One query embedding instead of ~200 document embeddings. ``vector_search`` silently drops ids the
    sidecar doesn't cover, so during the ramp the uncovered remainder is embedded at query time and
    cosine-scored against the SAME query vector — the covered and freshly-embedded jobs share one
    cosine scale and sort together by true similarity. At most ``_UNCOVERED_EMBED_CAP`` uncovered jobs
    (taken in the pool's incoming lexical/BM25 order) are embedded; any uncovered jobs beyond the cap
    are kept in lexical order and appended AFTER all cosine-scored jobs (never dropped, never given a
    fake score). Returns None when no sidecar is available OR anything in the vector path fails — it
    never raises — so the caller falls back to today's query-time rerank, then lexical order. Never
    builds ``VectorIndex``: the candidate-restricted ``vector_search`` reads only the pool's rows,
    whereas ``VectorIndex`` would materialize every vector as float32 (~2.26 GB at 1.47M jobs).
    """
    path = _rich_path()
    if path is None:
        return None
    try:
        from ..semantic import _cosine
        from .rich import open_rich, vector_search

        reranker = get_semantic_reranker()
        qvec = reranker.embed_query(query.keywords or "")
        con = open_rich(path)
        try:
            scored = dict(
                vector_search(con, qvec, limit=len(pool), candidate_ids=[j.id for j in pool])
            )
        finally:
            con.close()
        # pool is in lexical (BM25) order. Embed at most _UNCOVERED_EMBED_CAP of the uncovered
        # remainder — in that lexical order — on the same cosine scale as the stored vectors.
        uncovered = [j for j in pool if j.id not in scored]
        embed_now, overflow = uncovered[:_UNCOVERED_EMBED_CAP], uncovered[_UNCOVERED_EMBED_CAP:]
        if embed_now:
            for j, dv in zip(embed_now, reranker.embed_jobs(embed_now), strict=True):
                scored[j.id] = _cosine(qvec, dv)
        # covered + freshly embedded (lexical order preserved) -> stable sort by cosine desc.
        cosine_jobs = [j for j in pool if j.id in scored]
        for j in cosine_jobs:
            j.score = scored[j.id]
        cosine_jobs.sort(key=lambda j: j.score if j.score is not None else 0.0, reverse=True)
        for j in overflow:
            j.score = None  # not vector-scored: ranked after all cosine-scored jobs, lexical order
        return (cosine_jobs + overflow)[:want]
    except Exception as exc:  # noqa: BLE001 - vector path is a fast path; None => caller reranks live
        log.warning("vector rerank unavailable (%s); query-time rerank fallback", exc)
        return None


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
        # Rerank a WIDER candidate pool. _vector_rerank prefers PRE-STORED vectors (one query
        # embedding) and embeds only a capped slice of the uncovered remainder. If the sidecar is
        # absent OR anything in the vector path fails it returns None (never raises), so we then fall
        # back to today's behaviour: a query-time rerank (rank), and only then the index's lexical
        # order.
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
