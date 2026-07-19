"""Provider contract + registry (FROZEN CONTRACT).

A *provider* knows how to talk to one job source (an ATS like Greenhouse, or an aggregator
like RemoteOK). Providers are registered with ``@register("name")`` and discovered by the
orchestrator and the auto-discovery resolver.

Implement either against the ``Provider`` Protocol directly, or by subclassing
``BaseProvider`` for the shared helpers.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast, runtime_checkable

from ..models import DetailFetch, JobPosting, Location, RawJob, SearchQuery

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = [
    "Provider",
    "BaseProvider",
    "register",
    "get_provider",
    "iter_providers",
    "provider_names",
    "load_builtins",
    "load_plugins",
]

# Names of first-party provider modules under ergon_tracker.providers to import on startup.
_BUILTIN_MODULES = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "remoteok",
    "smartrecruiters",
    "workable",
    "workable_network",
    "recruitee",
    "personio",
    "bamboohr",
    "breezy",
    "teamtailor",
    "join",
    "rippling",
    "pinpoint",
    "paylocity",
    "eightfold",
    "successfactors",
    "oracle",
    "taleo",
    "taleobe",
    "icims",
    "avature",
    "applicantpro",
    "jazzhr",
    "jobvite",
    "phenom",
    "brassring",
    "schemaorg",
    "apicapture",
    "tesla",
    "coveo",
    "peopleadmin",
    "peopleclick",
    "jobdiva",
    "ripplehire",
    "zwayam",
    "ceipal",
    "radancy",
    "pageup",
    "peoplesoft",
    "ukg",
    "adp",
    "dayforce",
    "paycom",
    "remotive",
    "arbeitnow",
    "jobicy",
    "himalayas",
    "themuse",
    "adzuna",
    "usajobs",
    "dejobs",
)

_ENTRYPOINT_GROUP = "ergon_tracker.providers"


@runtime_checkable
class Provider(Protocol):
    """Structural contract every provider satisfies."""

    name: str

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Return the board token if ``url_or_host`` belongs to this provider, else ``None``.

        Used by auto-discovery to map a careers URL/domain to (provider, token).
        """
        ...

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        """Fetch raw postings for one board ``token``. May pre-filter using ``query`` when the
        source supports server-side filtering; otherwise return everything and let the
        orchestrator apply ``query.matches`` client-side."""
        ...

    def normalize(self, raw: RawJob) -> JobPosting:
        """Map one ``RawJob`` to a canonical ``JobPosting``."""
        ...

    def conditional_url(self, token: str) -> str | None:
        """The single URL whose ETag/Last-Modified validates this board's WHOLE response, or
        None if the provider can't be validated cheaply (multi-page, no validator headers).

        Must equal the exact URL+query ``fetch`` requests, so the stored validator corresponds
        to the same representation. Used by the crawler for cross-build conditional requests."""
        ...

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch the full JD detail resource for one posting (Tier-3 recovery / freshness-sweep
        per-posting confirm). Every registered provider satisfies this via ``BaseProvider``'s
        default (unsupported -> ``None``) or an override; declared here so callers that resolve a
        provider through ``get_provider`` (e.g. ``index/freshness.py``'s search-index confirm
        path) can call it without an unchecked ``getattr``. See ``BaseProvider.fetch_detail`` for
        the full return/raise contract (``None`` == confirmed-gone; raise == indeterminate)."""
        ...


class BaseProvider:
    """Optional convenience base with shared helpers. Subclasses must set ``name`` and
    implement ``fetch``/``normalize`` (and usually override ``matches``)."""

    name: str = ""

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        raise NotImplementedError

    def normalize(self, raw: RawJob) -> JobPosting:
        raise NotImplementedError

    def conditional_url(self, token: str) -> str | None:
        """Default: not cheaply validatable. Providers with a single full-board response and
        ETag/Last-Modified support override this (see conditional-requests plan)."""
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch the full JD detail resource for one posting (Tier-3 detail recovery).

        Default: unsupported — the base provider has no per-posting detail endpoint to call.
        Providers opt in by overriding this. Return the JD text as a ``str``, or — when the same
        detail response also yields a STRUCTURED pay field — a ``DetailFetch(text, salary)`` so the
        reconcile prefers the structured range over re-parsing it from prose.

        RETURN/RAISE CONTRACT (an override MUST obey this — the freshness sweep's
        ``confirm_departed`` treats a returned ``None`` as "posting is GONE → expire it", and a
        raised exception as "could not determine → KEEP it"):
          - ALIVE  -> return the ``str``/``DetailFetch`` content.
          - DEFINITIVELY GONE -> return ``None`` ONLY on a real not-found signal: an explicit HTTP
            404/410, or a VERIFIED provider soft-404 body ("posting no longer available"). This is
            the only path that expires a live-index row.
          - INDETERMINATE / TRANSIENT -> RAISE (let it propagate): 5xx, 429, timeouts, connection/
            circuit errors, any non-404 HTTP status, parse failures, an unbuildable detail URL, or a
            200 whose shape you can't classify. A transient error is NEVER evidence of death.
        The reconcile/drain pass (``index.detail.reconcile_detail_tier``) and ``index.liveness``
        both catch a raised exception and treat it as a failed fetch (retried later), so raising is
        safe for every caller. This base default returns ``None`` only because "no detail endpoint
        exists" is a permanent capability gap, not a transient error — which is exactly why
        base-default providers are excluded from the freshness/liveness confirm source sets."""
        return None

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob] | None:
        """Parse an already-downloaded body into RawJobs (lets the crawler reuse a conditional
        200 instead of refetching). Default None = unsupported; the caller falls back to fetch."""
        return None

    # --- shared helpers -------------------------------------------------

    @staticmethod
    def extract_jsonld_jobs(html: str) -> list[dict[str, Any]]:
        """Parse all schema.org/JobPosting JSON-LD blocks from a careers page."""
        from selectolax.parser import HTMLParser

        out: list[dict[str, Any]] = []
        tree = HTMLParser(html)
        for node in tree.css('script[type="application/ld+json"]'):
            text = node.text(strip=False)
            if not text:
                continue
            try:
                data = _json.loads(text)
            except ValueError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in ("JobPosting", ["JobPosting"]):
                    out.append(item)
        return out

    @staticmethod
    def jsonld_locations(job_location: Any) -> list[Location]:
        """schema.org ``jobLocation`` -> ``Location`` list. Accepts a single ``Place`` or a list;
        reads ``address.{addressLocality,addressRegion,addressCountry}``. Skips entries with no
        usable field. Shared by every provider whose detail page carries JSON-LD (jobvite/radancy/…)
        so the reconcile can fill the index row's NULL city/country."""
        entries = job_location if isinstance(job_location, list) else [job_location]
        out: list[Location] = []
        for entry in entries:
            addr = entry.get("address") if isinstance(entry, dict) else None
            if not isinstance(addr, dict):
                continue
            city = (addr.get("addressLocality") or "").strip() or None
            region = (addr.get("addressRegion") or "").strip() or None
            country = (addr.get("addressCountry") or "").strip() or None
            if not any((city, region, country)):
                continue
            raw = ", ".join(p for p in (city, region, country) if p)
            out.append(Location(raw=raw, city=city, region=region, country=country))
        return out


_REGISTRY: dict[str, Provider] = {}

T = TypeVar("T")


def register(name: str) -> Callable[[type[T]], type[T]]:
    """Class decorator: instantiate the provider (no-arg) and register it under ``name``."""

    def decorator(cls: type[T]) -> type[T]:
        cls.name = name  # type: ignore[attr-defined]
        _REGISTRY[name] = cast("Provider", cls())
        return cls

    return decorator


def get_provider(name: str) -> Provider | None:
    return _REGISTRY.get(name)


def iter_providers() -> list[Provider]:
    return list(_REGISTRY.values())


def provider_names() -> list[str]:
    return list(_REGISTRY.keys())


def load_builtins() -> None:
    """Import first-party provider modules so their ``@register`` decorators run.

    Tolerant of missing modules during incremental development (Phase 1 in progress)."""
    for mod in _BUILTIN_MODULES:
        try:
            import_module(f"ergon_tracker.providers.{mod}")
        except ModuleNotFoundError:
            continue


def load_plugins() -> None:
    """Discover third-party providers via the ``ergon_tracker.providers`` entry-point group.

    ``entry_points(group=...)`` is supported on Python 3.10+ (our minimum)."""
    for ep in entry_points(group=_ENTRYPOINT_GROUP):
        if ep.name in _REGISTRY:
            continue
        obj = ep.load()
        instance = obj() if isinstance(obj, type) else obj
        _REGISTRY.setdefault(ep.name, cast("Provider", instance))
