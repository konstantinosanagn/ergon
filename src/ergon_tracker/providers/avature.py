"""Avature career-portal job-board provider.

Avature-hosted career portals (e.g. Bloomberg ``bloomberg.avature.net/careers/...``)
serve their public job list as **server-rendered HTML** with no JS and no public JSON
search API. The list is paginated by a ``jobOffset`` query parameter::

    GET https://{host}/{portalPath}/SearchJobs?jobRecordsPerPage={N}&jobOffset={K}

The server 302-redirects to a locale-prefixed URL (``/en_US/{portalPath}/SearchJobs``);
the HTTP client follows redirects automatically. Each result card carries a job link::

    <a href="https://{host}/{portalPath}/JobDetail/{slug}/{numericId}">Job Title</a>

Why HTML (and not a feed)
-------------------------
Avature exposes an RSS feed at ``/{portalPath}/SearchJobs/feed/?jobRecordsPerPage=20``,
but it is **hard-capped at 20 items and ignores ``jobOffset``** — useless for paging a
full board — so HTML pagination is the primary path. There is no public JSON/REST search
endpoint (the "JSON Jobs API" is a contracted per-customer feed) and stock JobDetail pages
carry no JSON-LD.

Theme robustness
----------------
Avature themes vary wildly per tenant, so parsing anchors on the **stable
``JobDetail/{slug}/{id}`` href + visible title text** — never on tenant-specific CSS
classes. Location is read from a card-scoped element whose class contains ``location``
when present; themes that don't class-tag it yield ``None`` (never invented).

Token shape
-----------
``"{host}|{portalPath}"`` (e.g. ``"bloomberg.avature.net|careers"``). ``portalPath`` is
per-tenant (commonly ``careers`` or ``main``). A bare ``"{host}"`` token is also accepted:
we then try ``careers`` then ``main`` and use the first that returns jobs.

Pitfalls
--------
Some tenants block non-browser clients (HTTP 202 + 0-byte body, e.g. ``koch``) or require
login; those degrade to ``[]``. The search card exposes only title + location, so posting
date, department, salary, and description normalize to ``None`` here — never invented.
"""

from __future__ import annotations

import html as _html
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser, Node

from ..models import JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["AvatureProvider"]

PER_PAGE = 100  # jobRecordsPerPage; jobOffset advances by this each page
_SEARCH = "https://{host}/{portal}/{page}?jobRecordsPerPage={n}&jobOffset={offset}"
# The standard Avature search page; some tenants rename it (Two Sigma -> "OpenRoles"), supplied
# as the optional 3rd token segment "{host}|{portal}|{page}".
_DEFAULT_PAGE = "SearchJobs"
# Default portalPath candidates tried (in order) for a bare-host token.
_DEFAULT_PORTALS = ("careers", "main")
# Stable Avature job link: .../JobDetail/{slug}/{numericId} (slug may be absent on some
# themes, hence the non-greedy middle). The trailing numeric id is the decisive signal.
_JOB_RE = re.compile(r"/JobDetail/.*?/(\d+)")
# The page names that pin the portalPath in a URL path (segment immediately before them).
_PAGES = ("SearchJobs", "JobDetail")


@register("avature")
class AvatureProvider(BaseProvider):
    name = "avature"

    MAX_PAGES = 200  # bound full pulls (=20k jobs) when no limit is given

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise an Avature portal URL/host -> ``"{host}|{portalPath}"`` token.

        Matches ``*.avature.net`` hosts. When the path contains ``SearchJobs``/``JobDetail``
        the ``portalPath`` is the segment just before it (locale prefixes are skipped
        automatically); otherwise the bare host is returned and the portal is resolved at
        fetch time. Non-Avature hosts return ``None``.
        """
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if not host.endswith(".avature.net"):
            return None
        segs = [s for s in parts.path.split("/") if s]
        for page in _PAGES:
            if page in segs:
                i = segs.index(page)
                if i >= 1:
                    return f"{host}|{segs[i - 1].lower()}"
        return host  # bare host: portalPath discovered at fetch time

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, portal, page_name = self._split(token)
        if portal:
            raws = await self._fetch_portal(host, portal, page_name, query, fetcher)
            # JS-widget themes (e.g. Slalom) render no JobDetail anchors in static HTML, so the
            # HTML parse yields 0; the 20-item RSS feed is then the only reachable source.
            return raws or await self._fetch_rss(host, portal, query.limit, fetcher)
        # Bare host: try the default portalPaths and use the first that yields jobs.
        for cand in _DEFAULT_PORTALS:
            raws = await self._fetch_portal(host, cand, page_name, query, fetcher)
            if raws:
                return raws
            rss = await self._fetch_rss(host, cand, query.limit, fetcher)
            if rss:
                return rss
        return []

    @staticmethod
    def _rss_company(host: str) -> str:
        # bloomberg.avature.net -> "bloomberg"; own-domain feeds (jobs.slalom.com,
        # careers.bv.com) -> strip the generic prefix and use the brand label.
        parts = host.split(".")
        first = parts[0] if parts else host
        if first in {"jobs", "careers", "career", "job", "apply", "work", "talent"} and len(parts) >= 3:
            return parts[1]
        return first

    async def _fetch_rss(
        self, host: str, portal: str, limit: int | None, fetcher: AsyncFetcher
    ) -> list[RawJob]:
        """Fallback for JS-widget themes: the Avature RSS feed (hard-capped at 20 items)."""
        url = f"https://{host}/{portal}/SearchJobs/feed/?jobRecordsPerPage=20"
        try:
            text = await fetcher.get_text(url)
        except Exception:
            return []
        company = self._rss_company(host)
        out: list[RawJob] = []
        seen: set[str] = set()
        for block in re.findall(r"<item>(.*?)</item>", text, re.S | re.I):
            link_m = re.search(r"<link>\s*(.*?)\s*</link>", block, re.S | re.I)
            href = _html.unescape(link_m.group(1).strip()) if link_m else ""
            id_m = _JOB_RE.search(href)
            if not id_m:
                continue
            jid = id_m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S | re.I)
            title = _html.unescape((title_m.group(1) if title_m else "").strip())
            out.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=company,
                    token=f"{host}|{portal}",
                    url=href,
                    payload={"title": title, "location": "", "url": href, "id": jid},
                )
            )
            if limit is not None and len(out) >= limit:
                break
        return out

    @staticmethod
    def _split(token: str) -> tuple[str, str, str]:
        """Return ``(host, portalPath, pageName)``. Token: ``"{host}"`` | ``"{host}|{portal}"`` |
        ``"{host}|{portal}|{page}"`` (pageName defaults to the standard ``SearchJobs``)."""
        parts = [p.strip() for p in token.split("|")]
        host = parts[0].lower()
        portal = parts[1].lower() if len(parts) > 1 and parts[1] else ""
        page = parts[2] if len(parts) > 2 and parts[2] else _DEFAULT_PAGE
        return host, portal, page

    async def _fetch_portal(
        self, host: str, portal: str, page_name: str, query: SearchQuery, fetcher: AsyncFetcher
    ) -> list[RawJob]:
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        offset = 0
        for _page in range(self.MAX_PAGES):
            url = _SEARCH.format(
                host=host, portal=portal, page=page_name, n=PER_PAGE, offset=offset
            )
            try:
                resp = await fetcher.request("GET", url)
            except Exception:
                break  # network/HTTP failure — stop gracefully with what we have
            if resp.status_code != 200:
                break  # 202/404/login -> blocked or wrong portalPath: degrade
            html = resp.text
            if not html.strip():
                break  # empty body (anti-bot) -> degrade

            rows = self._parse_rows(html, host, portal)
            if not rows:
                break  # past the last page (no job links)
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
                break  # offset overran -> server repeated a page; stop
            # Advance by the page's ACTUAL size, not a fixed PER_PAGE: some tenants serve a small
            # fixed page (Two Sigma -> 10) regardless of jobRecordsPerPage, so stepping by PER_PAGE
            # would skip every job between the real page size and PER_PAGE.
            offset += len(rows)
        return raws

    def _parse_rows(self, html: str, host: str, portal: str) -> list[tuple[str, RawJob]]:
        """Extract ``(job_id, RawJob)`` for each unique JobDetail link on a search page."""
        tree = HTMLParser(html)
        # Group anchors by job id; a card has a title link plus an "Apply" button sharing the
        # same id — keep the longest text (the real title, not "Apply").
        best: dict[str, tuple[Node, str]] = {}
        for a in tree.css("a[href*=JobDetail]"):
            href = a.attributes.get("href") or ""
            m = _JOB_RE.search(href)
            if not m:
                continue
            jid = m.group(1)
            title = _html.unescape(a.text(strip=True))
            cur = best.get(jid)
            if cur is None or len(title) > len(cur[1]):
                best[jid] = (a, title)

        out: list[tuple[str, RawJob]] = []
        for jid, (anchor, title) in best.items():
            if not title:
                continue  # only image/empty anchors for this id
            href = anchor.attributes.get("href") or ""
            if href.startswith("http"):
                url = href
            else:
                url = f"https://{host}{href if href.startswith('/') else '/' + href}"
            location = self._card_location(anchor)
            out.append(
                (
                    jid,
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=host.split(".")[0],
                        token=f"{host}|{portal}",
                        url=url,
                        payload={"title": title, "location": location, "url": url, "id": jid},
                    ),
                )
            )
        return out

    @staticmethod
    def _card_location(anchor: Node) -> str:
        """Best-effort location text from the anchor's card, via a class containing
        ``location``. Returns ``""`` when the theme doesn't class-tag it (never invented)."""
        node: Node | None = anchor
        for _ in range(8):
            node = node.parent if node is not None else None
            if node is None:
                return ""
            if node.tag in ("article", "li"):
                loc = node.css_first('[class*="location"]')
                if loc is not None:
                    return _html.unescape(loc.text(strip=True))
                return ""
        return ""

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
            salary=None,  # not exposed on the search card
            posted_at=None,  # lives on the per-job detail page, not fetched in bulk
            updated_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )
