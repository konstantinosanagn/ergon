"""iCIMS career-site job-board provider (two generations, auto-detected).

iCIMS ships two structurally different public surfaces and this provider detects and
branches between them at fetch time:

**A. New "Career Sites" (Jibe) — PUBLIC JSON API (preferred path)**::

    GET https://{host}/api/jobs?page={N}&limit=100      # 1-indexed page; limit honored

Returns ``200 application/json`` with no auth/cookies. Top keys are
``jobs, locations, totalCount, count, ...``; each entry is ``{"data": {...}}`` carrying a
fully structured record (``req_id, title, description`` full HTML, ``apply_url,
posted_date, city/state/country, employment_type, categories, salary_min_value/
salary_max_value, hiring_organization``). We loop ``page`` to ``ceil(totalCount/100)`` —
one request per page, no per-detail fetch. Live: ``careers.amd.com`` totalCount≈1047,
``careers.icims.com``≈25.

**B. Classic — server-rendered HTML + per-detail JSON-LD (fallback)**::

    GET https://{host}/jobs/search?pr={P}&in_iframe=1   # listing, 20/page, P 0-indexed
    GET https://{host}/jobs/{id}/{slug}/job?in_iframe=1  # detail, carries JSON-LD JobPosting

On a classic host ``/api/jobs`` returns HTML (not JSON) — that's the detection signal. We
paginate the listing (parsing ``/jobs/{id}/{slug}/job`` hrefs and the literal "Page X of Y"
total), then fetch each detail and parse its ``application/ld+json`` ``JobPosting``
(``title, datePosted, employmentType, hiringOrganization, jobLocation, description``).
Live-verified against ``careers-winco.icims.com`` (10 pages, JSON-LD on every detail).

Detection: probe ``GET {host}/api/jobs?page=1&limit=100``. JSON with ``jobs``/``totalCount``
⇒ new (use API); anything else ⇒ classic. Hybrid Jibe-skinned classic hosts that answer
``/api/jobs`` with HTML route to classic correctly.

Token shape: ``"{host}"`` (e.g. ``"careers.amd.com"`` or ``"careers-winco.icims.com"``);
generation is auto-detected. ``"{host}|new"`` / ``"{host}|classic"`` pins a generation.

Never invented: missing fields normalize to ``None`` / ``UNKNOWN``. ``sitemap.xml`` is
IP-allowlisted (403) and the Platform/XML APIs are auth-gated — none are used here.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from ..models import (
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    Salary,
    SearchQuery,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["ICIMSProvider"]

_API = "https://{host}/api/jobs?page={page}&limit={limit}"
_SEARCH = "https://{host}/jobs/search?in_iframe=1&pr={pr}"
_DETAIL = "https://{host}/jobs/{jid}/{slug}/job?in_iframe=1"

# Classic listing job link: /jobs/{numeric id}/{slug}/job
_JOB_LINK_RE = re.compile(r"/jobs/(\d+)/([a-z0-9%-]+)/job", re.IGNORECASE)
# Any career-site posting URL carrying a numeric id right after /jobs/ (slug/trailing segment
# optional/garbage — iCIMS accepts it). Used to derive the Tier-3 detail URL from apply_url/
# listing_url, which may carry an arbitrary trailing shape (e.g. ``/jobs/12345/some-title/login``).
_JOBS_ID_RE = re.compile(r"/jobs/(\d+)(?:/|$)", re.IGNORECASE)
# Classic listing pagination footer: "Page X of Y"
_PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
# An iCIMS host (classic tenants live on *.icims.com; new sites use vanity domains).
_ICIMS_HOST_RE = re.compile(r"\.icims\.com$", re.IGNORECASE)
# Career-site URL path shapes that signal iCIMS even on a vanity domain.
_ICIMS_PATH_RE = re.compile(r"/(?:jobs/search|careers-home/jobs|jobs/\d+/)", re.IGNORECASE)

# iCIMS / schema.org employment_type codes -> our enum (deterministic).
_EMPLOYMENT = {
    "FULL_TIME": EmploymentType.FULL_TIME,
    "FULLTIME": EmploymentType.FULL_TIME,
    "PART_TIME": EmploymentType.PART_TIME,
    "PARTTIME": EmploymentType.PART_TIME,
    "CONTRACT": EmploymentType.CONTRACT,
    "CONTRACTOR": EmploymentType.CONTRACT,
    "INTERN": EmploymentType.INTERNSHIP,
    "INTERNSHIP": EmploymentType.INTERNSHIP,
    "TEMPORARY": EmploymentType.TEMPORARY,
    "TEMP": EmploymentType.TEMPORARY,
    "PER_DIEM": EmploymentType.OTHER,
    "OTHER": EmploymentType.OTHER,
}


def _parse_date(value: Any) -> datetime | None:
    """Parse an ISO-8601 or ``YYYY-MM-DD`` date to a tz-aware datetime, else None.

    Handles the shapes iCIMS emits: ``2026-06-17T15:57:00+0000`` (new API) and
    ``2026-06-17T04:00:00.000Z`` / ``2026-06-17`` (classic JSON-LD).
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    candidate = text.replace("Z", "+00:00")
    # Normalize a trailing numeric offset without a colon (e.g. +0000 -> +00:00).
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clean(value: Any) -> str | None:
    """Return a stripped non-empty string, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


@register("icims")
class ICIMSProvider(BaseProvider):
    name = "icims"

    NEW_PER_PAGE = 100  # /api/jobs honors limit; 100 = one page per 100 jobs
    NEW_MAX_PAGES = 200  # bound full pulls (=20k jobs)
    CLASSIC_MAX_PAGES = 200  # classic lists ~20/page -> bound at ~4k jobs

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise an iCIMS host/URL -> ``"{host}"`` token, else None.

        Matches ``*.icims.com`` hosts and career-site URLs whose path carries an iCIMS
        shape (``/jobs/search``, ``/careers-home/jobs``, ``/jobs/{id}/``). Bare non-iCIMS
        hosts are rejected to avoid over-matching generic domains.
        """
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if not host:
            return None
        if _ICIMS_HOST_RE.search(host):
            return host
        # Vanity domain: only claim it when the path looks like an iCIMS career site.
        if parts.path and _ICIMS_PATH_RE.search(parts.path):
            return host
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, pin = self._split(token)
        if not host:
            return []

        if pin == "classic":
            return await self._fetch_classic(host, query, fetcher)

        probe: Any = None
        if pin != "classic":
            url = _API.format(host=host, page=1, limit=self.NEW_PER_PAGE)
            try:
                probe = await fetcher.get_json(url)
            except Exception:
                probe = None

        if isinstance(probe, dict) and ("jobs" in probe or "totalCount" in probe):
            return await self._fetch_new(host, query, fetcher, probe)
        if pin == "new":
            return []  # pinned new but no JSON surface
        return await self._fetch_classic(host, query, fetcher)

    @staticmethod
    def _split(token: str) -> tuple[str, str | None]:
        """Split a ``"{host}"`` / ``"{host}|new"`` / ``"{host}|classic"`` token."""
        if "|" in token:
            host, pin = token.split("|", 1)
            pin = pin.strip().lower()
            return host.strip().lower(), (pin if pin in ("new", "classic") else None)
        return token.strip().lower(), None

    # --- new "Career Sites" (Jibe) JSON API ------------------------------

    async def _fetch_new(
        self, host: str, query: SearchQuery, fetcher: AsyncFetcher, first: dict[str, Any]
    ) -> list[RawJob]:
        limit = query.limit
        raws: list[RawJob] = []
        total = first.get("totalCount")
        total_pages = self.NEW_MAX_PAGES
        if isinstance(total, int) and total >= 0:
            total_pages = min(self.NEW_MAX_PAGES, max(1, math.ceil(total / self.NEW_PER_PAGE)))

        page = 1
        data: dict[str, Any] | None = first
        while page <= total_pages:
            if data is None:
                url = _API.format(host=host, page=page, limit=self.NEW_PER_PAGE)
                try:
                    fetched = await fetcher.get_json(url)
                except Exception:
                    break
                if not isinstance(fetched, dict):
                    break
                data = fetched
            jobs = data.get("jobs")
            if not isinstance(jobs, list) or not jobs:
                break
            for entry in jobs:
                rec = entry.get("data") if isinstance(entry, dict) else None
                if not isinstance(rec, dict):
                    continue
                raws.append(self._raw_new(rec, host))
                if limit is not None and len(raws) >= limit:
                    return raws[:limit]
            page += 1
            data = None
        return raws

    def _raw_new(self, rec: dict[str, Any], host: str) -> RawJob:
        jid = str(rec.get("req_id") or rec.get("slug") or "")
        company = _clean(rec.get("hiring_organization")) or self._host_company(host)
        rec = {**rec, "_gen": "new"}
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=company,
            token=host,
            url=_clean(rec.get("apply_url")),
            payload=rec,
        )

    # --- classic HTML + JSON-LD ------------------------------------------

    async def _fetch_classic(
        self, host: str, query: SearchQuery, fetcher: AsyncFetcher
    ) -> list[RawJob]:
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        total_pages = self.CLASSIC_MAX_PAGES

        for pr in range(self.CLASSIC_MAX_PAGES):
            if pr >= total_pages:
                break
            url = _SEARCH.format(host=host, pr=pr)
            try:
                html = await fetcher.get_text(url)
            except Exception:
                break

            if pr == 0:
                m = _PAGE_OF_RE.search(html)
                if m:
                    total_pages = min(self.CLASSIC_MAX_PAGES, int(m.group(2)))

            links = self._parse_links(html)
            new = 0
            for jid, slug in links:
                if jid in seen:
                    continue
                seen.add(jid)
                new += 1
                raw = await self._fetch_detail(host, jid, slug, fetcher)
                if raw is not None:
                    raws.append(raw)
                    if limit is not None and len(raws) >= limit:
                        return raws[:limit]
            if new == 0:
                break  # past the last page (or dupes only) -> stop
        return raws

    @staticmethod
    def _parse_links(html: str) -> list[tuple[str, str]]:
        """Extract ordered, de-duplicated ``(job_id, slug)`` pairs from a listing page."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        tree = HTMLParser(html)
        for node in tree.css("a"):
            href = node.attributes.get("href") or ""
            m = _JOB_LINK_RE.search(href)
            if not m:
                continue
            jid = m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            out.append((jid, m.group(2)))
        return out

    async def _fetch_detail(
        self, host: str, jid: str, slug: str, fetcher: AsyncFetcher
    ) -> RawJob | None:
        url = _DETAIL.format(host=host, jid=jid, slug=slug)
        try:
            html = await fetcher.get_text(url)
        except Exception:
            return None
        jobs = self.extract_jsonld_jobs(html)
        if not jobs:
            return None
        ld = {**jobs[0], "_gen": "classic"}
        org = ld.get("hiringOrganization")
        company = None
        if isinstance(org, dict):
            company = _clean(org.get("name"))
        company = company or self._host_company(host)
        page_url = _clean(ld.get("url")) or f"https://{host}/jobs/{jid}/{slug}/job"
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=company,
            token=host,
            url=page_url,
            payload=ld,
        )

    # --- detail (Tier-3 JD recovery) --------------------------------------

    @classmethod
    def _detail_url(cls, url: str) -> str | None:
        """Derive the per-posting classic detail URL from a public iCIMS careers URL.

        The index ``apply_url``/``listing_url`` shape is ``https://{host}/jobs/{id}/{slug}/...``
        (e.g. ``.../jobs/12345/some-title/login`` or ``.../job``) — ``slug``/trailing segments
        vary and are irrelevant here. iCIMS serves the same JSON-LD-bearing detail page at
        ``https://{host}/jobs/{id}/job`` regardless of slug (a missing/garbage slug is accepted),
        so we only need ``host`` and the numeric ``id`` right after ``/jobs/``. The ``in_iframe=1``
        query param is REQUIRED — live-verified: without it iCIMS serves a heavier framed page
        with no ``application/ld+json`` block at all (same param the classic listing/detail
        templates above already carry). Returns ``None`` (never raises) for anything that isn't a
        recognisable iCIMS host/path or has no numeric ``/jobs/{id}`` segment."""
        try:
            host = cls.matches(url)
            if not host:
                return None
            m = _JOBS_ID_RE.search(urlsplit(url).path)
            if not m:
                return None
            return f"https://{host}/jobs/{m.group(1)}/job?in_iframe=1"
        except Exception:
            return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via the classic per-posting detail page (Tier-3 recovery).

        The detail URL is derived deterministically from ``ref.apply_url`` (falling back to
        ``ref.listing_url``) — see :meth:`_detail_url`. The fetched HTML's ``application/ld+json``
        ``JobPosting`` block is parsed (reusing :meth:`BaseProvider.extract_jsonld_jobs`) and its
        ``description`` returned. Non-raising: any unparseable URL, fetch failure, missing/empty
        JSON-LD, or shape mismatch returns ``None``, never an exception."""
        detail_url: str | None = None
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            detail_url = self._detail_url(url)
            if detail_url:
                break
        if detail_url is None:
            return None
        try:
            html = await fetcher.get_text(detail_url)
        except Exception:
            return None
        if not isinstance(html, str) or not html:
            return None
        jobs = self.extract_jsonld_jobs(html)
        for job in jobs:
            description = job.get("description")
            if isinstance(description, str) and description.strip():
                return description
        return None

    # --- shared ----------------------------------------------------------

    @staticmethod
    def _host_company(host: str) -> str:
        """Derive a company label from the host (strip ``careers``/``uscareers`` prefixes)."""
        seg = host.split(".")[0]
        for prefix in ("careers-", "uscareers-", "careers", "uscareers"):
            if seg.startswith(prefix):
                trimmed = seg[len(prefix) :].lstrip("-")
                if trimmed:
                    return trimmed
        return seg

    def normalize(self, raw: RawJob) -> JobPosting:
        if raw.payload.get("_gen") == "classic":
            return self._normalize_classic(raw)
        return self._normalize_new(raw)

    def _normalize_new(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        locations: list[Location] = []
        city = _clean(p.get("city"))
        region = _clean(p.get("state"))
        country = _clean(p.get("country"))
        raw_loc = _clean(p.get("location_name")) or _clean(p.get("full_location"))
        if city or region or country or raw_loc:
            label = raw_loc or ", ".join(x for x in (city, region, country) if x)
            is_remote = "remote" in (label or "").lower()
            locations.append(
                Location(city=city, region=region, country=country, raw=label, is_remote=is_remote)
            )

        remote = (
            RemoteType.REMOTE if any(loc.is_remote for loc in locations) else RemoteType.UNKNOWN
        )

        et = (_clean(p.get("employment_type")) or "").upper().replace("-", "_").replace(" ", "_")
        employment = _EMPLOYMENT.get(et, EmploymentType.UNKNOWN)

        department = _clean(p.get("department"))
        if not department:
            cats = p.get("categories")
            if isinstance(cats, list) and cats and isinstance(cats[0], dict):
                department = _clean(cats[0].get("name"))

        salary = self._salary_from_minmax(p.get("salary_min_value"), p.get("salary_max_value"))

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=_clean(p.get("title")) or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=employment,
            department=department,
            salary=salary,
            posted_at=_parse_date(p.get("posted_date")),
            updated_at=_parse_date(p.get("update_date")),
            description_html=_clean(p.get("description")),
            description_text=None,
            raw=raw.payload,
        )

    def _normalize_classic(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        locations = self._jsonld_locations(p.get("jobLocation"))
        remote = (
            RemoteType.REMOTE if any(loc.is_remote for loc in locations) else RemoteType.UNKNOWN
        )

        emp = p.get("employmentType")
        if isinstance(emp, list):
            emp = emp[0] if emp else None
        et = (_clean(emp) or "").upper().replace("-", "_").replace(" ", "_")
        employment = _EMPLOYMENT.get(et, EmploymentType.UNKNOWN)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=_clean(p.get("title")) or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=employment,
            department=None,
            salary=self._jsonld_salary(p),
            posted_at=_parse_date(p.get("datePosted")),
            updated_at=None,
            description_html=_clean(p.get("description")),
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _salary_from_minmax(lo: Any, hi: Any) -> Salary | None:
        def _num(v: Any) -> float | None:
            if isinstance(v, bool):
                return None
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
            if isinstance(v, str):
                try:
                    f = float(v.replace(",", ""))
                except ValueError:
                    return None
                return f if f > 0 else None
            return None

        lo_n, hi_n = _num(lo), _num(hi)
        if lo_n is None and hi_n is None:
            return None
        return Salary(min_amount=lo_n, max_amount=hi_n)

    @staticmethod
    def _jsonld_salary(p: dict[str, Any]) -> Salary | None:
        base = p.get("baseSalary")
        if not isinstance(base, dict):
            return None
        currency = _clean(base.get("currency")) or _clean(p.get("salaryCurrency"))
        value = base.get("value")
        lo = hi = None
        if isinstance(value, dict):
            lo = value.get("minValue")
            hi = value.get("maxValue")
            single = value.get("value")
            if lo is None and hi is None and single is not None:
                lo = hi = single
        elif isinstance(value, (int, float)):
            lo = hi = value

        def _num(v: Any) -> float | None:
            if isinstance(v, bool):
                return None
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
            return None

        lo_n, hi_n = _num(lo), _num(hi)
        if lo_n is None and hi_n is None:
            return None
        return Salary(min_amount=lo_n, max_amount=hi_n, currency=currency)

    @staticmethod
    def _jsonld_locations(job_location: Any) -> list[Location]:
        items = job_location if isinstance(job_location, list) else [job_location]
        out: list[Location] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            addr = item.get("address")
            if not isinstance(addr, dict):
                continue
            city = _clean(addr.get("addressLocality"))
            region = _clean(addr.get("addressRegion"))
            country = _clean(addr.get("addressCountry"))
            raw_label = ", ".join(x for x in (city, region, country) if x) or None
            is_remote = "remote" in (raw_label or "").lower()
            out.append(
                Location(
                    city=city, region=region, country=country, raw=raw_label, is_remote=is_remote
                )
            )
        return out
