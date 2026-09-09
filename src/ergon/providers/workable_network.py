"""Workable *network* provider — first-party enumeration of the whole Workable customer base.

Unlike the per-company ``workable`` provider (which crawls one ``apply.workable.com/{shortcode}``
board at a time), this is an **aggregator** over Workable's public network job board. The
endpoint::

    GET https://jobs.workable.com/api/v1/jobs?query={kw}&location={loc}

returns ~172k active jobs across every Workable customer, each carrying its company (name,
website), with **server-side** keyword/location filtering. It is the one ATS we've found that
exposes its entire active tenant base first-party — so a broad search reaches Workable companies
we never had a per-board token for.

Pagination is cursor-based and quirky: the FIRST request takes ``query``/``location``; every
subsequent request passes ONLY ``nextPageToken`` (the token encodes the original query, and
re-sending ``query`` resets the cursor to page 1). We also stop if a page yields no new ids, so a
non-advancing token can never loop forever.

The network board uses a different id space than ``apply.workable.com`` shortcodes, so these jobs
are served directly as an aggregator source (``matches`` returns None — never auto-claimed from a
URL, never written to the per-company registry).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://jobs.workable.com/api/v1/jobs"

# Workable ``employmentType`` labels -> canonical EmploymentType.
_EMP = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
}
# Workable ``workplace`` -> canonical RemoteType.
_WORKPLACE = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "on_site": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _domain(website: str | None) -> str | None:
    if not website:
        return None
    host = website.split("//")[-1].split("/")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


@register("workable_network")
class WorkableNetworkProvider(BaseProvider):
    name = "workable_network"
    is_aggregator = True

    # Per-call page cap so a broad/empty query can't page all ~8.6k pages of the network on a live
    # request. A bulk ingest can raise this; live search relies on server-side query filtering to
    # keep the relevant set small.
    MAX_PAGES = 25

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # network aggregator: never resolved from a company URL

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        kw = (query.keywords or "").strip()
        loc = (query.location or query.city or query.country or "").strip()
        out: list[RawJob] = []
        seen: set[str] = set()
        next_token: str | None = None

        for _ in range(self.MAX_PAGES):
            # First page carries the query; later pages carry ONLY the cursor (it encodes it).
            params = {"nextPageToken": next_token} if next_token else {"query": kw, "location": loc}
            data = await fetcher.get_json(_API, params=params)
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not jobs:
                break
            new = 0
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                jid = str(j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                company = j.get("company") or {}
                out.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=(company.get("title") if isinstance(company, dict) else "") or "",
                        token=None,
                        url=j.get("url"),
                        payload=j,
                    )
                )
                if query.limit is not None and len(out) >= query.limit:
                    return out
            next_token = data.get("nextPageToken")
            if not next_token or new == 0:  # cursor exhausted or not advancing
                break
        return out

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        company = p.get("company") or {}
        website = company.get("website") if isinstance(company, dict) else None
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            company_domain=_domain(website),
            title=(p.get("title") or "").strip(),
            description_html=p.get("description"),
            locations=self._locations(p),
            remote=_WORKPLACE.get(str(p.get("workplace") or "").lower(), RemoteType.UNKNOWN),
            employment_type=_EMP.get(
                str(p.get("employmentType") or "").lower(), EmploymentType.UNKNOWN
            ),
            apply_url=p.get("url") or p.get("linkoutUrl"),
            posted_at=_parse_dt(p.get("created")),
            fetched_at=raw.fetched_at,
            raw=p,
        )

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        remote = str(p.get("workplace") or "").lower() == "remote"
        loc = p.get("location") or {}
        if isinstance(loc, dict) and (loc.get("city") or loc.get("countryName")):
            city = (loc.get("city") or "").strip() or None
            return [
                Location(
                    city=city,
                    region=(loc.get("subregion") or None),
                    country=(loc.get("countryName") or None),
                    raw=", ".join(x for x in (city, loc.get("countryName")) if x) or None,
                    is_remote=remote,
                )
            ]
        return [Location(is_remote=remote)] if remote else []
