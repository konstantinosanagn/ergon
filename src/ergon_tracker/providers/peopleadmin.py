"""PeopleAdmin provider — the dominant higher-ed / public-sector ATS (universities, school
districts, state agencies).

Every PeopleAdmin tenant exposes its ENTIRE posting list as a single Atom feed at
``https://{host}/postings/search.atom`` (e.g. ``unmc.peopleadmin.com`` → 215 entries in one
document, no pagination — ``?page=N`` just repeats the same feed). Each ``<entry>`` carries the
posting id, title, apply link, HTML description (``<content>``) and the hiring unit
(``<author><name>``). Location isn't in the feed (it lives on the detail page, which we don't
bulk-fetch), so it normalizes to ``None`` — never invented.

Token: the PeopleAdmin host (``"unmc.peopleadmin.com"``) or bare subdomain (``"unmc"`` → expanded
to ``{sub}.peopleadmin.com``). ``matches()`` resolves any ``*.peopleadmin.com`` careers URL.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..models import JobPosting, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PeopleAdminProvider"]

_FEED = "https://{host}/postings/search.atom"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S | re.I)
_ID = re.compile(r"<id>\s*https?://[^<]*?/postings/(\d+)\s*</id>", re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_LINK = re.compile(r'<link[^>]*rel="alternate"[^>]*href="([^"]+)"', re.I)
_CONTENT = re.compile(r"<content[^>]*>(.*?)</content>", re.S | re.I)
_AUTHOR = re.compile(r"<author>\s*<name>(.*?)</name>", re.S | re.I)
_PUBLISHED = re.compile(r"<published>(.*?)</published>", re.S | re.I)


@register("peopleadmin")
class PeopleAdminProvider(BaseProvider):
    name = "peopleadmin"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower()
        return host if host.endswith(".peopleadmin.com") else None

    @staticmethod
    def _host(token: str) -> str:
        # Bare subdomain ("unmc") -> {sub}.peopleadmin.com. A full host with a dot
        # ("jobs.rutgers.edu", "unmc.peopleadmin.com") is used as-is — many universities
        # white-label the same PeopleAdmin Atom feed on their own domain.
        h = token.strip().lower().rstrip("/")
        return h if "." in h else f"{h}.peopleadmin.com"

    @staticmethod
    def _company(host: str) -> str:
        # "unmc.peopleadmin.com" -> "unmc"; a custom host "jobs.rutgers.edu" -> "rutgers"
        # (the registrable label, not the "jobs"/"careers" service prefix).
        parts = host.split(".")
        if host.endswith(".peopleadmin.com"):
            return parts[0]
        return parts[-2] if len(parts) >= 2 else parts[0]

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host = self._host(token)
        try:
            text = await fetcher.get_text(_FEED.format(host=host))
        except Exception:
            return []
        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        for block in _ENTRY.findall(text):
            id_m = _ID.search(block)
            if not id_m:
                continue
            jid = id_m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            link_m = _LINK.search(block)
            url = link_m.group(1).strip() if link_m else f"https://{host}/postings/{jid}"
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=self._company(host),
                    token=host,
                    url=url,
                    payload={
                        "title": self._text(_TITLE, block),
                        "department": self._text(_AUTHOR, block),
                        "description": self._text(_CONTENT, block),
                        "published": self._text(_PUBLISHED, block),
                    },
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    @staticmethod
    def _text(pat: re.Pattern[str], block: str) -> str:
        m = pat.search(block)
        return _html.unescape(m.group(1).strip()) if m else ""

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[],  # not in the Atom feed; never invented
            remote=RemoteType.UNKNOWN,
            department=self._clean(p.get("department")),
            posted_at=self._date(p.get("published")),
            description_html=self._clean(p.get("description")),
        )

    @staticmethod
    def _clean(v: object) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _date(v: object) -> datetime | None:
        if not isinstance(v, str) or not v.strip():
            return None
        try:
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
