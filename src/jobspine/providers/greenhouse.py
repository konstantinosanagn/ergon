"""Greenhouse job-board provider.

Greenhouse exposes a free, unauthenticated public board API:
``GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``
which returns every active posting for a board in one call. There are no server-side
filters, so :meth:`fetch` returns the full board and the orchestrator applies
``SearchQuery.matches`` client-side.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import TYPE_CHECKING, Any

from ..models import (
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    SearchQuery,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["GreenhouseProvider"]

_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

# Hosts we recognise, each capturing the board token as group 1.
_HOST_PATTERNS = (
    re.compile(r"boards-api\.greenhouse\.io/v1/boards/([^/?#]+)", re.I),
    re.compile(r"(?:job-)?boards\.greenhouse\.io/([^/?#]+)", re.I),
)

_REMOTE_BY_WORKPLACE = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "on-site": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
    "in office": RemoteType.ONSITE,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@register("greenhouse")
class GreenhouseProvider(BaseProvider):
    name = "greenhouse"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token not in ("v1", "boards"):
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Greenhouse has no server-side filtering: pull the whole board in one request.
        url = _API.format(token=token)
        data = await fetcher.get_json(url, params={"content": "true"})
        postings: list[dict[str, Any]] = data.get("jobs", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for posting in postings:
            company = posting.get("company_name") or token
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(posting.get("id", "")),
                    company=company,
                    token=token,
                    url=posting.get("absolute_url"),
                    payload=posting,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        # Locations: prefer the structured offices list, fall back to the single location blob.
        locations: list[Location] = []
        for office in p.get("offices") or []:
            name = (office.get("name") or office.get("location") or "").strip()
            if name:
                locations.append(Location(raw=name, is_remote="remote" in name.lower()))
        if not locations:
            loc_name = ((p.get("location") or {}).get("name") or "").strip()
            if loc_name:
                locations.append(Location(raw=loc_name, is_remote="remote" in loc_name.lower()))

        remote = self._remote(p, locations)

        departments = p.get("departments") or []
        department = departments[0].get("name") if departments else None

        content = p.get("content")
        description_html = unescape(content) if content else None
        description_text = self._to_text(description_html)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("absolute_url"),
            locations=locations,
            remote=remote,
            employment_type=EmploymentType.UNKNOWN,  # not exposed by the board API
            department=department,
            salary=None,  # not exposed by the board API
            posted_at=_parse_dt(p.get("first_published")),
            updated_at=_parse_dt(p.get("updated_at")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    @staticmethod
    def _remote(p: dict[str, Any], locations: list[Location]) -> RemoteType:
        for entry in p.get("metadata") or []:
            if (entry.get("name") or "").strip().lower() == "workplace type":
                value = str(entry.get("value") or "").strip().lower()
                mapped = _REMOTE_BY_WORKPLACE.get(value)
                if mapped is not None:
                    return mapped
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
