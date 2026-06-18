"""Breezy HR job-board provider.

Breezy HR exposes a free, unauthenticated public positions API:
``GET https://{token}.breezy.hr/json`` which returns a JSON *array* of every published
position for a company in one call. There is no server-side filtering, so :meth:`fetch`
returns the whole board and the orchestrator applies ``SearchQuery.matches`` client-side.
"""

from __future__ import annotations

import re
from datetime import datetime
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

__all__ = ["BreezyProvider"]

_API = "https://{token}.breezy.hr/json"

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.breezy\.hr", re.I),)

# Breezy ``type.name`` values (e.g. "Full-Time", "Part-Time", "Contractor", "Intern").
_EMPLOYMENT_BY_NAME = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Breezy timestamps look like "2026-06-04T23:24:11.988Z".
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _employment(type_obj: Any) -> EmploymentType:
    name = type_obj.get("name") if isinstance(type_obj, dict) else type_obj
    if not name:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT_BY_NAME.get(str(name).strip().lower(), EmploymentType.UNKNOWN)


@register("breezy")
class BreezyProvider(BaseProvider):
    name = "breezy"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token != "www":
                    return token
        return None

    def conditional_url(self, token: str) -> str | None:
        # Whole board in one JSON response with a strong ETag (honors If-None-Match -> 304).
        return _API.format(token=token)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Breezy has no server-side filtering: pull the whole board in one request.
        data = await fetcher.get_json(_API.format(token=token))
        return self._raws_from_data(data, token)

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob]:
        """Parse an already-downloaded body (from a conditional 200), avoiding a refetch."""
        import json

        return self._raws_from_data(json.loads(body), token)

    def _raws_from_data(self, data: Any, token: str) -> list[RawJob]:
        positions: list[dict[str, Any]] = data if isinstance(data, list) else []
        raws: list[RawJob] = []
        for pos in positions:
            company = pos.get("company")
            company_name = company.get("name") if isinstance(company, dict) else None
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(pos.get("id") or pos.get("_id") or ""),
                    company=company_name or token,
                    token=token,
                    url=pos.get("url"),
                    payload=pos,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        location = self._location(p)
        remote = (
            RemoteType.REMOTE
            if (location is not None and location.is_remote)
            else (RemoteType.UNKNOWN)
        )

        category = p.get("category")
        department = (
            (category.get("name") if isinstance(category, dict) else None)
            or (p.get("department") if isinstance(p.get("department"), str) else None)
            or None
        )

        description_html = p.get("description") or None
        description_text = self._to_text(description_html)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("name") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url"),
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p.get("type")),
            department=department,
            salary=None,  # present in feed as a free-text string, not normalized here
            posted_at=_parse_dt(p.get("published_date") or p.get("creation_date")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    @staticmethod
    def _location(p: dict[str, Any]) -> Location | None:
        loc = p.get("location")
        if not isinstance(loc, dict):
            return None
        city = (loc.get("city") or "").strip() or None
        state = loc.get("state")
        region = (state.get("name") if isinstance(state, dict) else state) or None
        if isinstance(region, str):
            region = region.strip() or None
        country = loc.get("country")
        country = (country.get("name") if isinstance(country, dict) else country) or None
        if isinstance(country, str):
            country = country.strip() or None
        raw_loc = (loc.get("name") or "").strip() or None
        is_remote = bool(loc.get("is_remote")) or "remote" in (raw_loc or "").lower()
        if not any((city, region, country, raw_loc)) and not is_remote:
            return None
        return Location(
            city=city,
            region=region,
            country=country,
            raw=raw_loc,
            is_remote=is_remote,
        )

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
