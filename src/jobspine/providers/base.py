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

from ..models import JobPosting, RawJob, SearchQuery

if TYPE_CHECKING:
    from ..http import AsyncFetcher

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

# Names of first-party provider modules under jobspine.providers to import on startup.
_BUILTIN_MODULES = (
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "remoteok",
    "smartrecruiters",
    "workable",
    "recruitee",
    "personio",
    "remotive",
    "arbeitnow",
    "jobicy",
    "himalayas",
    "themuse",
)

_ENTRYPOINT_GROUP = "jobspine.providers"


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
            import_module(f"jobspine.providers.{mod}")
        except ModuleNotFoundError:
            continue


def load_plugins() -> None:
    """Discover third-party providers via the ``jobspine.providers`` entry-point group.

    ``entry_points(group=...)`` is supported on Python 3.10+ (our minimum)."""
    for ep in entry_points(group=_ENTRYPOINT_GROUP):
        if ep.name in _REGISTRY:
            continue
        obj = ep.load()
        instance = obj() if isinstance(obj, type) else obj
        _REGISTRY.setdefault(ep.name, cast("Provider", instance))
