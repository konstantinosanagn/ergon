"""The Muse provider — an aggregator (not a per-company ATS).

``GET https://www.themuse.com/api/public/jobs?page={p}`` returns a JSON object::

    {"page": 1, "page_count": N, "items_per_page": 20, "total": M,
     "results": [{"id", "name", "company": {"name"}, "locations": [{"name"}],
                  "levels": [{"name"}], "type", "refs": {"landing_page"},
                  "publication_date", "contents", "categories": [{"name"}]}, ...]}

Pages are **1-indexed**. Because this is an aggregator it is never auto-discovered from a
company URL, so ``matches`` always returns ``None`` and the orchestrator invokes it with an
empty ``token``. To satisfy ``query.limit`` we fetch multiple pages **concurrently** (capped at
``MAX_PAGES``) via an anyio task group.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import anyio
import httpx
from selectolax.parser import HTMLParser

from ..extract.level import level_from_ats_vocab
from ..models import EmploymentType, JobLevel, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef
    from ..models import SearchQuery

_API = "https://www.themuse.com/api/public/jobs"

# "Flexible / Remote" is The Muse's own remote location label.
_REMOTE_HINTS = ("remote", "flexible")

_EMPLOYMENT_MAP = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@register("themuse")
class TheMuseProvider(BaseProvider):
    name = "themuse"

    PAGE_SIZE = 20
    MAX_PAGES = 5
    COMPANY_MAX_PAGES = 25  # a company board pulls deeper (the employer filter is server-side)

    # Minimum <main> text length to trust as a real rendered JD page rather than a near-empty
    # shell (verified live: real JDs render 4.6k-6.8k chars across all 3 probe companies).
    _DETAIL_MIN_LEN = 200

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # A non-empty token = a COMPANY board: filter The Muse to that employer's postings — a
        # last-resort source for giants unreachable on their own ATS (e.g. DTCC is anti-bot-walled
        # but indexed here). Empty token = the original global aggregator.
        company = (token or "").strip()
        cap = self.COMPANY_MAX_PAGES if company else self.MAX_PAGES

        if query.limit is not None and not company:
            pages_needed = max(1, -(-query.limit // self.PAGE_SIZE))  # ceil div
        else:
            pages_needed = cap
        page_count = min(pages_needed, cap)

        params: dict[str, Any] = {}
        if company:
            params["company"] = company
        if query.location:
            params["location"] = query.location

        # Fetch pages 1..page_count concurrently; preserve page order in results.
        results: list[list[dict[str, Any]]] = [[] for _ in range(page_count)]

        async def _fetch_page(idx: int) -> None:
            page = idx + 1  # API pages are 1-indexed.
            data = await fetcher.get_json(_API, params={**params, "page": page})
            if isinstance(data, dict):
                items = data.get("results") or []
                results[idx] = [j for j in items if isinstance(j, dict)]

        async def _drain() -> None:
            async with anyio.create_task_group() as tg:
                for idx in range(page_count):
                    tg.start_soon(_fetch_page, idx)

        # Company boards stop early once a page returns no more of the employer's jobs.
        if company:
            from .adzuna import _company_match

            raws: list[RawJob] = []
            for idx in range(page_count):
                await _fetch_page(idx)
                kept = [
                    j
                    for j in results[idx]
                    if _company_match(company, (j.get("company") or {}).get("name") or "")
                ]
                if not results[idx]:
                    break  # past the last page
                raws.extend(self._to_raw(j) for j in kept)
                if query.limit is not None and len(raws) >= query.limit:
                    break
        else:
            await _drain()
            raws = [self._to_raw(job) for page_items in results for job in page_items]
        if query.limit is not None:
            raws = raws[: query.limit]
        return raws

    def _to_raw(self, job: dict[str, Any]) -> RawJob:
        company = job.get("company") or {}
        refs = job.get("refs") or {}
        return RawJob(
            source=self.name,
            source_job_id=str(job.get("id", "")),
            company=(company.get("name") if isinstance(company, dict) else None) or "",
            token=None,
            url=refs.get("landing_page") if isinstance(refs, dict) else None,
            payload=job,
        )

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via its public landing page (Tier-3 recovery).

        ``ref.apply_url``/``ref.listing_url`` are both the SAME public
        ``https://www.themuse.com/jobs/{company}/{slug}`` page (the index stores
        ``listing_url = apply_url`` for every source -- see ``index/mapping.py``). Fetching it
        directly recovers the full JD text from the server-rendered ``<main>`` region; verified
        live across all 3 probe companies (CRH/IBM/Navan) that a real posting's page renders the
        exact same body text carried in the JSON API's ``contents`` field (e.g. CRH job 21897374's
        page contains the same "Handle assignments in a repetitive..." text as its
        ``/api/public/jobs/21897374`` JSON).

        CONTRACT NOTE -- deliberately never returns ``None`` (this source's confirmed-gone branch is
        UNUSED, unlike every other hardened provider). Live verification found this page's own 404
        is NOT a reliable gone-signal: sampling 6 postings per probe company, CRH and Navan's
        landing-page status matched The Muse's own authoritative per-job API
        (``GET https://www.themuse.com/api/public/jobs/{id}``, confirmed to 404 only for a genuinely
        fabricated id) 6/6 times, but ALL 6 sampled IBM postings 404 on this landing page while the
        SAME ids are still HTTP 200 with full content on that authoritative API -- i.e. a
        systematically broken/stale frontend route for at least one real board, unrelated to
        whether the posting is actually alive. The authoritative API is exactly what the brief's
        recon pointed at, but its ``{id}`` path segment is a numeric job id that is NOT recoverable
        from ``apply_url``/``listing_url``: the landing-page URL's trailing slug is an unrelated
        short code (confirmed against 3 real ids -- neither hex nor any transform of the numeric
        id), and ``DetailRef`` carries no other field that could yield it. Since we can't reach the
        authoritative endpoint without that id, and this page's 404 is proven unreliable for at
        least one real board, a 404 here RAISES (indeterminate) instead of returning ``None`` --
        returning ``None`` on this unverified signal risks mass-false-expiring boards like IBM's.
        This provider therefore still recovers JD text for Tier-3 detail drain (the ALIVE path is
        solid), but should NOT be added to freshness's ``_BULK_RELIST_CONFIRM_SOURCES`` unless/until
        a per-posting numeric id becomes derivable from ``DetailRef``."""
        url = ref.apply_url or ref.listing_url
        if not url:
            raise RuntimeError(f"themuse detail: no derivable detail URL for {ref!s}")
        try:
            html = await fetcher.get_text(url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                # See docstring: NOT trusted as a confirmed-gone signal for this source -- a
                # verified false-404 exists (IBM, 6/6 sampled), so this stays indeterminate.
                raise RuntimeError(
                    f"themuse detail: landing-page {e.response.status_code} is not a verified "
                    f"gone-signal for {ref!s} (see fetch_detail docstring) -- treating as "
                    "indeterminate, not confirmed-gone"
                ) from e
            raise
        if not isinstance(html, str) or not html.strip():
            raise RuntimeError(f"themuse detail: empty page body for {ref!s}")
        tree = HTMLParser(html)
        main = tree.css_first("main")
        text = main.text(separator=" ", strip=True) if main is not None else None
        if not text:
            body = tree.body
            text = body.text(separator=" ", strip=True) if body is not None else None
        if not text or len(text) < self._DETAIL_MIN_LEN:
            raise RuntimeError(f"themuse detail: no extractable JD text for {ref!s}")
        return text

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        refs = p.get("refs") or {}
        apply_url = refs.get("landing_page") if isinstance(refs, dict) else None
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("name") or "").strip(),
            description_html=p.get("contents"),
            locations=self._locations(p),
            remote=self._remote(p),
            employment_type=self._employment_type(p.get("type")),
            department=self._department(p),
            level=self._level(p),
            apply_url=apply_url,
            posted_at=_parse_dt(p.get("publication_date")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _location_names(p: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for loc in p.get("locations") or []:
            if isinstance(loc, dict):
                name = loc.get("name")
                if name:
                    out.append(name)
        return out

    @classmethod
    def _locations(cls, p: dict[str, Any]) -> list[Location]:
        locs: list[Location] = []
        for name in cls._location_names(p):
            is_remote = any(h in name.lower() for h in _REMOTE_HINTS)
            locs.append(Location(raw=name, is_remote=is_remote))
        return locs

    @classmethod
    def _remote(cls, p: dict[str, Any]) -> RemoteType:
        for name in cls._location_names(p):
            if any(h in name.lower() for h in _REMOTE_HINTS):
                return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    @staticmethod
    def _employment_type(value: str | None) -> EmploymentType:
        if not value:
            return EmploymentType.UNKNOWN
        return _EMPLOYMENT_MAP.get(value.strip().lower(), EmploymentType.UNKNOWN)

    @staticmethod
    def _department(p: dict[str, Any]) -> str | None:
        cats = p.get("categories") or []
        if cats and isinstance(cats[0], dict):
            return cats[0].get("name")
        return None

    @staticmethod
    def _level(p: dict[str, Any]) -> JobLevel:
        levels = p.get("levels") or []
        if levels and isinstance(levels[0], dict):
            return level_from_ats_vocab(levels[0].get("name"))
        return JobLevel.UNKNOWN
