"""SmartRecruiters job-board provider.

SmartRecruiters exposes a free, unauthenticated public Posting API:
``GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=0``
returning ``{offset, limit, totalFound, content: [...]}``. The API supports server-side
``q`` (keyword), ``country`` and ``city`` filters, which :meth:`fetch` forwards from the
``SearchQuery`` when present.

The listing endpoint is paginated by ``offset`` (``limit`` caps at 100). :meth:`fetch`
pulls the first page to learn ``totalFound`` and then fetches the remaining pages
concurrently. The listing carries enough to normalize (title, location, department,
release date, apply url) but not the full description or salary — those would require a
per-posting detail call, which we deliberately skip to keep ``fetch`` to one batch.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import anyio

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

__all__ = ["SmartRecruitersProvider"]

_API = "https://api.smartrecruiters.com/v1/companies/{token}/postings"

_PAGE_LIMIT = 100

# Hosts we recognise, each capturing the board token as group 1.
_HOST_PATTERNS = (
    re.compile(r"api\.smartrecruiters\.com/v1/companies/([^/?#]+)", re.I),
    re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([^/?#]+)", re.I),
)

# SmartRecruiters ``typeOfEmployment.id`` / ``.label`` → canonical EmploymentType.
_EMPLOYMENT_BY_TYPE = {
    "permanent": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "regular": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "trainee": EmploymentType.INTERNSHIP,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # SmartRecruiters uses RFC3339 with a trailing ``Z`` which fromisoformat rejects
    # on older Pythons; normalize it to a UTC offset.
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@register("smartrecruiters")
class SmartRecruitersProvider(BaseProvider):
    name = "smartrecruiters"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token not in ("v1", "companies"):
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        url = _API.format(token=token)
        base_params: dict[str, str] = {}
        if query.keywords:
            base_params["q"] = query.keywords
        if query.country:
            base_params["country"] = query.country
        if query.city:
            base_params["city"] = query.city

        first = await fetcher.get_json(
            url, params={**base_params, "limit": str(_PAGE_LIMIT), "offset": "0"}
        )
        postings: list[dict[str, Any]] = list(self._content(first))

        total = int(first.get("totalFound", len(postings)) or 0)
        pages = math.ceil(total / _PAGE_LIMIT) if total else 1
        if pages > 1:
            # Fetch the remaining pages concurrently; collect per-offset to preserve order.
            results: dict[int, list[dict[str, Any]]] = {}

            async def _fetch_page(offset: int) -> None:
                data = await fetcher.get_json(
                    url, params={**base_params, "limit": str(_PAGE_LIMIT), "offset": str(offset)}
                )
                results[offset] = list(self._content(data))

            async with anyio.create_task_group() as tg:
                for page in range(1, pages):
                    tg.start_soon(_fetch_page, page * _PAGE_LIMIT)

            for offset in sorted(results):
                postings.extend(results[offset])

        raws: list[RawJob] = []
        for posting in postings:
            company = (posting.get("company") or {}).get("name") or token
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(posting.get("id", "")),
                    company=company,
                    token=token,
                    url=self._apply_url(posting, token),
                    payload=posting,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = p.get("location") or {}

        location = self._location(loc)
        locations = [location] if location else []

        remote = self._remote(loc)
        employment_type = self._employment_type(p.get("typeOfEmployment") or {})
        department = (p.get("department") or {}).get("label")

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("name") or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=department,
            salary=None,  # not exposed by the listing endpoint
            posted_at=_parse_dt(p.get("releasedDate")),
            raw=raw.payload,
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _content(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, list):
                return content
        return []

    @staticmethod
    def _apply_url(posting: dict[str, Any], token: str) -> str | None:
        job_id = posting.get("id")
        if job_id:
            return f"https://jobs.smartrecruiters.com/{token}/{job_id}"
        return None

    @staticmethod
    def _location(loc: dict[str, Any]) -> Location | None:
        city = loc.get("city")
        region = loc.get("region")
        country = loc.get("country")
        is_remote = bool(loc.get("remote"))
        raw = loc.get("fullLocation") or ", ".join(str(p) for p in (city, region, country) if p)
        if not any((city, region, country, raw, is_remote)):
            return None
        return Location(
            city=city or None,
            region=region or None,
            country=(str(country).upper() if country else None),
            raw=raw or None,
            is_remote=is_remote,
        )

    @staticmethod
    def _remote(loc: dict[str, Any]) -> RemoteType:
        if loc.get("remote"):
            return RemoteType.REMOTE
        if loc.get("hybrid"):
            return RemoteType.HYBRID
        # An explicit location object with both flags False implies on-site.
        if any(loc.get(k) for k in ("city", "region", "country", "fullLocation")):
            return RemoteType.ONSITE
        return RemoteType.UNKNOWN

    @staticmethod
    def _employment_type(type_obj: dict[str, Any]) -> EmploymentType:
        for key in ("id", "label"):
            value = str(type_obj.get(key) or "").strip().lower()
            mapped = _EMPLOYMENT_BY_TYPE.get(value)
            if mapped is not None:
                return mapped
        return EmploymentType.UNKNOWN
