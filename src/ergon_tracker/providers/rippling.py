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
from typing import TYPE_CHECKING

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
    from ..index.detail import DetailRef

__all__ = ["RipplingProvider"]

_API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"

# Per-posting detail resource (Tier-3 JD recovery): the list URL above with ``/{uuid}`` appended.
_DETAIL_API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{uuid}"

# Capture the slug from ``ats.rippling.com/{slug}`` or ``ats.rippling.com/{slug}/jobs``.
_HOST_RE = re.compile(r"ats\.rippling\.com/([^/?#]+)", re.IGNORECASE)

# Public apply/listing URL shape: ``ats.rippling.com/{token}/jobs/{uuid}``. Captures both the
# board token (group 1) and the posting uuid (group 2).
_DETAIL_URL_RE = re.compile(r"ats\.rippling\.com/([^/?#]+)/jobs/([^/?#]+)", re.IGNORECASE)


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

    @staticmethod
    def _parse_detail_ref(ref: DetailRef) -> tuple[str, str] | None:
        """Derive (token, uuid) for the detail-resource URL from ``ref``.

        Prefers parsing ``apply_url``/``listing_url`` (the ``ats.rippling.com/{token}/jobs/{uuid}``
        shape) for robustness; falls back to ``ref.token`` paired with ``ref.id`` (the uuid) when
        the URL doesn't parse. Returns ``None`` if neither yields a usable (token, uuid) pair."""
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            m = _DETAIL_URL_RE.search(url)
            if m:
                token = m.group(1).strip("/")
                uuid = m.group(2).strip("/")
                if token and uuid:
                    return token, uuid
        if ref.token and ref.id:
            return ref.token, ref.id
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via the per-posting detail resource (Tier-3 recovery).

        The detail URL is the list URL (``_API``) with ``/{uuid}`` appended; ``token`` and
        ``uuid`` are derived from ``ref.apply_url``/``ref.listing_url`` (the
        ``ats.rippling.com/{token}/jobs/{uuid}`` shape), falling back to ``ref.token``/``ref.id``.
        The response's ``description`` is a DICT of HTML sections keyed by heading (e.g.
        ``{"company": "<p>...</p>", "role": "..."}``) — all string values are concatenated (in
        insertion order) joined by ``"\\n"``. A plain-string ``description`` is used directly.
        Non-raising: any unparseable ref, fetch failure, non-JSON payload, or shape mismatch
        (including a truthy non-dict payload/description) returns ``None``, never an exception.
        """
        parsed = self._parse_detail_ref(ref)
        if parsed is None:
            return None
        token, uuid = parsed
        url = _DETAIL_API.format(token=token, uuid=uuid)
        try:
            data = await fetcher.get_json(url)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        description = data.get("description")
        if isinstance(description, str):
            return description if description.strip() else None
        if isinstance(description, dict):
            parts = [v for v in description.values() if isinstance(v, str) and v.strip()]
            if not parts:
                return None
            return "\n".join(parts)
        return None

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
