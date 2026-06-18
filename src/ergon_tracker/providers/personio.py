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
            salary=None,  # not exposed by the feed
            posted_at=_parse_dt(p.get("createdAt")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

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
