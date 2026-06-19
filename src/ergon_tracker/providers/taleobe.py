"""Taleo Business Edition (TBE / "CwsV2") provider — a DIFFERENT product from enterprise Taleo
(``{tenant}.taleo.net`` careersections, handled by ``taleo.py``). TBE career sites live on
``*.tbe.taleo.net`` and serve a server-rendered job list (NO browser needed):

    GET https://{hostpath}/ats/careers/v2/searchResults?org={ORG}&cws={CWS}
    GET https://{hostpath}/ats/careers/v2/searchResults?org={ORG}&cws={CWS}&next&rowFrom={N}&act=null&sortColumn=null&sortOrder=null

``hostpath`` is e.g. ``phf.tbe.taleo.net/phf03``; ``org`` (e.g. ``CALTECH``) and ``cws`` (e.g. ``37``)
identify the career site. The page lists 10 jobs each; ``rowFrom`` (0,10,20,…) pages through the
rest. Each result row is::

    <h4 class="oracletaleocwsv2-head-title"><a href="...viewRequisition?...rid={id}">Title</a></h4>
    <div tabindex="0">City, ST</div>

Used by Caltech, Sullivan & Cromwell, and other mid-size employers/firms.

Token: ``"{hostpath}|{org}|{cws}"`` or ``"{hostpath}|{org}|{cws}|{Company Name}"`` (a display label,
since TBE rows carry no employer field). Example: ``"phf.tbe.taleo.net/phf03|CALTECH|37|Caltech"``.
"""

from __future__ import annotations

import html as _html
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["TaleoBEProvider"]

_PER_PAGE = 10
_MAX_PAGES = 100
_BASE = "https://{hostpath}/ats/careers/v2/searchResults"
# One regex over the consistent CwsV2 markup: (url, rid, title) then the next location div.
_ROW = re.compile(
    r'<h4 class="oracletaleocwsv2-head-title">\s*<a href="([^"]*?rid=(\d+)[^"]*?)"[^>]*>(.*?)</a>'
    r"\s*</h4>\s*<div[^>]*>(.*?)</div>",
    re.S | re.I,
)


@register("taleobe")
class TaleoBEProvider(BaseProvider):
    name = "taleobe"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower()
        return host if host.endswith(".tbe.taleo.net") else None

    @staticmethod
    def _parse(token: str) -> tuple[str, str, str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        hostpath = parts[0].strip("/")
        org = parts[1] if len(parts) > 1 else ""
        cws = parts[2] if len(parts) > 2 else ""
        company = parts[3] if len(parts) > 3 and parts[3] else None
        return hostpath, org, cws, company

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        hostpath, org, cws, company = self._parse(token)
        if not hostpath or not org or not cws:
            return []
        base = _BASE.format(hostpath=hostpath)
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for page in range(_MAX_PAGES):
            row_from = page * _PER_PAGE
            url = f"{base}?org={org}&cws={cws}"
            if row_from:
                url += f"&next&rowFrom={row_from}&act=null&sortColumn=null&sortOrder=null"
            try:
                html = await fetcher.get_text(url)
            except Exception:
                break
            new = 0
            for m in _ROW.finditer(html):
                href, rid, title, loc = m.groups()
                if rid in seen:
                    continue
                seen.add(rid)
                new += 1
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=rid,
                        company=company or org.title(),
                        token=token,
                        url=_html.unescape(href.strip()),
                        payload={
                            "title": _html.unescape(re.sub(r"<[^>]+>", "", title)).strip(),
                            "location": _html.unescape(re.sub(r"<[^>]+>", "", loc)).strip(),
                        },
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws
            if new == 0:
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = str(p.get("location") or "").strip()
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if loc:
            is_remote = "remote" in loc.lower()
            locations.append(Location(raw=loc, is_remote=is_remote))
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
        )
