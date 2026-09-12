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

import httpx
from selectolax.parser import HTMLParser

from ..models import (
    DetailFetch,
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    Salary,
    SalaryInterval,
)
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


# schema.org JobPosting.employmentType values, normalized (lowercased/underscore-stripped) since
# some Radancy tenants emit the humanized form ("Full time") rather than the enum token
# ("FULL_TIME") -- both are matched by this table.
_JSONLD_EMPLOYMENT: dict[str, EmploymentType] = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "volunteer": EmploymentType.OTHER,
    "per diem": EmploymentType.OTHER,
    "other": EmploymentType.OTHER,
}

_JSONLD_INTERVAL: dict[str, SalaryInterval] = {
    "year": SalaryInterval.YEAR,
    "month": SalaryInterval.MONTH,
    "week": SalaryInterval.WEEK,
    "day": SalaryInterval.DAY,
    "hour": SalaryInterval.HOUR,
}


def _employment_from_jsonld(value: Any) -> EmploymentType | None:
    """schema.org ``employmentType`` (a string, or a list of them) -> EmploymentType, first hit
    wins. Unrecognised/absent -> None (never guess, never raise)."""
    values = value if isinstance(value, list) else [value]
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        norm = " ".join(v.replace("_", " ").replace("-", " ").lower().split())
        mapped = _JSONLD_EMPLOYMENT.get(norm)
        if mapped is not None:
            return mapped
    return None


def _salary_from_jsonld(value: Any) -> Salary | None:
    """schema.org ``baseSalary`` (a ``MonetaryAmount`` wrapping a ``QuantitativeValue``) ->
    Salary. Handles both a single ``value`` and a ``minValue``/``maxValue`` range. None when the
    block carries no usable amount (e.g. present-but-empty keys, as seen on most sampled tenants)."""
    if not isinstance(value, dict):
        return None
    currency = (value.get("currency") or "").strip() or None
    qv = value.get("value")
    qv = qv if isinstance(qv, dict) else {}
    min_amount = qv.get("minValue") if isinstance(qv.get("minValue"), (int, float)) else None
    max_amount = qv.get("maxValue") if isinstance(qv.get("maxValue"), (int, float)) else None
    if min_amount is None and max_amount is None:
        single = qv.get("value") if isinstance(qv.get("value"), (int, float)) else None
        min_amount = max_amount = single
    if min_amount is None and max_amount is None:
        return None
    unit = str(qv.get("unitText") or "").strip().lower() or None
    interval = _JSONLD_INTERVAL.get(unit) if unit else None
    return Salary(
        min_amount=float(min_amount) if min_amount is not None else None,
        max_amount=float(max_amount) if max_amount is not None else None,
        currency=currency,
        interval=interval,
    )


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

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD via its own CMS-rendered detail page (Tier-3 recovery).

        Verified by recon (7/7 live tenants): the Radancy ``apply_url``/``listing_url`` (built by
        :meth:`_parse_cards`) IS ALREADY the full job detail page -- there is no separate detail
        API to call, unlike Workday/SmartRecruiters. A per-tenant ``div.job-description`` selector
        is UNRELIABLE though: on ~4/7 tenants the first match is a short 62-172 char meta/summary
        chip, not the JD body. So we try a container-selector chain
        (:attr:`_DETAIL_SELECTORS`) and take the FIRST match whose text clears
        :attr:`_DETAIL_MIN_LEN`; if none clears it, fall back to the whole-page text (recon's
        robust default -- nav-chrome noise is acceptable, and it reliably surfaces the JD).
        Returns ``None`` ONLY on a confirmed-gone signal: a real HTTP 404/410 from the detail
        page (Radancy has no separate soft-404 body verified by recon — the CMS page itself
        4xx/5xxs). A missing derivable URL is NOT evidence of death, and every other
        indeterminate/transient condition — other HTTP statuses, timeouts, rate limits, or an
        empty page body — RAISES instead of returning ``None``, so the freshness sweep never
        expires a still-live posting on an ambiguous signal."""
        url = ref.apply_url or ref.listing_url
        if not url:
            raise RuntimeError(f"radancy detail: no apply_url/listing_url for {ref!s}")
        try:
            html = await fetcher.get_text(url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        if not isinstance(html, str) or not html.strip():
            raise RuntimeError(f"radancy detail: empty page body for {ref!s}")
        tree = HTMLParser(html)
        text: str | None = None
        for selector in self._DETAIL_SELECTORS:
            node = tree.css_first(selector)
            if node is None:
                continue
            chunk = node.text(separator=" ", strip=True)
            if len(chunk) >= self._DETAIL_MIN_LEN:
                text = node.html or chunk
                break
        if text is None:
            body = tree.body
            text = (
                body.text(separator=" ", strip=True)
                if body is not None
                else tree.text(separator=" ", strip=True)
            ) or None
        if text is None:
            raise RuntimeError(f"radancy detail: no extractable text for {ref!s}")
        # Most tenants embed a JSON-LD JobPosting whose `jobLocation` is a STRUCTURED address
        # (city/region/country) -> return it so the merge fills the index row's NULL country. The
        # same block often ALSO carries `employmentType`/`baseSalary` (not universal -- some tenant
        # templates carry no ld+json, or empty values on those keys); those degrade to the bare-str
        # body/UNKNOWN/None exactly as before.
        locations: list[Location] = []
        employment_type: EmploymentType | None = None
        salary: Salary | None = None
        for job in self.extract_jsonld_jobs(html):
            if not locations:
                locations = self.jsonld_locations(job.get("jobLocation"))
            if employment_type is None:
                employment_type = _employment_from_jsonld(job.get("employmentType"))
            if salary is None:
                salary = _salary_from_jsonld(job.get("baseSalary"))
            if locations and employment_type is not None and salary is not None:
                break
        if locations or employment_type is not None or salary is not None:
            return DetailFetch(
                text=text, salary=salary, locations=locations, employment_type=employment_type
            )
        return text

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
