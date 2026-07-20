"""Rippling ATS job-board provider.

Rippling exposes a free, unauthenticated public board API:
``GET https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs`` returning a
JSON array of summary postings. The careers host is ``ats.rippling.com/{token}/jobs``;
the API host is ``api.rippling.com``. The board ``token`` is the careers-URL slug
verbatim (e.g. ``11fs-group-ltd``, ``1nhealth``) — no ``-careers`` suffix.

Each list entry is summary-only (no description, salary, or dates), e.g.::

    {
      "uuid": "3c36...",
      "name": "Senior Sales Executive",
      "department": {"id": "Pulse", "label": "Pulse"},
      "url": "https://ats.rippling.com/11fs-group-ltd/jobs/3c36...",
      "workLocation": {"label": "London, United Kingdom", "id": "London, United Kingdom"}
    }
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import httpx

from ..extract.comp import coerce_amount
from ..models import (
    DetailFetch,
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    Salary,
    SalaryInterval,
    SearchQuery,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["RipplingProvider"]

_API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"

# Per-posting detail resource (Tier-3 JD recovery): the list URL above with ``/{uuid}`` appended.
_DETAIL_API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{uuid}"

# Capture the slug from ``ats.rippling.com/{slug}`` or ``ats.rippling.com/{slug}/jobs``.
_HOST_RE = re.compile(r"ats\.rippling\.com/([^/?#]+)", re.IGNORECASE)

# Public apply/listing URL shape: ``ats.rippling.com/{token}/jobs/{uuid}``. Captures both the
# board token (group 1) and the posting uuid (group 2).
_DETAIL_URL_RE = re.compile(r"ats\.rippling\.com/([^/?#]+)/jobs/([^/?#]+)", re.IGNORECASE)


@register("rippling")
class RipplingProvider(BaseProvider):
    name = "rippling"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        match = _HOST_RE.search(url_or_host)
        if not match:
            return None
        token = match.group(1).strip()
        return token or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        url = _API.format(token=token)
        data = await fetcher.get_json(url)
        # Response is a JSON array; tolerate a dict wrapper defensively.
        if isinstance(data, list):
            jobs = data
        elif isinstance(data, dict):
            jobs = data.get("jobs") or data.get("results") or data.get("data") or []
        else:
            jobs = []

        raws: list[RawJob] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(job.get("uuid", "")),
                    company=token,
                    token=token,
                    url=job.get("url"),
                    payload=job,
                )
            )
        return raws

    @staticmethod
    def _parse_detail_ref(ref: DetailRef) -> tuple[str, str] | None:
        """Derive (token, uuid) for the detail-resource URL from ``ref``.

        Prefers parsing ``apply_url``/``listing_url`` (the ``ats.rippling.com/{token}/jobs/{uuid}``
        shape) for robustness; falls back to ``ref.token`` paired with ``ref.id`` (the uuid) when
        the URL doesn't parse. Returns ``None`` if neither yields a usable (token, uuid) pair."""
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            m = _DETAIL_URL_RE.search(url)
            if m:
                token = m.group(1).strip("/")
                uuid = m.group(2).strip("/")
                if token and uuid:
                    return token, uuid
        if ref.token and ref.id:
            return ref.token, ref.id
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD via the per-posting detail resource (Tier-3 recovery).

        The detail URL is the list URL (``_API``) with ``/{uuid}`` appended; ``token`` and
        ``uuid`` are derived from ``ref.apply_url``/``ref.listing_url`` (the
        ``ats.rippling.com/{token}/jobs/{uuid}`` shape), falling back to ``ref.token``/``ref.id``.
        The response's ``description`` is a DICT of HTML sections keyed by heading (e.g.
        ``{"company": "<p>...</p>", "role": "..."}``) — all string values are concatenated (in
        insertion order) joined by ``"\\n"``. A plain-string ``description`` is used directly.

        Contract (see ``providers/base.py`` / ``fetch_detail_contract.md``): returns ``None`` ONLY
        on a confirmed-gone signal -- a real HTTP 404/410 from the per-posting resource (the REST
        convention for a single-resource GET; Rippling has no verified soft-404 BODY for a removed
        posting). This matters because rippling is in ``liveness.CONFIRM_VIA_DETAIL_SOURCES`` with a
        confirmed-streak threshold of 1, so a ``None`` here immediately expires a still-live posting.
        An unbuildable detail URL (token/uuid not parseable from the ref) is NOT evidence of death,
        and every other indeterminate/transient condition -- other HTTP statuses, timeouts, rate
        limits, a non-dict payload, an empty/missing/malformed ``description``, or a shape mismatch
        (a truthy non-dict/non-str description, an empty section dict) -- RAISES instead, so the
        freshness/liveness sweep never expires a still-live posting on an ambiguous signal.
        """
        parsed = self._parse_detail_ref(ref)
        if parsed is None:
            raise RuntimeError(f"rippling detail: no derivable detail URL for {ref!s}")
        token, uuid = parsed
        url = _DETAIL_API.format(token=token, uuid=uuid)
        try:
            data = await fetcher.get_json(url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        if not isinstance(data, dict):
            raise RuntimeError(f"rippling detail: non-dict payload for {ref!s}")
        description = data.get("description")
        if isinstance(description, str):
            text = description if description.strip() else None
        elif isinstance(description, dict):
            parts = [v for v in description.values() if isinstance(v, str) and v.strip()]
            text = "\n".join(parts) if parts else None
        else:
            text = None
        if text is None:
            raise RuntimeError(f"rippling detail: no JD text in description for {ref!s}")
        # The SAME detail response carries a STRUCTURED pay array (payRangeDetails) and a location
        # STRING list (workLocations, e.g. ["London, United Kingdom"] -- note the DETAIL uses the
        # plural, unlike the list view's workLocation.label). Return both so the reconcile prefers
        # the structured pay and the merge fills the index row's NULL country (geo derives it).
        salary = self._salary_from_payrange(data.get("payRangeDetails"))
        locations = [
            Location(raw=s.strip())
            for s in (data.get("workLocations") or [])
            if isinstance(s, str) and s.strip()
        ]
        if salary is not None or locations:
            return DetailFetch(text=text, salary=salary, locations=locations or None)
        return text

    # Rippling's payRangeDetails.frequency vocab -> canonical interval (values seen upper-case).
    _INTERVAL_BY_FREQUENCY: dict[str, SalaryInterval] = {
        "YEAR": SalaryInterval.YEAR,
        "ANNUAL": SalaryInterval.YEAR,
        "ANNUALLY": SalaryInterval.YEAR,
        "YEARLY": SalaryInterval.YEAR,
        "MONTH": SalaryInterval.MONTH,
        "MONTHLY": SalaryInterval.MONTH,
        "WEEK": SalaryInterval.WEEK,
        "WEEKLY": SalaryInterval.WEEK,
        "DAY": SalaryInterval.DAY,
        "DAILY": SalaryInterval.DAY,
        "HOUR": SalaryInterval.HOUR,
        "HOURLY": SalaryInterval.HOUR,
    }

    @classmethod
    def _salary_from_payrange(cls, pay_ranges: Any) -> Salary | None:
        """Structured pay from the detail response's ``payRangeDetails`` array, e.g.
        ``[{"currency":"USD","frequency":"YEAR","rangeStart":55000.0,"rangeEnd":65000.0}]``.

        Multiple entries are common (pay-transparency GEO TIERS — "US Tier 2" vs "US Tier 3" —
        and, less often, a second CURRENCY like a CAD range beside the USD one). Reduce them to
        one range by taking the first usable entry's (currency, frequency) as the headline group
        and SPANNING every entry that shares it (min of starts, max of ends); entries in a
        different currency are ignored, never numerically merged (USD + CAD must not average).
        Equal bounds are a valid single-point figure. Returns ``None`` (never raises, never invents
        a figure) on a missing/empty/non-list value or entries with no usable amount, so the
        reconcile falls back to body extraction. Unknown frequency -> amounts kept, interval unset.
        """
        if not isinstance(pay_ranges, list):
            return None
        headline_ccy: str | None = None
        headline_freq: str | None = None
        los: list[float] = []
        his: list[float] = []
        for entry in pay_ranges:
            if not isinstance(entry, dict):
                continue
            lo = coerce_amount(entry.get("rangeStart"))
            hi = coerce_amount(entry.get("rangeEnd"))
            if lo is None and hi is None:
                continue
            ccy = str(entry.get("currency") or "").strip().upper() or None
            freq = str(entry.get("frequency") or "").strip().upper() or None
            if not los and not his:  # first usable entry defines the headline group
                headline_ccy, headline_freq = ccy, freq
            elif ccy != headline_ccy or freq != headline_freq:
                continue  # a different currency/period tier -- don't merge it in
            if lo is not None:
                los.append(lo)
            if hi is not None:
                his.append(hi)
        if not los and not his:
            return None
        return Salary(
            min_amount=min(los) if los else None,
            max_amount=max(his) if his else None,
            currency=headline_ccy,
            interval=cls._INTERVAL_BY_FREQUENCY.get(headline_freq or ""),
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        department = (p.get("department") or {}).get("label")

        work_location = p.get("workLocation") or {}
        label = work_location.get("label")
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if label:
            is_remote = "remote" in label.lower()
            if is_remote:
                remote = RemoteType.REMOTE
            locations = [self._location(label, is_remote)]

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("name") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url"),
            locations=locations,
            remote=remote,
            employment_type=EmploymentType.UNKNOWN,
            department=department,
            salary=None,
            posted_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _location(label: str, is_remote: bool) -> Location:
        """Parse ``"City, Country"`` when trivially splittable, else keep the raw label."""
        city = country = None
        # Only split the plain "City, Country" shape (no parentheses, exactly two parts).
        if "(" not in label and ")" not in label:
            parts = [part.strip() for part in label.split(",")]
            if len(parts) == 2 and all(parts):
                city, country = parts
        return Location(raw=label, city=city, country=country, is_remote=is_remote)
