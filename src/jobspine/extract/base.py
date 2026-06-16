"""Extractor contract + registry (FROZEN CONTRACT for the extract package)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

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


@dataclass
class ExtractInput:
    """A light, provider-agnostic view of one posting for extractors to read.

    Extractors must depend only on this — never on the full ``JobPosting`` — so they stay
    decoupled and trivially unit-testable.
    """

    title: str
    description_text: str | None = None
    location_raw: str | None = None
    company_key: str | None = None
    company_domain: str | None = None
    structured_salary: Salary | None = None


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


def input_from_job(job: JobPosting, *, company_key: str | None = None) -> ExtractInput:
    """Build an ``ExtractInput`` from a (possibly partly-normalized) ``JobPosting``."""
    location_raw = None
    if job.locations:
        loc = job.locations[0]
        location_raw = loc.raw or loc.as_text() or None
    return ExtractInput(
        title=job.title,
        description_text=job.description_text,
        location_raw=location_raw,
        company_key=company_key,
        company_domain=job.company_domain,
        structured_salary=job.salary,
    )
