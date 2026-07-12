"""Paycom Applicant Tracking careers provider — BROWSER-BACKED.

Paycom career boards live at ``paycomonline.net/v4/ats/web.php/jobs?clientkey={KEY}`` (KEY = a
32-hex client id; also seen as ``/v4/ats/web.php/portal/{KEY}/career-page``). The board is a SPA
that loads jobs from ``portal-applicant-tracking.{region}.paycomonline.net/api/ats/
job-posting-previews/search`` — a POST that requires a per-session JWT ``authorization`` header
the app mints on load. Plain HTTP gets 401, so (like Dayforce) ``fetch`` drives headless Chromium
via Playwright (lazily imported -> degrades to empty if absent):

  1. load the portal; let the app fire its own search request,
  2. capture that request's URL + JWT ``authorization`` header,
  3. re-issue it from the page context, paginated (``skip``/``take``), parsing ``jobPostingPreviews``
     (``jobId``, ``jobTitle``, ``locations`` (string), ``remoteType``, ``postedOn``, ``description``).

Token: ``"{KEY}"`` or ``"{KEY}|{company}"`` to carry a display name.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PaycomProvider"]

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_CK_RE = re.compile(r"[0-9A-Fa-f]{32}")
_PAGE = 50
# Paycom ``positionType`` (filter facet ``positionTypes`` in the search body) -> canonical
# EmploymentType. Confirmed value from a live sample: "Full Time"; the rest mirror the other
# payroll/HCM-vendor ATSes (paylocity, bamboohr) in this codebase.
_EMPLOYMENT = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "seasonal": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}
# In-page: re-issue the app's own search request (captured auth header) with our paging.
_JS = """async (a) => {
  const out = []; const seen = {};
  for (let page = 0; page < a.maxPages; page++) {
    const r = await fetch(a.url, {method:'POST',
      headers:{'content-type':'application/json','authorization':a.auth},
      body: JSON.stringify({skip: page*a.take, take: a.take, filtersForQuery:{distanceFrom:0,
        workEnvironments:[],positionTypes:[],educationLevels:[],categories:[],travelTypes:[],
        shiftTypes:[],otherFilters:[],keywordSearchText:'',location:'',sortOption:''}})});
    if (r.status !== 200) break;
    const d = await r.json();
    const posts = (d && d.jobPostingPreviews) || [];
    if (!posts.length) break;
    let fresh = 0;
    for (const p of posts) { if (!seen[p.jobId]) { seen[p.jobId]=1; out.push(p); fresh++; } }
    if (fresh === 0 || posts.length < a.take) break;
    if (a.limit && out.length >= a.limit) break;
  }
  return out;
}"""


@register("paycom")
class PaycomProvider(BaseProvider):
    name = "paycom"

    MAX_PAGES = 40

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a Paycom board URL -> token (the 32-hex clientkey), else None."""
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if not host.endswith("paycomonline.net"):
            return None
        key = (parse_qs(parts.query).get("clientkey") or [""])[0].strip()
        if not _CK_RE.fullmatch(key):
            m = _CK_RE.search(parts.path) or _CK_RE.search(url_or_host)
            key = m.group(0) if m else ""
        if not _CK_RE.fullmatch(key):
            return None
        return key.upper()

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        key = parts[0].upper()
        company = parts[1] if len(parts) > 1 and parts[1] else None
        return key, company

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        key, company = self._parse(token)
        if not _CK_RE.fullmatch(key):
            return []
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []
        limit = query.limit
        portal = (
            f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={key}&fromClientSide=true"
        )
        captured: dict[str, str] = {}
        posts: list[dict[str, Any]] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                ctx = await browser.new_context(user_agent=_UA)
                page = await ctx.new_page()

                async def _grab(req: Any) -> None:
                    if "job-posting-previews/search" in req.url and "url" not in captured:
                        headers = await req.all_headers()
                        if headers.get("authorization"):
                            captured["url"] = req.url
                            captured["auth"] = headers["authorization"]

                page.on("request", lambda r: __import__("asyncio").ensure_future(_grab(r)))
                with contextlib.suppress(Exception):
                    await page.goto(portal, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(7000)  # let the app fire its authed search request
                if "url" in captured:
                    result = await page.evaluate(
                        _JS,
                        {
                            "url": captured["url"],
                            "auth": captured["auth"],
                            "take": _PAGE,
                            "maxPages": self.MAX_PAGES,
                            "limit": limit or 0,
                        },
                    )
                    if isinstance(result, list):
                        posts = [r for r in result if isinstance(r, dict)]
            finally:
                await browser.close()
        raws: list[RawJob] = []
        for rec in posts:
            jid = str(rec.get("jobId") or "")
            if not jid:
                continue
            raws.append(self._to_raw(rec, key, company, jid))
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def _to_raw(self, rec: dict[str, Any], key: str, company: str | None, jid: str) -> RawJob:
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=company or key,
            token=key if not company else f"{key}|{company}",
            url=f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={key}&jobId={jid}",
            payload=rec,
        )

    @staticmethod
    def _location(rec: dict[str, Any]) -> Location | None:
        raw = str(rec.get("locations") or "").strip()
        if not raw:
            return None
        remote = "remote" in raw.lower() or "remote" in str(rec.get("remoteType") or "").lower()
        # "Cincinnati BA - Blue Ash, OH 45242" -> best-effort city/region from the trailing parts.
        city = region = None
        if "," in raw:
            head, _, tail = raw.rpartition(",")
            region = tail.strip().split(" ")[0] or None
            city = head.split("-")[-1].strip() or None
        return Location(city=city, region=region, raw=raw, is_remote=remote)

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = self._location(p)
        rt = str(p.get("remoteType") or "").lower()
        remote = (
            RemoteType.REMOTE
            if "remote" in rt or (loc and loc.is_remote)
            else RemoteType.HYBRID
            if "hybrid" in rt
            else RemoteType.UNKNOWN
        )
        employment = _EMPLOYMENT.get(
            str(p.get("positionType") or "").strip().lower(), EmploymentType.UNKNOWN
        )
        # NOTE: the ``job-posting-previews/search`` endpoint's ``description`` is a HARD-TRUNCATED
        # ~153-char teaser (confirmed by sampling: every non-empty value observed was exactly 153
        # chars, cut off mid-word), NOT the full job description — Paycom only serves the complete
        # JD from a separate per-job detail page/endpoint this provider doesn't fetch. We still
        # store the teaser in ``description_html`` (it's short plain text, not HTML) because a
        # short snippet still aids keyword search/dedup and won't false-positive against the
        # precision-oriented degree/comp/yoe extractors run over it -- but it must never be
        # treated as a complete JD by downstream consumers.
        desc = p.get("description")
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("jobTitle") or "").strip(),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            employment_type=employment,
            description_html=desc if isinstance(desc, str) and desc.strip() else None,
        )
