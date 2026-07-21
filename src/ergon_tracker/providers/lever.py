"""Lever job-board provider.

Lever exposes a free, unauthenticated public postings API:
``GET https://api.lever.co/v0/postings/{token}?mode=json`` returning a JSON list of
postings. Unlike most ATS feeds, Lever supports a few server-side filters
(``location``, ``team``, ``commitment``) which :meth:`fetch` forwards from the
``SearchQuery`` when present.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

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

__all__ = ["LeverProvider"]

_API = "https://api.lever.co/v0/postings/{token}"
# Lever EU data-residency host. A "|eu" token suffix (e.g. "cirrus|eu") routes here — some
# EU-hosted boards (Cirrus Logic) 404 on the default US host.
_EU_API = "https://api.eu.lever.co/v0/postings/{token}"

_HOST_NEEDLE = "jobs.lever.co/"

# Public listing/apply URL shape: ``jobs.lever.co/{token}/{id}[/apply]``. Captures the board token
# (group 1) and the posting id (group 2) as a fallback when ``ref.token``/``ref.id`` are unusable.
_DETAIL_URL_RE = re.compile(r"jobs\.lever\.co/([^/?#]+)/([^/?#]+)", re.IGNORECASE)


def _slug_base(token: str) -> tuple[str, str]:
    if token.endswith("|eu"):
        return token[:-3], _EU_API
    return token, _API


_REMOTE_BY_WORKPLACE = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "on-site": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
}

# Lever ``commitment`` strings → canonical EmploymentType.
_EMPLOYMENT_BY_COMMITMENT = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "permanent": EmploymentType.FULL_TIME,
    "regular": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
}

# Reverse map for forwarding a query's employment_type to Lever's ``commitment`` param.
_COMMITMENT_BY_EMPLOYMENT = {
    EmploymentType.FULL_TIME: "Full-time",
    EmploymentType.PART_TIME: "Part-time",
    EmploymentType.CONTRACT: "Contract",
    EmploymentType.INTERNSHIP: "Internship",
    EmploymentType.TEMPORARY: "Temporary",
}

# Lever ``salaryRange.interval`` strings → canonical SalaryInterval.
_INTERVAL_BY_LEVER = {
    "per-year-salary": SalaryInterval.YEAR,
    "per-month-salary": SalaryInterval.MONTH,
    "per-week-salary": SalaryInterval.WEEK,
    "per-day-wage": SalaryInterval.DAY,
    "per-hour-wage": SalaryInterval.HOUR,
}


@register("lever")
class LeverProvider(BaseProvider):
    name = "lever"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        lowered = url_or_host.lower()
        idx = lowered.find(_HOST_NEEDLE)
        if idx == -1:
            return None
        rest = url_or_host[idx + len(_HOST_NEEDLE) :]
        token = rest.split("/")[0].split("?")[0].split("#")[0].strip()
        return token or None

    def conditional_url(self, token: str) -> str | None:
        # Whole board in one JSON response with a strong ETag. The crawler fetches with an empty
        # query, so the validatable representation is exactly ?mode=json.
        slug, base = _slug_base(token)
        return base.format(token=slug) + "?mode=json"

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        params: dict[str, str] = {"mode": "json"}
        if query.location:
            params["location"] = query.location
        if query.employment_type:
            commitment = _COMMITMENT_BY_EMPLOYMENT.get(query.employment_type)
            if commitment:
                params["commitment"] = commitment

        slug, base = _slug_base(token)
        url = base.format(token=slug)
        data = await fetcher.get_json(url, params=params)
        return self._raws_from_data(data, slug)

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob]:
        """Parse an already-downloaded response body (from a conditional 200), avoiding a refetch."""
        import json

        return self._raws_from_data(json.loads(body), _slug_base(token)[0])

    @staticmethod
    def _detail_url(ref: DetailRef) -> str | None:
        """Build the per-posting detail URL ``{base}/{slug}/{id}?mode=json``.

        Primary: ``ref.token`` (the board token, ``|eu`` suffix routing to the EU host via
        ``_slug_base``) paired with ``ref.id`` (the posting id). Fallback: parse the
        ``jobs.lever.co/{token}/{id}`` shape out of ``ref.apply_url``/``ref.listing_url`` (defaults
        to the US host — the public careers URL carries no residency hint). Returns ``None`` when no
        (token, id) pair is derivable (an unbuildable ref -> the caller RAISES, never guesses dead).
        """
        if ref.token and ref.id:
            slug, base = _slug_base(ref.token)
            if slug:
                return base.format(token=slug) + f"/{ref.id}?mode=json"
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            m = _DETAIL_URL_RE.search(url)
            if m:
                token = m.group(1).strip("/")
                pid = m.group(2).strip("/")
                if token and pid and pid != "apply":
                    return _API.format(token=token) + f"/{pid}?mode=json"
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD via Lever's per-posting resource (Tier-3 recovery + liveness
        confirm). The detail URL is the board URL with ``/{id}`` appended (see ``_detail_url``); the
        response is a single posting object whose ``descriptionPlain`` is the plain-text JD (with
        ``opening``/``descriptionBody``/``description`` concatenated as a fallback).

        Contract (see ``providers/base.py``): returns ``None`` ONLY on a real HTTP 404/410 — Lever's
        textbook single-resource not-found (``{"ok":false,"error":"Document not found"}``), the same
        clean signal ``rippling.fetch_detail`` relies on. This matters because lever is in
        ``liveness.CONFIRM_VIA_DETAIL_SOURCES`` / ``freshness``'s confirm path with a confirmed-streak
        threshold of 1, so a ``None`` here immediately expires a still-live posting. An unbuildable
        detail URL, a non-dict payload, an empty/missing description, and every other
        indeterminate/transient condition (other HTTP statuses, timeouts, rate limits, parse errors)
        RAISE instead, so the sweep never expires a live posting on an ambiguous signal.
        """
        url = self._detail_url(ref)
        if url is None:
            raise RuntimeError(f"lever detail: no derivable detail URL for {ref!s}")
        try:
            data = await fetcher.get_json(url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        if not isinstance(data, dict):
            raise RuntimeError(f"lever detail: non-dict payload for {ref!s}")
        text = self._detail_text(data)
        if not text:
            raise RuntimeError(f"lever detail: no JD text for {ref!s}")
        return text

    @staticmethod
    def _detail_text(data: dict[str, Any]) -> str | None:
        """The JD text from a detail payload: ``descriptionPlain`` when present, else the
        ``opening``/``descriptionBody``/``description`` sections concatenated (HTML is fine — the
        drain runs ``html_to_text`` over whatever this returns). ``None`` when nothing usable."""
        plain = data.get("descriptionPlain")
        if isinstance(plain, str) and plain.strip():
            return plain.strip()
        parts = [
            data[key]
            for key in ("opening", "descriptionBody", "description")
            if isinstance(data.get(key), str) and data[key].strip()
        ]
        joined = "\n".join(parts).strip()
        return joined or None

    def _raws_from_data(self, data: Any, token: str) -> list[RawJob]:
        postings: list[dict[str, Any]] = data if isinstance(data, list) else []
        company = token.replace("-", " ").title()
        raws: list[RawJob] = []
        for posting in postings:
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(posting.get("id", "")),
                    company=company,
                    token=token,
                    url=posting.get("hostedUrl") or posting.get("applyUrl"),
                    payload=posting,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        categories: dict[str, Any] = p.get("categories") or {}
        workplace = str(p.get("workplaceType") or "").strip().lower()

        all_locations = categories.get("allLocations") or []
        if not all_locations and categories.get("location"):
            all_locations = [categories["location"]]
        is_remote = workplace == "remote"
        # Lever reports the posting's ISO country separately from the free-text location
        # segments in `categories`; carry it onto each Location so geo filtering doesn't
        # depend solely on parsing the raw string. `normalize_geo` (extract/geo.py)
        # canonicalizes an already-set `country` via its alias table, so passing the raw
        # ISO code through here is sufficient.
        top_country = str(p.get("country") or "").strip() or None
        locations = [
            Location(
                raw=loc,
                country=top_country,
                is_remote=is_remote or "remote" in str(loc).lower(),
            )
            for loc in all_locations
            if loc
        ]

        remote = _REMOTE_BY_WORKPLACE.get(workplace, RemoteType.UNKNOWN)

        commitment = str(categories.get("commitment") or "").strip().lower()
        employment_type = _EMPLOYMENT_BY_COMMITMENT.get(
            commitment, EmploymentType.OTHER if commitment else EmploymentType.UNKNOWN
        )

        department = categories.get("department") or categories.get("team")

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("text") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("applyUrl") or p.get("hostedUrl"),
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=department,
            salary=self._salary(p.get("salaryRange")),
            posted_at=self._posted_at(p.get("createdAt")),
            description_html=p.get("description"),
            description_text=p.get("descriptionPlain"),
            raw=raw.payload,
        )

    @staticmethod
    def _salary(rng: dict[str, Any] | None) -> Salary | None:
        if not rng:
            return None
        interval_raw = str(rng.get("interval") or "").strip().lower()
        return Salary(
            min_amount=rng.get("min"),
            max_amount=rng.get("max"),
            currency=rng.get("currency"),
            interval=_INTERVAL_BY_LEVER.get(interval_raw),
        )

    @staticmethod
    def _posted_at(created_at: int | float | None) -> datetime | None:
        if created_at is None:
            return None
        return datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
