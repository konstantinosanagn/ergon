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

Multi-brand sites: some Radancy tenants host several brands on one board and tag each job card's
anchor with a ``brand-facet__{brand}`` CSS class (UnitedHealth Group: ``brand-facet__optum`` /
``brand-facet__uhc`` / ``brand-facet__uhg``). The site's facet UI can't be filtered server-side
without JS, but the per-card class lets us scope to ONE entity. An optional third token field is a
substring the card's anchor ``class`` must contain to be kept, e.g.
``"careers.unitedhealthgroup.com|Optum|brand-facet__optum"`` — captures only Optum's postings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from selectolax.parser import HTMLParser

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef
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

    # --- detail (Tier-3 JD recovery) -----------------------------------------

    # Below this, a matched container is probably a short meta/summary chip, not the JD body
    # (recon: on ~4/7 tenants the first ``div.job-description`` match is 62-172 chars).
    _DETAIL_MIN_LEN = 400
    _DETAIL_SELECTORS: tuple[str, ...] = (
        "div.job-description",
        'div[class*="description"]',
        "main",
        "article",
    )

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (needs the careers host + company label); never auto-claims

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None, str | None]:
        parts = [p.strip() for p in token.split("|")]
        host = parts[0].replace("https://", "").replace("http://", "").strip("/")
        company = parts[1] if len(parts) > 1 and parts[1] else None
        brand = parts[2] if len(parts) > 2 and parts[2] else None
        return host, company, brand

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, company, brand = self._parse(token)
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
            cards = self._parse_cards(html, host, company, token, brand)
            new = 0
            for jid, raw in cards:
                if jid in seen:
                    continue
                seen.add(jid)  # dedup + end-detection for ALL cards, even brand-filtered ones
                new += 1
                if raw is None:  # card exists but doesn't match the requested brand facet
                    continue
                raws.append(raw)
                if limit is not None and len(raws) >= limit:
                    return raws
            if new == 0:  # page yielded no unseen cards (true end), regardless of brand
                break
        return raws

    def _parse_cards(
        self, html: str, host: str, company: str | None, token: str, brand: str | None = None
    ) -> list[tuple[str, RawJob | None]]:
        # Returns (jid, raw) per card; raw is None when a brand filter is set and the card's anchor
        # class lacks it (still yielded so the caller can dedup/detect end-of-results correctly).
        out: list[tuple[str, RawJob | None]] = []
        for a in HTMLParser(html).css("a[href*='/job/']"):
            jid = a.attributes.get("data-job-id")
            href = a.attributes.get("href") or ""
            if not jid:
                continue
            if brand and brand not in (a.attributes.get("class") or ""):
                out.append((jid, None))
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

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via its own CMS-rendered detail page (Tier-3 recovery).

        Verified by recon (7/7 live tenants): the Radancy ``apply_url``/``listing_url`` (built by
        :meth:`_parse_cards`) IS ALREADY the full job detail page -- there is no separate detail
        API to call, unlike Workday/SmartRecruiters. A per-tenant ``div.job-description`` selector
        is UNRELIABLE though: on ~4/7 tenants the first match is a short 62-172 char meta/summary
        chip, not the JD body. So we try a container-selector chain
        (:attr:`_DETAIL_SELECTORS`) and take the FIRST match whose text clears
        :attr:`_DETAIL_MIN_LEN`; if none clears it, fall back to the whole-page text (recon's
        robust default -- nav-chrome noise is acceptable, and it reliably surfaces the JD).
        Non-raising: a missing URL, fetch failure, or empty page returns ``None``, never an
        exception."""
        url = ref.apply_url or ref.listing_url
        if not url:
            return None
        try:
            html = await fetcher.get_text(url)
        except Exception:
            return None
        if not isinstance(html, str) or not html.strip():
            return None
        tree = HTMLParser(html)
        for selector in self._DETAIL_SELECTORS:
            node = tree.css_first(selector)
            if node is None:
                continue
            text = node.text(separator=" ", strip=True)
            if len(text) >= self._DETAIL_MIN_LEN:
                return node.html or text
        body = tree.body
        page_text = (
            body.text(separator=" ", strip=True)
            if body is not None
            else tree.text(separator=" ", strip=True)
        )
        return page_text or None

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
