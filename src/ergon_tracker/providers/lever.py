"""Lever job-board provider.

Lever exposes a free, unauthenticated public postings API:
``GET https://api.lever.co/v0/postings/{token}?mode=json`` returning a JSON list of
postings. Unlike most ATS feeds, Lever supports a few server-side filters
(``location``, ``team``, ``commitment``) which :meth:`fetch` forwards from the
``SearchQuery`` when present.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

__all__ = ["LeverProvider"]

_API = "https://api.lever.co/v0/postings/{token}"
# Lever EU data-residency host. A "|eu" token suffix (e.g. "cirrus|eu") routes here — some
# EU-hosted boards (Cirrus Logic) 404 on the default US host.
_EU_API = "https://api.eu.lever.co/v0/postings/{token}"

_HOST_NEEDLE = "jobs.lever.co/"


def _slug_base(token: str) -> tuple[str, str]:
    if token.endswith("|eu"):
        return token[:-3], _EU_API
    return token, _API


_REMOTE_BY_WORKPLACE = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "on-site": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
}

# Lever ``commitment`` strings → canonical EmploymentType.
_EMPLOYMENT_BY_COMMITMENT = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "permanent": EmploymentType.FULL_TIME,
    "regular": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
}

# Reverse map for forwarding a query's employment_type to Lever's ``commitment`` param.
_COMMITMENT_BY_EMPLOYMENT = {
    EmploymentType.FULL_TIME: "Full-time",
    EmploymentType.PART_TIME: "Part-time",
    EmploymentType.CONTRACT: "Contract",
    EmploymentType.INTERNSHIP: "Internship",
    EmploymentType.TEMPORARY: "Temporary",
}

# Lever ``salaryRange.interval`` strings → canonical SalaryInterval.
_INTERVAL_BY_LEVER = {
    "per-year-salary": SalaryInterval.YEAR,
    "per-month-salary": SalaryInterval.MONTH,
    "per-week-salary": SalaryInterval.WEEK,
    "per-day-wage": SalaryInterval.DAY,
    "per-hour-wage": SalaryInterval.HOUR,
}


@register("lever")
class LeverProvider(BaseProvider):
    name = "lever"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        lowered = url_or_host.lower()
        idx = lowered.find(_HOST_NEEDLE)
        if idx == -1:
            return None
        rest = url_or_host[idx + len(_HOST_NEEDLE) :]
        token = rest.split("/")[0].split("?")[0].split("#")[0].strip()
        return token or None

    def conditional_url(self, token: str) -> str | None:
        # Whole board in one JSON response with a strong ETag. The crawler fetches with an empty
        # query, so the validatable representation is exactly ?mode=json.
        slug, base = _slug_base(token)
        return base.format(token=slug) + "?mode=json"

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        params: dict[str, str] = {"mode": "json"}
        if query.location:
            params["location"] = query.location
        if query.employment_type:
            commitment = _COMMITMENT_BY_EMPLOYMENT.get(query.employment_type)
            if commitment:
                params["commitment"] = commitment

        slug, base = _slug_base(token)
        url = base.format(token=slug)
        data = await fetcher.get_json(url, params=params)
        return self._raws_from_data(data, slug)

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob]:
        """Parse an already-downloaded response body (from a conditional 200), avoiding a refetch."""
        import json

        return self._raws_from_data(json.loads(body), _slug_base(token)[0])

    def _raws_from_data(self, data: Any, token: str) -> list[RawJob]:
        postings: list[dict[str, Any]] = data if isinstance(data, list) else []
        company = token.replace("-", " ").title()
        raws: list[RawJob] = []
        for posting in postings:
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(posting.get("id", "")),
                    company=company,
                    token=token,
                    url=posting.get("hostedUrl") or posting.get("applyUrl"),
                    payload=posting,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        categories: dict[str, Any] = p.get("categories") or {}
        workplace = str(p.get("workplaceType") or "").strip().lower()

        all_locations = categories.get("allLocations") or []
        if not all_locations and categories.get("location"):
            all_locations = [categories["location"]]
        is_remote = workplace == "remote"
        locations = [
            Location(raw=loc, is_remote=is_remote or "remote" in str(loc).lower())
            for loc in all_locations
            if loc
        ]

        remote = _REMOTE_BY_WORKPLACE.get(workplace, RemoteType.UNKNOWN)

        commitment = str(categories.get("commitment") or "").strip().lower()
        employment_type = _EMPLOYMENT_BY_COMMITMENT.get(
            commitment, EmploymentType.OTHER if commitment else EmploymentType.UNKNOWN
        )

        department = categories.get("department") or categories.get("team")

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("text") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("applyUrl") or p.get("hostedUrl"),
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=department,
            salary=self._salary(p.get("salaryRange")),
            posted_at=self._posted_at(p.get("createdAt")),
            description_html=p.get("description"),
            description_text=p.get("descriptionPlain"),
            raw=raw.payload,
        )

    @staticmethod
    def _salary(rng: dict[str, Any] | None) -> Salary | None:
        if not rng:
            return None
        interval_raw = str(rng.get("interval") or "").strip().lower()
        return Salary(
            min_amount=rng.get("min"),
            max_amount=rng.get("max"),
            currency=rng.get("currency"),
            interval=_INTERVAL_BY_LEVER.get(interval_raw),
        )

    @staticmethod
    def _posted_at(created_at: int | float | None) -> datetime | None:
        if created_at is None:
            return None
        return datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
