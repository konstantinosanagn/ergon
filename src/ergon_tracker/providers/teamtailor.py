"""Teamtailor job-board provider.

Teamtailor exposes a free, unauthenticated public careers feed:
``GET https://{token}.teamtailor.com/jobs.json`` returns every open job for a company in
one call as a `JSON Feed <https://www.jsonfeed.org/>`_ document (``items`` list). This is the
public careers-site feed, **not** the keyed ``api.teamtailor.com`` REST API.

The feed is largely a *summary*: each item carries ``title``, ``url``, ``date_published`` and
an HTML body (``content_html``), plus an embedded schema.org ``JobPosting`` block under
``_jobposting`` that supplies the company name and structured ``jobLocation`` address. Fields
such as ``employmentType`` and ``jobLocationType`` are usually absent, so those normalize to
``UNKNOWN`` unless the payload actually contains them. There is no server-side filtering, so
:meth:`fetch` returns the whole board and the orchestrator applies ``SearchQuery.matches``
client-side.
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

__all__ = ["TeamtailorProvider"]

_API = "https://{token}.teamtailor.com/jobs.json"

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.teamtailor\.com", re.I),)

# Subdomains that are not company boards.
_RESERVED = {"www", "api"}

# schema.org employmentType values -> our enum (the feed rarely sets these).
_EMPLOYMENT_BY_CODE = {
    "full_time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "other": EmploymentType.OTHER,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _employment(value: Any) -> EmploymentType:
    if not value:
        return EmploymentType.UNKNOWN
    code = str(value).strip().lower().replace("-", "_")
    return _EMPLOYMENT_BY_CODE.get(code, EmploymentType.UNKNOWN)


@register("teamtailor")
class TeamtailorProvider(BaseProvider):
    name = "teamtailor"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token.lower() not in _RESERVED:
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Teamtailor has no server-side filtering: pull the whole board in one request.
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        items: list[dict[str, Any]] = data.get("items", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for item in items:
            jp = item.get("_jobposting") or {}
            org = jp.get("hiringOrganization") or {}
            company = (org.get("name") or "").strip() or token
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(item.get("id", "")),
                    company=company,
                    token=token,
                    url=item.get("url") or None,
                    payload=item,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        jp = p.get("_jobposting") or {}

        title = (p.get("title") or jp.get("title") or "").strip()
        apply_url = p.get("url") or None

        locations = self._locations(jp)
        remote = self._remote(jp, title, locations)

        description_html = p.get("content_html") or jp.get("description") or None
        description_text = self._to_text(description_html)

        posted_at = _parse_dt(p.get("date_published") or jp.get("datePosted"))

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            fetched_at=raw.fetched_at,
            apply_url=apply_url,
            locations=locations,
            remote=remote,
            employment_type=_employment(jp.get("employmentType")),
            posted_at=posted_at,
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    @staticmethod
    def _locations(jp: dict[str, Any]) -> list[Location]:
        raw_places = jp.get("jobLocation")
        if isinstance(raw_places, dict):
            raw_places = [raw_places]
        if not isinstance(raw_places, list):
            return []
        out: list[Location] = []
        for place in raw_places:
            if not isinstance(place, dict):
                continue
            addr = place.get("address") or {}
            city = (addr.get("addressLocality") or "").strip() or None
            region = (addr.get("addressRegion") or "").strip() or None
            country = (addr.get("addressCountry") or "").strip() or None
            if not any((city, region, country)):
                continue
            raw_text = ", ".join(p for p in (city, region, country) if p) or None
            out.append(Location(city=city, region=region, country=country, raw=raw_text))
        return out

    @staticmethod
    def _remote(jp: dict[str, Any], title: str, locations: list[Location]) -> RemoteType:
        loc_type = str(jp.get("jobLocationType") or "").strip().upper()
        if loc_type == "TELECOMMUTE":
            return RemoteType.REMOTE
        haystack = " ".join([title, *(loc.raw or "" for loc in locations)]).lower()
        if "remote" in haystack:
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
