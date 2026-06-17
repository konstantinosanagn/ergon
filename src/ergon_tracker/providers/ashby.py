"""Ashby provider — a per-company ATS (``jobs.ashbyhq.com/<token>``).

Public posting API, no auth. We request ``includeCompensation=true`` to get the richest
salary data Ashby exposes. One board ``token`` maps to exactly one HTTP request, so no
fan-out is needed here.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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

_API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
_HOST = "jobs.ashbyhq.com"

_EMPLOYMENT: dict[str, EmploymentType] = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}

_INTERVAL: dict[str, SalaryInterval] = {
    "1 year": SalaryInterval.YEAR,
    "1 month": SalaryInterval.MONTH,
    "1 week": SalaryInterval.WEEK,
    "1 day": SalaryInterval.DAY,
    "1 hour": SalaryInterval.HOUR,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@register("ashby")
class AshbyProvider(BaseProvider):
    name = "ashby"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        split = urlsplit(url_or_host if "//" in url_or_host else f"//{url_or_host}")
        if split.netloc != _HOST:
            return None
        token = split.path.strip("/").split("/")[0]
        return token or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        data = await fetcher.get_json(_API.format(token=token))
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [
            RawJob(
                source=self.name,
                source_job_id=str(job.get("id", "")),
                company=token,
                token=token,
                url=job.get("jobUrl"),
                payload=job,
            )
            for job in jobs
        ]

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("title") or "").strip(),
            description_text=p.get("descriptionPlain"),
            description_html=p.get("descriptionHtml"),
            department=p.get("department"),
            locations=self._locations(p),
            remote=self._remote(p.get("isRemote")),
            employment_type=self._employment(p.get("employmentType")),
            salary=self._salary(p.get("compensation")),
            apply_url=p.get("applyUrl") or p.get("jobUrl"),
            posted_at=_parse_dt(p.get("publishedAt")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _remote(is_remote: bool | None) -> RemoteType:
        if is_remote is None:
            return RemoteType.UNKNOWN
        return RemoteType.REMOTE if is_remote else RemoteType.ONSITE

    @staticmethod
    def _employment(value: str | None) -> EmploymentType:
        if not value:
            return EmploymentType.UNKNOWN
        return _EMPLOYMENT.get(value.replace(" ", "").lower(), EmploymentType.OTHER)

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        is_remote = bool(p.get("isRemote"))
        postal = (p.get("address") or {}).get("postalAddress") or {}
        loc = Location(
            city=postal.get("addressLocality"),
            region=postal.get("addressRegion"),
            country=postal.get("addressCountry"),
            raw=p.get("location"),
            is_remote=is_remote,
        )
        if not (loc.city or loc.region or loc.country or loc.raw):
            return []
        return [loc]

    @staticmethod
    def _salary(compensation: dict[str, Any] | None) -> Salary | None:
        if not compensation:
            return None
        components = compensation.get("summaryComponents") or []
        for comp in components:
            if comp.get("compensationType") != "Salary":
                continue
            min_v = comp.get("minValue")
            max_v = comp.get("maxValue")
            if min_v is None and max_v is None:
                continue
            interval_key = str(comp.get("interval") or "").lower()
            return Salary(
                min_amount=min_v,
                max_amount=max_v,
                currency=comp.get("currencyCode"),
                interval=_INTERVAL.get(interval_key),
            )
        return None
