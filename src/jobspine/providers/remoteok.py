"""RemoteOK provider — an aggregator (not a per-company ATS).

``GET https://remoteok.com/api`` returns a JSON list whose **first element is legal/metadata**
and must be skipped. Every posting is remote by definition. Because this is an aggregator it is
never auto-discovered from a company URL, so ``matches`` always returns ``None`` and the
orchestrator invokes it with an empty ``token``. A single request covers the whole feed, so no
fan-out is required.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import JobPosting, Location, RawJob, RemoteType, Salary
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://remoteok.com/api"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@register("remoteok")
class RemoteOKProvider(BaseProvider):
    name = "remoteok"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        data = await fetcher.get_json(_API)
        # First element is legal/metadata — skip it.
        items = [j for j in data[1:] if isinstance(j, dict)] if isinstance(data, list) else []
        if query.limit is not None:
            items = items[: query.limit]
        return [
            RawJob(
                source=self.name,
                source_job_id=str(job.get("id", "")),
                company=job.get("company") or "",
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
            title=(p.get("position") or "").strip(),
            description_html=p.get("description"),
            locations=self._locations(p),
            remote=RemoteType.REMOTE,
            salary=self._salary(p),
            apply_url=p.get("apply_url") or p.get("url"),
            posted_at=_parse_dt(p.get("date")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        raw_loc = (p.get("location") or "").strip()
        if not raw_loc:
            return [Location(is_remote=True)]
        return [Location(raw=raw_loc, is_remote=True)]

    @staticmethod
    def _salary(p: dict[str, Any]) -> Salary | None:
        min_v = p.get("salary_min") or None
        max_v = p.get("salary_max") or None
        if not min_v and not max_v:
            return None
        return Salary(min_amount=min_v, max_amount=max_v, currency="USD")
