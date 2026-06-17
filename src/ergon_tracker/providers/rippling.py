"""Rippling ATS job-board provider.

Rippling exposes a free, unauthenticated public board API:
``GET https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs`` returning a
JSON array of summary postings. The careers host is ``ats.rippling.com/{token}/jobs``;
the API host is ``api.rippling.com``. The board ``token`` is the careers-URL slug
verbatim (e.g. ``11fs-group-ltd``, ``1nhealth``) — no ``-careers`` suffix.

Each list entry is summary-only (no description, salary, or dates), e.g.::

    {
      "uuid": "3c36...",
      "name": "Senior Sales Executive",
      "department": {"id": "Pulse", "label": "Pulse"},
      "url": "https://ats.rippling.com/11fs-group-ltd/jobs/3c36...",
      "workLocation": {"label": "London, United Kingdom", "id": "London, United Kingdom"}
    }
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

__all__ = ["RipplingProvider"]

_API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"

# Capture the slug from ``ats.rippling.com/{slug}`` or ``ats.rippling.com/{slug}/jobs``.
_HOST_RE = re.compile(r"ats\.rippling\.com/([^/?#]+)", re.IGNORECASE)


@register("rippling")
class RipplingProvider(BaseProvider):
    name = "rippling"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        match = _HOST_RE.search(url_or_host)
        if not match:
            return None
        token = match.group(1).strip()
        return token or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        # Response is a JSON array; tolerate a dict wrapper defensively.
        if isinstance(data, list):
            jobs = data
        elif isinstance(data, dict):
            jobs = data.get("jobs") or data.get("results") or data.get("data") or []
        else:
            jobs = []

        raws: list[RawJob] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(job.get("uuid", "")),
                    company=token,
                    token=token,
                    url=job.get("url"),
                    payload=job,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        department = (p.get("department") or {}).get("label")

        work_location = p.get("workLocation") or {}
        label = work_location.get("label")
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if label:
            is_remote = "remote" in label.lower()
            if is_remote:
                remote = RemoteType.REMOTE
            locations = [self._location(label, is_remote)]

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("name") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url"),
            locations=locations,
            remote=remote,
            employment_type=EmploymentType.UNKNOWN,
            department=department,
            salary=None,
            posted_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _location(label: str, is_remote: bool) -> Location:
        """Parse ``"City, Country"`` when trivially splittable, else keep the raw label."""
        city = country = None
        # Only split the plain "City, Country" shape (no parentheses, exactly two parts).
        if "(" not in label and ")" not in label:
            parts = [part.strip() for part in label.split(",")]
            if len(parts) == 2 and all(parts):
                city, country = parts
        return Location(raw=label, city=city, country=country, is_remote=is_remote)
