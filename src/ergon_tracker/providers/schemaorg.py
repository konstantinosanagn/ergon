"""Generic schema.org ``JobPosting`` provider — the "proxied giants" path.

Mega-employers (Walmart, CVS, Lowe's, Disney, …) proxy their ATS server-side, so the ATS
API is unreachable. But many publish their public job data on their **own careers domain**
as **XML sitemaps** of per-job URLs plus **schema.org ``JobPosting`` JSON-LD** on each detail
page (so Google for Jobs can index them). No auth, cookie, API key, or browser is required —
just an ordinary GET. This provider is the generic reader of that surface::

    robots.txt ``Sitemap:`` lines (or common paths)   # discover the sitemap
      -> walk any nested <sitemapindex>               # resolve to per-job <urlset>s
        -> collect /job(s)/... detail URLs            # bounded, de-duped
          -> GET each detail page                     # parse <script ld+json> JobPosting

The make-or-break requirement is that the JSON-LD is **server-rendered** (present in the raw
HTML). Google's crawler renders JavaScript, so many sites inject the ``JobPosting`` JSON-LD
client-side — invisible here. Such giants (and ones whose detail pages carry no JSON-LD at
all, e.g. Walmart's embedded AEM blob) simply yield ``[]`` gracefully: their pages are fetched
but produce no ``JobPosting``, so they are skipped.

Live-confirmed 2026-06-18 (server-rendered ``@type:JobPosting``):

* ``jobs.cvshealth.com``  — ``/us/en/sitemap_index.xml`` -> ``sitemap{1..N}.xml`` (777+/doc)
* ``talent.lowes.com``    — ``/us/en/sitemap_index.xml`` -> ``sitemap{1..7}.xml`` (500/doc)

Both are Phenom-People tenants emitting the *same* JSON-LD shape, so one parser covers both
(Disney, also Phenom, renders client-side and is NOT crackable — verify per tenant).

Token shape
-----------
``schemaorg`` is a **generic, opt-in** provider: it must not auto-claim arbitrary career hosts
during discovery, so ``matches()`` only resolves an explicit ``schemaorg:``/``schema:`` scheme
prefix (else ``None``). The token (after the prefix, or passed straight to ``fetch``) is either
a **careers host** (``"jobs.cvshealth.com"`` — robots.txt / common paths probed for the sitemap)
or a **full sitemap URL** (``"https://talent.lowes.com/us/en/sitemap_index.xml"``).

Never invented: missing JSON-LD fields normalize to ``None`` / ``UNKNOWN``. ``validThrough`` is
intentionally not mapped (``JobPosting`` has no expiry field).
"""

from __future__ import annotations

import html as _html
import re
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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

__all__ = ["SchemaOrgProvider"]

# Sitemap <loc> extraction (entities are unescaped after capture).
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
# A per-job detail URL: a ``/job/`` or ``/jobs/`` path segment followed by more path (so a
# bare ``/jobs`` listing or a ``technology-jobs/`` category slug does NOT match).
_JOB_PATH_RE = re.compile(r"/jobs?/[^/]", re.IGNORECASE)
# Listing/search shapes to exclude even though they carry a /job(s)/ segment.
_NOT_JOB_RE = re.compile(r"/(?:search|search-results|category|results)\b", re.IGNORECASE)
# Opt-in scheme prefix the discovery matcher recognises.
_SCHEME_RE = re.compile(r"^schema(?:org)?:", re.IGNORECASE)

# Common sitemap locations probed when robots.txt lists none (ordered most→least specific).
_CANDIDATE_PATHS = (
    "/us/en/sitemap_index.xml",
    "/sitemap_index.xml",
    "/sitemap.xml",
    "/job-sitemap.xml",
    "/sitemap-jobs.xml",
    "/jobs/sitemap.xml",
)

# schema.org employmentType codes -> our enum (deterministic).
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


def _clean(value: Any) -> str | None:
    """Return a stripped non-empty string, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _parse_date(value: Any) -> datetime | None:
    """Parse ISO-8601 or ``YYYY-MM-DD`` to a tz-aware datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    candidate = text.replace("Z", "+00:00")
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


@register("schemaorg")
class SchemaOrgProvider(BaseProvider):
    name = "schemaorg"

    MAX_SITEMAP_DOCS = 40  # bound how many sitemap documents we fetch while resolving
    DEFAULT_CAP = 50  # detail fetches when no query.limit is given (detail GETs are expensive)
    HARD_CAP = 500  # absolute ceiling on detail fetches even when a large limit is requested

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Resolve an explicit ``schemaorg:``/``schema:`` token, else None.

        This provider is a generic JSON-LD fallback, so it deliberately does NOT auto-claim
        bare career hosts (that would collide with every other provider during discovery). A
        caller opts in with ``schemaorg:<host-or-sitemap-url>``.
        """
        m = _SCHEME_RE.match(url_or_host.strip())
        if not m:
            return None
        token = url_or_host.strip()[m.end() :].strip()
        return token or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        token = _SCHEME_RE.sub("", token.strip()).strip()
        if not token:
            return []

        cap = query.limit if query.limit is not None else self.DEFAULT_CAP
        cap = max(0, min(cap, self.HARD_CAP))
        if cap == 0:
            return []

        roots, host = await self._resolve_sitemaps(token, fetcher)
        if not roots:
            return []

        # Over-collect a little: some detail pages carry no server-rendered JobPosting and are
        # skipped, so we need a buffer of candidate URLs to still reach ``cap`` real jobs.
        collect_target = min(cap * 3, self.HARD_CAP * 3)
        job_urls = await self._collect_job_urls(roots, fetcher, collect_target)

        raws: list[RawJob] = []
        for url in job_urls:
            raw = await self._fetch_detail(url, host, fetcher)
            if raw is not None:
                raws.append(raw)
                if len(raws) >= cap:
                    break
        return raws

    # --- sitemap resolution ----------------------------------------------

    async def _resolve_sitemaps(self, token: str, fetcher: AsyncFetcher) -> tuple[list[str], str]:
        """Return ``(sitemap_root_urls, host)`` for a host or a direct sitemap URL token."""
        if "://" in token:
            host = urlsplit(token).netloc.split("@")[-1].split(":")[0].lower()
            return [token], host

        host = token.strip().lower()
        roots: list[str] = []
        try:
            robots = await fetcher.get_text(f"https://{host}/robots.txt")
        except Exception:
            robots = ""
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                loc = line.split(":", 1)[1].strip()
                if loc:
                    roots.append(loc)
        if roots:
            return roots, host

        for path in _CANDIDATE_PATHS:
            url = f"https://{host}{path}"
            try:
                text = await fetcher.get_text(url)
            except Exception:
                continue
            if "<loc" in text.lower():
                return [url], host
        return [], host

    async def _collect_job_urls(
        self, roots: list[str], fetcher: AsyncFetcher, target: int
    ) -> list[str]:
        """BFS the sitemap graph, collecting de-duped per-job detail URLs up to ``target``."""
        seen_docs: set[str] = set()
        seen_urls: set[str] = set()
        out: list[str] = []
        queue: deque[str] = deque(roots)
        docs = 0

        while queue and docs < self.MAX_SITEMAP_DOCS and len(out) < target:
            sm = queue.popleft()
            if sm in seen_docs:
                continue
            seen_docs.add(sm)
            try:
                text = await fetcher.get_text(sm)
            except Exception:
                continue
            docs += 1

            is_index = "<sitemapindex" in text.lower()
            for match in _LOC_RE.findall(text):
                loc = _html.unescape(match).strip()
                if not loc:
                    continue
                if is_index:
                    if loc not in seen_docs:
                        queue.append(loc)
                    continue
                if loc in seen_urls:
                    continue
                path = urlsplit(loc).path
                if _JOB_PATH_RE.search(path) and not _NOT_JOB_RE.search(path):
                    seen_urls.add(loc)
                    out.append(loc)
                    if len(out) >= target:
                        break
        return out

    # --- detail parsing --------------------------------------------------

    async def _fetch_detail(self, url: str, host: str, fetcher: AsyncFetcher) -> RawJob | None:
        try:
            html = await fetcher.get_text(url)
        except Exception:
            return None
        jobs = self.extract_jsonld_jobs(html)
        if not jobs:
            return None
        ld = jobs[0]
        company = self._org_name(ld.get("hiringOrganization")) or self._host_company(host)
        page_url = _clean(ld.get("url")) or url
        return RawJob(
            source=self.name,
            source_job_id=self._identifier(ld, url),
            company=company,
            token=host,
            url=page_url,
            payload=ld,
        )

    @staticmethod
    def _identifier(ld: dict[str, Any], url: str) -> str:
        """Stable job id from ``identifier`` (PropertyValue.value / string), else the URL."""
        idf = ld.get("identifier")
        if isinstance(idf, dict):
            value = _clean(idf.get("value")) or _clean(idf.get("name"))
            if value:
                return value
        elif isinstance(idf, (str, int)):
            text = str(idf).strip()
            if text:
                return text
        return url

    @staticmethod
    def _org_name(org: Any) -> str | None:
        if isinstance(org, dict):
            return _clean(org.get("name"))
        return _clean(org)

    @staticmethod
    def _host_company(host: str) -> str:
        """Derive a company label from the host (strip ``careers``/``jobs``/``talent`` prefixes)."""
        seg = host.split(".")[0] if host else host
        for prefix in ("careers-", "jobs-", "talent-", "careers", "jobs", "talent"):
            if seg.startswith(prefix):
                trimmed = seg[len(prefix) :].lstrip("-")
                if trimmed:
                    return trimmed
        return seg or host

    def normalize(self, raw: RawJob) -> JobPosting:
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
            department=_clean(p.get("occupationalCategory")),
            salary=self._jsonld_salary(p),
            posted_at=_parse_date(p.get("datePosted")),
            updated_at=None,
            description_html=_clean(p.get("description")),
            description_text=None,
            raw=raw.payload,
        )

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

    @staticmethod
    def _jsonld_salary(p: dict[str, Any]) -> Salary | None:
        base = p.get("baseSalary")
        if not isinstance(base, dict):
            return None
        currency = _clean(base.get("currency"))
        value = base.get("value")
        lo: Any = None
        hi: Any = None
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
        return Salary(min_amount=lo_n, max_amount=hi_n, currency=currency)
