"""jobspine — unified, reliable, typed job-fetching SDK."""

from __future__ import annotations

from .client import AsyncJobSpine
from .exceptions import (
    FetchError,
    JobSpineError,
    ProviderError,
    RateLimitError,
    ResolveError,
)
from .models import (
    EmploymentType,
    JobLevel,
    JobPosting,
    Location,
    Provenance,
    RawJob,
    RemoteType,
    Salary,
    SalaryInterval,
    SearchQuery,
    SearchResult,
    SourceHealth,
)
from .sync import JobSpine, search

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # clients
    "search",
    "JobSpine",
    "AsyncJobSpine",
    # models
    "JobPosting",
    "SearchQuery",
    "SearchResult",
    "Salary",
    "SalaryInterval",
    "Location",
    "RawJob",
    "Provenance",
    "SourceHealth",
    "RemoteType",
    "EmploymentType",
    "JobLevel",
    # exceptions
    "JobSpineError",
    "ProviderError",
    "FetchError",
    "RateLimitError",
    "ResolveError",
]
