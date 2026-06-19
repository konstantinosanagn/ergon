"""Zwayam career-portal provider (public.zwayam.com).

Several Indian IT/services firms (e.g. Tavant) run their public careers board on Zwayam, an
Elasticsearch-backed ATS. The board is fetchable over plain HTTP with NO browser, via a two-step
multipart flow against the shared ``public.zwayam.com`` backend:

1. POST ``/data-service/v2/public-configurations`` with multipart field ``companyUrl`` = the firm's
   own careers host (e.g. ``careers.tavant.com``) -> ``responseObject.company`` (``id``,
   ``careerSiteUrl``). An ``Origin``/``Referer`` of that host is REQUIRED (else 403 "Invalid
   CompanyUrl"); the ``.zwayam.com`` subdomain is NOT accepted — it must be the brand careers host.
2. POST ``/jobs/search`` with multipart fields ``filterCri`` (a JSON string carrying
   ``paginationStartNo``), ``domain`` (the same companyUrl), and ``companyId`` (the numeric id,
   **base64-encoded**) -> ``data.{totalCount, data:[{_source:{...}}]}`` (10 hits/page; step
   ``paginationStartNo`` by 10).

Each ``_source`` carries ``id``, ``jobTitle``, ``location`` (e.g. "Bengaluru, Karnataka, India"),
``departmentName``/``jobFunction``, ``jobUrl`` (slug). The apply URL is ``https://{careerSiteUrl}/
job/{jobUrl}``. Per-job company is the firm itself, so the name is carried in the token.

Token: ``"{companyUrl}|{Company}"`` (e.g. ``"careers.tavant.com|Tavant"``). ``companyUrl`` is the
brand careers host the config endpoint validates against.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["ZwayamProvider"]

_CONFIG = "https://public.zwayam.com/data-service/v2/public-configurations"
_SEARCH = "https://public.zwayam.com/jobs/search"
_PAGE = 10  # Zwayam returns 10 hits/page; paginationStartNo steps by this
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@register("zwayam")
class ZwayamProvider(BaseProvider):
    name = "zwayam"

    MAX_PAGES = 300  # bound full pulls (=3000 jobs) when no limit is given

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (needs the brand companyUrl + name); never auto-claims

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        return parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)

    @staticmethod
    def _filter_cri(start: int) -> str:
        return json.dumps(
            {
                "paginationStartNo": start,
                "selectedCall": "sort",
                "sortCriteria": {"name": "modifiedDate", "isAscending": False},
                "anyOfTheseWords": "",
            }
        )

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        company_url, company = self._parse(token)
        if not company_url:
            return []
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": f"https://{company_url}",
            "Referer": f"https://{company_url}/",
        }
        try:
            cfg = await fetcher.request(
                "POST", _CONFIG, files={"companyUrl": (None, company_url)}, headers=headers
            )
            comp = (cfg.json().get("responseObject") or {}).get("company") or {}
        except Exception:
            return []
        company_id = comp.get("id")
        if company_id is None:
            return []
        site = (comp.get("careerSiteUrl") or company_url).rstrip("/")
        cid_b64 = base64.b64encode(str(company_id).encode()).decode()

        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        total: int | None = None
        for page in range(self.MAX_PAGES):
            try:
                resp = await fetcher.request(
                    "POST",
                    _SEARCH,
                    files={
                        "filterCri": (None, self._filter_cri(page * _PAGE)),
                        "domain": (None, company_url),
                        "companyId": (None, cid_b64),
                    },
                    headers=headers,
                )
                data = resp.json().get("data") or {}
            except Exception:
                break
            if total is None and isinstance(data.get("totalCount"), int):
                total = data["totalCount"]
            hits = data.get("data")
            if not isinstance(hits, list) or not hits:
                break
            new = 0
            for hit in hits:
                src = hit.get("_source") if isinstance(hit, dict) else None
                if not isinstance(src, dict):
                    continue
                jid = str(src.get("id") or src.get("jobCode") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                slug = src.get("jobUrl")
                url = f"https://{site}/job/{slug}" if slug else f"https://{site}"
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=company or comp.get("companyName") or company_url,
                        token=token,
                        url=url,
                        payload=src,
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws
            if new == 0 or (total is not None and len(seen) >= total):
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        s = raw.payload
        loc = self._clean(s.get("location")) or self._clean(s.get("locationSeparatedbySlash"))
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        work_mode = str(s.get("workMode") or "").lower()
        if loc:
            is_remote = "remote" in loc.lower() or "remote" in work_mode
            locations.append(Location(raw=loc, is_remote=is_remote))
            if is_remote:
                remote = RemoteType.REMOTE
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=self._clean(s.get("jobTitle")) or self._clean(s.get("designation")) or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=self._clean(s.get("departmentName")) or self._clean(s.get("jobFunction")),
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None
