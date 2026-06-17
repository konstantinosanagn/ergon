"""BambooHR job-board provider.

BambooHR exposes a free, unauthenticated public careers feed:
``GET https://{token}.bamboohr.com/careers/list`` which returns every open posting for a
company in one call as ``{"meta": {...}, "result": [ ... ]}``. There is no server-side
filtering, so :meth:`fetch` returns the whole board and the orchestrator applies
``SearchQuery.matches`` client-side.

The list feed is intentionally thin: each entry carries an id, ``jobOpeningName``,
``departmentLabel``, ``employmentStatusLabel``, a location (either the legacy ``location``
``{city, state}`` blob or the newer structured ``atsLocation`` ``{country, state, province,
city}``), an ``isRemote`` flag and a ``locationType`` code. It exposes neither a posting date
nor a description, so those normalize to ``None`` (never invented). The apply URL is the
canonical ``https://{token}.bamboohr.com/careers/{id}`` page.
"""

from __future__ import annotations

import re
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

__all__ = ["BambooHRProvider"]

_API = "https://{token}.bamboohr.com/careers/list"
_APPLY = "https://{token}.bamboohr.com/careers/{job_id}"

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.bamboohr\.com", re.I),)

# BambooHR's ``employmentStatusLabel`` (free-text, e.g. "Full-Time", "Part Time", "Intern").
_EMPLOYMENT_BY_KEY = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "seasonal": EmploymentType.TEMPORARY,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
}


def _employment(label: str | None) -> EmploymentType:
    if not label:
        return EmploymentType.UNKNOWN
    key = re.sub(r"[^a-z]", "", label.lower())
    return _EMPLOYMENT_BY_KEY.get(key, EmploymentType.UNKNOWN)


@register("bamboohr")
class BambooHRProvider(BaseProvider):
    name = "bamboohr"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token != "www":
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # BambooHR has no server-side filtering: pull the whole board in one request.
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        jobs: list[dict[str, Any]] = data.get("result", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for job in jobs:
            job_id = str(job.get("id", ""))
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=job_id,
                    company=token,  # company name is not exposed by the feed
                    token=token,
                    url=_APPLY.format(token=token, job_id=job_id),
                    payload=job,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        location = self._location(p)
        remote = self._remote(p, location)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("jobOpeningName") or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p.get("employmentStatusLabel")),
            department=(p.get("departmentLabel") or "").strip() or None,
            salary=None,  # not exposed by the feed
            posted_at=None,  # the list feed carries no posting date
            description_html=None,  # not exposed by the feed
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _location(p: dict[str, Any]) -> Location | None:
        # Prefer the newer structured ``atsLocation``; fall back to the legacy ``location``.
        ats = p.get("atsLocation") or {}
        city = (ats.get("city") or "").strip() or None
        region = (ats.get("state") or ats.get("province") or "").strip() or None
        country = (ats.get("country") or "").strip() or None

        if not any((city, region, country)):
            legacy = p.get("location") or {}
            city = (legacy.get("city") or "").strip() or None
            region = (legacy.get("state") or "").strip() or None

        is_remote = bool(p.get("isRemote"))
        if not any((city, region, country)) and not is_remote:
            return None

        raw_loc = ", ".join(part for part in (city, region, country) if part) or None
        return Location(
            city=city,
            region=region,
            country=country,
            raw=raw_loc,
            is_remote=is_remote,
        )

    @staticmethod
    def _remote(p: dict[str, Any], location: Location | None) -> RemoteType:
        if p.get("isRemote"):
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN
