"""join.com provider — jobs are server-rendered into the careers page's ``__NEXT_DATA__``.

join.com (a large European ATS, ~23k company career sites) is a Next.js app with no usable
public JSON API (the ``/api/...`` endpoints are auth-walled / 422). But every careers page
``https://join.com/companies/{token}`` embeds its jobs in the page's
``<script id="__NEXT_DATA__">`` blob at ``props.pageProps.initialState.jobs.items`` — so one
unauthenticated GET yields structured job data, no browser required.

Pagination: ``initialState.jobs.pagination`` reports ``perPage`` (5) and ``pageCount``; extra
pages are fetched with ``?page=N`` (SSR re-renders the slice). We fetch page 1 to learn the
count, then fetch the remaining pages **concurrently**, bounded by ``MAX_PAGES`` and
``query.limit``.

The list blob has no job description (that lives on the per-job detail page), so
``description`` is ``None`` here — never invented. Salary amounts *are* present in the list
blob under ``salaryAmountFrom``/``salaryAmountTo`` (nested ``{"amount": <minor units>,
"currency": ...}`` objects, divided by 100 for whole-currency units) even when
``settings.showSalary`` is ``false`` — see ``_salary()``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import anyio

from ..extract.comp import coerce_amount
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
    from ..index.detail import DetailRef

__all__ = ["JoinProvider"]

_CAREERS = "https://join.com/companies/{token}"
_JOB_URL = "https://join.com/companies/{token}/jobs/{id_param}"

# join.com auto-reposts evergreen jobs, so a stale posting URL can redirect through EVERY prior
# repost (live-observed chains of 22-23 hops) before landing on the current live posting.
# ``AsyncFetcher``'s shared httpx client is built with ``max_redirects=30`` (see http.py) --
# comfortably above these chains -- and follows them all internally within ONE
# ``fetcher.get_text(url)`` call, i.e. one rate-limit token per posting rather than one per hop.

# Hosts/paths we recognise, capturing the company token (slug) as group 1.
_HOST_PATTERNS = (re.compile(r"join\.com/companies/([^/?#\s]+)", re.IGNORECASE),)

# Per-job detail page shape (Tier-3 JD recovery): ``join.com/companies/{token}/jobs/{id_param}``
# — same as ``_JOB_URL`` above but matched loosely to validate an already-built apply/listing URL
# without needing to re-derive the token/id_param from it.
_JOB_DETAIL_RE = re.compile(r"join\.com/companies/[^/?#\s]+/jobs/[^/?#\s]+", re.IGNORECASE)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)

# join.com ``employmentType.name`` vocabulary -> our enum.
_EMPLOYMENT = {
    "employee": EmploymentType.FULL_TIME,
    "working student": EmploymentType.PART_TIME,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "apprenticeship": EmploymentType.INTERNSHIP,
    "trainee": EmploymentType.INTERNSHIP,
    "freelancer": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
}

_WORKPLACE = {
    "onsite": RemoteType.ONSITE,
    "on_site": RemoteType.ONSITE,
    "remote": RemoteType.REMOTE,
    "hybrid": RemoteType.HYBRID,
}

# join.com ``salaryFrequency`` vocabulary -> our enum.
_FREQ = {
    "PER_YEAR": SalaryInterval.YEAR,
    "PER_MONTH": SalaryInterval.MONTH,
    "PER_WEEK": SalaryInterval.WEEK,
    "PER_DAY": SalaryInterval.DAY,
    "PER_HOUR": SalaryInterval.HOUR,
}

# Zero-decimal currencies (no minor unit) — Stripe's published zero-decimal set. join reports
# amounts in "minor units" divided by 100, but these have no minor unit, so dividing by 100 would
# understate them 100x; divide by 1 instead.
# CAVEAT: this is Stripe's list, not strictly ISO-4217. ISK is genuinely zero-decimal under ISO-4217
# (Amendment 137, 2007) but Stripe excludes it for card-network reasons — so an ISK salary from join
# would be divided by 100 here. Left as-is because join's ISK amount convention is unverified (no
# Icelandic sample) and such postings are vanishingly rare; revisit if ISK salaries appear wrong.
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)


def _minor_amount(obj: Any) -> tuple[float | None, str | None]:
    """(major-unit amount, currency) from a join salary sub-object, or ``(None, None)``.

    join nests each bound as ``{"amount": <minor units>, "currency": "USD"}`` rather than a
    bare number -- confirmed against live payloads (a flat-number schema was assumed
    earlier but is not what the API returns). The amount is minor units (cents), so it is
    divided by 100 to get whole currency units -- except for zero-decimal ISO-4217
    currencies (JPY, KRW, ...), which have no minor unit and so are divided by 1.
    """
    if not isinstance(obj, dict):
        return None, None
    currency = obj.get("currency") or None
    amount = coerce_amount(obj.get("amount"))
    if amount is None:
        return None, currency
    divisor = 1 if (currency or "").strip().upper() in _ZERO_DECIMAL_CURRENCIES else 100
    return amount / divisor, currency


def _salary(p: dict[str, Any]) -> Salary | None:
    """Join carries amounts in the list blob under salaryAmountFrom/To; present even when
    settings.showSalary is false."""
    lo, lo_currency = _minor_amount(p.get("salaryAmountFrom"))
    hi, hi_currency = _minor_amount(p.get("salaryAmountTo"))
    if lo is None and hi is None:
        return None
    return Salary(
        min_amount=lo,
        max_amount=hi,
        currency=lo_currency or hi_currency,
        interval=_FREQ.get(str(p.get("salaryFrequency") or "").upper()),
    )


def _parse_initial_state(html: str) -> dict[str, Any]:
    """Extract ``props.pageProps.initialState`` from a careers page, or ``{}``."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    state = data.get("props", {}).get("pageProps", {}).get("initialState", {})
    return state if isinstance(state, dict) else {}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _employment(job: dict[str, Any]) -> EmploymentType:
    name = ((job.get("employmentType") or {}).get("name") or "").strip().lower()
    return _EMPLOYMENT.get(name, EmploymentType.UNKNOWN)


@register("join")
class JoinProvider(BaseProvider):
    name = "join"

    PER_PAGE = 5  # join.com renders 5 jobs per SSR page
    MAX_PAGES = 20  # per-board page cap (=100 jobs) to bound pagination cost

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token:
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        url = _CAREERS.format(token=token)

        # Page 1 (sequential) gives us the job count and the company name.
        first = _parse_initial_state(await fetcher.get_text(url))
        jobs_block = first.get("jobs") or {}
        company = (first.get("company") or {}).get("name") or token

        pagination = jobs_block.get("pagination") or {}
        page_count = int(pagination.get("pageCount") or 1)
        want_pages = min(page_count, self.MAX_PAGES)
        if query.limit is not None:
            want_pages = min(want_pages, max(1, -(-query.limit // self.PER_PAGE)))  # ceil

        pages: dict[int, list[dict[str, Any]]] = {1: list(jobs_block.get("items") or [])}

        # Remaining pages CONCURRENTLY — one task per page, collected by page number.
        if want_pages > 1:
            async with anyio.create_task_group() as tg:
                for page in range(2, want_pages + 1):
                    tg.start_soon(self._fetch_page, fetcher, url, page, pages)

        raws: list[RawJob] = []
        for page in sorted(pages):
            for job in pages[page]:
                raws.append(self._to_raw(job, token, company))
        return raws

    async def _fetch_page(
        self, fetcher: AsyncFetcher, url: str, page: int, sink: dict[int, list[dict[str, Any]]]
    ) -> None:
        state = _parse_initial_state(await fetcher.get_text(url, params={"page": page}))
        sink[page] = list((state.get("jobs") or {}).get("items") or [])

    def _to_raw(self, job: dict[str, Any], token: str, company: str) -> RawJob:
        id_param = str(job.get("idParam") or job.get("id") or "")
        return RawJob(
            source=self.name,
            source_job_id=str(job.get("id") or ""),
            company=company,
            token=token,
            url=_JOB_URL.format(token=token, id_param=id_param) if id_param else None,
            payload=job,
        )

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via the per-job detail page (Tier-3 JD recovery).

        ``ref.apply_url`` (falling back to ``ref.listing_url``) is already the
        ``join.com/companies/{token}/jobs/{id_param}`` shape built by :meth:`_to_raw` — no
        token/id reconstruction needed, just validate the shape and fetch it. The detail page
        embeds the full JD in the same ``__NEXT_DATA__`` blob the careers-listing page uses
        (see :func:`_parse_initial_state`), at ``initialState.job.schemaDescription`` (genuine
        HTML) with a fallback to ``initialState.job.description`` (Markdown-flavored plain
        text) when ``schemaDescription`` is empty. ``initialState.job.unifiedDescription`` is a
        bool flag, not content — never read for JD text. Fetched via plain ``fetcher.get_text``:
        the shared client's ``max_redirects=30`` (see http.py) is comfortably above join's
        22-23-hop evergreen-repost redirect chains, so the whole chain resolves inside that one
        rate-limited call. Non-raising: any unparseable URL, fetch failure (including exceeding
        the redirect cap), non-JSON payload, or shape mismatch (including a truthy non-dict at
        ``job``) returns ``None``, never an exception."""
        url: str | None = None
        for candidate in (ref.apply_url, ref.listing_url):
            if candidate and _JOB_DETAIL_RE.search(candidate):
                url = candidate
                break
        if url is None:
            return None
        try:
            html = await fetcher.get_text(url)
        except Exception:
            return None
        state = _parse_initial_state(html)
        job = state.get("job")
        if not isinstance(job, dict):
            return None
        schema_description = job.get("schemaDescription")
        if isinstance(schema_description, str) and schema_description.strip():
            return schema_description
        description = job.get("description")
        if isinstance(description, str) and description.strip():
            return description
        return None

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        city = p.get("city") or {}
        country = p.get("country") or {}
        location = self._location(city, country)

        workplace = str(p.get("workplaceType") or "").strip().lower()
        remote = _WORKPLACE.get(workplace, RemoteType.UNKNOWN)

        department = (p.get("category") or {}).get("name") or None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p),
            department=department,
            salary=_salary(p),
            posted_at=_parse_dt(p.get("createdAt")),
            description_html=None,  # description lives on the per-job detail page
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _location(city: dict[str, Any], country: dict[str, Any]) -> Location | None:
        city_name = (city.get("cityName") or "").strip() or None
        country_name = (city.get("countryName") or "").strip() or None
        iso = (country.get("iso3166") or "").strip() or None
        if not any((city_name, country_name, iso)):
            return None
        raw_parts = [part for part in (city_name, country_name) if part]
        return Location(
            city=city_name,
            country=country_name or iso,
            raw=", ".join(raw_parts) or None,
        )
