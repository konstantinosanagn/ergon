"""Decide whether a query should be served from the index, and do it safely (never raise)."""

from __future__ import annotations

import logging
import os

from ..models import JobPosting, SearchQuery
from .backend import ShardedIndexBackend, SqliteIndexBackend
from .cache import IndexCache, ShardCache

log = logging.getLogger("ergon_tracker.index")


def _load_sharded(query: SearchQuery) -> ShardedIndexBackend | None:
    """v2 path: download only the shard(s) this query needs, return a sharded backend."""
    shard_dir = ShardCache().ensure(query)
    return ShardedIndexBackend(shard_dir) if shard_dir else None


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
        backend = _load_backend()
        if backend is None or not backend.available():
            return None
        return backend.search(query)
    except Exception as exc:  # noqa: BLE001 - index is a fast path, never a hard dependency
        log.warning("index query failed (%s); live fallback", exc)
        return None
