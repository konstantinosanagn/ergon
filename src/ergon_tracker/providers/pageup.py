"""PageUp People careers provider (the ``careers.pageuppeople.com`` RSS surface).

Many universities and enterprises (Michigan State, U. Alabama, RPI, Lehigh, the Cal State system,
…) run their careers site on PageUp. The public vanity domains (``careers.msu.edu``,
``careers.ua.edu``, ``careers.rpi.edu``) are usually behind an AWS-WAF JS challenge that a no-JS
client cannot pass — BUT the WAF is bound to the vanity host, not the tenant. Every tenant is ALSO
served on the canonical host ``careers.pageuppeople.com/{tenantID}/…``, which is NOT WAF-walled, and
that host exposes a complete RSS feed returning ALL jobs in a single request::

    GET https://careers.pageuppeople.com/{tenantID}/{section}/{locale}/rss

The feed is ``application/rss+xml`` with PageUp's ``xmlns:job="http://pageuppeople.com/"`` namespace.
Each ``<item>`` carries clean fields: ``<job:refNo>`` (the numeric job id, also in ``<link>``
``…/job/{id}``), ``<title>``, ``<job:location>``, ``<job:category>``/``<job:subCategory>``,
``<job:workType>``, ``<job:businessLayer1>`` (org unit), ``<job:applyLink>``, ``<job:description>``
(full HTML, double-escaped), ``<description>`` (plain teaser), and ``<a10:updated>``/``<pubDate>``.
No pagination — one request is the whole board.

Token: ``"{tenantID}|{Company}"`` (e.g. ``"669|University of Alabama"``); optional trailing
``|{locale}|{section}`` overrides the defaults ``locale=en-us``, ``section=cw`` (external candidates).
``tenantID`` is the stable numeric id from the canonical PageUp URL (postings carry ``/{ID}/`` in
their path; web-search ``careers.pageuppeople.com {org}`` to find it).
"""

from __future__ import annotations

import html as _htmlmod
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PageUpProvider"]

_HOST = "careers.pageuppeople.com"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_ITEM_RE = re.compile(r"<item[ >](.*?)</item>", re.S | re.I)
_EMPLOYMENT = {
    "full time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "casual": EmploymentType.TEMPORARY,
    "temporary": EmploymentType.TEMPORARY,
    "fixed term": EmploymentType.TEMPORARY,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}


def _tag(block: str, tag: str) -> str | None:
    """First value of ``<tag>…</tag>`` (CDATA-unwrapped, entity-decoded), else None."""
    m = re.search(
        rf"<{re.escape(tag)}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{re.escape(tag)}>",
        block,
        re.S | re.I,
    )
    if not m:
        return None
    text = _htmlmod.unescape(m.group(1)).strip()
    return text or None


@register("pageup")
class PageUpProvider(BaseProvider):
    name = "pageup"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (needs the numeric tenant id + company label); never auto-claims

    @staticmethod
    def _parse_token(token: str) -> tuple[str, str | None, str, str]:
        parts = [p.strip() for p in token.split("|")]
        tid = parts[0]
        company = parts[1] if len(parts) > 1 and parts[1] else None
        locale = parts[2] if len(parts) > 2 and parts[2] else "en-us"
        section = parts[3] if len(parts) > 3 and parts[3] else "cw"
        return tid, company, locale, section

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        tid, company, locale, section = self._parse_token(token)
        if not tid:
            return []
        url = f"https://{_HOST}/{tid}/{section}/{locale}/rss"
        try:
            text = await fetcher.get_text(url, headers={"User-Agent": _UA, "Accept": "*/*"})
        except Exception:
            return []
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for block in _ITEM_RE.findall(text):
            link = _tag(block, "link") or _tag(block, "guid") or ""
            jid = _tag(block, "job:refNo")
            if not jid:
                m = re.search(r"/job/(\d+)", link)
                jid = m.group(1) if m else None
            title = _tag(block, "title")
            if not jid or not title or jid in seen:
                continue
            seen.add(jid)
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=company or f"pageup-{tid}",
                    token=token,
                    url=_tag(block, "job:applyLink") or link or None,
                    payload={
                        "title": title,
                        "location": _tag(block, "job:location"),
                        "category": _tag(block, "job:subCategory") or _tag(block, "job:category"),
                        "department": _tag(block, "job:businessLayer1"),
                        "work_type": _tag(block, "job:workType"),
                        "description_html": _tag(block, "job:description"),
                        "description_text": _tag(block, "description"),
                        "updated": _tag(block, "a10:updated"),
                        "pub_date": _tag(block, "pubDate"),
                        "link": link or None,
                    },
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    @staticmethod
    def _location(raw: str | None) -> Location | None:
        # PageUp packs some tenants' location as "State|City" (or just "City, ST"); present a clean
        # comma-joined label and flag remote.
        if not raw:
            return None
        label = ", ".join(p.strip() for p in raw.split("|") if p.strip()) or raw.strip()
        if not label:
            return None
        return Location(raw=label, is_remote="remote" in label.lower())

    @staticmethod
    def _date(raw: str | None) -> datetime | None:
        if not raw:
            return None
        text = raw.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        try:
            return parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None

    def normalize(self, raw: RawJob) -> JobPosting:
        p: dict[str, Any] = raw.payload
        loc = self._location(p.get("location"))
        remote = RemoteType.REMOTE if (loc and loc.is_remote) else RemoteType.UNKNOWN
        work = (p.get("work_type") or "").strip().lower()
        employment = EmploymentType.UNKNOWN
        for marker, et in _EMPLOYMENT.items():
            if marker in work:
                employment = et
                break
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            employment_type=employment,
            department=p.get("department") or p.get("category") or None,
            posted_at=self._date(p.get("updated")) or self._date(p.get("pub_date")),
            description_html=p.get("description_html"),
            description_text=p.get("description_text"),
            raw=raw.payload,
        )
