"""Jobvite career-site job-board provider.

Jobvite hosts each customer's public career site at ``jobs.jobvite.com/{company}``. The
no-auth, no-browser way to list a tenant's open reqs is the server-rendered "view all" page,
which returns the FULL active-req list in a single HTML document (no pagination)::

    GET https://jobs.jobvite.com/{company}/jobs/viewall   # follow 303 -> /careers/{company}/jobs

Two career-site generations share the same job-card markup family and are handled
transparently by following redirects: classic (``/{company}/jobs/viewall`` serves directly)
and newer "Engage" (303 -> ``/careers/{company}/jobs``).

Each job is a link ``/{company}/job/{slug}`` (an 8-char id). Three card layouts appear in the
wild — all carry a title cell (``.jv-job-list-name`` / ``.jv-featured-job-title``) and a
location cell (``.jv-job-list-location`` / ``.jv-featured-job-location``) — so we parse on
those stable ``jv-*`` classes, not tenant CSS. The list exposes only title + location + slug;
posting date, department, salary and description live only on the per-job detail page (which
carries JSON-LD ``JobPosting``) and are NOT fetched in bulk, so they normalize to ``None`` —
never invented.

Token shape: ``"{company}"`` (e.g. ``"buckman"``). The authenticated JSON/XML feeds
(``api.jobvite.com/v1/jobFeed``) need a per-customer key/secret and are out of scope.
"""

from __future__ import annotations

import html as _html
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser, Node

from ..exceptions import ProviderError
from ..extract.degree import degree_from_ats_vocab
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

__all__ = ["JobviteProvider"]

_VIEWALL = "https://jobs.jobvite.com/{company}/jobs/viewall"
_JOB_URL = "https://jobs.jobvite.com/{company}/job/{slug}"
_TITLE_SEL = ".jv-job-list-name, .jv-featured-job-title"
_LOC_SEL = ".jv-job-list-location, .jv-featured-job-location"
# A Jobvite job link: /{company}/job/{slug}  (slug is an 8-ish-char alnum id).
_JOB_HREF_RE = re.compile(r"/job/([A-Za-z0-9_-]+)/?$")

# schema.org JobPosting.employmentType values (the per-job detail page's JSON-LD), normalized
# (lowercased/underscore-stripped) since a tenant may emit either the humanized form or the enum
# token.
_JSONLD_EMPLOYMENT: dict[str, EmploymentType] = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "volunteer": EmploymentType.OTHER,
    "per diem": EmploymentType.OTHER,
    "other": EmploymentType.OTHER,
}

_JSONLD_INTERVAL: dict[str, SalaryInterval] = {
    "year": SalaryInterval.YEAR,
    "month": SalaryInterval.MONTH,
    "week": SalaryInterval.WEEK,
    "day": SalaryInterval.DAY,
    "hour": SalaryInterval.HOUR,
}


def _employment_from_jsonld(value: Any) -> EmploymentType | None:
    """schema.org ``employmentType`` (a string, or a list of them) -> EmploymentType, first hit
    wins. Unrecognised/absent -> None (never guess, never raise)."""
    values = value if isinstance(value, list) else [value]
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        norm = " ".join(v.replace("_", " ").replace("-", " ").lower().split())
        mapped = _JSONLD_EMPLOYMENT.get(norm)
        if mapped is not None:
            return mapped
    return None


def _salary_from_jsonld(value: Any) -> Salary | None:
    """schema.org ``baseSalary`` (a ``MonetaryAmount`` wrapping a ``QuantitativeValue``) ->
    Salary. Handles both a single ``value`` and a ``minValue``/``maxValue`` range. None when the
    block carries no usable amount."""
    if not isinstance(value, dict):
        return None
    currency = (value.get("currency") or "").strip() or None
    qv = value.get("value")
    qv = qv if isinstance(qv, dict) else {}
    min_amount = qv.get("minValue") if isinstance(qv.get("minValue"), (int, float)) else None
    max_amount = qv.get("maxValue") if isinstance(qv.get("maxValue"), (int, float)) else None
    if min_amount is None and max_amount is None:
        single = qv.get("value") if isinstance(qv.get("value"), (int, float)) else None
        min_amount = max_amount = single
    if min_amount is None and max_amount is None:
        return None
    unit = str(qv.get("unitText") or "").strip().lower() or None
    interval = _JSONLD_INTERVAL.get(unit) if unit else None
    return Salary(
        min_amount=float(min_amount) if min_amount is not None else None,
        max_amount=float(max_amount) if max_amount is not None else None,
        currency=currency,
        interval=interval,
    )


def _education_text(value: Any) -> str | None:
    """schema.org ``educationRequirements`` -- either a plain string, or (newer schema.org) an
    ``EducationalOccupationalCredential`` object. Mapped to ``DetailFetch.degree_min`` when it is
    one of the closed ATS education vocabulary values, and ALSO folded into the JD text so the
    degree text-extractor can still mine the free-prose values the vocabulary deliberately
    refuses to guess at ("Bachelor's degree in CS or equivalent")."""
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, dict):
        for key in ("credentialCategory", "name", "description"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(value, list):
        for item in value:
            t = _education_text(item)
            if t:
                return t
    return None


@register("jobvite")
class JobviteProvider(BaseProvider):
    name = "jobvite"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a ``jobs.jobvite.com`` URL -> ``"{company}"`` token, else None.

        Handles both ``/{company}/...`` and the Engage ``/careers/{company}/...`` paths. Custom
        Jobvite-powered vanity domains can't be detected by host, so they aren't matched here.
        """
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        if host != "jobs.jobvite.com":
            return None
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            return None
        company = segs[1] if segs[0] == "careers" and len(segs) >= 2 else segs[0]
        return company.lower() or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        company = token.strip().lower()
        if not company:
            return []
        try:
            html = await fetcher.get_text(_VIEWALL.format(company=company))
        except Exception as exc:
            # never []: an empty list reads as "board is empty" and expires live rows.
            raise ProviderError("jobvite", f"the board fetch failed for {token!r}") from exc

        limit = query.limit
        raws: list[RawJob] = []
        for slug, title, location in self._parse_rows(html, company):
            raws.append(self._to_raw(company, slug, title, location))
            if limit is not None and len(raws) >= limit:
                break
        return raws

    @classmethod
    def _parse_rows(cls, html: str, company: str) -> list[tuple[str, str, str]]:
        """Extract de-duplicated ``(slug, title, location)`` for each job card (variant-agnostic)."""
        tree = HTMLParser(html)
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for cell in tree.css(_TITLE_SEL):
            anchor = cell.css_first("a[href*='/job/']") or cls._ancestor_anchor(cell)
            if anchor is None:
                continue
            href = anchor.attributes.get("href") or ""
            m = _JOB_HREF_RE.search(href)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen:
                continue
            title = _html.unescape(cell.text(strip=True))
            if not title:
                continue
            seen.add(slug)
            out.append((slug, title, cls._card_location(cell)))
        return out

    @staticmethod
    def _ancestor_anchor(node: Node) -> Node | None:
        """Nearest ancestor ``<a>`` (the classic variant wraps the title cell in the link)."""
        cur: Node | None = node.parent
        for _ in range(6):
            if cur is None:
                return None
            if cur.tag == "a":
                return cur
            cur = cur.parent
        return None

    @staticmethod
    def _card_location(cell: Node) -> str:
        """Location text from the card's nearest ``li``/``tr`` ancestor, else ``""``."""
        cur: Node | None = cell
        for _ in range(8):
            cur = cur.parent if cur is not None else None
            if cur is None:
                return ""
            if cur.tag in ("li", "tr"):
                loc = cur.css_first(_LOC_SEL)
                return _html.unescape(loc.text(strip=True)) if loc is not None else ""
        return ""

    def _to_raw(self, company: str, slug: str, title: str, location: str) -> RawJob:
        url = _JOB_URL.format(company=company, slug=slug)
        return RawJob(
            source=self.name,
            source_job_id=slug,
            company=company,
            token=company,
            url=url,
            payload={"title": title, "location": location, "url": url, "id": slug},
        )

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's full JD + structured location from its detail page (Tier-3 recovery).

        jobvite is list-only — the bulk ``viewall`` gives no description/pay/date, and its location
        is unreliable (a ``"N Locations"`` placeholder for multi-location jobs, or missing entirely
        for some company templates). The per-job page (== ``ref.apply_url``) has an
        ``application/ld+json`` ``JobPosting`` with the full ``description`` AND a structured
        ``jobLocation`` (city/region/country), plus the standard sibling properties
        ``employmentType``/``baseSalary``/``educationRequirements`` (schema.org, not universal --
        absent on templates that don't populate them). Return the body (so yoe/degree/level
        extract) plus the structured locations/employment_type/salary/degree_min so the merge can
        fill the index row's NULL fields; ``educationRequirements`` is additionally folded into the
        returned text, so free prose the closed degree vocabulary won't guess at still gets mined.
        RETURN/RAISE CONTRACT (see BaseProvider.fetch_detail): ``None`` ONLY on a real HTTP 404/410,
        because a returned ``None`` EXPIRES A LIVE INDEX ROW. A missing URL, a timeout, a 5xx/429,
        an empty body, or absent/empty JSON-LD ``description`` all RAISE. jobvite is in
        DETERMINISTIC_SOURCES, so real departures are caught by board membership; this confirm
        exists only to reject list-reshuffle false positives."""
        url = ref.apply_url or ref.listing_url
        if not url:
            raise RuntimeError(f"jobvite detail: no derivable detail URL for {ref!s}")
        try:
            html = await fetcher.get_text(url)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code in (404, 410):
                return None
            raise
        if not isinstance(html, str) or not html:
            raise RuntimeError(f"jobvite detail: empty page body for {ref!s}")
        for job in self.extract_jsonld_jobs(html):
            description = job.get("description")
            if isinstance(description, str) and description.strip():
                locations = self.jsonld_locations(
                    job.get("jobLocation")
                )  # shared BaseProvider helper
                employment_type = _employment_from_jsonld(job.get("employmentType"))
                salary = _salary_from_jsonld(job.get("baseSalary"))
                edu_text = _education_text(job.get("educationRequirements"))
                degree_min = degree_from_ats_vocab(edu_text)
                text = description
                if edu_text:
                    text = f"{description}\n\nEducation requirements: {edu_text}."
                if locations or employment_type is not None or salary is not None or edu_text:
                    return DetailFetch(
                        text=text,
                        locations=locations,
                        employment_type=employment_type,
                        salary=salary,
                        degree_min=degree_min,
                    )
                return description
        raise RuntimeError(f"jobvite detail: no JobPosting JSON-LD description for {ref!s}")

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        location = str(p.get("location") or "").strip()
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if location:
            is_remote = "remote" in location.lower()
            locations.append(Location(raw=location, is_remote=is_remote))
            if is_remote:
                remote = RemoteType.REMOTE

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=None,
            salary=None,  # detail-page only
            posted_at=None,  # detail-page JSON-LD only, not fetched in bulk
            updated_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )
