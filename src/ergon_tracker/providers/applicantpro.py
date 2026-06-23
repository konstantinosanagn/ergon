"""ApplicantPro job-board provider.

ApplicantPro boards live at ``{subdomain}.applicantpro.com`` and render the listing as a Vue SPA, but
the SPA is backed by a free, unauthenticated public JSON endpoint discovered by capturing its XHR::

    GET https://{subdomain}.applicantpro.com/core/jobs/{domainId}?getParams=<json>
        -> {"success": true, "data": {"jobs": [{"id","title","city","orgTitle","classification",...}]}}

``domainId`` is the tenant's numeric id. A registry token may carry it directly (``{sub}|{domainId}``)
or just the subdomain (``{sub}``), in which case :meth:`fetch` discovers the id once from the careers
HTML. The list endpoint returns the whole board in one call (no server-side filtering), so ``fetch``
returns everything and the orchestrator applies ``SearchQuery.matches`` client-side. The list carries no
description, so postings normalize without one (title + location + department + apply link).
"""

from __future__ import annotations

import json
import re
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

__all__ = ["ApplicantProProvider"]

_HOST = re.compile(r"([a-z0-9-]+)\.applicantpro\.com", re.I)
# domainId, as seen in the careers HTML (share-widget JSON `domain_id:NNNN`, or the /core/jobs/NNNN call).
_DOMAIN_ID = re.compile(r'(?:domain_id["\']?\s*[:=]\s*["\']?|/core/(?:jobs|widget)/)(\d{2,})', re.I)
_CAREERS = "https://{sub}.applicantpro.com/jobs/"
_API = "https://{sub}.applicantpro.com/core/jobs/{domain_id}"
# The SPA sends a large display-config blob; this minimal subset is all the list endpoint needs.
_GET_PARAMS = json.dumps({"isInternal": 0, "showLocation": 1, "showEmploymentType": 1})

_EMPLOYMENT = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "seasonal": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}


def _employment(value: str | None) -> EmploymentType:
    if not value:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT.get(value.strip().lower(), EmploymentType.UNKNOWN)


@register("applicantpro")
class ApplicantProProvider(BaseProvider):
    name = "applicantpro"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        m = _HOST.search(url_or_host)
        if not m:
            return None
        sub = m.group(1).lower()
        if sub in {"www", "jobs", "static"}:
            return None
        return sub

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        sub, _, domain_id = token.partition("|")
        if not domain_id:
            domain_id = await self._discover_domain_id(sub, fetcher)
            if not domain_id:
                return []
        url = _API.format(sub=sub, domain_id=domain_id)
        data = await fetcher.get_json(
            url, params={"getParams": _GET_PARAMS}, headers={"X-Requested-With": "XMLHttpRequest"}
        )
        jobs = self._jobs_from(data)
        raws: list[RawJob] = []
        for job in jobs:
            jid = str(job.get("id", "")).strip()
            if not jid or not (job.get("title") or "").strip():
                continue
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=sub,  # registry maps board_token -> display name; subdomain is the fallback
                    token=f"{sub}|{domain_id}",  # canonicalize so re-crawls skip discovery
                    url=f"https://{sub}.applicantpro.com/jobs/{jid}",
                    payload=job,
                )
            )
        return raws

    @staticmethod
    async def _discover_domain_id(sub: str, fetcher: AsyncFetcher) -> str:
        try:
            html = await fetcher.get_text(_CAREERS.format(sub=sub))
        except Exception:
            return ""
        m = _DOMAIN_ID.search(html or "")
        return m.group(1) if m else ""

    @staticmethod
    def _jobs_from(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        node = data.get("data", data)
        jobs = node.get("jobs") if isinstance(node, dict) else None
        return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        sub = (raw.token or "").partition("|")[0]
        location = self._location(p)
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("title") or "").strip(),
            fetched_at=raw.fetched_at,
            apply_url=raw.url or f"https://{sub}.applicantpro.com/jobs/{raw.source_job_id}",
            locations=[location] if location else [],
            remote=RemoteType.UNKNOWN,
            employment_type=_employment(p.get("classification") or p.get("employmentType")),
            department=(p.get("orgTitle") or "").strip() or None,
            raw=p,
        )

    @staticmethod
    def _location(p: dict[str, Any]) -> Location | None:
        city = (p.get("city") or "").strip() or None
        region = (p.get("state") or p.get("stateAbbreviation") or "").strip() or None
        country = (p.get("iso3") or p.get("countryAbbreviation") or "").strip() or None
        if not any((city, region, country)):
            return None
        return Location(city=city, region=region, country=country, raw=city)
