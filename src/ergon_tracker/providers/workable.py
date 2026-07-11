"""Workable job-board provider.

Workable exposes a free, unauthenticated public widget endpoint:
``GET https://apply.workable.com/api/v1/widget/accounts/{token}`` returning
``{name, description, jobs: [...]}`` in a single call (no pagination). Each job carries a
``shortcode`` (the stable id), ``title``, ``employment_type`` label, structured
``locations`` plus flat ``country``/``city``/``state`` fields, a ``telecommuting`` remote
flag, ``department`` and an apply ``url``. There is no server-side filtering, so
:meth:`fetch` returns the whole board and the orchestrator applies
``SearchQuery.matches`` client-side.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any

from ..extract.level import level_from_ats_vocab
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

__all__ = ["WorkableProvider"]

_API = "https://apply.workable.com/api/v1/widget/accounts/{token}"

# Hosts we recognise, each capturing the board token as group 1.
_HOST_PATTERNS = (
    re.compile(r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([^/?#]+)", re.I),
    re.compile(r"([^./?#]+)\.workable\.com", re.I),
)

# Workable ``employment_type`` labels → canonical EmploymentType.
_EMPLOYMENT_BY_LABEL = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


@register("workable")
class WorkableProvider(BaseProvider):
    name = "workable"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token not in ("apply", "www", "api"):
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Single call: Workable returns the whole board (no server-side filters).
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        account = data.get("name") or token if isinstance(data, dict) else token
        jobs: list[dict[str, Any]] = data.get("jobs", []) if isinstance(data, dict) else []

        raws: list[RawJob] = []
        for job in jobs:
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(job.get("shortcode", "")),
                    company=account,
                    token=token,
                    url=job.get("url") or job.get("shortlink"),
                    payload=job,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        locations = self._locations(p)
        telecommuting = bool(p.get("telecommuting"))
        remote = RemoteType.REMOTE if telecommuting else self._remote(locations)
        employment_type = self._employment_type(p.get("employment_type"))

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url") or p.get("application_url") or p.get("shortlink"),
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=p.get("department") or None,
            level=level_from_ats_vocab(p.get("experience")),
            salary=None,  # not exposed by the widget endpoint
            posted_at=_parse_date(p.get("published_on")),
            raw=raw.payload,
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        telecommuting = bool(p.get("telecommuting"))
        structured = p.get("locations")
        if isinstance(structured, list) and structured:
            out: list[Location] = []
            for loc in structured:
                if not isinstance(loc, dict):
                    continue
                out.append(
                    Location(
                        city=loc.get("city") or None,
                        region=loc.get("region") or None,
                        country=loc.get("country") or None,
                        raw=WorkableProvider._raw_text(
                            loc.get("city"), loc.get("region"), loc.get("country")
                        ),
                        is_remote=telecommuting,
                    )
                )
            if out:
                return out

        # Fall back to the flat country/city/state fields.
        city = p.get("city")
        region = p.get("state")
        country = p.get("country")
        raw = WorkableProvider._raw_text(city, region, country)
        if not any((city, region, country, telecommuting)):
            return []
        return [
            Location(
                city=city or None,
                region=region or None,
                country=country or None,
                raw=raw,
                is_remote=telecommuting,
            )
        ]

    @staticmethod
    def _raw_text(*parts: Any) -> str | None:
        text = ", ".join(str(p) for p in parts if p)
        return text or None

    @staticmethod
    def _remote(locations: list[Location]) -> RemoteType:
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        if locations:
            return RemoteType.ONSITE
        return RemoteType.UNKNOWN

    @staticmethod
    def _employment_type(label: str | None) -> EmploymentType:
        key = str(label or "").strip().lower()
        if not key:
            return EmploymentType.UNKNOWN
        return _EMPLOYMENT_BY_LABEL.get(key, EmploymentType.OTHER)
