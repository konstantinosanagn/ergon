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
PCSX"}`` with HTTP 200. We discover the domain robustly:

1. ``GET .../api/apply/v2/jobs`` with NO params. OPEN tenants (e.g. ``fcx``)
   return a config dict that includes a ``"domain"`` field -> use it.
2. If step 1 is a locked-tenant error (a ``{"message": ...}`` dict with no
   ``"domain"``), fall back to ``domain={tenant}.com``.
3. If pagination then still returns a ``{"message": ...}`` error / no positions,
   we stop and return ``[]`` — some tenants are genuinely locked. We never raise;
   locked/empty tenants degrade gracefully to an empty list.

The summary record has no salary and (in the list view) an empty description, so
``salary``/``description`` are ``None`` here — never invented.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..models import (
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

__all__ = ["EightfoldProvider"]

_API = "https://{tenant}.eightfold.ai/api/apply/v2/jobs"
_JOB_URL = "https://{tenant}.eightfold.ai/careers/job/{id}"

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

    PER_PAGE = 20  # positions requested per ``num`` page
    MAX_PAGES = 50  # per-board page cap (=1000 jobs) to bound pagination cost

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
        domain = await self._discover_domain(base, token, fetcher)

        limit = query.limit
        positions: list[dict[str, Any]] = []
        for page in range(self.MAX_PAGES):
            start = page * self.PER_PAGE
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

            batch = data.get("positions") or []
            if not batch:
                break
            positions.extend(p for p in batch if isinstance(p, dict))

            if limit is not None and len(positions) >= limit:
                positions = positions[:limit]
                break

            count = data.get("count")
            if isinstance(count, int) and start + len(batch) >= count:
                break

        return [self._to_raw(p, token) for p in positions]

    async def _discover_domain(self, base: str, token: str, fetcher: AsyncFetcher) -> str:
        """Discover the tenant-specific ``domain`` param (see module docstring)."""
        try:
            data = await fetcher.get_json(base)
        except Exception:
            data = None
        if isinstance(data, dict):
            dom = data.get("domain")
            if isinstance(dom, str) and dom:
                return dom
        # Locked tenant (or odd response): best-effort fallback.
        return f"{token}.com"

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
        labels = [str(loc).strip() for loc in (p.get("locations") or []) if str(loc).strip()]
        if not labels:
            single = str(p.get("location") or "").strip()
            labels = [single] if single else []
        out: list[Location] = []
        for label in labels:
            out.append(Location(raw=label, is_remote="remote" in label.lower()))
        return out

    @staticmethod
    def _remote(p: dict[str, Any], locations: list[Location]) -> RemoteType:
        option = str(p.get("work_location_option") or "").strip().lower()
        if option in _WORK_OPTION:
            return _WORK_OPTION[option]
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN
