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

* ``limit`` maxes out at 20 per page; paginate by ``offset`` (0, 20, 40, ...).
* Pagination is bounded by ``MAX_PAGES`` (per-board ceiling) and by ``query.limit`` when set,
  so we never pull thousands of postings to satisfy a small search.
* ``query.keywords`` is passed straight into ``searchText`` for server-side search, which
  shrinks ``total`` at the source before we paginate.

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
    MAX_RESULTS = 30000  # absolute hard pagination cap (per board, and per facet bucket). Raised
    # from 10k so mega-tenants (CVS Health ~16k, large banks/retailers) aren't truncated. Each
    # Workday tenant is a SEPARATE host with its own per-host rate bucket, so pulling a big tenant
    # fully never throttles other providers/tenants.
    MAX_PAGES = 1500  # per-board page cap — kept consistent with MAX_RESULTS (1500*20=30000) so the
    # flat path isn't silently truncated below MAX_RESULTS. Pages fetch CONCURRENTLY (bounded by
    # the fetcher). A ~30k-posting tenant = 1500 paged requests at the per-tenant rate (~minutes).

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

    CAP = 2000  # many Workday instances cap the flat result set (offset can't exceed this)

    @classmethod
    def _body(
        cls, offset: int, search_text: str, applied: dict[str, list[str]] | None = None
    ) -> dict[str, Any]:
        return {
            "appliedFacets": applied or {},
            "limit": cls.PAGE_SIZE,
            "offset": offset,
            "searchText": search_text,
        }

    @classmethod
    def _best_partition_facet(
        cls, facets: list[dict[str, Any]], total: int
    ) -> tuple[str, list[str]] | None:
        """When a board is capped, a facet whose value-counts SUM beyond the flat ``total`` reveals
        the true size. Return ``(facetParameter, [valueIds])`` for the facet that best partitions
        the board. Two requirements compete: COVERAGE (the facet's counts should sum to the true
        size — a facet many jobs lack, e.g. jobFamily, under-counts) and BUCKET SIZE (each bucket
        must be < CAP to be fully fetchable). We pick the highest-coverage facet whose largest
        bucket is < CAP; if none qualifies, fall back to the highest-coverage facet overall (its
        over-CAP buckets stay partially truncated — better than the flat 2000)."""
        scored: list[tuple[int, int, str, list[str]]] = []  # (coverage, max_bucket, param, ids)
        for fc in facets or []:
            param = fc.get("facetParameter")
            values = [v for v in (fc.get("values") or []) if v.get("id") and v.get("count")]
            if not param or not values:
                continue
            coverage = sum(int(v["count"]) for v in values)
            if coverage <= total:
                continue  # this facet doesn't reveal more than the flat total
            max_bucket = max(int(v["count"]) for v in values)
            scored.append((coverage, max_bucket, str(param), [str(v["id"]) for v in values]))
        if not scored:
            return None
        # Prefer facets whose every bucket is fetchable (< CAP); among those, max coverage.
        fetchable = [s for s in scored if s[1] < cls.CAP]
        pool = fetchable or scored
        best = max(pool, key=lambda s: s[0])
        return best[2], best[3]

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        tenant, wd, site = self._parse_token(token)
        url = self._jobs_url(tenant, wd, site)
        search_text = (query.keywords or "").strip()

        # Page 0 (sequential) tells us how many results exist.
        first = await fetcher.post_json(url, json=self._body(0, search_text))
        flat_total = int(first.get("total") or 0)

        # Capped instances report a flat total clamped at CAP while their facet counts reveal the
        # true (larger) size. If we want more than the cap, partition by a facet and union buckets.
        want_all = query.limit is None or query.limit > flat_total
        if flat_total >= self.CAP and want_all:
            facet = self._best_partition_facet(first.get("facets") or [], flat_total)
            if facet is not None:
                return await self._fetch_faceted(
                    fetcher, url, tenant, wd, site, token, search_text, facet, query.limit
                )

        total = min(flat_total, self.MAX_RESULTS)

        # Bound how much we actually pull. Workday tenants can have thousands of postings;
        # fetching every page (even concurrently) is wasteful. We cap by:
        #   - MAX_PAGES (a hard per-board ceiling), and
        #   - query.limit when set (no point pulling 5000 to satisfy a limit of 20).
        want = min(total, self.MAX_PAGES * self.PAGE_SIZE)
        if query.limit is not None:
            want = min(want, max(query.limit, self.PAGE_SIZE))

        pages: dict[int, list[dict[str, Any]]] = {0: list(first.get("jobPostings") or [])}
        remaining = range(self.PAGE_SIZE, want, self.PAGE_SIZE)

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
        applied: dict[str, list[str]] | None = None,
    ) -> None:
        data = await fetcher.post_json(url, json=self._body(offset, search_text, applied))
        # Each task writes a distinct offset key — no shared-slot races.
        sink[offset] = list(data.get("jobPostings") or [])

    async def _fetch_faceted(
        self,
        fetcher: AsyncFetcher,
        url: str,
        tenant: str,
        wd: str,
        site: str,
        token: str,
        search_text: str,
        facet: tuple[str, list[str]],
        limit: int | None,
    ) -> list[RawJob]:
        """Recover a capped board by querying each value of a partitioning facet separately and
        unioning the postings (deduped by externalPath). Each bucket is itself flat-paginated up
        to CAP; buckets still >= CAP stay partially truncated (rare — a 2nd-level partition would
        be needed), which is logged implicitly by returning fewer than the facet sum."""
        param, value_ids = facet
        by_id: dict[str, RawJob] = {}

        async def fetch_bucket(value_id: str) -> None:
            applied = {param: [value_id]}
            head = await fetcher.post_json(url, json=self._body(0, search_text, applied))
            btotal = min(int(head.get("total") or 0), self.CAP, self.MAX_RESULTS)
            buckets: dict[int, list[dict[str, Any]]] = {0: list(head.get("jobPostings") or [])}
            rest = range(self.PAGE_SIZE, btotal, self.PAGE_SIZE)
            if rest:
                async with anyio.create_task_group() as tg:
                    for offset in rest:
                        tg.start_soon(
                            self._fetch_page, fetcher, url, offset, search_text, buckets, applied
                        )
            for offset in sorted(buckets):
                for posting in buckets[offset]:
                    raw = self._to_raw(posting, tenant, wd, site, token)
                    by_id.setdefault(raw.source_job_id, raw)

        # Buckets fetched concurrently (each internally pages concurrently too; all bounded by the
        # shared fetcher's concurrency limit).
        async with anyio.create_task_group() as tg:
            for vid in value_ids:
                tg.start_soon(fetch_bucket, vid)

        raws = list(by_id.values())
        return raws[:limit] if limit is not None else raws

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
