"""Synchronous facade over the async core so casual users never touch asyncio.

Note: these helpers call ``anyio.run`` and therefore must NOT be called from within a running
event loop (e.g. inside ``async def`` or a Jupyter cell). In those contexts use
``AsyncErgonTracker`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio

from .client import AsyncErgonTracker
from .models import SearchQuery, SearchResult

if TYPE_CHECKING:
    from .registry.resolver import Resolution

__all__ = ["search", "ErgonTracker"]


async def _run_search(query: SearchQuery, options: dict[str, Any]) -> SearchResult:
    async with AsyncErgonTracker(**options) as js:
        # query.max_last_seen_age_days is already fully resolved by the callers below (search() /
        # ErgonTracker.search() both apply the include_stale default before building the query), so
        # tell the client not to re-default it — otherwise an explicit include_stale=True (which
        # resolves to max_last_seen_age_days=None) would get silently clobbered back to 21.
        return await js.search(query, include_stale=True)


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
    include_stale: bool = False,
    **query_fields: Any,
) -> SearchResult:
    """One-call synchronous search across all configured sources.

    include_stale: by default, postings whose board hasn't been re-confirmed in the last 21 days
        (the abandoned/erroring-board tail) are hidden — set True to also see them. Has no effect
        if the caller already passed an explicit ``max_last_seen_age_days=`` in ``query_fields``.
    """
    query_fields.setdefault("max_last_seen_age_days", None if include_stale else 21)
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


class ErgonTracker:
    """Synchronous client. Holds options; each call spins the async core to completion."""

    def __init__(self, *, concurrency: int = 16, cache: bool = False) -> None:
        self._options = {"concurrency": concurrency, "cache": cache}

    def search(
        self, keywords: str | None = None, *, include_stale: bool = False, **query_fields: Any
    ) -> SearchResult:
        """See ``search()`` module function for ``include_stale`` semantics."""
        query_fields.setdefault("max_last_seen_age_days", None if include_stale else 21)
        query = SearchQuery(keywords=keywords, **query_fields)
        return anyio.run(_run_search, query, self._options)

    def resolve(self, url_or_host: str) -> Resolution:
        from .registry.resolver import resolve

        return resolve(url_or_host)
