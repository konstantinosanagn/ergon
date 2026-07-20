"""DirectEmployers / dejobs.org provider — a recruiter-direct job federation (~900 large member
employers syndicate their postings). Its public Solr API is plain-HTTP fetchable (NO browser) and
filterable to ONE company, which lets us capture employers whose OWN careers site is bot-walled
(American Airlines, HCA) or JS-walled — the federation indexes them cleanly:

    GET https://prod-search-api.jobsyn.org/api/v1/solr/search?q=&company={slug}&page={N}
    header: x-origin: dejobs.org      (required — the API 4xxs without it)

Response: ``{"jobs": [...], "pagination": {"total", "page", "page_size": 15, "total_pages"}}``.
Each job: ``title_exact``, ``company_exact`` (the single filtered employer — entity-clean),
``location_exact``/``all_locations``, ``guid``, ``reqid``, ``date_added``, ``description``.

Token: the company slug (e.g. ``"american-airlines"``, ``"hca-healthcare"``) — the leading segment
of a job's ``company_slab_exact`` (``"abm-industries/careers::ABM Industries"`` → ``"abm-industries"``).
Lower priority than a firm's own ATS (recruiter-syndicated subset), above the adzuna fallback.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["DEJobsProvider"]

_API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
_HEADERS = {
    "x-origin": "dejobs.org",
    "referer": "https://dejobs.org/",
    "Accept": "application/json",
}
_MAX_PAGES = 400  # page_size is fixed at 15 server-side -> bound full pulls to ~6k jobs


@register("dejobs")
class DEJobsProvider(BaseProvider):
    name = "dejobs"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Seed-only aggregator (like adzuna/themuse) — never auto-claims a careers host.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        slug = token.strip().lower()
        if not slug:
            return []
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for page in range(1, _MAX_PAGES + 1):
            params = {"q": "", "company": slug, "page": page}
            try:
                data = await fetcher.get_json(_API, params=params, headers=_HEADERS)
            except Exception:
                break
            if not isinstance(data, dict):
                break
            jobs = data.get("jobs") or []
            if not isinstance(jobs, list) or not jobs:
                break
            grew = False
            for j in jobs:
                guid = str(j.get("guid") or j.get("reqid") or "")
                if not guid or guid in seen:
                    continue
                seen.add(guid)
                grew = True
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=guid,
                        company=str(j.get("company_exact") or slug),
                        token=slug,
                        url=self._url(j),
                        payload=j,
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws
            pg = data.get("pagination") or {}
            if not grew or not pg.get("has_more_pages"):
                break
        return raws

    async def board_count(self, token: str, fetcher: AsyncFetcher) -> int | None:
        """Cheap change-CANDIDATE signal: page-1 ``pagination.total`` (see
        ``BaseProvider.board_count``).

        Issues ONE minimal GET (same endpoint/headers ``fetch`` uses, ``page=1``) and reads
        ``pagination.total`` from the Solr response. Returns ``None`` ONLY on a confirmed-gone
        signal (404/410, or an empty token) for the company slug; everything else indeterminate/
        transient RAISES (mirrors ``fetch_detail``'s 404-vs-transient contract)."""
        slug = token.strip().lower()
        if not slug:
            return None
        params = {"q": "", "company": slug, "page": 1}
        try:
            data = await fetcher.get_json(_API, params=params, headers=_HEADERS)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        if not isinstance(data, dict):
            raise RuntimeError(f"dejobs board_count: non-dict payload for token {slug!r}")
        pagination = data.get("pagination") or {}
        total = pagination.get("total")
        if not isinstance(total, int):
            raise RuntimeError(
                f"dejobs board_count: missing/non-int pagination.total for token {slug!r}"
            )
        return total

    @staticmethod
    def _url(j: dict[str, Any]) -> str:
        slug = j.get("title_slug") or "job"
        guid = j.get("guid") or ""
        return f"https://dejobs.org/{slug}/{guid}/job/"

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = str(p.get("location_exact") or "").strip()
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if loc:
            is_remote = "remote" in loc.lower()
            locations.append(Location(raw=loc, is_remote=is_remote))
            if is_remote:
                remote = RemoteType.REMOTE
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title_exact") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            posted_at=self._date(p.get("date_added")),
            description_html=self._clean(p.get("description")),
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _date(v: Any) -> datetime | None:
        if isinstance(v, str) and v.strip():
            try:
                return datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        return None
