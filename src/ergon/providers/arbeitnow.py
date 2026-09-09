"""Arbeitnow provider — an aggregator (not a per-company ATS).

``GET https://www.arbeitnow.com/api/job-board-api`` returns
``{"data": [{slug, company_name, title, description, remote(bool), url, tags, job_types,
location, created_at(epoch)}], "links": {"next": ...}}``. The feed mixes remote and onsite
postings, so :data:`RemoteType` is derived from the per-job ``remote`` boolean. Because this
is an aggregator it is never auto-discovered from a company URL — ``matches`` always returns
``None`` and the orchestrator invokes ``fetch`` with an empty ``token``.

The feed is paginated via ``?page=``. When ``query.limit`` asks for more rows than one page
yields we fetch additional pages (up to :data:`_MAX_PAGES`) concurrently via an anyio task
group, preserving page order in the returned list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import anyio

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 5

# Arbeitnow job_types are free-text and multilingual (German/English). Map the common ones;
# any other non-empty value falls back to OTHER, and an empty list stays UNKNOWN.
_EMPLOYMENT: dict[str, EmploymentType] = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "vollzeit": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "teilzeit": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "praktikum": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}


def _parse_epoch(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _employment(job_types: Any) -> EmploymentType:
    if not isinstance(job_types, list) or not job_types:
        return EmploymentType.UNKNOWN
    first = str(job_types[0]).strip().lower()
    if not first:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT.get(first, EmploymentType.OTHER)


@register("arbeitnow")
class ArbeitnowProvider(BaseProvider):
    name = "arbeitnow"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        first = await self._fetch_page(fetcher, 1)
        items = list(first)

        # Page 1 satisfied the limit (or there is none) — no extra pages needed.
        if query.limit is not None and len(items) < query.limit:
            pages_needed = min(
                _MAX_PAGES,
                -(-query.limit // len(first)) if first else 1,
            )
            if pages_needed > 1:
                rest = await self._fetch_pages(fetcher, range(2, pages_needed + 1))
                for page in rest:
                    items.extend(page)

        if query.limit is not None:
            items = items[: query.limit]
        return [self._to_raw(job) for job in items if isinstance(job, dict)]

    async def _fetch_pages(
        self, fetcher: AsyncFetcher, page_numbers: range
    ) -> list[list[dict[str, Any]]]:
        results: dict[int, list[dict[str, Any]]] = {}

        async def _grab(page: int) -> None:
            results[page] = await self._fetch_page(fetcher, page)

        async with anyio.create_task_group() as tg:
            for page in page_numbers:
                tg.start_soon(_grab, page)
        return [results[p] for p in page_numbers]

    @staticmethod
    async def _fetch_page(fetcher: AsyncFetcher, page: int) -> list[dict[str, Any]]:
        data = await fetcher.get_json(_API, params={"page": page})
        rows = data.get("data", []) if isinstance(data, dict) else []
        return [j for j in rows if isinstance(j, dict)]

    def _to_raw(self, job: dict[str, Any]) -> RawJob:
        return RawJob(
            source=self.name,
            source_job_id=str(job.get("slug", "")),
            company=job.get("company_name") or "",
            token=None,
            url=job.get("url"),
            payload=job,
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        is_remote = bool(p.get("remote"))
        loc = (p.get("location") or "").strip()
        location = Location(raw=loc or None, is_remote=is_remote)
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("title") or "").strip(),
            description_html=p.get("description"),
            locations=[location],
            remote=RemoteType.REMOTE if is_remote else RemoteType.ONSITE,
            employment_type=_employment(p.get("job_types")),
            apply_url=p.get("url"),
            posted_at=_parse_epoch(p.get("created_at")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )
