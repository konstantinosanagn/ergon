"""Recruitee job-board provider.

Recruitee exposes a free, unauthenticated public offers API:
``GET https://{token}.recruitee.com/api/offers/`` which returns every published offer for a
company in one call. There is no server-side filtering, so :meth:`fetch` returns the whole
board and the orchestrator applies ``SearchQuery.matches`` client-side.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
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

__all__ = ["RecruiteeProvider"]

_API = "https://{token}.recruitee.com/api/offers/"

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.recruitee\.com", re.I),)

# Recruitee's ``employment_type_code`` (e.g. "fulltime_permanent", "parttime_fixed_term").
_EMPLOYMENT_BY_PREFIX = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
    "freelance": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
}

# Recruitee timestamps look like "2026-06-08 13:03:54 UTC".
_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    m = _DT_RE.match(value.strip())
    if not m:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
    except ValueError:
        return None
    # Recruitee feed reports times in UTC.
    return dt.replace(tzinfo=timezone.utc)


def _employment(code: str | None) -> EmploymentType:
    if not code:
        return EmploymentType.UNKNOWN
    prefix = code.strip().lower().split("_", 1)[0]
    return _EMPLOYMENT_BY_PREFIX.get(prefix, EmploymentType.UNKNOWN)


@register("recruitee")
class RecruiteeProvider(BaseProvider):
    name = "recruitee"

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
        # Recruitee has no server-side filtering: pull the whole board in one request.
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        offers: list[dict[str, Any]] = data.get("offers", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for offer in offers:
            company = offer.get("company_name") or token
            apply_url = offer.get("careers_apply_url") or offer.get("careers_url")
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(offer.get("id", "")),
                    company=company,
                    token=token,
                    url=apply_url,
                    payload=offer,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        location = self._location(p)
        remote = self._remote(p, location)

        description_html = p.get("description") or None
        description_text = self._to_text(description_html)

        apply_url = p.get("careers_apply_url") or p.get("careers_url")

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=apply_url,
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p.get("employment_type_code")),
            department=p.get("department") or None,
            salary=None,  # present in feed but not normalized here (kept in raw)
            posted_at=_parse_dt(p.get("published_at") or p.get("created_at")),
            updated_at=_parse_dt(p.get("updated_at")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    @staticmethod
    def _location(p: dict[str, Any]) -> Location | None:
        city = (p.get("city") or "").strip() or None
        region = (p.get("state_name") or "").strip() or None
        country = (p.get("country") or "").strip() or None
        raw_loc = (p.get("location") or "").strip() or None
        is_remote = bool(p.get("remote")) or (
            not bool(p.get("on_site", True)) and not city and not raw_loc
        )
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
    def _remote(p: dict[str, Any], location: Location | None) -> RemoteType:
        if p.get("remote"):
            return RemoteType.REMOTE
        if p.get("hybrid"):
            return RemoteType.HYBRID
        if p.get("on_site"):
            return RemoteType.ONSITE
        if location is not None and location.is_remote:
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
