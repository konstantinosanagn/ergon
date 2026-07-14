"""Greenhouse job-board provider.

Greenhouse exposes a free, unauthenticated public board API:
``GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``
which returns every active posting for a board in one call. There are no server-side
filters, so :meth:`fetch` returns the full board and the orchestrator applies
``SearchQuery.matches`` client-side.
"""

from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import TYPE_CHECKING, Any

from ..extract.comp import parse_salary
from ..models import (
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

__all__ = ["GreenhouseProvider"]

_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

# Hosts we recognise, each capturing the board token as group 1.
_HOST_PATTERNS = (
    re.compile(r"boards-api\.greenhouse\.io/v1/boards/([^/?#]+)", re.I),
    re.compile(r"(?:job-)?boards\.greenhouse\.io/([^/?#]+)", re.I),
)

_REMOTE_BY_WORKPLACE = {
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
    "on-site": RemoteType.ONSITE,
    "onsite": RemoteType.ONSITE,
    "in office": RemoteType.ONSITE,
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@register("greenhouse")
class GreenhouseProvider(BaseProvider):
    name = "greenhouse"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token not in ("v1", "boards"):
                    return token
        return None

    def conditional_url(self, token: str) -> str | None:
        # Whole board in one response with a strong ETag (honors If-None-Match -> 304). Must
        # match fetch's exact URL incl. ?content=true so the validator is for the same payload.
        return _API.format(token=token) + "?content=true"

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Greenhouse has no server-side filtering: pull the whole board in one request.
        url = _API.format(token=token)
        data = await fetcher.get_json(url, params={"content": "true"})
        return self._raws_from_data(data, token)

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob]:
        """Parse an already-downloaded response body (from a conditional 200), avoiding a refetch."""
        import json

        return self._raws_from_data(json.loads(body), token)

    def _raws_from_data(self, data: Any, token: str) -> list[RawJob]:
        postings: list[dict[str, Any]] = data.get("jobs", []) if isinstance(data, dict) else []
        raws: list[RawJob] = []
        for posting in postings:
            company = posting.get("company_name") or token
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(posting.get("id", "")),
                    company=company,
                    token=token,
                    url=posting.get("absolute_url"),
                    payload=posting,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        # Locations: prefer the structured offices list, fall back to the single location blob.
        locations: list[Location] = []
        for office in p.get("offices") or []:
            name = (office.get("name") or office.get("location") or "").strip()
            if name:
                locations.append(Location(raw=name, is_remote="remote" in name.lower()))
        if not locations:
            loc_name = ((p.get("location") or {}).get("name") or "").strip()
            if loc_name:
                locations.append(Location(raw=loc_name, is_remote="remote" in loc_name.lower()))

        remote = self._remote(p, locations)

        departments = p.get("departments") or []
        department = departments[0].get("name") if departments else None

        content = p.get("content")
        description_html = unescape(content) if content else None
        description_text = self._to_text(description_html)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("absolute_url"),
            locations=locations,
            remote=remote,
            employment_type=EmploymentType.UNKNOWN,  # not exposed by the board API
            department=department,
            salary=self._salary_from_metadata(p.get("metadata")),
            posted_at=_parse_dt(p.get("first_published")),
            updated_at=_parse_dt(p.get("updated_at")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    @staticmethod
    def _remote(p: dict[str, Any], locations: list[Location]) -> RemoteType:
        for entry in p.get("metadata") or []:
            if (entry.get("name") or "").strip().lower() == "workplace type":
                value = str(entry.get("value") or "").strip().lower()
                mapped = _REMOTE_BY_WORKPLACE.get(value)
                if mapped is not None:
                    return mapped
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        return RemoteType.UNKNOWN

    # Pay-frequency label -> interval. Greenhouse's "Pay Frequency" custom field is a
    # single-select; values vary in casing/phrasing across boards ("Annual", "Yearly", "Hourly").
    _PAY_FREQUENCY: dict[str, SalaryInterval] = {
        "annual": SalaryInterval.YEAR,
        "annually": SalaryInterval.YEAR,
        "yearly": SalaryInterval.YEAR,
        "year": SalaryInterval.YEAR,
        "per year": SalaryInterval.YEAR,
        "monthly": SalaryInterval.MONTH,
        "month": SalaryInterval.MONTH,
        "per month": SalaryInterval.MONTH,
        "weekly": SalaryInterval.WEEK,
        "week": SalaryInterval.WEEK,
        "daily": SalaryInterval.DAY,
        "day": SalaryInterval.DAY,
        "hourly": SalaryInterval.HOUR,
        "hour": SalaryInterval.HOUR,
        "per hour": SalaryInterval.HOUR,
    }

    @classmethod
    def _salary_from_metadata(cls, metadata: Any) -> Salary | None:
        """Salary from greenhouse's structured pay-transparency custom fields.

        The board API DOES expose pay — not in the JD body but in ``metadata`` custom fields
        (verified live: on pay-transparency-law boards like SoFi, 62/62 jobs carry it while only
        ~2/62 inline it in ``content``). Two shapes, tried in order:

        1. ``value_type == "currency_range"`` -> ``{"unit","min_value","max_value"}`` — the
           unambiguous structured form; no text parsing needed.
        2. Fallback: a ``long_text``/``short_text`` field whose NAME mentions pay/salary/comp
           (e.g. "Pay Range" = "$172,800.00 - $297,000.00") -> ``parse_salary``.

        The interval comes from a sibling "Pay Frequency" select (default: annual). Returns
        ``None`` on anything unparseable so ``enrich_in_place`` can still fall back to the body.
        """
        if not isinstance(metadata, list):
            return None

        interval = SalaryInterval.YEAR
        pay_range_text: str | None = None
        structured: Salary | None = None

        for entry in metadata:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip().lower()
            value = entry.get("value")
            if name == "pay frequency" and isinstance(value, str):
                interval = cls._PAY_FREQUENCY.get(value.strip().lower(), interval)
            elif entry.get("value_type") == "currency_range" and isinstance(value, dict):
                lo, hi = cls._coerce(value.get("min_value")), cls._coerce(value.get("max_value"))
                if lo is not None or hi is not None:
                    unit = (value.get("unit") or "").strip().upper() or None
                    structured = Salary(min_amount=lo, max_amount=hi, currency=unit)
            elif (
                pay_range_text is None
                and isinstance(value, str)
                and any(w in name for w in ("pay", "salary", "compensation"))
                and "language" not in name  # skip "Pay Language" boilerplate, not a figure
            ):
                pay_range_text = value

        if structured is not None:
            return Salary(
                min_amount=structured.min_amount,
                max_amount=structured.max_amount,
                currency=structured.currency,
                interval=interval,
            )
        if pay_range_text is not None:
            parsed = parse_salary(pay_range_text)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _coerce(v: Any) -> float | None:
        try:
            return float(v) if v is not None and str(v).strip() != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
