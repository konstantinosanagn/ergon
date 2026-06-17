"""Pinpoint job-board provider.

Pinpoint (pinpointhq.com) exposes a free, unauthenticated public postings API:
``GET https://{token}.pinpointhq.com/postings.json`` which returns every published posting for
a company in one call under a top-level ``data`` array. There is no server-side filtering, so
:meth:`fetch` returns the whole board and the orchestrator applies ``SearchQuery.matches``
client-side.
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
    Salary,
    SalaryInterval,
    SearchQuery,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["PinpointProvider"]

_API = "https://{token}.pinpointhq.com/postings.json"

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.pinpointhq\.com", re.I),)

# Pinpoint's ``employment_type`` codes (e.g. "full_time", "contract_to_hire").
_EMPLOYMENT_BY_CODE = {
    "full_time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contract_to_hire": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
    "apprentice": EmploymentType.OTHER,
    "apprenticeship": EmploymentType.OTHER,
}

# Pinpoint's ``workplace_type`` → canonical remote classification.
_REMOTE_BY_WORKPLACE = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "onsite": RemoteType.ONSITE,
}

# Pinpoint's ``compensation_frequency`` → canonical salary interval.
_INTERVAL_BY_FREQUENCY = {
    "year": SalaryInterval.YEAR,
    "yearly": SalaryInterval.YEAR,
    "annual": SalaryInterval.YEAR,
    "month": SalaryInterval.MONTH,
    "monthly": SalaryInterval.MONTH,
    "week": SalaryInterval.WEEK,
    "weekly": SalaryInterval.WEEK,
    "day": SalaryInterval.DAY,
    "daily": SalaryInterval.DAY,
    "hour": SalaryInterval.HOUR,
    "hourly": SalaryInterval.HOUR,
}


def _employment(code: str | None) -> EmploymentType:
    if not code:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT_BY_CODE.get(code.strip().lower(), EmploymentType.UNKNOWN)


@register("pinpoint")
class PinpointProvider(BaseProvider):
    name = "pinpoint"

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
        # Pinpoint has no server-side filtering: pull the whole board in one request.
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        postings: list[dict[str, Any]] = data.get("data", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for posting in postings:
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(posting.get("id", "")),
                    company=token,
                    token=token,
                    url=posting.get("url") or None,
                    payload=posting,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        location = self._location(p)
        remote = _REMOTE_BY_WORKPLACE.get(
            (p.get("workplace_type") or "").strip().lower(), RemoteType.UNKNOWN
        )

        description_html = p.get("description") or None
        description_text = self._to_text(description_html)

        department = None
        job = p.get("job")
        if isinstance(job, dict):
            dept = job.get("department")
            if isinstance(dept, dict):
                department = (dept.get("name") or "").strip() or None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url") or None,
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p.get("employment_type")),
            department=department,
            salary=self._salary(p),
            posted_at=None,  # Pinpoint's postings.json exposes no created/published timestamp.
            updated_at=None,
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
        region = (loc.get("province") or "").strip() or None
        raw_loc = (loc.get("name") or "").strip() or None
        if not any((city, region, raw_loc)):
            return None
        return Location(city=city, region=region, country=None, raw=raw_loc, is_remote=False)

    @staticmethod
    def _salary(p: dict[str, Any]) -> Salary | None:
        if not p.get("compensation_visible"):
            return None
        lo = p.get("compensation_minimum")
        hi = p.get("compensation_maximum")
        if not isinstance(lo, (int, float)) and not isinstance(hi, (int, float)):
            return None
        interval = _INTERVAL_BY_FREQUENCY.get(
            (p.get("compensation_frequency") or "").strip().lower()
        )
        currency = (p.get("compensation_currency") or "").strip() or None
        return Salary(
            min_amount=float(lo) if isinstance(lo, (int, float)) else None,
            max_amount=float(hi) if isinstance(hi, (int, float)) else None,
            currency=currency,
            interval=interval,
        )

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
