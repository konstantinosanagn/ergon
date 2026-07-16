"""Eightfold AI (Talent Intelligence) job-board provider.

Eightfold-hosted career sites expose a public JSON search API::

    GET https://{tenant}.eightfold.ai/api/apply/v2/jobs
        ?domain={domain}&start={offset}&num={N}&sort_by=relevance
    -> {"positions": [...], "count": <int>, "domain": "...", ...}

Each ``positions`` entry is a summary record, e.g.::

    {
      "id": 42478672,
      "name": "Manager Engineering - Electrical",
      "location": "New Orleans, LA USA 70112",
      "locations": ["New Orleans, LA USA 70112"],
      "department": "Engineering Services",
      "t_create": 1781660892,            # epoch seconds
      "display_job_id": "144860",
      "work_location_option": "onsite",  # onsite | remote | hybrid
      "canonicalPositionUrl": "https://talent.fmjobs.com/careers/job/42478672",
      ...
    }

The ``domain`` wrinkle (important)
----------------------------------
The ``domain`` query param is REQUIRED and tenant-specific. Sending the wrong
domain (or none on a locked tenant) yields ``{"message": "Not authorized for
PCSX"}`` (HTTP 200 or 403). We discover the domain robustly:

1. ``GET .../api/apply/v2/jobs`` with NO params. OPEN tenants (e.g. ``fcx``,
   ``netflix``) return a config dict that includes a ``"domain"`` field -> use it
   and page the ``apply/v2`` API.
2. If step 1 fails (locked-tenant error / 403 / no ``"domain"``), the tenant is
   on Eightfold's newer **PCSX** Career Hub, whose ``apply/v2`` API is disabled.
   Fall back to the PCSX search API (below) with ``domain={tenant}.com``.

The PCSX fallback (unlocks the "locked" tenants)
------------------------------------------------
Newer Eightfold deployments (e.g. ``starbucks`` = 21k jobs, ``ericsson``,
``lamresearch``) serve jobs from a different namespace::

    GET https://{tenant}.eightfold.ai/api/pcsx/search
        ?domain={domain}&query=&location=&start={offset}&num={N}&sort_by=relevance
    -> {"status": 200, "data": {"positions": [...], "count": <int>, ...}}

Each PCSX ``positions`` entry is camelCase (``displayJobId``, ``postedTs``,
``workLocationOption``, ``positionUrl`` [relative], ``locations`` [list]). We map
those onto the same snake_case keys the apply/v2 path uses, so a single
``normalize`` handles both. Tenants where PCSX is *also* disabled (e.g. ``ey`` ->
``"PCSX is not enabled for this user."``) are genuinely closed and yield ``[]``.

The summary record has no salary and (in the list view) an empty description, so
``salary``/``description`` are ``None`` here — never invented. We never raise;
locked/empty tenants degrade gracefully to an empty list.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..models import (
    DetailFetch,
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    SearchQuery,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["EightfoldProvider"]

_API = "https://{tenant}.eightfold.ai/api/apply/v2/jobs"
_PCSX_API = "https://{tenant}.eightfold.ai/api/pcsx/search"
_JOB_URL = "https://{tenant}.eightfold.ai/careers/job/{id}"
_HOST = "https://{tenant}.eightfold.ai"
# Per-posting detail resource (Tier-3 JD recovery): a single-job GET, distinct from the
# paginated listing above. No auth required (recon-verified 7/7 tenants).
_DETAIL_API = "https://{tenant}.eightfold.ai/api/apply/v2/jobs/{id}"

# Capture the tenant slug from ``{tenant}.eightfold.ai`` (exclude www/app fronts).
_HOST_RE = re.compile(r"(?:https?://)?([a-z0-9][a-z0-9-]*)\.eightfold\.ai", re.IGNORECASE)
_EXCLUDED_SUBDOMAINS = {"www", "app"}

# ``work_location_option`` -> our enum (deterministic onsite/remote/hybrid signal).
_WORK_OPTION = {
    "onsite": RemoteType.ONSITE,
    "on_site": RemoteType.ONSITE,
    "on-site": RemoteType.ONSITE,
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
}

# Best-effort ``type`` -> employment enum. In practice ``type`` is the source
# marker ("ATS") rather than an employment kind, so this almost always misses
# and we fall back to UNKNOWN — never inventing a value.
_EMPLOYMENT = {
    "full_time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}


def _is_locked(data: Any) -> bool:
    """True when the API returned a locked-tenant error (``{"message": ...}``)."""
    return isinstance(data, dict) and "message" in data and "domain" not in data


def _parse_epoch(value: Any) -> datetime | None:
    """Parse a ``t_create`` epoch-seconds value (int or numeric str), else None."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


@register("eightfold")
class EightfoldProvider(BaseProvider):
    name = "eightfold"

    PER_PAGE = 20  # positions requested per ``num`` page (apply/v2 honors it; PCSX caps at ~10)
    MAX_RESULTS = 10000  # absolute per-board job ceiling; we advance ``start`` by the ACTUAL
    # batch length (PCSX returns ~10/page regardless of ``num``), so a fixed-step loop both
    # SKIPPED jobs and truncated big boards (Citi showed 500 of its real ~3800).

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        match = _HOST_RE.search(url_or_host)
        if not match:
            return None
        tenant = match.group(1).strip().lower()
        if not tenant or tenant in _EXCLUDED_SUBDOMAINS:
            return None
        return tenant

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        base = _API.format(tenant=token)
        domain, apply_open = await self._discover_domain(base, token, fetcher)

        if apply_open:
            positions = await self._fetch_apply_v2(base, domain, query, fetcher)
        else:
            # apply/v2 is disabled for this tenant -> it's on the PCSX Career Hub.
            positions = await self._fetch_pcsx(token, domain, query, fetcher)
        return [self._to_raw(p, token) for p in positions]

    async def _discover_domain(
        self, base: str, token: str, fetcher: AsyncFetcher
    ) -> tuple[str, bool]:
        """Return ``(domain, apply_v2_open)`` (see module docstring).

        ``apply_v2_open`` is True only when the no-param config probe returns a
        ``"domain"`` — i.e. the legacy apply/v2 API is live. Otherwise the tenant
        is PCSX-only (or locked) and we fall back to ``{tenant}.com``.
        """
        try:
            data = await fetcher.get_json(base)
        except Exception:
            data = None
        if isinstance(data, dict):
            dom = data.get("domain")
            if isinstance(dom, str) and dom:
                return dom, True
        return f"{token}.com", False

    async def _fetch_apply_v2(
        self, base: str, domain: str, query: SearchQuery, fetcher: AsyncFetcher
    ) -> list[dict[str, Any]]:
        """Page the legacy ``/api/apply/v2/jobs`` API (open tenants)."""
        limit = query.limit
        positions: list[dict[str, Any]] = []
        start = 0
        while len(positions) < self.MAX_RESULTS:
            params = {
                "domain": domain,
                "start": start,
                "num": self.PER_PAGE,
                "sort_by": "relevance",
            }
            try:
                data = await fetcher.get_json(base, params=params)
            except Exception:
                # Network/JSON failure on a page — stop gracefully with what we have.
                break

            if not isinstance(data, dict) or _is_locked(data):
                break  # locked tenant / unexpected shape: degrade to []

            batch = [p for p in (data.get("positions") or []) if isinstance(p, dict)]
            if not batch:
                break
            positions.extend(batch)
            start += len(batch)  # advance by the ACTUAL count returned (never skip/overstep)

            if limit is not None and len(positions) >= limit:
                return positions[:limit]

            count = data.get("count")
            if isinstance(count, int) and start >= count:
                break
        return positions[: self.MAX_RESULTS]

    async def _fetch_pcsx(
        self, token: str, domain: str, query: SearchQuery, fetcher: AsyncFetcher
    ) -> list[dict[str, Any]]:
        """Page the newer ``/api/pcsx/search`` API and canonicalise each record."""
        url = _PCSX_API.format(tenant=token)
        limit = query.limit
        positions: list[dict[str, Any]] = []
        start = 0
        while len(positions) < self.MAX_RESULTS:
            params = {
                "domain": domain,
                "query": "",
                "location": "",
                "start": start,
                "num": self.PER_PAGE,
                "sort_by": "relevance",
            }
            try:
                data = await fetcher.get_json(url, params=params)
            except Exception:
                break  # 403 / network failure (PCSX also disabled) -> degrade to []

            if not isinstance(data, dict) or data.get("status") != 200:
                break
            block = data.get("data")
            if not isinstance(block, dict):
                break
            batch = [p for p in (block.get("positions") or []) if isinstance(p, dict)]
            if not batch:
                break
            positions.extend(self._pcsx_canonical(p, token) for p in batch)
            start += len(batch)  # PCSX returns ~10/page regardless of num — step by what we got

            if limit is not None and len(positions) >= limit:
                return positions[:limit]

            count = block.get("count")
            if isinstance(count, int) and start >= count:
                break
        return positions[: self.MAX_RESULTS]

    @staticmethod
    def _pcsx_canonical(p: dict[str, Any], token: str) -> dict[str, Any]:
        """Alias PCSX camelCase fields onto the snake_case keys ``normalize`` reads.

        Mutates and returns ``p`` (the original camelCase fields are kept for the
        raw payload). ``positionUrl`` is relative, so it's made absolute.
        """
        rel = p.get("positionUrl") or ""
        if isinstance(rel, str) and rel.startswith("/"):
            p["canonicalPositionUrl"] = _HOST.format(tenant=token) + rel
        elif rel:
            p["canonicalPositionUrl"] = rel
        p.setdefault("display_job_id", p.get("displayJobId"))
        p.setdefault("t_create", p.get("postedTs"))
        p.setdefault("work_location_option", p.get("workLocationOption"))
        return p

    def _to_raw(self, position: dict[str, Any], token: str) -> RawJob:
        sid = str(position.get("id") or position.get("display_job_id") or "")
        url = position.get("canonicalPositionUrl") or (
            _JOB_URL.format(tenant=token, id=sid) if sid else None
        )
        return RawJob(
            source=self.name,
            source_job_id=sid,
            company=token,
            token=token,
            url=url,
            payload=position,
        )

    # --- detail (Tier-3 JD recovery) -----------------------------------------

    @staticmethod
    def _job_id_from_url(url: str | None) -> str | None:
        """Trailing path segment of a posting URL (``.../careers/job/{id}`` -> ``{id}``)."""
        if not url:
            return None
        try:
            segments = [seg for seg in urlsplit(url).path.split("/") if seg]
        except Exception:
            return None
        return segments[-1] if segments else None

    @staticmethod
    def _looks_like_tenant_slug(token: str | None) -> bool:
        """A bare tenant slug has no scheme/dots — a custom domain or full URL doesn't qualify."""
        if not token:
            return False
        t = token.strip()
        return bool(t) and "." not in t and "://" not in t

    @classmethod
    def _tenant_for_detail(cls, ref: DetailRef) -> str | None:
        """Derive the eightfold subdomain for the detail call (see module + method docstring
        asymmetry note in :meth:`fetch_detail`)."""
        tenant = cls.matches(ref.apply_url) if ref.apply_url else None
        if tenant:
            return tenant
        token = ref.token
        if token and cls._looks_like_tenant_slug(token):
            return token.strip().lower()
        return None

    @classmethod
    def _detail_url(cls, src: str | None, ref: DetailRef, job_id: str) -> str | None:
        """Build the ``/api/apply/v2/jobs/{id}`` detail URL for ``job_id``.

        White-label eightfold tenants (hsbc/bayer/netflix/...) serve the IDENTICAL apply/v2 detail
        resource on their OWN careers host, from which no ``{tenant}.eightfold.ai`` subdomain is
        derivable -- so the old "no tenant -> skip" path silently dropped ~94% of failed eightfold
        rows even though a 200 with the full ``job_description`` sat on the ref's own host.

        Precedence is tenant-FIRST so rows that already resolved stay byte-identical: when a tenant is
        derivable (``{tenant}.eightfold.ai`` host, or a bare tenant-slug ``ref.token``) use the
        canonical backend. ONLY when no tenant is derivable (the white-label custom-domain-no-token
        case = the ~94%) fall back to the ref's OWN host. That host is trusted (this row's ``source``
        is already eightfold, so our crawler stored it), and ``src`` is the exact URL ``job_id`` was
        parsed from, so host and id can never drift."""
        tenant = cls._tenant_for_detail(ref)
        if tenant is not None:
            return _DETAIL_API.format(tenant=tenant, id=job_id)
        parts = urlsplit(src) if src else None
        if parts and parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/api/apply/v2/jobs/{job_id}"
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD via the per-tenant detail resource (Tier-3 recovery).

        ``{id}`` is the trailing path segment of ``ref.apply_url`` (shape
        ``https://{host}/careers/job/{id}``), falling back to ``ref.listing_url``. The apply/v2
        detail URL is then built from that SAME URL's host (see :meth:`_detail_url`): white-label
        tenants (hsbc/bayer/netflix/...) serve the identical resource on their own careers host, so
        fetching from the ref's host recovers them -- previously they were skipped as "no tenant
        derivable" (which silently dropped ~94% of failed eightfold rows). The
        ``{tenant}.eightfold.ai`` form is the fallback for refs that carry only a bare tenant slug.

        Non-raising: any unparseable URL, fetch failure, non-JSON/non-dict payload, or a
        missing/empty/non-string ``job_description`` returns ``None``, never an exception.
        """
        src: str | None = None
        job_id: str | None = None
        for candidate in (ref.apply_url, ref.listing_url):
            jid = self._job_id_from_url(candidate)
            if jid:
                src, job_id = candidate, jid
                break
        if job_id is None:
            return None
        url = self._detail_url(src, ref, job_id)
        if url is None:
            return None
        try:
            data = await fetcher.get_json(url)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        job_description = data.get("job_description")
        if not isinstance(job_description, str) or not job_description.strip():
            return None
        # The detail response also carries a location STRING ("Phoenix, AZ USA 85040" / "USA -
        # Remote"); build a raw Location and let enrich's geo derive the country (fills the index
        # row's NULL country). Format is inconsistent so no safe structured city/region split.
        loc = data.get("location")
        if isinstance(loc, str) and loc.strip():
            return DetailFetch(text=job_description, locations=[Location(raw=loc.strip())])
        return job_description

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        locations = self._locations(p)
        remote = self._remote(p, locations)
        department = (p.get("department") or "").strip() or None
        emp = _EMPLOYMENT.get(str(p.get("type") or "").strip().lower(), EmploymentType.UNKNOWN)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("name") or p.get("posting_name") or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=emp,
            department=department,
            salary=None,  # not exposed in the list view
            posted_at=_parse_epoch(p.get("t_create")),
            updated_at=_parse_epoch(p.get("t_update")),
            description_html=None,
            description_text=None,  # list view's job_description is empty
            raw=raw.payload,
        )

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        # PCSX-mode tenants expose ``standardizedLocations``: a list of comma-joined
        # "City, Region, CC" strings (e.g. "Azusa, CA, US") — structured geo we didn't
        # previously parse. Only some tenants carry it; when present it's strictly better
        # than the free-text ``locations``/``location`` fields, so it takes priority.
        # Anything malformed/absent falls straight through to the existing raw-string path
        # so tenants without it are never regressed.
        std = p.get("standardizedLocations")
        if isinstance(std, list):
            std_labels = [str(loc).strip() for loc in std if str(loc).strip()]
            if std_labels:
                return [EightfoldProvider._structured_location(label) for label in std_labels]

        labels = [str(loc).strip() for loc in (p.get("locations") or []) if str(loc).strip()]
        if not labels:
            single = str(p.get("location") or "").strip()
            labels = [single] if single else []
        out: list[Location] = []
        for label in labels:
            out.append(Location(raw=label, is_remote="remote" in label.lower()))
        return out

    @staticmethod
    def _structured_location(label: str) -> Location:
        """Parse one ``standardizedLocations`` entry ("City, Region, CC") into a Location.

        Guarded against every arity we've observed/can anticipate:
        - 3+ comma-separated parts -> city, region, country (extra middle segments ignored;
          the last segment is always the country per the observed shape).
        - 2 parts -> city, country (no region token, e.g. "Berlin, Germany").
        - 1 part (or unparseable) -> ambiguous (could be "Remote", a bare country, etc.) —
          never guess; keep only ``raw``/``is_remote`` so we don't mislabel a field.
        """
        parts = [seg.strip() for seg in label.split(",") if seg.strip()]
        city = region = country = None
        if len(parts) >= 3:
            city, region, country = parts[0], parts[1], parts[-1]
        elif len(parts) == 2:
            city, country = parts[0], parts[1]
        return Location(
            city=city,
            region=region,
            country=country,
            raw=label,
            is_remote="remote" in label.lower(),
        )

    @staticmethod
    def _remote(p: dict[str, Any], locations: list[Location]) -> RemoteType:
        option = str(p.get("work_location_option") or "").strip().lower()
        if option in _WORK_OPTION:
            return _WORK_OPTION[option]
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN
