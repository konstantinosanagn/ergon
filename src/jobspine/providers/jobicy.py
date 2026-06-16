"""Jobicy provider — an aggregator (not a per-company ATS).

``GET https://jobicy.com/api/v2/remote-jobs?count={n}`` returns ``{"jobs": [...]}`` where every
posting is remote by definition. Because this is an aggregator it is never auto-discovered from a
company URL, so ``matches`` always returns ``None`` and the orchestrator invokes it with an empty
``token``. A single request covers the feed (``count`` mirrors ``query.limit``), so no fan-out is
required.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import (
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    Salary,
    SalaryInterval,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://jobicy.com/api/v2/remote-jobs"
_DEFAULT_COUNT = 50

# Map Jobicy ``jobType`` strings to the canonical EmploymentType enum.
_EMPLOYMENT = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
    "freelance": EmploymentType.CONTRACT,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _employment(job_type: Any) -> EmploymentType:
    # ``jobType`` is a list (e.g. ["Full-Time"]); take the first recognized entry.
    if isinstance(job_type, list):
        candidates = job_type
    elif isinstance(job_type, str):
        candidates = [job_type]
    else:
        return EmploymentType.UNKNOWN
    for raw in candidates:
        if isinstance(raw, str):
            mapped = _EMPLOYMENT.get(raw.strip().lower())
            if mapped is not None:
                return mapped
    return EmploymentType.UNKNOWN


@register("jobicy")
class JobicyProvider(BaseProvider):
    name = "jobicy"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        count = query.limit if query.limit is not None else _DEFAULT_COUNT
        data = await fetcher.get_json(_API, params={"count": count})
        raw_jobs = data.get("jobs") if isinstance(data, dict) else None
        items = [j for j in raw_jobs if isinstance(j, dict)] if isinstance(raw_jobs, list) else []
        if query.limit is not None:
            items = items[: query.limit]
        return [
            RawJob(
                source=self.name,
                source_job_id=str(job.get("id", "")),
                company=job.get("companyName") or "",
                token=None,
                url=job.get("url"),
                payload=job,
            )
            for job in items
        ]

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        geo = (p.get("jobGeo") or "").strip()
        location = Location(raw=geo, is_remote=True) if geo else Location(is_remote=True)
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("jobTitle") or "").strip(),
            description_html=p.get("jobExcerpt"),
            locations=[location],
            remote=RemoteType.REMOTE,
            employment_type=_employment(p.get("jobType")),
            salary=self._salary(p),
            apply_url=p.get("url"),
            posted_at=_parse_dt(p.get("pubDate")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _salary(p: dict[str, Any]) -> Salary | None:
        # Live API exposes salaryMin/salaryMax/salaryCurrency; accept the documented
        # annualSalary* aliases too. Only mint a Salary when an amount is present.
        min_v = p.get("annualSalaryMin") or p.get("salaryMin") or None
        max_v = p.get("annualSalaryMax") or p.get("salaryMax") or None
        if not min_v and not max_v:
            return None
        currency = p.get("salaryCurrency") or p.get("currency") or None
        return Salary(
            min_amount=min_v,
            max_amount=max_v,
            currency=currency,
            interval=SalaryInterval.YEAR,
        )
