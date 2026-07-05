"""HTTP QUERY serving surface for the job index (RFC 10008).

A dependency-light ASGI app that exposes the index-backed search (the same ``SqliteIndexBackend``
path the MCP uses) over the HTTP ``QUERY`` method — safe, idempotent, body-carrying, cacheable —
with a POST fallback for stacks that don't grok QUERY yet. See :mod:`ergon_tracker.serve.query_app`.
"""

from __future__ import annotations

from .query_app import QueryApp, SearchService, create_app

__all__ = ["QueryApp", "SearchService", "create_app"]
