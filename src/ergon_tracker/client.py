"""Async core: ``AsyncJobSpine`` holds the fetcher + provider registry and runs searches.

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

__all__ = ["AsyncJobSpine"]

_providers_loaded = False


def _ensure_providers_loaded() -> None:
    global _providers_loaded
    if not _providers_loaded:
        load_builtins()
        load_plugins()
        _providers_loaded = True


class AsyncJobSpine:
    def __init__(
        self,
        *,
        fetcher: AsyncFetcher | None = None,
        concurrency: int = 16,
        cache: bool = False,
    ) -> None:
        _ensure_providers_loaded()
        self._fetcher = fetcher or AsyncFetcher(concurrency=concurrency, cache=cache)

    async def search(self, query: SearchQuery) -> SearchResult:
        from .engine import run_search  # lazy import avoids the search-name collision

        result: SearchResult = await run_search(query, self._fetcher)
        return result

    def resolve(self, url_or_host: str) -> Resolution:
        from .registry.resolver import resolve  # lazy: implemented in Phase 1 (agent D)

        return resolve(url_or_host)

    async def aclose(self) -> None:
        await self._fetcher.aclose()

    async def __aenter__(self) -> AsyncJobSpine:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
