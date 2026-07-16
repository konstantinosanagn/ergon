"""Jobvite career-site job-board provider.

Jobvite hosts each customer's public career site at ``jobs.jobvite.com/{company}``. The
no-auth, no-browser way to list a tenant's open reqs is the server-rendered "view all" page,
which returns the FULL active-req list in a single HTML document (no pagination)::

    GET https://jobs.jobvite.com/{company}/jobs/viewall   # follow 303 -> /careers/{company}/jobs

Two career-site generations share the same job-card markup family and are handled
transparently by following redirects: classic (``/{company}/jobs/viewall`` serves directly)
and newer "Engage" (303 -> ``/careers/{company}/jobs``).

Each job is a link ``/{company}/job/{slug}`` (an 8-char id). Three card layouts appear in the
wild — all carry a title cell (``.jv-job-list-name`` / ``.jv-featured-job-title``) and a
location cell (``.jv-job-list-location`` / ``.jv-featured-job-location``) — so we parse on
those stable ``jv-*`` classes, not tenant CSS. The list exposes only title + location + slug;
posting date, department, salary and description live only on the per-job detail page (which
carries JSON-LD ``JobPosting``) and are NOT fetched in bulk, so they normalize to ``None`` —
never invented.

Token shape: ``"{company}"`` (e.g. ``"buckman"``). The authenticated JSON/XML feeds
(``api.jobvite.com/v1/jobFeed``) need a per-customer key/secret and are out of scope.
"""

from __future__ import annotations

import html as _html
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser, Node

from ..models import DetailFetch, JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["JobviteProvider"]

_VIEWALL = "https://jobs.jobvite.com/{company}/jobs/viewall"
_JOB_URL = "https://jobs.jobvite.com/{company}/job/{slug}"
_TITLE_SEL = ".jv-job-list-name, .jv-featured-job-title"
_LOC_SEL = ".jv-job-list-location, .jv-featured-job-location"
# A Jobvite job link: /{company}/job/{slug}  (slug is an 8-ish-char alnum id).
_JOB_HREF_RE = re.compile(r"/job/([A-Za-z0-9_-]+)/?$")


@register("jobvite")
class JobviteProvider(BaseProvider):
    name = "jobvite"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a ``jobs.jobvite.com`` URL -> ``"{company}"`` token, else None.

        Handles both ``/{company}/...`` and the Engage ``/careers/{company}/...`` paths. Custom
        Jobvite-powered vanity domains can't be detected by host, so they aren't matched here.
        """
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if host != "jobs.jobvite.com":
            return None
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            return None
        company = segs[1] if segs[0] == "careers" and len(segs) >= 2 else segs[0]
        return company.lower() or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        company = token.strip().lower()
        if not company:
            return []
        try:
            html = await fetcher.get_text(_VIEWALL.format(company=company))
        except Exception:
            return []  # network/HTTP failure (or wrong tenant) — degrade gracefully

        limit = query.limit
        raws: list[RawJob] = []
        for slug, title, location in self._parse_rows(html, company):
            raws.append(self._to_raw(company, slug, title, location))
            if limit is not None and len(raws) >= limit:
                break
        return raws

    @classmethod
    def _parse_rows(cls, html: str, company: str) -> list[tuple[str, str, str]]:
        """Extract de-duplicated ``(slug, title, location)`` for each job card (variant-agnostic)."""
        tree = HTMLParser(html)
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for cell in tree.css(_TITLE_SEL):
            anchor = cell.css_first("a[href*='/job/']") or cls._ancestor_anchor(cell)
            if anchor is None:
                continue
            href = anchor.attributes.get("href") or ""
            m = _JOB_HREF_RE.search(href)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen:
                continue
            title = _html.unescape(cell.text(strip=True))
            if not title:
                continue
            seen.add(slug)
            out.append((slug, title, cls._card_location(cell)))
        return out

    @staticmethod
    def _ancestor_anchor(node: Node) -> Node | None:
        """Nearest ancestor ``<a>`` (the classic variant wraps the title cell in the link)."""
        cur: Node | None = node.parent
        for _ in range(6):
            if cur is None:
                return None
            if cur.tag == "a":
                return cur
            cur = cur.parent
        return None

    @staticmethod
    def _card_location(cell: Node) -> str:
        """Location text from the card's nearest ``li``/``tr`` ancestor, else ``""``."""
        cur: Node | None = cell
        for _ in range(8):
            cur = cur.parent if cur is not None else None
            if cur is None:
                return ""
            if cur.tag in ("li", "tr"):
                loc = cur.css_first(_LOC_SEL)
                return _html.unescape(loc.text(strip=True)) if loc is not None else ""
        return ""

    def _to_raw(self, company: str, slug: str, title: str, location: str) -> RawJob:
        url = _JOB_URL.format(company=company, slug=slug)
        return RawJob(
            source=self.name,
            source_job_id=slug,
            company=company,
            token=company,
            url=url,
            payload={"title": title, "location": location, "url": url, "id": slug},
        )

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD + structured location from its detail page (Tier-3 recovery).

        jobvite is list-only — the bulk ``viewall`` gives no description/pay/date, and its location
        is unreliable (a ``"N Locations"`` placeholder for multi-location jobs, or missing entirely
        for some company templates). The per-job page (== ``ref.apply_url``) has an
        ``application/ld+json`` ``JobPosting`` with the full ``description`` AND a structured
        ``jobLocation`` (city/region/country). Return the body (so yoe/degree/level extract) plus the
        structured locations so the merge can fill the index row's NULL city/country. Non-raising:
        any missing URL, fetch failure, or absent/empty JSON-LD ``description`` returns ``None``."""
        url = ref.apply_url or ref.listing_url
        if not url:
            return None
        try:
            html = await fetcher.get_text(url)
        except Exception:
            return None
        if not isinstance(html, str) or not html:
            return None
        for job in self.extract_jsonld_jobs(html):
            description = job.get("description")
            if isinstance(description, str) and description.strip():
                locations = self.jsonld_locations(
                    job.get("jobLocation")
                )  # shared BaseProvider helper
                if locations:
                    return DetailFetch(text=description, locations=locations)
                return description
        return None

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        location = str(p.get("location") or "").strip()
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if location:
            is_remote = "remote" in location.lower()
            locations.append(Location(raw=location, is_remote=is_remote))
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
            department=None,
            salary=None,  # detail-page only
            posted_at=None,  # detail-page JSON-LD only, not fetched in bulk
            updated_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )
