"""Canonical data models for jobspine — the FROZEN CONTRACT all providers normalize to.

Every provider produces ``RawJob`` from its source, then maps it into a ``JobPosting``.
Missing fields are ``None`` (or an ``UNKNOWN`` enum) — never invented.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RemoteType",
    "EmploymentType",
    "SalaryInterval",
    "JobLevel",
    "Location",
    "Salary",
    "RawJob",
    "Provenance",
    "JobPosting",
    "SearchQuery",
    "SourceHealth",
    "SearchResult",
    "make_job_id",
]


class RemoteType(str, Enum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    OTHER = "other"
    UNKNOWN = "unknown"


class SalaryInterval(str, Enum):
    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"


class JobLevel(str, Enum):
    """Seniority ladder, inferred from the job title during enrichment."""

    INTERN = "intern"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"
    UNKNOWN = "unknown"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_job_id(source: str, source_job_id: str) -> str:
    """Stable, short id derived from (source, source_job_id)."""
    return hashlib.sha1(f"{source}:{source_job_id}".encode()).hexdigest()[:16]


class Location(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None
    raw: str | None = None
    is_remote: bool = False

    def as_text(self) -> str:
        parts = [p for p in (self.city, self.region, self.country) if p]
        return ", ".join(parts) if parts else (self.raw or "")


class Salary(BaseModel):
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    interval: SalaryInterval | None = None


class RawJob(BaseModel):
    """Pre-normalization container: exactly what a source returned for one posting."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str
    source_job_id: str
    company: str
    token: str | None = None
    url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=_utcnow)


class Provenance(BaseModel):
    """A record of one source that yielded a (possibly merged) posting."""

    source: str
    source_job_id: str
    apply_url: str | None = None
    fetched_at: datetime = Field(default_factory=_utcnow)


class JobPosting(BaseModel):
    """The canonical, normalized posting returned to users."""

    id: str
    source: str
    source_job_id: str
    company: str
    company_domain: str | None = None
    title: str
    description_text: str | None = None
    description_html: str | None = None
    locations: list[Location] = Field(default_factory=list)
    remote: RemoteType = RemoteType.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    level: JobLevel = JobLevel.UNKNOWN
    department: str | None = None
    sector: str | None = None
    salary: Salary | None = None
    apply_url: str | None = None
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: str,
        source_job_id: str | int,
        company: str,
        title: str,
        fetched_at: datetime | None = None,
        apply_url: str | None = None,
        **fields: Any,
    ) -> JobPosting:
        """Ergonomic constructor for providers.

        Auto-fills ``id`` and a default single-entry ``provenance`` so normalize() code stays
        short and consistent. Pass any other ``JobPosting`` field via kwargs.
        """
        sid = str(source_job_id)
        fetched = fetched_at or _utcnow()
        provenance = fields.pop("provenance", None) or [
            Provenance(source=source, source_job_id=sid, apply_url=apply_url, fetched_at=fetched)
        ]
        return cls(
            id=make_job_id(source, sid),
            source=source,
            source_job_id=sid,
            company=company,
            title=title,
            apply_url=apply_url,
            provenance=provenance,
            **fields,
        )


class SearchQuery(BaseModel):
    """A unified query. ``matches()`` is the client-side filter applied to sources that have
    no server-side keyword search (i.e. most ATS feeds)."""

    keywords: str | None = None
    location: str | None = None
    remote: bool | None = None
    employment_type: EmploymentType | None = None
    posted_after: datetime | None = None
    limit: int | None = None
    companies: list[str] | None = None
    sources: list[str] | None = None
    # advanced filters (applied after enrichment)
    level: JobLevel | None = None
    country: str | None = None
    city: str | None = None
    sector: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    include_unknown_salary: bool = True

    def _salary_ok(self, job: JobPosting) -> bool:
        if self.salary_min is None and self.salary_max is None:
            return True
        s = job.salary
        if s is None or (s.min_amount is None and s.max_amount is None):
            return self.include_unknown_salary
        if (
            self.salary_currency
            and s.currency
            and s.currency.upper() != self.salary_currency.upper()
        ):
            return False
        job_lo = s.min_amount if s.min_amount is not None else s.max_amount
        job_hi = s.max_amount if s.max_amount is not None else s.min_amount
        if job_lo is None or job_hi is None:  # unreachable given the guard above
            return self.include_unknown_salary
        want_lo = self.salary_min if self.salary_min is not None else float("-inf")
        want_hi = self.salary_max if self.salary_max is not None else float("inf")
        # keep when the job's range overlaps the requested range
        return not (job_hi < want_lo or job_lo > want_hi)

    def _geo_ok(self, job: JobPosting) -> bool:
        for field, value in (("country", self.country), ("city", self.city)):
            if not value:
                continue
            needle = value.lower()
            hit = any(
                (getattr(loc, field) or "").lower() == needle or needle in (loc.raw or "").lower()
                for loc in job.locations
            )
            if not hit:
                return False
        return True

    def matches(self, job: JobPosting) -> bool:
        if self.keywords:
            haystack = " ".join(
                filter(
                    None,
                    [job.title, job.department, job.company, job.description_text or ""],
                )
            ).lower()
            if not all(tok in haystack for tok in self.keywords.lower().split()):
                return False

        if self.location:
            loc_text = " ".join(loc.as_text() for loc in job.locations).lower()
            if self.location.lower() not in loc_text:
                return False

        if self.remote is True:
            is_remote = job.remote in (RemoteType.REMOTE, RemoteType.HYBRID) or any(
                loc.is_remote for loc in job.locations
            )
            # Keep UNKNOWN-remote jobs only when no location constraint contradicts it.
            if not is_remote and job.remote != RemoteType.UNKNOWN:
                return False

        if self.employment_type and job.employment_type not in (
            self.employment_type,
            EmploymentType.UNKNOWN,
        ):
            return False

        if self.level is not None and job.level != self.level:
            return False

        if self.sector and self.sector.lower() not in (job.sector or "").lower():
            return False

        if not self._geo_ok(job):
            return False

        if not self._salary_ok(job):
            return False

        return not (
            self.posted_after is not None
            and job.posted_at is not None
            and job.posted_at < self.posted_after
        )


class SourceHealth(BaseModel):
    """Per-source outcome of a search. Surfaced so callers never see a silent empty result."""

    source: str
    ok: bool
    count: int = 0
    error: str | None = None
    elapsed_ms: int = 0
    truncated: bool = False


class SearchResult(BaseModel):
    jobs: list[JobPosting] = Field(default_factory=list)
    health: list[SourceHealth] = Field(default_factory=list)

    def __iter__(self) -> Iterator[JobPosting]:  # type: ignore[override]
        return iter(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [j.model_dump(mode="json") for j in self.jobs]

    def to_pandas(self) -> Any:  # optional dep, dynamic return type
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError("DataFrame export needs: pip install 'jobspine[pandas]'") from exc
        return pd.DataFrame(self.to_dicts())

    def to_polars(self) -> Any:  # optional dep, dynamic return type
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError("DataFrame export needs: pip install 'jobspine[polars]'") from exc
        return pl.DataFrame(self.to_dicts())

    @property
    def ok_sources(self) -> list[str]:
        return [h.source for h in self.health if h.ok]

    @property
    def failed_sources(self) -> list[SourceHealth]:
        return [h for h in self.health if not h.ok]
