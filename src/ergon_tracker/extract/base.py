"""Extractor contract + registry (FROZEN CONTRACT for the extract package)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from ..models import JobPosting, Salary

__all__ = [
    "ExtractInput",
    "FieldExtractor",
    "register_extractor",
    "get_extractor",
    "iter_extractors",
    "input_from_job",
]

_T = TypeVar("_T")


def _vocab(lang: str, table: dict[str, _T], fallback: str = "en") -> _T:
    """Look up a language-keyed vocab table entry, falling back to ``fallback`` (default "en").

    The single choke point every multilingual extractor uses to go from
    ``ExtractInput.language`` to the right vocab/regex for that language. A language missing
    from ``table`` (unsupported, or rollout not yet done for that field) transparently falls
    back to English — so partial multilingual rollout is always safe and never raises.
    """
    return table.get(lang, table[fallback])


@dataclass
class ExtractInput:
    """A light, provider-agnostic view of one posting for extractors to read.

    Extractors must depend only on this — never on the full ``JobPosting`` — so they stay
    decoupled and trivially unit-testable.
    """

    title: str
    description_text: str | None = None
    location_raw: str | None = None
    company: str | None = None
    company_key: str | None = None
    company_domain: str | None = None
    structured_salary: Salary | None = None
    language: str = "en"
    """ISO-639-1 code (``"en"``, ``"de"``, ...) for the posting's description text. Every
    multilingual extractor reads this to pick its vocab table (see ``_vocab``); the default
    ``"en"`` keeps every pre-existing construction site (incl. the ``test_*_recall.py`` fixtures)
    on the exact original English behavior."""


@runtime_checkable
class FieldExtractor(Protocol):
    """Maps an ``ExtractInput`` to a single field value (type depends on the field)."""

    name: str

    def extract(self, inp: ExtractInput) -> Any: ...


_REGISTRY: dict[str, FieldExtractor] = {}


def register_extractor(extractor: FieldExtractor) -> FieldExtractor:
    """Register an extractor instance under its ``name``."""
    _REGISTRY[extractor.name] = extractor
    return extractor


def get_extractor(name: str) -> FieldExtractor | None:
    return _REGISTRY.get(name)


def iter_extractors() -> list[FieldExtractor]:
    return list(_REGISTRY.values())


def html_to_text(html: str | None) -> str | None:
    """Strip HTML to plain text (selectolax). Returns None for empty input."""
    if not html:
        return None
    from selectolax.parser import HTMLParser

    text = HTMLParser(html).text(separator=" ", strip=True)
    return text or None


def input_from_job(job: JobPosting, *, company_key: str | None = None) -> ExtractInput:
    """Build an ``ExtractInput`` from a (possibly partly-normalized) ``JobPosting``.

    Many providers (most aggregators) populate only ``description_html``; fall back to a
    stripped-text version so every text extractor (comp, yoe, sector, sponsorship) sees the JD.
    """
    location_raw = None
    if job.locations:
        loc = job.locations[0]
        location_raw = loc.raw or loc.as_text() or None
    return ExtractInput(
        title=job.title,
        description_text=job.description_text or html_to_text(job.description_html),
        location_raw=location_raw,
        company=job.company,
        company_key=company_key,
        company_domain=job.company_domain,
        structured_salary=job.salary,
    )
