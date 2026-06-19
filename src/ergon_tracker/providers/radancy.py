"""Radancy / TalentBrew careers provider (the ``/search-jobs`` platform).

Many large enterprises (PwC, Carnival, …) run their careers site on Radancy (formerly TMP
Worldwide / TalentBrew). The job list is fetchable over plain HTTP with NO browser via the site's
own AJAX results endpoint::

    GET https://{host}/search-jobs/results?ActiveFacetID=0&CurrentPage={N}&RecordsPerPage=100&...
        (with header ``X-Requested-With: XMLHttpRequest``)

It returns JSON ``{"results": "<html job cards>", "hasJobs": bool, ...}``. Each card is an anchor::

    <a href="/job/{city}/{slug}/{n}/{jobId}" data-job-id="{jobId}">
        <h2>{title}</h2>
        <span class="job-location">{location}</span>
        <span class="job-category">{category}</span>

So title/location/category/id parse cleanly (NOT slug-derived). Paginate ``CurrentPage`` until a
page yields no cards. Per-job company is the site owner, carried in the token.

Token: ``"{host}|{Company}"`` (e.g. ``"jobs.us.pwc.com|PwC"``). ``host`` is the careers host whose
``/search-jobs`` page is Radancy-powered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from selectolax.parser import HTMLParser

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["RadancyProvider"]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Fixed Radancy results-endpoint params (defaults the site's own JS sends); only CurrentPage varies.
_PARAMS: dict[str, Any] = {
    "ActiveFacetID": 0,
    "RecordsPerPage": 100,
    "Distance": 0,
    "RadiusUnitType": 0,
    "Latitude": 0,
    "Longitude": 0,
    "ShowRadius": "False",
    "IsPagination": "False",
    "CustomFacetName": "",
    "FacetTerm": "",
    "FacetType": 0,
    "SearchResultsModuleName": "Search Results",
    "SearchFiltersModuleName": "Search Filters",
    "SortCriteria": 0,
    "SortDirection": 0,
    "SearchType": 5,
}


@register("radancy")
class RadancyProvider(BaseProvider):
    name = "radancy"

    MAX_PAGES = 200  # bound full pulls (=20k jobs) when no limit is given

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (needs the careers host + company label); never auto-claims

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        host = parts[0].replace("https://", "").replace("http://", "").strip("/")
        return host, (parts[1] if len(parts) > 1 and parts[1] else None)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, company = self._parse(token)
        if not host:
            return []
        url = f"https://{host}/search-jobs/results"
        headers = {"User-Agent": _UA, "X-Requested-With": "XMLHttpRequest", "Accept": "*/*"}
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for page in range(1, self.MAX_PAGES + 1):
            params = {**_PARAMS, "CurrentPage": page}
            try:
                resp = await fetcher.request("GET", url, params=params, headers=headers)
                data = resp.json()
            except Exception:
                break
            html = data.get("results") if isinstance(data, dict) else None
            if not isinstance(html, str) or "data-job-id" not in html:
                break
            cards = self._parse_cards(html, host, company, token)
            new = 0
            for jid, raw in cards:
                if jid in seen:
                    continue
                seen.add(jid)
                raws.append(raw)
                new += 1
                if limit is not None and len(raws) >= limit:
                    return raws
            if new == 0:
                break
        return raws

    def _parse_cards(
        self, html: str, host: str, company: str | None, token: str
    ) -> list[tuple[str, RawJob]]:
        out: list[tuple[str, RawJob]] = []
        for a in HTMLParser(html).css("a[href*='/job/']"):
            jid = a.attributes.get("data-job-id")
            href = a.attributes.get("href") or ""
            if not jid:
                continue
            h2 = a.css_first("h2")
            title = h2.text(strip=True) if h2 else ""
            if not title:
                continue
            loc_el = a.css_first("span.job-location")
            cat_el = a.css_first("span.job-category")
            url = href if href.startswith("http") else f"https://{host}{href}"
            out.append(
                (
                    jid,
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=company or host.split(".")[0],
                        token=token,
                        url=url,
                        payload={
                            "title": title,
                            "location": loc_el.text(strip=True) if loc_el else "",
                            "category": cat_el.text(strip=True) if cat_el else "",
                            "url": url,
                        },
                    ),
                )
            )
        return out

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = str(p.get("location") or "").strip()
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
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=str(p.get("category") or "") or None,
        )
