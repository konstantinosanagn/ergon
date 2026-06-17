"""ATS auto-discovery resolver: ``url/host -> (ats, token)``.

Resolution happens in three escalating tiers, cheapest first:

1. **Pattern match (sync, offline).** Every registered provider's :meth:`matches` is tried
   against the raw input; the first non-``None`` token wins. This recognises canonical ATS
   URLs/hosts directly (``boards.greenhouse.io/stripe`` -> greenhouse/stripe, etc.).
2. **Seed registry (sync, offline).** If no pattern matches, the input host (and its parent
   domains) is looked up in the packaged seed registry (``data/seed.json``) which maps known
   company domains to a verified ``(ats, token)``.
3. **Embedded-signature discovery (async, network).** :func:`aresolve` fetches the careers
   page and scans the HTML for embedded ATS signatures (Greenhouse/Ashby/Lever/Workday),
   then probes the discovered candidate URLs **concurrently** to confirm which is live.

:func:`resolve` is pure and never raises — on any failure it returns an unmatched
:class:`Resolution`. Network access only happens in :func:`aresolve`, and only via the passed
``AsyncFetcher`` (which bounds concurrency and rate); we never build our own HTTP client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import anyio

from ..providers.base import iter_providers, load_builtins
from .store import SeedRegistry

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["Resolution", "resolve", "aresolve"]


@dataclass
class Resolution:
    """Outcome of resolving a careers URL/host to an ATS board.

    ``bool(resolution)`` is ``True`` only when a provider+token (or seed entry) was found.
    """

    ats: str | None = None
    token: str | None = None
    domain: str | None = None
    source: str = ""
    matched: bool = False

    def __bool__(self) -> bool:
        return self.matched


# Embedded ATS signatures we look for in a careers page's HTML (iframes, links, scripts).
# Ordered greenhouse -> ashby -> lever -> workday; candidates are re-sorted by document
# position so the first signature encountered in the page wins.
_SIGNATURES: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://(?:job-)?boards\.greenhouse\.io/[^\s\"'<>)]+", re.IGNORECASE),
    re.compile(r"https?://grnh\.se/[^\s\"'<>)]+", re.IGNORECASE),
    re.compile(r"https?://jobs\.ashbyhq\.com/[^\s\"'<>)]+", re.IGNORECASE),
    re.compile(r"https?://jobs\.lever\.co/[^\s\"'<>)]+", re.IGNORECASE),
    re.compile(r"https?://[\w.-]+\.myworkdayjobs\.com/[^\s\"'<>)]+", re.IGNORECASE),
)

_seed_registry: SeedRegistry | None = None


def _get_seed() -> SeedRegistry:
    global _seed_registry
    if _seed_registry is None:
        _seed_registry = SeedRegistry()
    return _seed_registry


def _to_host(value: str) -> str:
    """Best-effort extraction of the bare host from a URL or bare host string."""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "//" + candidate
    netloc = urlsplit(candidate).netloc
    return netloc.split("@")[-1].split(":")[0].lower()


def _match_providers(url_or_host: str) -> Resolution | None:
    """Tier 1: first provider whose ``matches`` returns a token wins."""
    load_builtins()
    for provider in iter_providers():
        try:
            token = provider.matches(url_or_host)
        except Exception:
            token = None
        if token:
            return Resolution(
                ats=provider.name,
                token=token,
                domain=None,
                source=url_or_host,
                matched=True,
            )
    return None


def resolve(url_or_host: str) -> Resolution:
    """Resolve a careers URL/host to a :class:`Resolution` (sync, offline, never raises)."""
    try:
        hit = _match_providers(url_or_host)
        if hit is not None:
            return hit

        host = _to_host(url_or_host)
        if host:
            seed_hit = _get_seed().lookup_domain(host)
            if seed_hit is not None:
                seed_hit.source = url_or_host
                return seed_hit
    except Exception:
        return Resolution(source=url_or_host, matched=False)

    return Resolution(source=url_or_host, matched=False)


def _extract_candidates(html: str) -> list[str]:
    """Pull embedded ATS URLs out of careers-page HTML, in document order, deduplicated."""
    found: list[tuple[int, str]] = []
    for pattern in _SIGNATURES:
        for m in pattern.finditer(html):
            url = m.group(0).rstrip("\"'<>) ")
            found.append((m.start(), url))
    found.sort(key=lambda item: item[0])

    ordered: list[str] = []
    for _, url in found:
        if url not in ordered:
            ordered.append(url)
    return ordered


async def aresolve(url_or_host: str, fetcher: AsyncFetcher) -> Resolution:
    """Network-assisted resolve: fall back to embedded-signature discovery.

    If the offline :func:`resolve` already matches, return it. Otherwise fetch the careers
    page via the passed ``AsyncFetcher``, scan for embedded ATS signatures, and probe every
    discovered candidate URL **concurrently** (via :func:`anyio.create_task_group`) to confirm
    which endpoint is live. The earliest-in-document live candidate wins. Never raises.
    """
    sync_hit = resolve(url_or_host)
    if sync_hit:
        return sync_hit

    page_url = url_or_host if "://" in url_or_host else f"https://{url_or_host}"
    try:
        html = await fetcher.get_text(page_url)
    except Exception:
        return Resolution(source=url_or_host, matched=False)

    candidates = _extract_candidates(html)
    if not candidates:
        return Resolution(source=url_or_host, matched=False)

    # Probe ALL candidates concurrently; each task confirms its own endpoint is live before
    # recording a hit. Results are keyed by the candidate's document order so the first
    # signature in the page wins deterministically regardless of which probe finished first.
    results: dict[int, Resolution] = {}

    async def _probe(index: int, candidate: str) -> None:
        res = resolve(candidate)
        if not res:
            return
        try:
            await fetcher.get_text(candidate)  # confirm the discovered endpoint is live
        except Exception:
            return
        res.source = url_or_host
        results[index] = res

    async with anyio.create_task_group() as tg:
        for index, candidate in enumerate(candidates):
            tg.start_soon(_probe, index, candidate)

    for index in sorted(results):
        return results[index]
    return Resolution(source=url_or_host, matched=False)
