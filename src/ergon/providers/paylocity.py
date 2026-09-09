"""Paylocity Recruiting job-board provider (the public ``recruiting.paylocity.com`` feed).

Paylocity is a US HCM/payroll vendor whose Recruiting product powers many mid-market employers'
public career pages at ``recruiting.paylocity.com/recruiting/jobs/All/{guid}/{Slug}``. Each board
exposes a free, unauthenticated JSON feed keyed by the company's GUID — the whole board in one call,
no token/cookie/CSRF::

    GET https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}
        Accept: application/json
    -> {"displayName", "showVideo", "jobs": [ {"jobId", "title", "companyName", "applyUrl",
            "publishedDate", "description", "jobLocation": {"city","state","country","postalCode"},
            "department", "employmentType", ...}, ... ]}

(The legacy v1 path ``/recruiting/api/feed/jobs/{guid}`` returns the same shape in PascalCase; we
prefer v2 and fall back to v1.) Token is the board ``{guid}`` (e.g.
``"b181f77f-0432-453f-b229-869d786bb46c"``). Location fields are tolerated both nested under
``jobLocation`` and flattened at the top level (tenants vary).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PaylocityProvider"]

_V2 = "https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}"
_V1 = "https://recruiting.paylocity.com/recruiting/api/feed/jobs/{guid}"
_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_EMPLOYMENT = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "seasonal": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}


def _ci(d: dict[str, Any], *names: str) -> Any:
    """Case-insensitive lookup across candidate keys (v2 camelCase / v1 PascalCase)."""
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return v
    return None


@register("paylocity")
class PaylocityProvider(BaseProvider):
    name = "paylocity"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        if "recruiting.paylocity.com" not in url_or_host.lower():
            return None
        m = _GUID_RE.search(url_or_host)
        return m.group(0) if m else None

    def conditional_url(self, token: str) -> str | None:
        return _V2.format(guid=token) if token else None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        guid = (token or "").strip()
        if not guid:
            return []
        data: Any = None
        for url in (_V2.format(guid=guid), _V1.format(guid=guid)):
            try:
                data = await fetcher.get_json(url, headers={"Accept": "application/json"})
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                break
            data = None
        if not isinstance(data, dict):
            return []
        return self._raws_from_data(data, guid, query.limit)

    @staticmethod
    def _raws_from_data(data: dict[str, Any], guid: str, limit: int | None) -> list[RawJob]:
        jobs = _ci(data, "jobs") or []
        company = _ci(data, "displayName")
        raws: list[RawJob] = []
        seen: set[str] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            jid = str(_ci(job, "jobId", "requisitionId", "id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            raws.append(
                RawJob(
                    source="paylocity",
                    source_job_id=jid,
                    company=str(_ci(job, "companyName") or company or "paylocity"),
                    token=guid,
                    url=_ci(job, "applyUrl", "jobUrl", "url"),
                    payload=job,
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob] | None:
        import json

        try:
            data = json.loads(body)
        except ValueError:
            return None
        return self._raws_from_data(data, token, None) if isinstance(data, dict) else None

    @staticmethod
    def _location(job: dict[str, Any]) -> Location | None:
        loc = _ci(job, "jobLocation", "location")
        city = state = country = None
        if isinstance(loc, dict):
            city = _ci(loc, "city")
            state = _ci(loc, "state", "stateProvince")
            country = _ci(loc, "country")
        elif isinstance(loc, str) and loc.strip():
            label = loc.strip()
            return Location(raw=label, is_remote="remote" in label.lower())
        city = city or _ci(job, "city")
        state = state or _ci(job, "state")
        country = country or _ci(job, "country")
        parts = [str(p).strip() for p in (city, state, country) if p and str(p).strip()]
        if not parts:
            return None
        label = ", ".join(parts)
        return Location(raw=label, is_remote="remote" in label.lower())

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def normalize(self, raw: RawJob) -> JobPosting:
        p: dict[str, Any] = raw.payload
        loc = self._location(p)
        locations = [loc] if loc else []
        remote = RemoteType.REMOTE if (loc and loc.is_remote) else RemoteType.UNKNOWN
        emp_raw = str(_ci(p, "employmentType", "jobType", "type") or "").strip().lower()
        employment = _EMPLOYMENT.get(emp_raw, EmploymentType.UNKNOWN)
        desc_html = _ci(p, "description", "jobDescription")
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(_ci(p, "title", "jobTitle") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=employment,
            department=_ci(p, "department", "category"),
            salary=None,
            posted_at=self._date(_ci(p, "publishedDate", "datePosted", "postedDate")),
            updated_at=None,
            description_html=desc_html,
            description_text=None,
            raw=raw.payload,
        )
