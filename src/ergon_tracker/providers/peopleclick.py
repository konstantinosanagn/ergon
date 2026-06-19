"""PeopleClick / PeopleFluent candidate-portal provider (careers.peopleclick.com).

A few large orgs (MIT) run their public careers site on PeopleClick. Its job list comes from a
JSON endpoint reachable with a plain HTTP cookie-primed session — NO browser:

1. GET  /careerscp/{client}/external/search/search.html        # sets JSESSIONID
2. POST /careerscp/{client}/external/results/searchResult.html # establishes the results context
3. GET  /careerscp/api/{client}/external/site/getJobs          # -> {totalHits, jobList:[...]}

Each job: ``jobPostId`` (id) and an ``attributes`` map carrying ``FLD_JP_POSTING_TITLE`` (title),
``JPM_LOCATION`` (e.g. "Cambridge, MA"), ``FLD_JP_DEPARTMENT``. PARTIAL by design: the API returns
only the first page (``hitsPerPage`` 50) and pagination is JS/session-driven server-side (no
plain-HTTP page param works), so we capture the first 50 of ``totalHits`` — entity-clean and far
better than the aggregator fallback, but not the whole board.

Token: ``"{client}"`` or ``"{client}|{Company Name}"`` (e.g. ``"client_mit|MIT"``). ``client`` is
the path segment (``client_mit``); per-job payload has no employer field, so the name is in the token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PeopleClickProvider"]

_BASE = "https://careers.peopleclick.com/careerscp"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@register("peopleclick")
class PeopleClickProvider(BaseProvider):
    name = "peopleclick"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (niche ATS); avoid auto-claiming

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        return parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        client, company = self._parse(token)
        if not client:
            return []
        search = f"{_BASE}/{client}/external/search/search.html"
        hdr = {"User-Agent": _UA, "Referer": search}
        try:
            # Prime the session, establish the results context, then read the jobs JSON.
            await fetcher.get_text(search, headers=hdr)
            await fetcher.request(
                "POST",
                f"{_BASE}/{client}/external/results/searchResult.html",
                data={"keyword": ""},
                headers=hdr,
            )
            data = await fetcher.get_json(
                f"{_BASE}/api/{client}/external/site/getJobs",
                headers={**hdr, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
        except Exception:
            return []
        jobs = data.get("jobList") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return []
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for j in jobs:
            jid = str(j.get("jobPostId") or (j.get("identity") or {}).get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            attrs = j.get("attributes") or {}
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=company or client.replace("client_", ""),
                    token=token,
                    url=f"{_BASE}/{client}/external/jobdetails/{jid}",
                    payload=attrs,
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        a = raw.payload
        loc = self._clean(a.get("JPM_LOCATION"))
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
            title=self._clean(a.get("FLD_JP_POSTING_TITLE"))
            or self._clean(a.get("JPM_TITLE"))
            or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=self._clean(a.get("FLD_JP_DEPARTMENT")),
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None
