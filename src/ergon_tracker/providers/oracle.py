"""Oracle Recruiting Cloud (ORC / Fusion HCM) job-board provider.

Oracle's modern recruiting product (distinct from legacy Taleo) exposes a fully PUBLIC,
unauthenticated REST API that serves the same data its career-site SPA consumes — no
token, no cookie, no browser::

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList&totalResults=true
        &finder=findReqs;siteNumber={site},limit={N},offset={M}

``{host}`` is ``*.fa.*.oraclecloud.com``; ``{site}`` is the ``CX_xxxx`` site number from
the career URL (``.../sites/{site}/requisitions``). Two structural quirks matter:

* Jobs live in ``items[0].requisitionList[]`` (NOT ``items[]``), and that key only appears
  when ``expand=requisitionList`` is sent.
* The true count is ``items[0].TotalJobsCount``; the top-level ``totalResults`` is always 1.

There's a per-request cap of 200 regardless of a higher ``limit`` (live-verified: ``limit=500``
returned exactly 200 rows), so we page with ``limit=200`` and walk ``offset`` to
``TotalJobsCount``.

Token shape: ``"{host}|{siteNumber}"`` (e.g. ``"eeho.fa.us2.oraclecloud.com|CX_1"``). A bare
host token defaults the site to ``CX_1`` (the common default).

The list record carries ``ShortDescriptionStr`` (HTML summary) but the full description lives
on the detail endpoint, which we don't fetch in bulk — so ``description_text`` is ``None``
here and ``description_html`` is the short summary when present. Never invented.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..models import DetailFetch, JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["OracleProvider"]

_API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_VIEW = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}"
# ORC career host + site: .../sites/{CX_xxxx}/...  on a *.fa.*.oraclecloud.com host.
_HOST_RE = re.compile(r"([a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com)", re.IGNORECASE)
_SITE_RE = re.compile(r"/sites/(CX[0-9_]*)\b", re.IGNORECASE)

# Per-requisition detail resource (Tier-3 JD recovery): distinct from the paginated list API
# above. Same public, unauthenticated ORC REST surface. ``onlyData=true`` + a ``ById`` finder
# keyed on the requisition id and site number -- NOT ``expand=requisitionList`` (that's a
# list-endpoint param that 400s here).
_DETAIL_API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"

# WorkplaceTypeCode -> our enum (deterministic).
_WORKPLACE = {
    "ORA_ONSITE": RemoteType.ONSITE,
    "ORA_ON_SITE": RemoteType.ONSITE,
    "ORA_REMOTE": RemoteType.REMOTE,
    "ORA_HYBRID": RemoteType.HYBRID,
}


def _parse_date(value: Any) -> datetime | None:
    """Parse a ``YYYY-MM-DD`` posted date to a tz-aware datetime, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register("oracle")
class OracleProvider(BaseProvider):
    name = "oracle"

    # Live-verified: recruitingCEJobRequisitions serves up to 200/page (limit=500 returned
    # exactly 200) -- 8x fewer LISTING pagination requests than the old 25/page default.
    PER_PAGE = 200
    MAX_PAGES = 200  # bound full pulls (=40,000 jobs at PER_PAGE=200)

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise an ORC career/REST URL -> ``"{host}|{siteNumber}"`` (site defaults CX_1)."""
        host_m = _HOST_RE.search(url_or_host)
        if not host_m:
            return None
        host = host_m.group(1).lower()
        site_m = _SITE_RE.search(url_or_host)
        site = site_m.group(1).upper() if site_m else "CX_1"
        return f"{host}|{site}"

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, site = self._split(token)
        if not host:
            return []
        url = _API.format(host=host)
        limit = query.limit
        raws: list[RawJob] = []
        total: int | None = None
        for page in range(self.MAX_PAGES):
            offset = page * self.PER_PAGE
            params = {
                "onlyData": "true",
                "expand": "requisitionList",
                "totalResults": "true",
                "finder": f"findReqs;siteNumber={site},limit={self.PER_PAGE},offset={offset}",
            }
            try:
                data = await fetcher.get_json(url, params=params)
            except Exception:
                break  # network/HTTP failure — stop gracefully

            item = self._first_item(data)
            if item is None:
                break
            if total is None and isinstance(item.get("TotalJobsCount"), int):
                total = item["TotalJobsCount"]
            batch = item.get("requisitionList") or []
            if not batch:
                break
            for req in batch:
                if isinstance(req, dict):
                    raws.append(self._to_raw(req, host, site))
                    if limit is not None and len(raws) >= limit:
                        return raws[:limit]
            if total is not None and offset + len(batch) >= total:
                break
        return raws

    @staticmethod
    def _split(token: str) -> tuple[str, str]:
        if "|" in token:
            host, site = token.split("|", 1)
            return host.strip().lower(), (site.strip() or "CX_1")
        return token.strip().lower(), "CX_1"

    @staticmethod
    def _first_item(data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        items = data.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    def _to_raw(self, req: dict[str, Any], host: str, site: str) -> RawJob:
        jid = str(req.get("Id") or "")
        url = _VIEW.format(host=host, site=site, jid=jid) if jid else None
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=host.split(".")[0],
            token=f"{host}|{site}",
            url=url,
            payload=req,
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc_label = str(p.get("PrimaryLocation") or "").strip()
        locations: list[Location] = []
        if loc_label:
            locations.append(Location(raw=loc_label, is_remote="remote" in loc_label.lower()))

        code = str(p.get("WorkplaceTypeCode") or p.get("WorkplaceType") or "").strip().upper()
        remote = _WORKPLACE.get(code, RemoteType.UNKNOWN)
        if remote is RemoteType.UNKNOWN and any(loc.is_remote for loc in locations):
            remote = RemoteType.REMOTE

        short = str(p.get("ShortDescriptionStr") or "").strip() or None
        department = (str(p.get("Department") or "").strip()) or None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("Title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=department,
            salary=None,
            posted_at=_parse_date(p.get("PostedDate")),
            updated_at=None,
            description_html=short,  # list view's short HTML summary
            description_text=None,  # full text only on the detail endpoint (not fetched in bulk)
            raw=raw.payload,
        )

    # --- detail (Tier-3 JD recovery) -----------------------------------------

    @staticmethod
    def _detail_request(url: str) -> tuple[str, dict[str, str]] | None:
        """Derive the ORC details-resource URL + query params from a public ``_VIEW`` URL.

        The public shape is ``https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}``
        (``apply_url``/``listing_url`` as built by :meth:`_to_raw`). ``host`` must look like an
        ORC career host (reuses ``_HOST_RE``); ``site`` and ``jid`` are the path segments right
        after ``sites`` and ``job`` respectively. The ``finder`` value is literally
        ``ById;Id={jid},siteNumber={site}`` -- passed as a query param (not hand-encoded) so the
        fetcher's own encoder handles it exactly like the proven list-endpoint ``finder`` in
        :meth:`fetch`. Returns ``None`` (never raises) for anything unparseable or non-ORC."""
        try:
            parts = urlsplit(url)
            host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
            if not host or not _HOST_RE.search(host):
                return None
            segments = [seg for seg in parts.path.split("/") if seg]
            low_segs = [seg.lower() for seg in segments]
            if "sites" not in low_segs or "job" not in low_segs:
                return None
            site_idx = low_segs.index("sites") + 1
            job_idx = low_segs.index("job") + 1
            if site_idx >= len(segments) or job_idx >= len(segments):
                return None
            site = segments[site_idx]
            jid = segments[job_idx]
            if not site or not jid:
                return None
            detail_url = _DETAIL_API.format(host=host)
            params = {
                "onlyData": "true",
                "finder": f"ById;Id={jid},siteNumber={site}",
            }
            return detail_url, params
        except Exception:
            return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD via the per-requisition ORC details resource (Tier-3
        recovery). The URL+params are derived deterministically from ``ref.apply_url`` (falling
        back to ``ref.listing_url``) -- see :meth:`_detail_request`. Like the list endpoint, the
        payload wraps the requisition in ``items[0]`` (reuses :meth:`_first_item`). Concatenates
        ``ExternalDescriptionStr`` + ``ExternalResponsibilitiesStr`` + ``ExternalQualificationsStr``
        when present. Non-raising: any unparseable URL, fetch failure, non-JSON payload, or shape
        mismatch (including a truthy non-dict payload/item) returns ``None``, never an exception."""
        request: tuple[str, dict[str, str]] | None = None
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            request = self._detail_request(url)
            if request:
                break
        if request is None:
            return None
        detail_url, params = request
        try:
            data = await fetcher.get_json(detail_url, params=params)
        except Exception:
            return None
        item = self._first_item(data)
        if item is None:
            return None
        # Collect EVERY JD-relevant field that carries text -- do NOT hard-require
        # ExternalDescriptionStr. Measured (sampling real drained-and-failed rows): ~25% of failed
        # oracle postings have an EMPTY ExternalDescriptionStr but real content in
        # ExternalResponsibilitiesStr/ExternalQualificationsStr (up to 60KB+), which the old "bail if
        # ExternalDescriptionStr empty" logic dropped entirely -- so those postings were never
        # recovered even though the JD sat in a sibling field of the SAME payload.
        parts: list[str] = []
        for key in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
        if not parts:
            return None
        text = "\n".join(parts)
        # The detail resource adds location the list feed lacks: PrimaryLocation (string, e.g.
        # "Orlando, FL, United States") + PrimaryLocationCountry (ISO-2, "US"). Both geo-resolve
        # (raw "…, United States" / ISO-2 -> country); return them so the merge fills NULL country.
        loc_str = str(item.get("PrimaryLocation") or "").strip() or None
        country = str(item.get("PrimaryLocationCountry") or "").strip() or None
        if loc_str or country:
            return DetailFetch(
                text=text, locations=[Location(raw=loc_str or country or "", country=country)]
            )
        return text
