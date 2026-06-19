"""USAJOBS provider — a keyed aggregator (free US federal jobs search API).

``GET https://data.usajobs.gov/api/search`` returns
``{"SearchResult": {"SearchResultItems": [{"MatchedObjectId", "MatchedObjectDescriptor":
{PositionID, PositionTitle, PositionURI, OrganizationName, DepartmentName,
PositionLocation:[{LocationName, CountryCode, CityName}], PositionRemuneration:[{MinimumRange,
MaximumRange, RateIntervalCode}], PositionSchedule:[{Name}], PublicationStartDate,
UserArea:{Details:{JobSummary, RemoteIndicator}}}}], ...}}``.

Auth is header-based and *requires three headers*: ``Host: data.usajobs.gov``,
``User-Agent: <the email you registered with>`` and ``Authorization-Key: <api key>`` — the
API rejects requests missing the User-Agent. Credentials come from the environment
(``USAJOBS_API_KEY`` / ``USAJOBS_EMAIL``); when either is missing the provider yields nothing
rather than erroring, so an unconfigured key never breaks a search.

Like other aggregators it is never auto-discovered from a company URL (``matches`` returns
``None``); the orchestrator invokes ``fetch`` with an empty token and relies on USAJOBS'
server-side ``Keyword``/``LocationName`` filtering plus the client-side ``query.matches`` pass.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..config import get_env
from ..models import (
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    Salary,
    SalaryInterval,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

_API = "https://data.usajobs.gov/api/search"
_HOST = "data.usajobs.gov"
_MAX_RESULTS = 500  # USAJOBS hard cap for ResultsPerPage.

_SCHEDULE: dict[str, EmploymentType] = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "intermittent": EmploymentType.TEMPORARY,
    "seasonal": EmploymentType.TEMPORARY,
    "multiple schedules": EmploymentType.OTHER,
}

# USAJOBS sends short RateIntervalCode codes (e.g. "PA" = Per Annum); the readable
# label lives in the sibling "Description" field. We map the codes, and fall back to
# the description text when an unfamiliar code shows up.
_INTERVAL_CODE: dict[str, SalaryInterval] = {
    "pa": SalaryInterval.YEAR,  # Per Annum
    "fy": SalaryInterval.YEAR,  # Fiscal Year
    "pm": SalaryInterval.MONTH,
    "bw": SalaryInterval.WEEK,  # Biweekly (closest bucket)
    "pw": SalaryInterval.WEEK,
    "pd": SalaryInterval.DAY,
    "ph": SalaryInterval.HOUR,
}
_INTERVAL_TEXT: dict[str, SalaryInterval] = {
    "per year": SalaryInterval.YEAR,
    "per month": SalaryInterval.MONTH,
    "per week": SalaryInterval.WEEK,
    "per day": SalaryInterval.DAY,
    "per hour": SalaryInterval.HOUR,
}


def _interval(entry: dict[str, Any]) -> SalaryInterval | None:
    code = (entry.get("RateIntervalCode") or "").strip().lower()
    if code in _INTERVAL_CODE:
        return _INTERVAL_CODE[code]
    # Some codes arrive already as readable text; the Description field is also readable.
    text = code or (entry.get("Description") or "").strip().lower()
    return _INTERVAL_TEXT.get(
        (entry.get("Description") or "").strip().lower()
    ) or _INTERVAL_TEXT.get(text)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        # USAJOBS emits 7-digit fractional seconds; trim to 6 so fromisoformat accepts it
        # (Python 3.10's parser rejects >6 fractional digits).
        try:
            return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", v))
        except ValueError:
            return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@register("usajobs")
class USAJobsProvider(BaseProvider):
    name = "usajobs"
    is_aggregator = True

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        api_key = get_env("USAJOBS_API_KEY")
        email = get_env("USAJOBS_EMAIL")
        if not api_key or not email:
            # Unconfigured: skip silently rather than failing the whole search.
            return []

        headers = {
            "Host": _HOST,
            "User-Agent": email,  # required: the registered email, sent as User-Agent.
            "Authorization-Key": api_key,
        }
        params: dict[str, Any] = {
            "ResultsPerPage": min(query.limit or 50, _MAX_RESULTS),
        }
        # A non-empty token = a single federal AGENCY board: USAJOBS' `Organization` agency-code
        # filter (e.g. "HE38" = National Institutes of Health) returns only that agency's postings.
        # This lets a federal-agency giant be seeded as {ats: usajobs, token: "<agency code>"};
        # the empty-token aggregator path (matches() -> None) is unaffected.
        org = (token or "").strip()
        if org:
            params["Organization"] = org
        if query.keywords:
            params["Keyword"] = query.keywords
        where = query.city or query.location
        if where:
            params["LocationName"] = where

        data = await fetcher.get_json(_API, params=params, headers=headers)
        result = data.get("SearchResult", {}) if isinstance(data, dict) else {}
        items = [j for j in result.get("SearchResultItems", []) if isinstance(j, dict)]
        if query.limit is not None:
            items = items[: query.limit]
        out: list[RawJob] = []
        for item in items:
            descriptor = item.get("MatchedObjectDescriptor") or {}
            out.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(
                        item.get("MatchedObjectId") or descriptor.get("PositionID") or ""
                    ),
                    company=descriptor.get("OrganizationName")
                    or descriptor.get("DepartmentName")
                    or "",
                    token=None,
                    url=descriptor.get("PositionURI"),
                    payload=descriptor,
                )
            )
        return out

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        details = (
            p.get("UserArea", {}).get("Details", {}) if isinstance(p.get("UserArea"), dict) else {}
        )
        remote = RemoteType.REMOTE if details.get("RemoteIndicator") else RemoteType.UNKNOWN
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("PositionTitle") or "").strip(),
            description_text=details.get("JobSummary") or None,
            locations=self._locations(p),
            remote=remote,
            employment_type=self._employment(p),
            department=p.get("DepartmentName") or None,
            salary=self._salary(p),
            apply_url=p.get("PositionURI"),
            posted_at=_parse_dt(p.get("PublicationStartDate")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _employment(p: dict[str, Any]) -> EmploymentType:
        schedule = p.get("PositionSchedule") or []
        if schedule and isinstance(schedule[0], dict):
            name = (schedule[0].get("Name") or "").strip().lower()
            return _SCHEDULE.get(name, EmploymentType.UNKNOWN)
        return EmploymentType.UNKNOWN

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        out: list[Location] = []
        for loc in p.get("PositionLocation") or []:
            if not isinstance(loc, dict):
                continue
            out.append(
                Location(
                    city=loc.get("CityName") or None,
                    country=loc.get("CountryCode") or None,
                    raw=loc.get("LocationName") or None,
                )
            )
        return out

    @staticmethod
    def _salary(p: dict[str, Any]) -> Salary | None:
        rem = p.get("PositionRemuneration") or []
        if not rem or not isinstance(rem[0], dict):
            return None
        entry = rem[0]
        lo = _to_float(entry.get("MinimumRange"))
        hi = _to_float(entry.get("MaximumRange"))
        if lo is None and hi is None:
            return None
        return Salary(min_amount=lo, max_amount=hi, currency="USD", interval=_interval(entry))
