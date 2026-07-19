"""Phenom (Phenom People) career-site job-board provider.

Phenom powers large enterprise career sites (Activision, GE Healthcare, ...). The sites are
Vue SPAs that fetch jobs from a fully PUBLIC, unauthenticated POST endpoint on the tenant's
OWN host — no API key, cookie, CSRF token, or browser::

    POST https://{host}/widgets
    Content-Type: application/json
    {"ddoKey": "refineSearch", "jobs": true, "from": {offset}, "size": 100}

This is Phenom's "DDO widget" API (``widgetApiEndpoint`` in the page's ``phApp`` config) — NOT
raw GraphQL (a raw ``query{...}`` body is rejected with ``{"status":"failure"}``). The job
search DDO key is ``refineSearch``; ``jobs:true`` is REQUIRED for the ``data.jobs`` array to be
populated, and pagination is ``from``/``size`` (``pageNumber``/``pageSize`` are ignored).

Response::

    {"refineSearch": {"status":200, "hits":53, "totalHits":53,
                      "data": {"jobs": [ {record}, ... ]}}}

``totalHits`` is the true count; we page ``from`` by ``size`` (100/req) until
``from >= totalHits``. Each record carries ``jobSeqNo`` (stable unique id),
``title, category, city/state/country, cityStateCountry, postedDate, type, checkRemote,
descriptionTeaser, applyUrl``. The canonical posting page is ``https://{host}/job/{jobSeqNo}``;
``applyUrl`` is the real (often external, e.g. Workday) apply destination.

Token shape: ``"{host}"`` (e.g. ``"careers.activisionblizzard.com"``). The ``/widgets`` API and
``/job/...`` detail pages all live on that host.

Never invented: ``checkRemote`` and the ``type`` taxonomy are frequently null/tenant-specific —
only known values are mapped, everything else degrades to ``UNKNOWN``. ``descriptionTeaser`` is
a plain-text summary (the full description lives only on the detail page, not fetched in bulk).
"""

from __future__ import annotations

import re
from dataclasses import replace
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
from .successfactors import SuccessFactorsProvider
from .workday import WorkdayProvider

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["PhenomProvider"]

_API = "https://{host}/widgets"
_VIEW = "https://{host}/job/{seq}"

# Phenom CDN/track hosts (asset/API signatures) and career-site path shapes.
_PHENOM_HOST_RE = re.compile(r"\.phenompeople\.com$", re.IGNORECASE)
# Career-site URL paths that signal Phenom even on a vanity domain.
_PHENOM_PATH_RE = re.compile(r"/(?:search-results|job/[A-Z0-9]{6,})", re.IGNORECASE)

# checkRemote -> our enum (deterministic).
_REMOTE = {
    "ON-SITE": RemoteType.ONSITE,
    "ONSITE": RemoteType.ONSITE,
    "REMOTE": RemoteType.REMOTE,
    "HYBRID": RemoteType.HYBRID,
}

# Substring markers in the tenant-specific ``type`` field -> our enum (best-effort; the field
# is a tenant taxonomy, e.g. "Regular"/"Mid-Career"/"Co-op/Intern" — most values -> UNKNOWN).
_EMPLOYMENT_MARKERS = (
    ("intern", EmploymentType.INTERNSHIP),
    ("co-op", EmploymentType.INTERNSHIP),
    ("apprentice", EmploymentType.INTERNSHIP),
    ("part-time", EmploymentType.PART_TIME),
    ("part time", EmploymentType.PART_TIME),
    ("contract", EmploymentType.CONTRACT),
    ("fixed term", EmploymentType.TEMPORARY),
    ("temporary", EmploymentType.TEMPORARY),
    ("seasonal", EmploymentType.TEMPORARY),
    ("full-time", EmploymentType.FULL_TIME),
    ("full time", EmploymentType.FULL_TIME),
)


def _clean(value: Any) -> str | None:
    """Return a stripped non-empty string, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _parse_date(value: Any) -> datetime | None:
    """Parse Phenom's ``2026-03-13T00:00:00.000+0000`` (or ``YYYY-MM-DD``) to tz-aware dt."""
    text = _clean(value)
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    # Normalize a trailing numeric offset without a colon (+0000 -> +00:00).
    candidate = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", candidate)
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@register("phenom")
class PhenomProvider(BaseProvider):
    name = "phenom"

    PER_PAGE = 100  # /widgets honors size; 100 = one page per 100 jobs
    MAX_PAGES = 200  # bound full pulls (=20k jobs)

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a Phenom career host/URL -> ``"{host}"`` token, else None.

        Matches ``*.phenompeople.com`` hosts and career-site URLs whose path carries a Phenom
        shape (``/search-results`` or ``/job/{SEQNO}``). Bare vanity hosts without a Phenom
        path are rejected to avoid over-matching generic domains (tenants live on vanity
        domains, so host alone is not a reliable signal).
        """
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if not host:
            return None
        if _PHENOM_HOST_RE.search(host):
            return host
        if parts.path and _PHENOM_PATH_RE.search(parts.path):
            return host
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host = token.split("|", 1)[0].strip().lower()
        if not host:
            return []
        url = _API.format(host=host)
        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        total: int | None = None

        for page in range(self.MAX_PAGES):
            offset = page * self.PER_PAGE
            body = {
                "ddoKey": "refineSearch",
                "jobs": True,
                "from": offset,
                "size": self.PER_PAGE,
            }
            try:
                data = await fetcher.post_json(url, json=body)
            except Exception:
                break  # network/HTTP/non-JSON failure — stop gracefully

            block = data.get("refineSearch") if isinstance(data, dict) else None
            if not isinstance(block, dict):
                break
            if total is None and isinstance(block.get("totalHits"), int):
                total = block["totalHits"]
            inner = block.get("data")
            jobs = inner.get("jobs") if isinstance(inner, dict) else None
            if not isinstance(jobs, list) or not jobs:
                break

            for rec in jobs:
                if not isinstance(rec, dict):
                    continue
                jid = str(rec.get("jobSeqNo") or rec.get("reqId") or rec.get("jobId") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                raws.append(self._to_raw(rec, host, jid))
                if limit is not None and len(raws) >= limit:
                    return raws[:limit]

            if total is not None and offset + len(jobs) >= total:
                break
        return raws

    def _to_raw(self, rec: dict[str, Any], host: str, jid: str) -> RawJob:
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=self._host_company(host),
            token=host,
            url=_VIEW.format(host=host, seq=jid),
            payload=rec,
        )

    @staticmethod
    def _host_company(host: str) -> str:
        """Derive a company label from the host (strip ``careers``/``jobs`` prefixes)."""
        seg = host.split(".")[0]
        for prefix in ("careers-", "jobs-", "careers", "jobs"):
            if seg.startswith(prefix):
                trimmed = seg[len(prefix) :].lstrip("-")
                if trimmed:
                    return trimmed
        return seg

    # --- detail (Tier-3 JD recovery, re-route to the underlying ATS) --------

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Re-route to the ATS that actually hosts the JD (Tier-3 recovery).

        11,414 of 11,831 phenom rows are AGGREGATED listings whose ``apply_url`` points at
        Workday (11,083) or SuccessFactors (331) — ATSes we already have working
        ``fetch_detail`` for. Rather than build a phenom-native detail fetcher, dispatch by the
        ``apply_url`` (falling back to ``listing_url``) host to the matching provider.

        Contract (see ``providers/base.py``): a returned ``None`` means DEFINITIVELY gone (only the
        delegated Workday/SuccessFactors 404 produces one); an indeterminate case RAISES so the
        freshness/liveness confirm never expires a live posting on it. Genuine-phenom postings
        (canonical ``/job/{seq}``, fountain, driverapponline, ... — ~400 rows) have no re-route
        target, so we cannot confirm existence -> RAISE (keep), never ``None``. Delegated transient
        errors propagate (not swallowed), so a Workday/SF 5xx/timeout also keeps the row.
        """
        url = ref.apply_url or ref.listing_url
        if not url:
            raise RuntimeError(f"phenom detail: no apply/listing url for {ref!s}")
        candidate = url if "//" in url else "//" + url
        host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower()
        if not host:
            raise RuntimeError(f"phenom detail: unparseable host for {ref!s}")

        if host.endswith("myworkdayjobs.com"):
            # Phenom's Workday apply_urls carry a trailing "/apply" segment that direct-Workday
            # URLs never have and which breaks _cxs_detail_url (422) -- strip it before delegating.
            cleaned = url.rstrip("/")
            if cleaned.lower().endswith("/apply"):
                cleaned = cleaned[: -len("/apply")]
            cleaned = cleaned.rstrip("/")
            modified_ref = replace(ref, apply_url=cleaned, listing_url=cleaned)
            return await WorkdayProvider().fetch_detail(modified_ref, fetcher)

        if "successfactors.com" in host or "sapsf.com" in host:
            return await SuccessFactorsProvider().fetch_detail(ref, fetcher)

        raise RuntimeError(f"phenom detail: no re-route target (genuine-phenom) for {ref!s}")

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        city = _clean(p.get("city"))
        region = _clean(p.get("state"))
        country = _clean(p.get("country"))
        raw_loc = _clean(p.get("cityStateCountry")) or _clean(p.get("location"))
        locations: list[Location] = []
        if city or region or country or raw_loc:
            label = raw_loc or ", ".join(x for x in (city, region, country) if x)
            is_remote = "remote" in (label or "").lower()
            locations.append(
                Location(city=city, region=region, country=country, raw=label, is_remote=is_remote)
            )

        remote = _REMOTE.get((_clean(p.get("checkRemote")) or "").upper(), RemoteType.UNKNOWN)
        if remote is RemoteType.UNKNOWN and any(loc.is_remote for loc in locations):
            remote = RemoteType.REMOTE

        employment = EmploymentType.UNKNOWN
        type_text = (_clean(p.get("type")) or "").lower()
        for marker, et in _EMPLOYMENT_MARKERS:
            if marker in type_text:
                employment = et
                break

        department = _clean(p.get("category"))
        if not department:
            cats = p.get("multi_category")
            if isinstance(cats, list) and cats:
                department = _clean(cats[0])

        apply_url = _clean(p.get("applyUrl")) or raw.url

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=_clean(p.get("title")) or "",
            fetched_at=raw.fetched_at,
            apply_url=apply_url,
            locations=locations,
            remote=remote,
            employment_type=employment,
            department=department,
            salary=None,
            posted_at=_parse_date(p.get("postedDate")),
            updated_at=None,
            description_html=None,
            description_text=_clean(p.get("descriptionTeaser")),
            raw=raw.payload,
        )
