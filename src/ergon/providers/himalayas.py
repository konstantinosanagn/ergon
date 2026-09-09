"""Himalayas provider — an aggregator (not a per-company ATS).

``GET https://himalayas.app/jobs/api?limit={n}`` returns ``{"jobs": [...], "totalCount": N}`` where
every posting is remote by definition. Because this is an aggregator it is never auto-discovered
from a company URL, so ``matches`` always returns ``None`` and the orchestrator invokes it with an
empty ``token``. A single request covers the feed (``limit`` mirrors ``query.limit``), so no
fan-out is required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..extract.level import level_from_ats_vocab
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

_API = "https://himalayas.app/jobs/api"
_DEFAULT_LIMIT = 50

_EMPLOYMENT = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
    "freelance": EmploymentType.CONTRACT,
}


def _parse_epoch(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _employment(value: Any) -> EmploymentType:
    if isinstance(value, str):
        return _EMPLOYMENT.get(value.strip().lower(), EmploymentType.UNKNOWN)
    return EmploymentType.UNKNOWN


@register("himalayas")
class HimalayasProvider(BaseProvider):
    name = "himalayas"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        limit = query.limit if query.limit is not None else _DEFAULT_LIMIT
        data = await fetcher.get_json(_API, params={"limit": limit})
        raw_jobs = data.get("jobs") if isinstance(data, dict) else None
        items = [j for j in raw_jobs if isinstance(j, dict)] if isinstance(raw_jobs, list) else []
        if query.limit is not None:
            items = items[: query.limit]
        return [
            RawJob(
                source=self.name,
                source_job_id=self._job_id(job),
                company=job.get("companyName") or "",
                token=None,
                url=job.get("applicationLink") or job.get("guid"),
                payload=job,
            )
            for job in items
        ]

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("title") or "").strip(),
            description_html=p.get("description"),
            locations=self._locations(p),
            remote=RemoteType.REMOTE,
            employment_type=_employment(p.get("employmentType")),
            level=level_from_ats_vocab(self._first_seniority(p)),
            department=self._department(p),
            salary=self._salary(p),
            apply_url=p.get("applicationLink") or p.get("guid"),
            posted_at=_parse_epoch(p.get("pubDate")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _job_id(job: dict[str, Any]) -> str:
        # No stable numeric id is exposed; the guid/applicationLink is the canonical identifier.
        return str(job.get("guid") or job.get("applicationLink") or job.get("title") or "")

    @staticmethod
    def _first_seniority(p: dict[str, Any]) -> str | None:
        # `seniority` is a list of strings, e.g. ["Senior"]; take the first entry.
        values = p.get("seniority")
        first = values[0] if isinstance(values, list) and values else None
        return first if isinstance(first, str) else None

    @staticmethod
    def _department(p: dict[str, Any]) -> str | None:
        # `categories` is normally a list of strings; be defensive in case an entry is an
        # object (e.g. {"name": ...}) instead, matching the shape other aggregators use.
        categories = p.get("categories")
        if not isinstance(categories, list) or not categories:
            return None
        first = categories[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            name = first.get("name")
            return name if isinstance(name, str) else None
        return None

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        restrictions = p.get("locationRestrictions")
        first = restrictions[0] if isinstance(restrictions, list) and restrictions else None
        raw_loc = first.strip() if isinstance(first, str) else None
        return [Location(raw=raw_loc, is_remote=True)] if raw_loc else [Location(is_remote=True)]

    @staticmethod
    def _salary(p: dict[str, Any]) -> Salary | None:
        min_v = p.get("minSalary") or None
        max_v = p.get("maxSalary") or None
        if not min_v and not max_v:
            return None
        # Default to USD only when amounts are present but currency is unspecified.
        currency = p.get("currency") or "USD"
        return Salary(
            min_amount=min_v,
            max_amount=max_v,
            currency=currency,
            interval=SalaryInterval.YEAR,
        )
