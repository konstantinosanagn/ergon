"""Workable job-board provider.

Workable exposes a free, unauthenticated public widget endpoint:
``GET https://apply.workable.com/api/v1/widget/accounts/{token}`` returning
``{name, description, jobs: [...]}`` in a single call (no pagination). Each job carries a
``shortcode`` (the stable id), ``title``, ``employment_type`` label, structured
``locations`` plus flat ``country``/``city``/``state`` fields, a ``telecommuting`` remote
flag, ``department`` and an apply ``url``. There is no server-side filtering, so
:meth:`fetch` returns the whole board and the orchestrator applies
``SearchQuery.matches`` client-side.

Tier-3 detail recovery
-----------------------
The bulk widget response's ``url``/``shortlink`` is a BARE shortlink,
``https://apply.workable.com/j/{shortcode}`` — it does not embed the account token, and the
index does not store ``board_token`` for Workable rows. Recovering the full JD for one posting
is therefore a two-hop, unauthenticated flow:

1. ``GET https://apply.workable.com/j/{shortcode}`` with redirects disabled → a ``301`` whose
   ``Location`` reveals the token: ``/{token}/j/{shortcode}``.
2. ``GET https://apply.workable.com/api/v1/accounts/{token}/jobs/{shortcode}`` — a per-job JSON
   resource (same ``api/v1`` family as the bulk widget endpoint, just without the ``/widget/``
   prefix) returning ``{..., description, requirements, benefits}`` as separate HTML fields.

This was chosen over the authenticated ``spi/v3`` REST API (requires a per-account Bearer
token we don't have) and over the unofficial ``/{token}/jobs/view/{shortcode}.md`` LLM-crawler
markdown surface (undocumented, narrower "cleanliness" than a JSON resource in the same public
API family already used by :meth:`fetch`). Live-probed against 5 diverse accounts
(jobrack, skylight-frame, huzzle, aira, talentpluto) — all returned substantial ``description``
text. See :meth:`WorkableProvider.fetch_detail`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..extract.degree import degree_from_ats_vocab
from ..extract.level import level_from_ats_vocab
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
    from ..index.detail import DetailRef

__all__ = ["WorkableProvider"]

_API = "https://apply.workable.com/api/v1/widget/accounts/{token}"
_JOB_API = "https://apply.workable.com/api/v1/accounts/{token}/jobs/{shortcode}"
_SHORTLINK_HOST = "apply.workable.com"
# Path pieces around a shortlink URL: {..., "j", shortcode, ...}. Used both on the canonical
# bare shortlink (``apply.workable.com/j/{shortcode}``) and the post-redirect/full shape
# (``apply.workable.com/{token}/j/{shortcode}``) and its legacy host cousin
# (``{token}.workable.com/j/{shortcode}``).
_LOCATION_TOKEN_RE = re.compile(r"/([^/?#]+)/j/([^/?#]+)", re.I)

# Hosts we recognise, each capturing the board token as group 1.
_HOST_PATTERNS = (
    re.compile(r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([^/?#]+)", re.I),
    re.compile(r"([^./?#]+)\.workable\.com", re.I),
)

# Workable ``employment_type`` labels → canonical EmploymentType.
_EMPLOYMENT_BY_LABEL = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


@register("workable")
class WorkableProvider(BaseProvider):
    name = "workable"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token not in ("apply", "www", "api"):
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Single call: Workable returns the whole board (no server-side filters).
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        account = data.get("name") or token if isinstance(data, dict) else token
        jobs: list[dict[str, Any]] = data.get("jobs", []) if isinstance(data, dict) else []

        raws: list[RawJob] = []
        for job in jobs:
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(job.get("shortcode", "")),
                    company=account,
                    token=token,
                    url=job.get("url") or job.get("shortlink"),
                    payload=job,
                )
            )
        return raws

    # --- detail (Tier-3 JD recovery) -----------------------------------------

    @staticmethod
    def _full_shortlink(url: str) -> tuple[str, str] | None:
        """Return ``(token, shortcode)`` when ``url`` already embeds both.

        Handles the post-redirect/full public shape
        ``https://apply.workable.com/{token}/j/{shortcode}`` and the legacy host cousin
        ``https://{token}.workable.com/j/{shortcode}``. Never raises."""
        try:
            parts = urlsplit(url if "://" in url else f"https://{url}")
        except Exception:
            return None
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        if not host:
            return None
        if host == _SHORTLINK_HOST:
            m = _LOCATION_TOKEN_RE.search(parts.path)
            if m:
                return m.group(1), m.group(2)
            return None
        m = re.match(r"^([^.]+)\.workable\.com$", host)
        if not m:
            return None
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) >= 2 and segments[0].lower() == "j":
            return m.group(1), segments[1]
        return None

    @staticmethod
    def _bare_shortcode(url: str) -> str | None:
        """Return the shortcode from the BARE shortlink shape
        ``https://apply.workable.com/j/{shortcode}`` (no token embedded) — the shape the bulk
        widget endpoint actually returns and what the index stores as ``apply_url`` today.
        Never raises."""
        try:
            parts = urlsplit(url if "://" in url else f"https://{url}")
        except Exception:
            return None
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        if host != _SHORTLINK_HOST:
            return None
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) >= 2 and segments[0].lower() == "j":
            return segments[1]
        return None

    async def _resolve_token(self, shortcode: str, fetcher: AsyncFetcher) -> str | None:
        """Resolve the account token for a bare shortlink via ONE redirect hop: request
        ``/j/{shortcode}`` with redirects disabled and read the token out of the ``Location``
        header's path (``/{token}/j/{shortcode}``). Non-raising: any fetch failure, missing
        header, or unrecognisable shape returns ``None``."""
        url = f"https://{_SHORTLINK_HOST}/j/{shortcode}"
        try:
            resp = await fetcher.request("GET", url, follow_redirects=False)
        except Exception:
            return None
        location = resp.headers.get("location")
        if not isinstance(location, str) or not location:
            return None
        m = _LOCATION_TOKEN_RE.search(location)
        if not m:
            return None
        return m.group(1)

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via the per-job public JSON resource (Tier-3 recovery).

        The shortcode is parsed from ``ref.apply_url`` (falling back to ``ref.listing_url``);
        the account token is taken from ``ref.token`` when present, else resolved via one
        redirect hop (see :meth:`_resolve_token`) since the index does not store
        ``board_token`` for Workable rows today. Concatenates ``description`` +
        ``requirements`` + ``benefits`` (whichever are present) from the per-job resource.
        Non-raising: any unparseable URL, failed hop, non-dict payload, or missing/empty
        description returns ``None``, never an exception."""
        token: str | None = None
        shortcode: str | None = None
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            full = self._full_shortlink(url)
            if full is not None:
                token, shortcode = full
                break
        if shortcode is None:
            for url in (ref.apply_url, ref.listing_url):
                if not url:
                    continue
                shortcode = self._bare_shortcode(url)
                if shortcode:
                    break
        if not shortcode:
            return None
        if not token:
            token = ref.token or await self._resolve_token(shortcode, fetcher)
        if not token:
            return None

        url = _JOB_API.format(token=token, shortcode=shortcode)
        try:
            data = await fetcher.get_json(url)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            return None
        parts = [description]
        for key in ("requirements", "benefits"):
            extra = data.get(key)
            if isinstance(extra, str) and extra.strip():
                parts.append(extra)
        return "\n".join(parts)

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        locations = self._locations(p)
        telecommuting = bool(p.get("telecommuting"))
        remote = RemoteType.REMOTE if telecommuting else self._remote(locations)
        employment_type = self._employment_type(p.get("employment_type"))
        degree_min = degree_from_ats_vocab(p.get("education"))
        # Workable's "education" field is the ATS's own structured minimum-education setting for
        # the requisition, not free text — so a recognised value IS the posting's stated
        # requirement (never "preferred"). Setting degree_required=True here lets the enrich degree
        # guard (`degree_min is None and degree_required is None`) skip the text extractor only when
        # we actually have a mapped value; when education is absent/unrecognized both stay None so
        # the extractor still gets a chance to find a requirement in the description.
        degree_required = True if degree_min is not None else None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url") or p.get("application_url") or p.get("shortlink"),
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=p.get("department") or None,
            level=level_from_ats_vocab(p.get("experience")),
            degree_min=degree_min,
            degree_required=degree_required,
            salary=None,  # not exposed by the widget endpoint
            posted_at=_parse_date(p.get("published_on")),
            raw=raw.payload,
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        telecommuting = bool(p.get("telecommuting"))
        structured = p.get("locations")
        if isinstance(structured, list) and structured:
            out: list[Location] = []
            for loc in structured:
                if not isinstance(loc, dict):
                    continue
                out.append(
                    Location(
                        city=loc.get("city") or None,
                        region=loc.get("region") or None,
                        country=loc.get("country") or None,
                        raw=WorkableProvider._raw_text(
                            loc.get("city"), loc.get("region"), loc.get("country")
                        ),
                        is_remote=telecommuting,
                    )
                )
            if out:
                return out

        # Fall back to the flat country/city/state fields.
        city = p.get("city")
        region = p.get("state")
        country = p.get("country")
        raw = WorkableProvider._raw_text(city, region, country)
        if not any((city, region, country, telecommuting)):
            return []
        return [
            Location(
                city=city or None,
                region=region or None,
                country=country or None,
                raw=raw,
                is_remote=telecommuting,
            )
        ]

    @staticmethod
    def _raw_text(*parts: Any) -> str | None:
        text = ", ".join(str(p) for p in parts if p)
        return text or None

    @staticmethod
    def _remote(locations: list[Location]) -> RemoteType:
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        if locations:
            return RemoteType.ONSITE
        return RemoteType.UNKNOWN

    @staticmethod
    def _employment_type(label: str | None) -> EmploymentType:
        key = str(label or "").strip().lower()
        if not key:
            return EmploymentType.UNKNOWN
        return _EMPLOYMENT_BY_LABEL.get(key, EmploymentType.OTHER)
