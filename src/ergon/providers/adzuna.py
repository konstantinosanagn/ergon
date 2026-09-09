"""Adzuna provider — a keyed aggregator (free search API).

``GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}`` with ``app_id`` +
``app_key`` query params returns ``{"results": [{id, title, company:{display_name},
location:{display_name, area:[...]}, salary_min, salary_max, contract_time, contract_type,
created, redirect_url, description, category:{label}, ...}], "count": N}``.

Credentials come from the environment (``ADZUNA_APP_ID`` / ``ADZUNA_APP_KEY``); when either
is missing the provider yields nothing instead of erroring, so an unconfigured key never
breaks a search. Adzuna requires a country in the URL path: we map ``query.country`` to an
Adzuna country slug (defaulting to ``us``) and tag salaries with that country's currency,
since the API returns amounts in local currency without a currency field.

Like other aggregators this is never auto-discovered from a company URL (``matches`` returns
``None``); the orchestrator invokes ``fetch`` with an empty token and relies on Adzuna's
server-side ``what``/``where`` filtering plus the client-side ``query.matches`` pass.
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

_API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_DEFAULT_COUNTRY = "us"


def _company_match(target: str, board: str) -> bool:
    """True if an Adzuna result's employer matches the company token (guards Adzuna mixing in
    related firms). Shares a significant (>=4 char) word, or one collapsed name contains the
    other — so "JPMorgan Chase" matches "Chase"/"WELLS FARGO BANK" matches "Wells Fargo"."""
    tw = {w for w in re.sub(r"[^a-z0-9 ]", " ", target.lower()).split() if len(w) >= 4}
    bw = {w for w in re.sub(r"[^a-z0-9 ]", " ", board.lower()).split() if len(w) >= 4}
    if tw & bw:
        return True
    tc = re.sub(r"[^a-z0-9]", "", target.lower())
    bc = re.sub(r"[^a-z0-9]", "", board.lower())
    return bool(tc) and bool(bc) and (tc in bc or bc in tc)


# Countries Adzuna serves, mapped to local currency (the API omits a currency field).
_COUNTRY_CURRENCY: dict[str, str] = {
    "gb": "GBP",
    "us": "USD",
    "at": "EUR",
    "au": "AUD",
    "be": "EUR",
    "br": "BRL",
    "ca": "CAD",
    "ch": "CHF",
    "de": "EUR",
    "es": "EUR",
    "fr": "EUR",
    "in": "INR",
    "it": "EUR",
    "mx": "MXN",
    "nl": "EUR",
    "nz": "NZD",
    "pl": "PLN",
    "sg": "SGD",
    "za": "ZAR",
}

# Common country names / ISO codes -> Adzuna country slug.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk": "gb",
    "gb": "gb",
    "united kingdom": "gb",
    "great britain": "gb",
    "england": "gb",
    "us": "us",
    "usa": "us",
    "united states": "us",
    "united states of america": "us",
    "at": "at",
    "austria": "at",
    "au": "au",
    "australia": "au",
    "be": "be",
    "belgium": "be",
    "br": "br",
    "brazil": "br",
    "ca": "ca",
    "canada": "ca",
    "ch": "ch",
    "switzerland": "ch",
    "de": "de",
    "germany": "de",
    "deutschland": "de",
    "es": "es",
    "spain": "es",
    "fr": "fr",
    "france": "fr",
    "in": "in",
    "india": "in",
    "it": "it",
    "italy": "it",
    "mx": "mx",
    "mexico": "mx",
    "nl": "nl",
    "netherlands": "nl",
    "nz": "nz",
    "new zealand": "nz",
    "pl": "pl",
    "poland": "pl",
    "sg": "sg",
    "singapore": "sg",
    "za": "za",
    "south africa": "za",
}

_CONTRACT_TIME: dict[str, EmploymentType] = {
    "full_time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
}
_CONTRACT_TYPE: dict[str, EmploymentType] = {
    "permanent": EmploymentType.FULL_TIME,
    "contract": EmploymentType.CONTRACT,
}


def _country_slug(country: str | None) -> str:
    if not country:
        return _DEFAULT_COUNTRY
    return _COUNTRY_ALIASES.get(country.strip().lower(), _DEFAULT_COUNTRY)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _employment(job: dict[str, Any]) -> EmploymentType:
    time_val = (job.get("contract_time") or "").strip().lower()
    if time_val in _CONTRACT_TIME:
        return _CONTRACT_TIME[time_val]
    type_val = (job.get("contract_type") or "").strip().lower()
    return _CONTRACT_TYPE.get(type_val, EmploymentType.UNKNOWN)


@register("adzuna")
class AdzunaProvider(BaseProvider):
    name = "adzuna"
    is_aggregator = True

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        # Aggregator: never resolved from a company URL.
        return None

    MAX_PAGES = 20  # company-scoped pagination ceiling (=1000 jobs)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        app_id = get_env("ADZUNA_APP_ID")
        app_key = get_env("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            # Unconfigured: skip silently rather than failing the whole search.
            return []

        country = _country_slug(query.country)
        # A non-empty token = a COMPANY board: search that company's name and keep only the
        # results whose employer actually matches (Adzuna mixes in related firms). This is the
        # last-resort fallback for "proxied" giants whose own site exposes no fetchable jobs but
        # whose postings are aggregated here (JPMorgan, Deloitte, Cognizant, ...). Empty token =
        # the original global keyword aggregator.
        company = (token or "").strip()
        params: dict[str, Any] = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 50,
            "content-type": "application/json",
        }
        if company:
            params["what_phrase"] = company
        elif query.keywords:
            params["what"] = query.keywords
        where = query.city or query.location
        if where:
            params["where"] = where

        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        pages = self.MAX_PAGES if company else 1
        for page in range(1, pages + 1):
            url = _API.format(country=country, page=page)
            try:
                data = await fetcher.get_json(url, params=params)
            except Exception:
                break
            results = (
                [j for j in data.get("results", []) if isinstance(j, dict)]
                if isinstance(data, dict)
                else []
            )
            if not results:
                break
            for job in results:
                co = (job.get("company") or {}).get("display_name") or ""
                if company and not _company_match(company, co):
                    continue
                jid = str(job.get("id", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=co,
                        token=None,
                        url=job.get("redirect_url"),
                        payload={**job, "_country": country},
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws[:limit]
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        country = p.get("_country", _DEFAULT_COUNTRY)
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=(p.get("title") or "").strip(),
            description_html=p.get("description"),
            locations=self._locations(p),
            remote=RemoteType.UNKNOWN,
            employment_type=_employment(p),
            department=(p.get("category") or {}).get("label") or None,
            salary=self._salary(p, country),
            apply_url=p.get("redirect_url"),
            posted_at=_parse_dt(p.get("created")),
            fetched_at=raw.fetched_at,
            raw=raw.payload,
        )

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        loc = p.get("location") or {}
        raw_loc = (loc.get("display_name") or "").strip()
        area = loc.get("area") or []
        country = area[0] if isinstance(area, list) and area else None
        if not raw_loc and not country:
            return []
        return [Location(raw=raw_loc or None, country=country)]

    @staticmethod
    def _salary(p: dict[str, Any], country: str) -> Salary | None:
        lo = p.get("salary_min")
        hi = p.get("salary_max")
        if lo is None and hi is None:
            return None
        return Salary(
            min_amount=lo,
            max_amount=hi,
            currency=_COUNTRY_CURRENCY.get(country),
            interval=SalaryInterval.YEAR,
        )
