"""Workday provider — the hard one.

Workday is **per-tenant**, not a single global API. A board is identified by THREE parts:

1. ``tenant``      — the customer slug, e.g. ``nvidia`` / ``salesforce``
2. ``wd{N}``       — the data-center number, ``wd1`` .. ``wd12``, e.g. ``wd5``
3. ``site``        — the career-site path segment, e.g. ``NVIDIAExternalCareerSite``

Because a single string token must round-trip through the registry/resolver, we encode the
composite as a pipe-delimited token::

    "{tenant}|{wd}|{site}"          e.g.  "nvidia|wd5|NVIDIAExternalCareerSite"

``fetch`` parses the token back into its three parts. ``matches`` does the reverse: given a
careers URL or host it reconstructs the composite token (or returns ``None``).

Endpoints
---------
Jobs list (POST, no auth)::

    https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

with JSON body ``{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}`` →
``{"total": <int>, "jobPostings": [{"title", "externalPath", "locationsText",
"postedOn", "bulletFields"}, ...]}``.

* ``limit`` maxes out at 20 per page; paginate by ``offset`` (0, 20, 40, ...) up to ``total``,
  hard-capped at 10000 results.
* ``query.keywords`` is passed straight into ``searchText`` for server-side search.

Concurrency
-----------
The first page is fetched to learn ``total``; **all** remaining pages are then fetched
concurrently via ``anyio.create_task_group`` (one task per offset). We never page serially.
The ``AsyncFetcher`` already bounds global concurrency and per-host rate, so we simply launch
the tasks and collect each page into a dict keyed by its offset.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio

from ..models import JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["WorkdayProvider"]

# Workday host shape: {tenant}.wd{N}.myworkdayjobs.com
_HOST_RE = re.compile(r"^(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
# Locale path segments such as "en-US" / "en" that some careers URLs carry.
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)
# "Posted 3 Days Ago" / "Posted 30+ Days Ago"
_DAYS_AGO_RE = re.compile(r"(\d+)\s*\+?\s*days?\s+ago", re.IGNORECASE)


@register("workday")
class WorkdayProvider(BaseProvider):
    """Provider for Workday-hosted career sites (``*.myworkdayjobs.com``)."""

    name = "workday"

    PAGE_SIZE = 20  # Workday rejects limit > 20
    MAX_RESULTS = 10000  # hard pagination cap

    # --- token <-> composite ------------------------------------------------

    @staticmethod
    def make_token(tenant: str, wd: str, site: str) -> str:
        """Build the composite token from the three parts."""
        return f"{tenant}|{wd}|{site}"

    @staticmethod
    def _parse_token(token: str) -> tuple[str, str, str]:
        parts = token.split("|")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ValueError(
                f"invalid workday token {token!r}; expected '{{tenant}}|{{wd}}|{{site}}'"
            )
        tenant, wd, site = (p.strip() for p in parts)
        return tenant, wd, site

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Return the composite token for a Workday careers URL/host, else ``None``.

        Handles both the public careers URL (``/{tenant}/{site}/...`` or
        ``/en-US/{site}/...``) and the internal cxs API URL
        (``/wday/cxs/{tenant}/{site}/jobs``). Robust to the tenant appearing twice and to a
        varying number of leading segments (locale, etc.).
        """
        candidate = url_or_host.strip()
        if "://" not in candidate:
            candidate = "https://" + candidate
        parts = urlsplit(candidate)
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        m = _HOST_RE.match(host)
        if not m:
            return None
        tenant = m.group("tenant")
        wd = m.group("wd").lower()
        segments = [seg for seg in parts.path.split("/") if seg]
        site = cls._site_from_segments(segments, tenant)
        if not site:
            return None
        return cls.make_token(tenant, wd, site)

    @staticmethod
    def _site_from_segments(segments: list[str], tenant: str) -> str | None:
        """Pick the site segment, skipping framing/locale/tenant noise."""
        skip = {"wday", "cxs", "jobs", "job", tenant.lower()}
        for seg in segments:
            low = seg.lower()
            if low in skip or _LOCALE_RE.match(low):
                continue
            return seg
        return None

    # --- fetch --------------------------------------------------------------

    @classmethod
    def _jobs_url(cls, tenant: str, wd: str, site: str) -> str:
        return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    @classmethod
    def _body(cls, offset: int, search_text: str) -> dict[str, Any]:
        return {
            "appliedFacets": {},
            "limit": cls.PAGE_SIZE,
            "offset": offset,
            "searchText": search_text,
        }

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        tenant, wd, site = self._parse_token(token)
        url = self._jobs_url(tenant, wd, site)
        search_text = (query.keywords or "").strip()

        # Page 0 (sequential) tells us how many results exist.
        first = await fetcher.post_json(url, json=self._body(0, search_text))
        total = min(int(first.get("total") or 0), self.MAX_RESULTS)

        pages: dict[int, list[dict[str, Any]]] = {0: list(first.get("jobPostings") or [])}
        remaining = range(self.PAGE_SIZE, total, self.PAGE_SIZE)

        # All remaining pages CONCURRENTLY — one task per offset, collected by offset key.
        if remaining:
            async with anyio.create_task_group() as tg:
                for offset in remaining:
                    tg.start_soon(self._fetch_page, fetcher, url, offset, search_text, pages)

        raw_jobs: list[RawJob] = []
        for offset in sorted(pages):
            for posting in pages[offset]:
                raw_jobs.append(self._to_raw(posting, tenant, wd, site, token))
        return raw_jobs

    async def _fetch_page(
        self,
        fetcher: AsyncFetcher,
        url: str,
        offset: int,
        search_text: str,
        sink: dict[int, list[dict[str, Any]]],
    ) -> None:
        data = await fetcher.post_json(url, json=self._body(offset, search_text))
        # Each task writes a distinct offset key — no shared-slot races.
        sink[offset] = list(data.get("jobPostings") or [])

    def _to_raw(
        self, posting: dict[str, Any], tenant: str, wd: str, site: str, token: str
    ) -> RawJob:
        external_path = str(posting.get("externalPath") or "")
        return RawJob(
            source=self.name,
            source_job_id=self._source_job_id(external_path, posting),
            company=tenant,  # display name is resolved later via the registry
            token=token,
            url=f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{external_path}",
            payload=posting,
        )

    @staticmethod
    def _source_job_id(external_path: str, posting: dict[str, Any]) -> str:
        """Stable id derived from ``externalPath`` (unique per board)."""
        if external_path:
            return external_path
        bullets = posting.get("bulletFields") or []
        if bullets:
            return str(bullets[0])
        return str(posting.get("title") or "")

    # --- normalize ----------------------------------------------------------

    def normalize(self, raw: RawJob) -> JobPosting:
        payload = raw.payload
        title = str(payload.get("title") or "")
        locations_text = str(payload.get("locationsText") or "")
        locations = [Location(raw=locations_text)] if locations_text else []
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            apply_url=raw.url,
            locations=locations,
            remote=self._remote(locations_text, title),
            posted_at=self._parse_posted(payload.get("postedOn")),
            raw=payload,
        )

    @staticmethod
    def _remote(locations_text: str, title: str) -> RemoteType:
        if "remote" in f"{locations_text} {title}".lower():
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    @staticmethod
    def _parse_posted(posted_on: Any) -> datetime | None:
        """Best-effort parse of Workday's relative ``postedOn`` string.

        Workday only exposes relative strings like ``"Posted Today"`` /
        ``"Posted 3 Days Ago"`` / ``"Posted 30+ Days Ago"``. We map those to an approximate
        UTC timestamp; anything we cannot understand returns ``None`` (never invented).
        """
        if not isinstance(posted_on, str):
            return None
        text = posted_on.strip().lower()
        now = datetime.now(timezone.utc)
        if "today" in text:
            days = 0
        elif "yesterday" in text:
            days = 1
        else:
            m = _DAYS_AGO_RE.search(text)
            if not m:
                return None
            days = int(m.group(1))
        return now - timedelta(days=days)
