"""SAP SuccessFactors career-site (RMK / Career Site Builder) job-board provider.

SuccessFactors-hosted career sites (e.g. EY ``careers.ey.com/ey/...``, SAP
``jobs.sap.com/sap/...``) serve their public job list as **server-rendered HTML**,
paginated 25 jobs per page::

    GET https://{host}/{siteid}/search/?q=&startrow={N}      # N = 0, 25, 50, ...

Each result row is a ``<tr class="data-row">`` with a title link and a location::

    <a href="/ey/job/{slug}/{numericId}/" class="jobTitle-link">Analyst - ...</a>
    <span class="jobLocation">Mumbai, MH, IN, 400028</span>

Why HTML and not a feed
-----------------------
SuccessFactors *does* expose a structured RSS feed at ``/sitemal.xml`` (note the
``sitemaL`` typo) carrying every job in one document — but it's enormous (EY's is
**111 MB**), so downloading it just to verify or page a board is wasteful. The HTML
search page is light (~200 KB/page), respects a ``limit``, and — with no locale
selected — returns the *global* job set (EY's default view = 345 pages ≈ 8.6k jobs,
matching the feed). So HTML pagination is the primary path; the feed is a documented
fallback for >cap full pulls (not implemented here).

Token shape
-----------
``"{host}|{siteid}"`` (e.g. ``"careers.ey.com|ey"``). ``siteid`` is the first path
segment on every search/job URL. A bare ``"{host}"`` token is also accepted: we then
discover ``siteid`` from the site's landing page.

The search row exposes only title + location (no posting date, department, salary, or
description), so those normalize to ``None`` here — never invented. Posting dates live
on the per-job detail page, which we don't fetch in bulk.
"""

from __future__ import annotations

import html as _html
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from ..models import JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["SuccessFactorsProvider"]

PER_PAGE = 25  # SuccessFactors fixes the search page at 25 rows
_SEARCH = "https://{host}/{siteid}/search/?q=&startrow={start}"
# Some white-labeled CSB sites drop the siteid path segment entirely: search lives at ``/search/``
# and jobs at ``/job/{slug}/{id}/`` (e.g. careers.ltimindtree.com). Token then carries a ``*``
# siteid sentinel plus an explicit company label: ``"{host}|*|{Company}"``.
_SEARCH_ROOT = "https://{host}/search/?q=&startrow={start}"
_ROOT_SITEID = {"*", "-"}
# Classic "Recruiting Management" (RMK) sites don't use the path-based CSB search above; they
# expose an RSS job feed keyed by the company id (Skyworks: careers.skyworksinc.com, company=
# skyworks). We fall back to this when the CSB search yields nothing.
_RSS = "https://{host}/services/rss/job?company={siteid}"
_RSS_ITEM = re.compile(r"<item>(.*?)</item>", re.S | re.I)
_RSS_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S | re.I)
_RSS_LINK = re.compile(r"<link>(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?</link>", re.S | re.I)
_RSS_JOBID = re.compile(r"/job/[^/]*?/(\d{6,})/?", re.I)
_RSS_LOC = re.compile(r"\(([^()]+)\)\s*$")  # trailing "(City, ST, Country)" in the RSS title
# A SuccessFactors job URL: /{siteid}/job/{slug}/{numericId}/ — the LONG numeric id (SF ids are
# 9-10 digits) is the decisive, SF-specific signal.
_JOB_RE = re.compile(r"^/([^/]+)/job/.+/(\d{6,})/?$")
# A site landing link we can mine the siteid from: /{siteid}/search or /{siteid}/job/.../id/
_SITEID_RE = re.compile(r'href="/([a-z0-9][a-z0-9-]*)/(?:search|job)/', re.IGNORECASE)
# First-segment values that are locale/section markers, NOT an SF siteid — a bare
# ``/{seg}/search/`` is too generic (apple ``/en-us/search``, amazon ``/en``, ibm ``/careers``),
# so the /search/ shape only matches when the segment isn't one of these.
_GENERIC_SEG = frozenset(
    {
        "en",
        "en-us",
        "en-gb",
        "de",
        "fr",
        "es",
        "it",
        "ja",
        "zh",
        "pt",
        "nl",
        "global",
        "careers",
        "career",
        "search",
        "jobs",
        "job",
        "us",
        "uk",
        "content",
    }
)


@register("successfactors")
class SuccessFactorsProvider(BaseProvider):
    name = "successfactors"

    MAX_PAGES = 400  # bound full pulls (=10k jobs) when no limit is given

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a SuccessFactors career URL -> ``"{host}|{siteid}"`` token.

        Matches the SF-specific path shapes ``/{siteid}/job/{slug}/{digits}/`` and
        ``/{siteid}/search/`` so it won't collide with host-based ATS matchers.
        """
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if not host:
            return None
        path = parts.path
        m = _JOB_RE.match(path)
        if m:
            return f"{host}|{m.group(1).lower()}"
        seg = path.strip("/").split("/")
        if len(seg) >= 2 and seg[1] == "search" and seg[0] and seg[0].lower() not in _GENERIC_SEG:
            return f"{host}|{seg[0].lower()}"
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Token may be "{host}", "{host}|{siteid}", or root-CSB "{host}|*|{Company}".
        company_label: str | None = None
        if token.count("|") >= 2:
            host_s, siteid_s, company_label = (s.strip() for s in token.split("|", 2))
            host, siteid = host_s.lower(), siteid_s.lower()
        else:
            host, siteid = await self._resolve(token, fetcher)
        root = siteid in _ROOT_SITEID
        if not siteid:
            return []
        display = company_label or siteid

        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for page in range(self.MAX_PAGES):
            if root:
                url = _SEARCH_ROOT.format(host=host, start=page * PER_PAGE)
            else:
                url = _SEARCH.format(host=host, siteid=siteid, start=page * PER_PAGE)
            try:
                html = await fetcher.get_text(url)
            except Exception:
                break  # network/HTTP failure — stop gracefully with what we have

            rows = self._parse_rows(html, host, display)
            if not rows:
                break  # past the last page
            new = 0
            for jid, raw in rows:
                if jid in seen:
                    continue
                seen.add(jid)
                raws.append(raw)
                new += 1
                if limit is not None and len(raws) >= limit:
                    return raws
            if new == 0:
                break  # deep-pagination soft-cap returning dupes -> stop

        if not raws and not root:
            # Classic RMK site (no path-based CSB search) -> try the RSS job feed.
            raws = await self._fetch_rss(host, siteid, limit, fetcher)
        return raws

    async def _fetch_rss(
        self, host: str, siteid: str, limit: int | None, fetcher: AsyncFetcher
    ) -> list[RawJob]:
        """Classic-RMK fallback: parse the ``/services/rss/job?company={siteid}`` feed."""
        try:
            text = await fetcher.get_text(_RSS.format(host=host, siteid=siteid))
        except Exception:
            return []
        out: list[RawJob] = []
        seen: set[str] = set()
        for block in _RSS_ITEM.findall(text):
            link_m = _RSS_LINK.search(block)
            href = (link_m.group(1).strip() if link_m else "") or ""
            id_m = _RSS_JOBID.search(href)
            if not id_m:
                continue
            jid = id_m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            title_m = _RSS_TITLE.search(block)
            title = _html.unescape((title_m.group(1) if title_m else "").strip())
            loc_m = _RSS_LOC.search(title)
            location = ""
            if loc_m:
                location = loc_m.group(1).strip()
                title = title[: loc_m.start()].strip()
            out.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=siteid,
                    token=f"{host}|{siteid}",
                    url=href,
                    payload={"title": title, "location": location, "url": href, "id": jid},
                )
            )
            if limit is not None and len(out) >= limit:
                break
        return out

    async def _resolve(self, token: str, fetcher: AsyncFetcher) -> tuple[str, str]:
        """Return ``(host, siteid)`` from the token, discovering siteid if only a host is given."""
        if "|" in token:
            host, siteid = token.split("|", 1)
            return host.strip().lower(), siteid.strip().lower()
        host = token.strip().lower()
        try:
            landing = await fetcher.get_text(f"https://{host}/")
        except Exception:
            return host, ""
        m = _SITEID_RE.search(landing)
        return host, (m.group(1).lower() if m else "")

    def _parse_rows(self, html: str, host: str, siteid: str) -> list[tuple[str, RawJob]]:
        """Extract ``(job_id, RawJob)`` for each result row on a search page."""
        out: list[tuple[str, RawJob]] = []
        tree = HTMLParser(html)
        for row in tree.css("tr.data-row"):
            link = row.css_first("a.jobTitle-link")
            if link is None:
                continue
            href = link.attributes.get("href") or ""
            m = re.search(r"/job/.+/(\d+)/?$", href)
            if not m:
                continue
            jid = m.group(1)
            title = _html.unescape(link.text(strip=True))
            loc_node = row.css_first("span.jobLocation")
            location = loc_node.text(strip=True) if loc_node else ""
            url = f"https://{host}{href}" if href.startswith("/") else href
            out.append(
                (
                    jid,
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=siteid,
                        token=f"{host}|{siteid}",
                        url=url,
                        payload={"title": title, "location": location, "url": url, "id": jid},
                    ),
                )
            )
        return out

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD (Tier-3 recovery).

        Unlike Workday/SmartRecruiters, SuccessFactors has no separate per-posting detail API:
        ``ref.apply_url`` (falling back to ``ref.listing_url``) IS the job page already built by
        the list crawl (see ``_parse_rows``/``_fetch_rss``), so we simply re-fetch that same URL
        and pull the JD out of the search-result page's ``#jobdescription`` node (the CSB shape),
        falling back to the class-only ``.jobdescription`` (some white-label sites drop the id).
        Non-raising: no url, a failed/empty fetch, or an absent/empty JD node all return ``None``,
        never an exception.
        """
        url = ref.apply_url or ref.listing_url
        if not url:
            return None
        try:
            html = await fetcher.get_text(url)
        except Exception:
            return None
        if not html:
            return None
        tree = HTMLParser(html)
        node = tree.css_first("#jobdescription") or tree.css_first(".jobdescription")
        if node is None:
            return None
        if not (node.text(strip=True) or "").strip():
            return None  # tag present but empty (e.g. a JS-hydrated placeholder) -- nothing to keep
        jd_html = node.html
        if not jd_html:
            return None
        return jd_html

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
            salary=None,  # not exposed on the search row
            posted_at=None,  # lives on the per-job detail page, not fetched in bulk
            updated_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )
