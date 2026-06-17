"""Unified search orchestrator.

Fans out concurrently to every targeted source (ATS boards resolved from the registry +
aggregators), normalizes to canonical ``JobPosting``, deduplicates across sources, and
returns a ``SearchResult`` with a per-source health report.

Concurrency: every target is launched in a single ``anyio`` task group. Each provider's own
network calls go through the shared ``AsyncFetcher``, whose ``CapacityLimiter`` and per-host
rate limiter bound total in-flight requests — so we can launch all targets at once safely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import anyio

from .dedup import deduplicate
from .enrich import enrich_in_place
from .http import AsyncFetcher
from .models import JobPosting, SearchQuery, SearchResult, SourceHealth
from .observability import build_health
from .providers.base import get_provider, iter_providers, load_builtins, load_plugins
from .ranking import rank
from .registry.resolver import resolve
from .registry.store import SeedRegistry

__all__ = ["run_search"]

# Providers that are not per-company boards (no token; one call returns many companies).
AGGREGATOR_PROVIDERS = {
    "remoteok",
    "remotive",
    "arbeitnow",
    "jobicy",
    "himalayas",
    "themuse",
    "adzuna",
    "usajobs",
}


def _is_aggregator(name: str) -> bool:
    provider = get_provider(name)
    return bool(getattr(provider, "is_aggregator", False)) or name in AGGREGATOR_PROVIDERS


@dataclass
class _Target:
    provider: str
    token: str
    label: str
    domain: str | None = None


@dataclass
class _ProviderStat:
    success: int = 0
    fail: int = 0
    count: int = 0
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)


def _enabled(query: SearchQuery, name: str) -> bool:
    return query.sources is None or name in query.sources


def _plan_targets(query: SearchQuery) -> list[_Target]:
    targets: list[_Target] = []

    if query.companies:
        for company in query.companies:
            res = resolve(company)
            if res.matched and res.ats and res.token and _enabled(query, res.ats):
                targets.append(
                    _Target(provider=res.ats, token=res.token, label=company, domain=res.domain)
                )
        # Aggregators have no per-company token; include them only when explicitly requested.
        if query.sources:
            for provider in iter_providers():
                if _is_aggregator(provider.name) and provider.name in query.sources:
                    targets.append(_Target(provider=provider.name, token="", label=provider.name))
        return targets

    # No explicit companies: search the whole seed registry + all aggregators.
    seed = SeedRegistry()
    for key, entry in seed.all().items():
        ats = entry.get("ats")
        token = entry.get("token")
        if ats and token and _enabled(query, ats):
            targets.append(
                _Target(provider=ats, token=token, label=key, domain=entry.get("domain"))
            )

    for provider in iter_providers():
        if _is_aggregator(provider.name) and _enabled(query, provider.name):
            targets.append(_Target(provider=provider.name, token="", label=provider.name))

    return targets


async def run_search(query: SearchQuery, fetcher: AsyncFetcher) -> SearchResult:
    load_builtins()
    load_plugins()

    targets = _plan_targets(query)
    results: dict[int, list[JobPosting]] = {}
    stats: dict[str, _ProviderStat] = {}

    async def worker(index: int, target: _Target) -> None:
        provider = get_provider(target.provider)
        stat = stats.setdefault(target.provider, _ProviderStat())
        start = time.monotonic()
        if provider is None:
            stat.fail += 1
            stat.errors.append(f"unknown provider '{target.provider}'")
            results[index] = []
            return
        try:
            raws = await provider.fetch(target.token, query, fetcher)
            jobs: list[JobPosting] = []
            for raw in raws:
                try:
                    job = provider.normalize(raw)
                except Exception:  # noqa: BLE001 - one bad record must not sink the board
                    continue
                if target.domain and not job.company_domain:
                    job.company_domain = target.domain
                enrich_in_place(
                    job,
                    company_key=target.label,
                    infer_level_from_experience=query.infer_level_from_experience,
                )
                if query.matches(job):
                    jobs.append(job)
            results[index] = jobs
            stat.success += 1
            stat.count += len(jobs)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully per source
            stat.fail += 1
            stat.errors.append(f"{target.label}: {exc}")
            results[index] = []
        finally:
            stat.elapsed_ms += int((time.monotonic() - start) * 1000)

    async with anyio.create_task_group() as tg:
        for index, target in enumerate(targets):
            tg.start_soon(worker, index, target)

    # Reassemble in target order for deterministic output, then dedup across all sources.
    combined: list[JobPosting] = [job for i in sorted(results) for job in results[i]]
    deduped = deduplicate(combined)
    # Rank by relevance to the keyword query BEFORE applying the limit, so we keep the best
    # matches rather than whichever sources happened to return first. Semantic mode adds an
    # embeddings reranker over the lexical top-K (opt-in; needs the `semantic` extra).
    reranker = None
    if query.semantic and query.keywords:
        from .semantic import get_semantic_reranker

        reranker = get_semantic_reranker()
    deduped = rank(deduped, query.keywords, reranker=reranker)
    if query.limit is not None:
        deduped = deduped[: query.limit]

    health = _build_health(stats)
    return SearchResult(jobs=deduped, health=health)


def _build_health(stats: dict[str, _ProviderStat]) -> list[SourceHealth]:
    health: list[SourceHealth] = []
    for name, stat in sorted(stats.items()):
        total = stat.success + stat.fail
        ok = stat.fail == 0 and stat.success > 0
        error: str | None = None
        if stat.errors:
            shown = "; ".join(stat.errors[:3])
            error = f"{stat.fail}/{total} boards failed: {shown}"
        health.append(
            build_health(
                name,
                ok=ok,
                count=stat.count,
                error=error,
                elapsed_ms=stat.elapsed_ms,
            )
        )
    return health
