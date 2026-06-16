"""Remotive provider — an aggregator (not a per-company ATS).

``GET https://remotive.com/api/remote-jobs?limit={n}`` returns
``{"jobs": [{id, title, company_name, candidate_required_location, salary, url,
job_type, publication_date, description, category, ...}]}``. Every posting is remote by
definition, so ``remote`` is always :data:`RemoteType.REMOTE`. Because this is an
aggregator it is never auto-discovered from a company URL — ``matches`` always returns
``None`` and the orchestrator invokes ``fetch`` with an empty ``token``. A single request
covers the whole feed (server-side ``limit`` is honored), so no fan-out is required.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://remotive.com/api/remote-jobs"

# Remotive uses snake_case job_type values that line up cleanly with our enum.
_EMPLOYMENT: dict[str, EmploymentType] = {
    "full_time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
    "other": EmploymentType.OTHER,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _employment(value: str | None) -> EmploymentType:
    if not value:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT.get(value.strip().lower(), EmploymentType.OTHER)


@register("remotive")
class RemotiveProvider(BaseProvider):
    name = "remotive"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        params: dict[str, Any] = {}
        if query.limit is not None:
            params["limit"] = query.limit
        data = await fetcher.get_json(_API, params=params)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        items = [j for j in jobs if isinstance(j, dict)]
        if query.limit is not None:
            items = items[: query.limit]
        return [
            RawJob(
                source=self.name,
                source_job_id=str(job.get("id", "")),
                company=job.get("company_name") or "",
                token=None,
                url=job.get("url"),
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
            remote=RemoteType.REMOTE,  # every Remotive posting is remote.
            employment_type=_employment(p.get("job_type")),
            department=p.get("category") or None,
            apply_url=p.get("url"),
            posted_at=_parse_dt(p.get("publication_date")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        raw_loc = (p.get("candidate_required_location") or "").strip()
        if not raw_loc:
            return [Location(is_remote=True)]
        return [Location(raw=raw_loc, is_remote=True)]
