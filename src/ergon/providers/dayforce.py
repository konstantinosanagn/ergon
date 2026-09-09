"""Dayforce HCM (Ceridian) careers provider — BROWSER-BACKED.

Dayforce's unified candidate portal lives at ``jobs.dayforcehcm.com/{clientNamespace}/
CANDIDATEPORTAL`` (legacy ``www`` / ``us{N}.dayforcehcm.com/CandidatePortal/...`` hosts 301 here).
Its job API is a clean POST::

    POST https://jobs.dayforcehcm.com/api/geo/{ns}/jobposting/search
    body {"clientNamespace": ns, "jobBoardCode": "CANDIDATEPORTAL", "cultureCode": "en-US",
          "distanceUnit": 0, "paginationStart": N}
    -> {"jobPostings": [ {jobPostingId, jobTitle, jobDescription, postingStartTimestampUTC,
                          postingLocations:[{cityName,stateCode,isoCountryCode,formattedAddress}],
                          hasVirtualLocation, ...}, ... ]}   (page size 25)

UNLIKE every other provider this one is NOT pure-HTTP: the endpoint sits behind Cloudflare's JS
challenge (httpx, curl_cffi TLS-impersonation, and even a reused cf_clearance cookie all get 403)
AND requires an ``x-csrf-token`` from ``/api/auth/csrf``. The only reliable path is to issue the
calls *from inside a real browser context*, so ``fetch`` drives headless Chromium via Playwright
(lazily imported — if Playwright isn't installed the provider degrades to returning nothing). It's
heavier/slower than the JSON providers, so it's meant for a small, paced lane.

Token: ``"{ns}"`` (jobBoardCode defaults to ``CANDIDATEPORTAL``), or ``"{ns}|{jobBoardCode}"``, or
``"{ns}|{jobBoardCode}|{company}"`` to carry a display name.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["DayforceProvider"]

_BASE = "https://jobs.dayforcehcm.com"
_DEFAULT_BOARD = "CANDIDATEPORTAL"
_PAGE = 25
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Path framing tokens that are never the client namespace.
_FRAME = {"candidateportal", "site", "jobs", "go", "candidateportalv2"}
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)
# The in-page JS: grab a CSRF token, then page through the search API. Runs in the portal's own
# browser context so Cloudflare + same-origin cookies are satisfied.
_JS = """async (a) => {
  const out = []; const seen = {};
  const csrf = await (await fetch('/api/auth/csrf', {headers:{'accept':'application/json'}})).json();
  for (let page = 0; page < a.maxPages; page++) {
    const r = await fetch(`/api/geo/${a.ns}/jobposting/search`, {method:'POST',
      headers:{'content-type':'application/json','accept':'application/json','x-csrf-token':csrf.csrfToken},
      body: JSON.stringify({clientNamespace:a.ns, jobBoardCode:a.board, cultureCode:'en-US',
                            distanceUnit:0, paginationStart: page * a.pageSize})});
    if (r.status !== 200) break;
    const d = await r.json();
    const posts = (d && d.jobPostings) || [];
    if (!posts.length) break;
    let fresh = 0;
    for (const p of posts) { if (!seen[p.jobPostingId]) { seen[p.jobPostingId]=1; out.push(p); fresh++; } }
    if (fresh === 0 || posts.length < a.pageSize) break;
    if (a.limit && out.length >= a.limit) break;
  }
  return out;
}"""


@register("dayforce")
class DayforceProvider(BaseProvider):
    name = "dayforce"

    MAX_PAGES = 60  # bound full pulls (=1500 jobs)

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a Dayforce candidate-portal URL -> token (``ns`` or ``ns|board``), else None."""
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if not host.endswith("dayforcehcm.com"):
            return None
        segs = [s for s in parts.path.split("/") if s]
        # Strip a leading "CandidatePortal" mount and locale segments; the namespace is the first
        # remaining non-framing, non-locale segment. The board code is a CANDIDATEPORTAL* segment.
        ns = ""
        board = _DEFAULT_BOARD
        for s in segs:
            low = s.lower()
            if low.startswith("candidateportal"):
                board = s.upper()
                continue
            if low in _FRAME or _LOCALE_RE.match(low):
                continue
            if not ns:
                ns = s
        if not ns:
            return None
        ns = ns.lower()
        return ns if board == _DEFAULT_BOARD else f"{ns}|{board}"

    @staticmethod
    def _parse(token: str) -> tuple[str, str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        ns = parts[0].lower()
        board = parts[1] if len(parts) > 1 and parts[1] else _DEFAULT_BOARD
        company = parts[2] if len(parts) > 2 and parts[2] else None
        return ns, board, company

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        ns, board, company = self._parse(token)
        if not ns:
            return []
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return []  # browser lane unavailable -> degrade gracefully
        limit = query.limit
        posts: list[dict[str, Any]] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                ctx = await browser.new_context(user_agent=_UA)
                page = await ctx.new_page()
                with contextlib.suppress(Exception):  # challenge/slow page; in-page calls still run
                    await page.goto(
                        f"{_BASE}/{ns}/{board}", wait_until="domcontentloaded", timeout=40000
                    )
                await page.wait_for_timeout(5000)  # let Cloudflare clear + session settle
                result = await page.evaluate(
                    _JS,
                    {
                        "ns": ns,
                        "board": board,
                        "pageSize": _PAGE,
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
            jid = str(rec.get("jobPostingId") or "")
            if not jid:
                continue
            raws.append(self._to_raw(rec, ns, board, company, jid))
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def _to_raw(
        self, rec: dict[str, Any], ns: str, board: str, company: str | None, jid: str
    ) -> RawJob:
        token = ns if board == _DEFAULT_BOARD else f"{ns}|{board}"
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=company or ns,
            token=token,
            url=f"{_BASE}/{ns}/{board}/jobs/{jid}",
            payload=rec,
        )

    @staticmethod
    def _location(rec: dict[str, Any]) -> Location | None:
        locs = rec.get("postingLocations")
        item = locs[0] if isinstance(locs, list) and locs else None
        if not isinstance(item, dict):
            return None
        city = str(item.get("cityName") or "").strip()
        state = str(item.get("stateCode") or "").strip()
        label = str(item.get("formattedAddress") or "").strip() or ", ".join(
            x for x in (city, state) if x
        )
        if not label:
            return None
        return Location(city=city or None, region=state or None, raw=label, is_remote=False)

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        remote = RemoteType.REMOTE if p.get("hasVirtualLocation") is True else RemoteType.UNKNOWN
        loc = self._location(p)
        desc = p.get("jobDescription")
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("jobTitle") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            posted_at=self._date(p.get("postingStartTimestampUTC")),
            description_html=desc if isinstance(desc, str) and desc.strip() else None,
        )
