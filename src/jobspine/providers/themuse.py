"""The Muse provider — an aggregator (not a per-company ATS).

``GET https://www.themuse.com/api/public/jobs?page={p}`` returns a JSON object::

    {"page": 1, "page_count": N, "items_per_page": 20, "total": M,
     "results": [{"id", "name", "company": {"name"}, "locations": [{"name"}],
                  "levels": [{"name"}], "type", "refs": {"landing_page"},
                  "publication_date", "contents", "categories": [{"name"}]}, ...]}

Pages are **1-indexed**. Because this is an aggregator it is never auto-discovered from a
company URL, so ``matches`` always returns ``None`` and the orchestrator invokes it with an
empty ``token``. To satisfy ``query.limit`` we fetch multiple pages **concurrently** (capped at
``MAX_PAGES``) via an anyio task group.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import anyio

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://www.themuse.com/api/public/jobs"

# "Flexible / Remote" is The Muse's own remote location label.
_REMOTE_HINTS = ("remote", "flexible")

_EMPLOYMENT_MAP = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@register("themuse")
class TheMuseProvider(BaseProvider):
    name = "themuse"

    PAGE_SIZE = 20
    MAX_PAGES = 5

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Decide how many pages to pull from query.limit, capped at MAX_PAGES.
        if query.limit is not None:
            pages_needed = max(1, -(-query.limit // self.PAGE_SIZE))  # ceil div
        else:
            pages_needed = self.MAX_PAGES
        page_count = min(pages_needed, self.MAX_PAGES)

        params: dict[str, Any] = {}
        if query.location:
            params["location"] = query.location

        # Fetch pages 1..page_count concurrently; preserve page order in results.
        results: list[list[dict[str, Any]]] = [[] for _ in range(page_count)]

        async def _fetch_page(idx: int) -> None:
            page = idx + 1  # API pages are 1-indexed.
            data = await fetcher.get_json(_API, params={**params, "page": page})
            if isinstance(data, dict):
                items = data.get("results") or []
                results[idx] = [j for j in items if isinstance(j, dict)]

        async with anyio.create_task_group() as tg:
            for idx in range(page_count):
                tg.start_soon(_fetch_page, idx)

        raws = [self._to_raw(job) for page_items in results for job in page_items]
        if query.limit is not None:
            raws = raws[: query.limit]
        return raws

    def _to_raw(self, job: dict[str, Any]) -> RawJob:
        company = job.get("company") or {}
        refs = job.get("refs") or {}
        return RawJob(
            source=self.name,
            source_job_id=str(job.get("id", "")),
            company=(company.get("name") if isinstance(company, dict) else None) or "",
            token=None,
            url=refs.get("landing_page") if isinstance(refs, dict) else None,
            payload=job,
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        refs = p.get("refs") or {}
        apply_url = refs.get("landing_page") if isinstance(refs, dict) else None
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("name") or "").strip(),
            description_html=p.get("contents"),
            locations=self._locations(p),
            remote=self._remote(p),
            employment_type=self._employment_type(p.get("type")),
            department=self._department(p),
            apply_url=apply_url,
            posted_at=_parse_dt(p.get("publication_date")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _location_names(p: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for loc in p.get("locations") or []:
            if isinstance(loc, dict):
                name = loc.get("name")
                if name:
                    out.append(name)
        return out

    @classmethod
    def _locations(cls, p: dict[str, Any]) -> list[Location]:
        locs: list[Location] = []
        for name in cls._location_names(p):
            is_remote = any(h in name.lower() for h in _REMOTE_HINTS)
            locs.append(Location(raw=name, is_remote=is_remote))
        return locs

    @classmethod
    def _remote(cls, p: dict[str, Any]) -> RemoteType:
        for name in cls._location_names(p):
            if any(h in name.lower() for h in _REMOTE_HINTS):
                return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    @staticmethod
    def _employment_type(value: str | None) -> EmploymentType:
        if not value:
            return EmploymentType.UNKNOWN
        return _EMPLOYMENT_MAP.get(value.strip().lower(), EmploymentType.UNKNOWN)

    @staticmethod
    def _department(p: dict[str, Any]) -> str | None:
        cats = p.get("categories") or []
        if cats and isinstance(cats[0], dict):
            return cats[0].get("name")
        return None
