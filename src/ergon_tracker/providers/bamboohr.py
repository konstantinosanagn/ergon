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

from ..extract.comp import parse_salary
from ..models import (
    DetailFetch,
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

__all__ = ["BambooHRProvider"]

_API = "https://{token}.bamboohr.com/careers/list"
_APPLY = "https://{token}.bamboohr.com/careers/{job_id}"
_DETAIL_API = "https://{token}.bamboohr.com/careers/{job_id}/detail"
# Pull (token, id) from an apply/listing URL like ``https://acme.bamboohr.com/careers/109``.
_DETAIL_URL_RE = re.compile(r"([^/.\s]+)\.bamboohr\.com/careers/(\d+)", re.I)

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

    @staticmethod
    def _parse_detail_ref(ref: DetailRef) -> tuple[str, str] | None:
        for url in (ref.apply_url, ref.listing_url):
            if url:
                m = _DETAIL_URL_RE.search(url)
                if m:
                    return m.group(1), m.group(2)
        if ref.token and ref.id:
            return ref.token, ref.id
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD + structured pay (Tier-3 recovery).

        The list feed (``fetch``) is intentionally thin (no description, no pay), but the per-posting
        ``/careers/{id}/detail`` endpoint returns ``result.jobOpening`` with a full ``description``
        (HTML) AND a free-text ``compensation`` string (e.g. ``"$85K - 135K Base per year DOE"``).
        Return a ``DetailFetch`` carrying the description body (so yoe/degree also extract) plus the
        parsed ``compensation`` as a structured salary, preferred over re-parsing it from the body.
        Non-raising: any unparseable ref, fetch failure, non-dict payload, or missing/empty
        ``description`` returns ``None``.
        """
        parsed = self._parse_detail_ref(ref)
        if parsed is None:
            return None
        token, job_id = parsed
        try:
            data = await fetcher.get_json(_DETAIL_API.format(token=token, job_id=job_id))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        opening = (data.get("result") or {}).get("jobOpening")
        if not isinstance(opening, dict):
            return None
        description = opening.get("description")
        if not isinstance(description, str) or not description.strip():
            return None
        comp = opening.get("compensation")
        salary = parse_salary(comp) if isinstance(comp, str) else None
        locations = self._detail_location(opening)
        if salary is not None or locations:
            return DetailFetch(text=description, salary=salary, locations=locations or None)
        return description

    @staticmethod
    def _detail_location(opening: dict[str, Any]) -> list[Location]:
        """Structured location from the detail ``jobOpening`` — prefer ``location`` (carries
        ``addressCountry``); fall back to ``atsLocation``. Fills the index row's NULL city/country."""
        for key, ckey in (("location", "addressCountry"), ("atsLocation", "country")):
            loc = opening.get(key)
            if not isinstance(loc, dict):
                continue
            city = (loc.get("city") or "").strip() or None
            region = (loc.get("state") or loc.get("province") or "").strip() or None
            country = (loc.get(ckey) or "").strip() or None
            if any((city, region, country)):
                raw = ", ".join(p for p in (city, region, country) if p)
                return [Location(raw=raw, city=city, region=region, country=country)]
        return []

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        location = self._location(p)
        remote = self._remote(p)

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
    def _remote(p: dict[str, Any]) -> RemoteType:
        if p.get("isRemote"):
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN
