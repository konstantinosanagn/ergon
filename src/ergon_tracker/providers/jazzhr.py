"""JazzHR (formerly "The Resumator") career-site job-board provider.

JazzHR hosts each customer's career site at ``{subdomain}.applytojob.com`` (the product's
legacy brand ``jazz.co`` survives in the internal host ``app.jazz.co``). The fully PUBLIC,
no-auth, no-cookie, no-browser way to list a tenant's jobs is its global syndication feed::

    GET https://app.jazz.co/feeds/export/jobs/{subdomain}

It returns **one XML document containing every syndicated open job** — no params, no
pagination, server-cached ~24h. Each ``<job>`` carries CDATA-wrapped fields::

    <id>job_YYYYMMDDHHMMSS_<RANDOM></id>   <title>...</title>   <department>...</department>
    <url>http://{sub}.applytojob.com/apply/{code}/{slug}</url>
    <city/> <state/> <country/> <postalcode/>   <description><!-- HTML --></description>
    <type>Full Time</type>   <experience>Experienced</experience>   <status>Open</status>

Two things make a detail fetch unnecessary:

* The 14-digit prefix of ``id`` is the **post timestamp** — live-verified equal to the
  detail page's JSON-LD ``datePosted`` (e.g. ``job_20260605135030_...`` -> 2026-06-05).
* ``description`` already carries the full job-description HTML.

Token shape: ``"{subdomain}"`` (e.g. ``"firstadvantage"``). ``matches()`` derives it from a
``*.applytojob.com`` host/URL or an ``app.jazz.co/feeds/export/jobs/{sub}`` URL.

Pitfalls: the feed lists only jobs the customer chose to syndicate, so some tenants return
an empty ``<jobs/>`` even with live careers-page jobs — we degrade to ``[]``. Locations are
frequently blank and ``url`` is ``http://``. Fields absent from the feed (salary) -> None;
never invented.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

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

__all__ = ["JazzHRProvider"]

_FEED = "https://app.jazz.co/feeds/export/jobs/{sub}"
# Tenant host: {sub}.applytojob.com  (the subdomain is the JazzHR account key).
_HOST_RE = re.compile(r"(?:^|//)([a-z0-9][a-z0-9-]*)\.applytojob\.com", re.IGNORECASE)
# The public feed URL itself: app.jazz.co/feeds/export/jobs/{sub}
_FEED_RE = re.compile(r"app\.jazz\.co/feeds/export/jobs/([a-z0-9][a-z0-9-]+)", re.IGNORECASE)
# id prefix is the post timestamp: job_YYYYMMDDHHMMSS_<RANDOM>
_ID_TS_RE = re.compile(r"job_(\d{14})_", re.IGNORECASE)
# Subdomains that are JazzHR's own marketing/app hosts, not a tenant account.
_NON_TENANT = frozenset({"www", "info", "app", "blog", "support", "help"})

# JazzHR <type> -> our enum (deterministic).
_EMPLOYMENT = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
    "freelance": EmploymentType.CONTRACT,
    "commission": EmploymentType.OTHER,
}


def _posted_from_id(job_id: str) -> datetime | None:
    """Derive the posted datetime from a JazzHR ``job_YYYYMMDDHHMMSS_...`` id."""
    m = _ID_TS_RE.search(job_id)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register("jazzhr")
class JazzHRProvider(BaseProvider):
    name = "jazzhr"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a JazzHR career/feed URL -> ``"{subdomain}"`` token."""
        m = _FEED_RE.search(url_or_host)
        if m:
            return m.group(1).lower()
        m = _HOST_RE.search(url_or_host)
        if not m:
            # bare host without scheme, e.g. "firstadvantage.applytojob.com"
            host = urlsplit("//" + url_or_host).netloc.split("@")[-1].split(":")[0].lower()
            m = _HOST_RE.search("//" + host) if host else None
        if not m:
            return None
        sub = m.group(1).lower()
        return None if sub in _NON_TENANT else sub

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        sub = token.strip().lower()
        if not sub:
            return []
        try:
            xml = await fetcher.get_text(_FEED.format(sub=sub))
        except Exception:
            return []  # network/HTTP failure — degrade gracefully
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []

        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for node in root.findall("job"):
            fields = {child.tag: (child.text or "").strip() for child in node}
            jid = fields.get("id") or ""
            if not jid or jid in seen:
                continue
            if (fields.get("status") or "").lower() not in ("", "open"):
                continue
            seen.add(jid)
            raws.append(self._to_raw(fields, sub))
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def _to_raw(self, fields: dict[str, str], sub: str) -> RawJob:
        url = fields.get("url") or None
        return RawJob(
            source=self.name,
            source_job_id=fields["id"],
            company=sub,
            token=sub,
            url=url,
            payload=fields,
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        city = (p.get("city") or "").strip()
        region = (p.get("state") or "").strip()
        country = (p.get("country") or "").strip()
        title = (p.get("title") or "").strip()

        locations: list[Location] = []
        loc_text = ", ".join(part for part in (city, region, country) if part)
        is_remote = "remote" in loc_text.lower() or "remote" in title.lower()
        if city or region or country:
            locations.append(
                Location(
                    city=city or None,
                    region=region or None,
                    country=country or None,
                    raw=loc_text or None,
                    is_remote=is_remote,
                )
            )
        remote = RemoteType.REMOTE if is_remote else RemoteType.UNKNOWN

        emp = _EMPLOYMENT.get((p.get("type") or "").strip().lower(), EmploymentType.UNKNOWN)
        description = (p.get("description") or "").strip() or None
        department = (p.get("department") or "").strip() or None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=emp,
            department=department,
            level=level_from_ats_vocab(p.get("experience")),
            salary=None,  # not exposed in the feed
            posted_at=_posted_from_id(raw.source_job_id),
            updated_at=None,
            description_html=description,
            description_text=None,  # feed gives HTML only
            raw=raw.payload,
        )
