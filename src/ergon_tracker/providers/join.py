"""join.com provider — jobs are server-rendered into the careers page's ``__NEXT_DATA__``.

join.com (a large European ATS, ~23k company career sites) is a Next.js app with no usable
public JSON API (the ``/api/...`` endpoints are auth-walled / 422). But every careers page
``https://join.com/companies/{token}`` embeds its jobs in the page's
``<script id="__NEXT_DATA__">`` blob at ``props.pageProps.initialState.jobs.items`` — so one
unauthenticated GET yields structured job data, no browser required.

Pagination: ``initialState.jobs.pagination`` reports ``perPage`` (5) and ``pageCount``; extra
pages are fetched with ``?page=N`` (SSR re-renders the slice). We fetch page 1 to learn the
count, then fetch the remaining pages **concurrently**, bounded by ``MAX_PAGES`` and
``query.limit``.

The list blob has no job description or salary amount (those live on the per-job detail page),
so ``description``/``salary`` are ``None`` here — never invented.
"""

from __future__ import annotations

import json
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

__all__ = ["JoinProvider"]

_CAREERS = "https://join.com/companies/{token}"
_JOB_URL = "https://join.com/companies/{token}/jobs/{id_param}"

# Hosts/paths we recognise, capturing the company token (slug) as group 1.
_HOST_PATTERNS = (re.compile(r"join\.com/companies/([^/?#\s]+)", re.IGNORECASE),)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)

# join.com ``employmentType.name`` vocabulary -> our enum.
_EMPLOYMENT = {
    "employee": EmploymentType.FULL_TIME,
    "working student": EmploymentType.PART_TIME,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "apprenticeship": EmploymentType.INTERNSHIP,
    "trainee": EmploymentType.INTERNSHIP,
    "freelancer": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
}

_WORKPLACE = {
    "onsite": RemoteType.ONSITE,
    "on_site": RemoteType.ONSITE,
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
}


def _parse_initial_state(html: str) -> dict[str, Any]:
    """Extract ``props.pageProps.initialState`` from a careers page, or ``{}``."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    state = data.get("props", {}).get("pageProps", {}).get("initialState", {})
    return state if isinstance(state, dict) else {}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _employment(job: dict[str, Any]) -> EmploymentType:
    name = ((job.get("employmentType") or {}).get("name") or "").strip().lower()
    return _EMPLOYMENT.get(name, EmploymentType.UNKNOWN)


@register("join")
class JoinProvider(BaseProvider):
    name = "join"

    PER_PAGE = 5  # join.com renders 5 jobs per SSR page
    MAX_PAGES = 20  # per-board page cap (=100 jobs) to bound pagination cost

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token:
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        url = _CAREERS.format(token=token)

        # Page 1 (sequential) gives us the job count and the company name.
        first = _parse_initial_state(await fetcher.get_text(url))
        jobs_block = first.get("jobs") or {}
        company = (first.get("company") or {}).get("name") or token

        pagination = jobs_block.get("pagination") or {}
        page_count = int(pagination.get("pageCount") or 1)
        want_pages = min(page_count, self.MAX_PAGES)
        if query.limit is not None:
            want_pages = min(want_pages, max(1, -(-query.limit // self.PER_PAGE)))  # ceil

        pages: dict[int, list[dict[str, Any]]] = {1: list(jobs_block.get("items") or [])}

        # Remaining pages CONCURRENTLY — one task per page, collected by page number.
        if want_pages > 1:
            async with anyio.create_task_group() as tg:
                for page in range(2, want_pages + 1):
                    tg.start_soon(self._fetch_page, fetcher, url, page, pages)

        raws: list[RawJob] = []
        for page in sorted(pages):
            for job in pages[page]:
                raws.append(self._to_raw(job, token, company))
        return raws

    async def _fetch_page(
        self, fetcher: AsyncFetcher, url: str, page: int, sink: dict[int, list[dict[str, Any]]]
    ) -> None:
        state = _parse_initial_state(await fetcher.get_text(url, params={"page": page}))
        sink[page] = list((state.get("jobs") or {}).get("items") or [])

    def _to_raw(self, job: dict[str, Any], token: str, company: str) -> RawJob:
        id_param = str(job.get("idParam") or job.get("id") or "")
        return RawJob(
            source=self.name,
            source_job_id=str(job.get("id") or ""),
            company=company,
            token=token,
            url=_JOB_URL.format(token=token, id_param=id_param) if id_param else None,
            payload=job,
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        city = p.get("city") or {}
        country = p.get("country") or {}
        location = self._location(city, country)

        workplace = str(p.get("workplaceType") or "").strip().lower()
        remote = _WORKPLACE.get(workplace, RemoteType.UNKNOWN)

        department = (p.get("category") or {}).get("name") or None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p),
            department=department,
            salary=None,  # amounts not exposed in the list blob
            posted_at=_parse_dt(p.get("createdAt")),
            description_html=None,  # description lives on the per-job detail page
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _location(city: dict[str, Any], country: dict[str, Any]) -> Location | None:
        city_name = (city.get("cityName") or "").strip() or None
        country_name = (city.get("countryName") or "").strip() or None
        iso = (country.get("iso3166") or "").strip() or None
        if not any((city_name, country_name, iso)):
            return None
        raw_parts = [part for part in (city_name, country_name) if part]
        return Location(
            city=city_name,
            country=country_name or iso,
            raw=", ".join(raw_parts) or None,
        )
