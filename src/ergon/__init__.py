"""ergon — unified, reliable, typed job-fetching SDK."""

from __future__ import annotations

from .client import AsyncErgon
from .exceptions import (
    ErgonError,
    FetchError,
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
from .sync import Ergon, search

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # clients
    "search",
    "Ergon",
    "AsyncErgon",
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
    "ErgonError",
    "ProviderError",
    "FetchError",
    "RateLimitError",
    "ResolveError",
]
