"""Synchronous facade over the async core so casual users never touch asyncio.

Note: these helpers call ``anyio.run`` and therefore must NOT be called from within a running
event loop (e.g. inside ``async def`` or a Jupyter cell). In those contexts use
``AsyncJobSpine`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio

from .client import AsyncJobSpine
from .models import SearchQuery, SearchResult

if TYPE_CHECKING:
    from .registry.resolver import Resolution

__all__ = ["search", "JobSpine"]


async def _run_search(query: SearchQuery, options: dict[str, Any]) -> SearchResult:
    async with AsyncJobSpine(**options) as js:
        return await js.search(query)


def search(
    keywords: str | None = None,
    *,
    location: str | None = None,
    remote: bool | None = None,
    limit: int | None = None,
    companies: list[str] | None = None,
    sources: list[str] | None = None,
    concurrency: int = 16,
    cache: bool = False,
    **query_fields: Any,
) -> SearchResult:
    """One-call synchronous search across all configured sources."""
    query = SearchQuery(
        keywords=keywords,
        location=location,
        remote=remote,
        limit=limit,
        companies=companies,
        sources=sources,
        **query_fields,
    )
    options = {"concurrency": concurrency, "cache": cache}
    return anyio.run(_run_search, query, options)


class JobSpine:
    """Synchronous client. Holds options; each call spins the async core to completion."""

    def __init__(self, *, concurrency: int = 16, cache: bool = False) -> None:
        self._options = {"concurrency": concurrency, "cache": cache}

    def search(self, keywords: str | None = None, **query_fields: Any) -> SearchResult:
        query = SearchQuery(keywords=keywords, **query_fields)
        return anyio.run(_run_search, query, self._options)

    def resolve(self, url_or_host: str) -> Resolution:
        from .registry.resolver import resolve

        return resolve(url_or_host)
