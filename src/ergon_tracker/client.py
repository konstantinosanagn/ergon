"""Async core: ``AsyncErgonTracker`` holds the fetcher + provider registry and runs searches.

The actual orchestration lives in ``search.py`` (Phase 2) and resolution in
``registry/resolver.py`` (Phase 1); both are imported lazily so the package imports cleanly
while those modules are still being built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .http import AsyncFetcher
from .models import SearchQuery, SearchResult
from .providers.base import load_builtins, load_plugins

if TYPE_CHECKING:
    from .registry.resolver import Resolution

__all__ = ["AsyncErgonTracker"]

_providers_loaded = False


def _ensure_providers_loaded() -> None:
    global _providers_loaded
    if not _providers_loaded:
        load_builtins()
        load_plugins()
        _providers_loaded = True


class AsyncErgonTracker:
    def __init__(
        self,
        *,
        fetcher: AsyncFetcher | None = None,
        concurrency: int = 16,
        cache: bool = False,
    ) -> None:
        _ensure_providers_loaded()
        self._fetcher = fetcher or AsyncFetcher(concurrency=concurrency, cache=cache)

    async def search(self, query: SearchQuery, *, include_stale: bool = False) -> SearchResult:
        """Run ``query`` against the configured providers.

        include_stale: index-served staleness guard. By default (False), if the caller didn't
            already set ``query.max_last_seen_age_days``, it's defaulted to 21 days — hiding
            postings whose board hasn't been re-confirmed recently (the abandoned/erroring-board
            tail). Pass True to leave an explicit ``None`` on the query untouched (e.g. when a
            higher-level caller already resolved the guard itself and wants no further defaulting).
        """
        from .engine import run_search  # lazy import avoids the search-name collision

        if not include_stale and query.max_last_seen_age_days is None:
            query = query.model_copy(update={"max_last_seen_age_days": 21})
        result: SearchResult = await run_search(query, self._fetcher)
        return result

    def resolve(self, url_or_host: str) -> Resolution:
        from .registry.resolver import resolve  # lazy: implemented in Phase 1 (agent D)

        return resolve(url_or_host)

    async def aclose(self) -> None:
        await self._fetcher.aclose()

    async def __aenter__(self) -> AsyncErgonTracker:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
