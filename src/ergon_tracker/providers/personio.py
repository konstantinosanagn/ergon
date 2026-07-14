"""Personio recruiting XML-feed provider.

Personio exposes a free, unauthenticated public job feed as XML (not JSON):
``GET https://{token}.jobs.personio.de/xml`` which returns every published position for a
company in one document. There is no server-side filtering, so :meth:`fetch` returns the
whole feed and the orchestrator applies ``SearchQuery.matches`` client-side.

The XML shape is::

    <workzag-jobs>
      <position>
        <id>..</id><office>..</office><additionalOffices><office>..</office></additionalOffices>
        <department>..</department><name>(title)</name>
        <jobDescriptions><jobDescription><name>..</name><value>(html)</value></jobDescription></jobDescriptions>
        <employmentType>..</employmentType><seniority>..</seniority><schedule>..</schedule>
        <yearsOfExperience>..</yearsOfExperience><createdAt>..</createdAt>
      </position>
    </workzag-jobs>
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from ..extract.comp import coerce_amount
from ..extract.level import level_from_ats_vocab
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

__all__ = ["PersonioProvider"]

_API = "https://{token}.jobs.personio.de/xml"
_APPLY = "https://{token}.jobs.personio.de/job/{id}"

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.jobs\.personio\.(?:de|com)", re.I),)

# Personio's ``employmentType`` vocabulary.
_EMPLOYMENT = {
    "permanent": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "fulltime": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "parttime": EmploymentType.PART_TIME,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "trainee": EmploymentType.INTERNSHIP,
    "working_student": EmploymentType.PART_TIME,
    "temporary": EmploymentType.TEMPORARY,
    "contractor": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
}


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _employment(value: str | None) -> EmploymentType:
    if not value:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT.get(value.strip().lower(), EmploymentType.UNKNOWN)


def _years_range(v: str | None) -> tuple[int | None, int | None]:
    """Personio yearsOfExperience: 'lt-1'->(0,1), '1-2'->(1,2), '5-10'->(5,10), 'gt-10'->(10,None)."""
    if not v:
        return (None, None)
    s = v.strip().lower()
    if s.startswith("lt"):
        return (0, 1)
    if s.startswith("gt"):
        m = re.search(r"\d+", s)
        return ((int(m.group()) if m else None), None)
    nums = [int(n) for n in re.findall(r"\d+", s)]
    if len(nums) >= 2:
        return (nums[0], nums[1])
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (None, None)


@register("personio")
class PersonioProvider(BaseProvider):
    name = "personio"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token != "www":
                    return token
        return None

    def conditional_url(self, token: str) -> str | None:
        # Whole feed in one XML response with a strong ETag (honors If-None-Match -> 304). No
        # raws_from_body override: a 200 falls back to fetch (XML parse) — the 304 skip is the win.
        return _API.format(token=token)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Personio has no server-side filtering: pull the whole feed in one request.
        url = _API.format(token=token)
        text = await fetcher.get_text(url)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []

        raws: list[RawJob] = []
        # Positions may be nested directly or wrapped; ``iter`` is robust to either.
        for pos in root.iter("position"):
            payload = self._position_to_dict(pos)
            job_id = payload.get("id") or ""
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(job_id),
                    company=token,
                    token=token,
                    url=_APPLY.format(token=token, id=job_id) if job_id else None,
                    payload=payload,
                )
            )
        return raws

    @staticmethod
    def _position_to_dict(pos: ET.Element) -> dict[str, Any]:
        """Flatten one <position> into a plain dict (JSON-friendly for RawJob.payload)."""
        out: dict[str, Any] = {}
        for child in pos:
            tag = child.tag
            if tag == "additionalOffices":
                out[tag] = [_text(o) for o in child.findall("office") if _text(o)]
            elif tag == "jobDescriptions":
                sections: list[dict[str, str | None]] = []
                for jd in child.findall("jobDescription"):
                    sections.append(
                        {"name": _text(jd.find("name")), "value": _text(jd.find("value"))}
                    )
                out[tag] = sections
            elif tag == "salaryInformation":
                # Structured pay block: <min>/<max>/<currencyCode>/<type>. A nested element with no
                # direct text, so the generic _text() below would silently drop it (personio was
                # 6.7% salary despite handing us min/max/currency/interval for free).
                out[tag] = {c.tag: _text(c) for c in child}
            else:
                out[tag] = _text(child)
        return out

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        offices: list[str] = []
        primary = p.get("office")
        if primary:
            offices.append(primary)
        offices.extend(o for o in (p.get("additionalOffices") or []) if o)

        locations = [Location(raw=o, is_remote="remote" in o.lower()) for o in offices]
        remote = (
            RemoteType.REMOTE if any(loc.is_remote for loc in locations) else RemoteType.UNKNOWN
        )

        description_html = self._descriptions_to_html(p.get("jobDescriptions"))
        description_text = self._to_text(description_html)

        token = raw.token or raw.company
        job_id = raw.source_job_id
        apply_url = _APPLY.format(token=token, id=job_id) if job_id else None

        ymin, ymax = _years_range(p.get("yearsOfExperience"))

        return JobPosting.create(
            source=self.name,
            source_job_id=job_id,
            company=raw.company,
            title=p.get("name") or "",
            fetched_at=raw.fetched_at,
            apply_url=apply_url,
            locations=locations,
            remote=remote,
            employment_type=_employment(p.get("employmentType")),
            department=p.get("department") or None,
            level=level_from_ats_vocab(p.get("seniority")),
            years_experience_min=ymin,
            years_experience_max=ymax,
            salary=self._salary(p.get("salaryInformation")),
            posted_at=_parse_dt(p.get("createdAt")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    # Personio's <type> pay-period vocab -> canonical interval.
    _INTERVAL_BY_TYPE: dict[str, SalaryInterval] = {
        "yearly": SalaryInterval.YEAR,
        "annual": SalaryInterval.YEAR,
        "monthly": SalaryInterval.MONTH,
        "weekly": SalaryInterval.WEEK,
        "daily": SalaryInterval.DAY,
        "hourly": SalaryInterval.HOUR,
    }

    @classmethod
    def _salary(cls, info: Any) -> Salary | None:
        """Salary from the structured ``<salaryInformation>`` block (``min``/``max``/
        ``currencyCode``/``type``). Amounts arrive as strings ("34000.00"); ``coerce_amount``
        handles them. Returns ``None`` when the block is absent or carries no amounts, so
        ``enrich_in_place`` can still body-extract."""
        if not isinstance(info, dict):
            return None
        lo, hi = coerce_amount(info.get("min")), coerce_amount(info.get("max"))
        if lo is None and hi is None:
            return None
        currency = (info.get("currencyCode") or "").strip().upper() or None
        interval = cls._INTERVAL_BY_TYPE.get((info.get("type") or "").strip().lower())
        return Salary(min_amount=lo, max_amount=hi, currency=currency, interval=interval)

    @staticmethod
    def _descriptions_to_html(sections: Any) -> str | None:
        if not sections:
            return None
        parts: list[str] = []
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            name = sec.get("name")
            value = sec.get("value")
            if name:
                parts.append(f"<h3>{name}</h3>")
            if value:
                parts.append(value)
        return "\n".join(parts) or None

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
