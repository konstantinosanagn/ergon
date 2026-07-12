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

but tenant-configured cards render UP TO THREE such ``<div>`` cells (location, employment type,
department) — and NOT in a stable order: Caltech renders location/employment-type/department;
NVR Inc renders department/location (no employment-type cell); Sullivan & Cromwell renders a blank
placeholder div then location. Card parsing therefore classifies each div by content shape/vocab
rather than trusting position — see ``_classify_divs``.

Used by Caltech, Sullivan & Cromwell, NVR Inc, and other mid-size employers/firms.

Token: ``"{hostpath}|{org}|{cws}"`` or ``"{hostpath}|{org}|{cws}|{Company Name}"`` (a display label,
since TBE rows carry no employer field). Example: ``"phf.tbe.taleo.net/phf03|CALTECH|37|Caltech"``.
"""

from __future__ import annotations

import html as _html
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["TaleoBEProvider"]

_PER_PAGE = 10
_MAX_PAGES = 100
_BASE = "https://{hostpath}/ats/careers/v2/searchResults"
# One regex over the consistent CwsV2 markup: (url, rid, title) then ALL of the card's trailing
# <div> cells (0-3 of them, tenant-dependent) — classified by _classify_divs, not by position.
_ROW = re.compile(
    r'<h4 class="oracletaleocwsv2-head-title">\s*<a href="([^"]*?rid=(\d+)[^"]*?)"[^>]*>(.*?)</a>'
    r"\s*</h4>\s*(?P<divs>(?:<div[^>]*>.*?</div>\s*)*)",
    re.S | re.I,
)
_DIV_CELL = re.compile(r"<div[^>]*>(.*?)</div>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

# "City, ST" or "ST - City" shapes — used to recognize a location div by content, since div
# *position* varies per tenant (see module docstring / inventory-D).
_LOCATION_SHAPE_RE = re.compile(r",\s*[A-Z]{2,3}\b|^[A-Z]{2,3}\s*-\s*\S")

# Small controlled vocabulary for TBE's employment-type div (e.g. "Fulltime Regular", "Part-Time").
_EMPLOYMENT_PATTERNS: list[tuple[re.Pattern[str], EmploymentType]] = [
    (re.compile(r"\bfull[\s-]?time\b", re.I), EmploymentType.FULL_TIME),
    (re.compile(r"\bpart[\s-]?time\b", re.I), EmploymentType.PART_TIME),
    (re.compile(r"\bcontract(or)?\b", re.I), EmploymentType.CONTRACT),
    (re.compile(r"\btemp(orary)?\b", re.I), EmploymentType.TEMPORARY),
    (re.compile(r"\bseasonal\b", re.I), EmploymentType.TEMPORARY),
    (re.compile(r"\bintern(ship)?\b", re.I), EmploymentType.INTERNSHIP),
    (re.compile(r"\bcasual\b", re.I), EmploymentType.OTHER),
    (re.compile(r"\bregular\b", re.I), EmploymentType.FULL_TIME),
]


def _clean(text: str) -> str:
    return _html.unescape(_TAG.sub("", text)).strip()


def _match_employment(text: str) -> EmploymentType | None:
    """Map a TBE employment-type div's free text (e.g. "Fulltime Regular") to the taxonomy."""
    norm = text.strip()
    if not norm:
        return None
    for pattern, val in _EMPLOYMENT_PATTERNS:
        if pattern.search(norm):
            return val
    return None


def _looks_like_location(text: str) -> bool:
    """ "City, ST" / "ST - City" / remote-flagged text — the shapes TBE location divs use."""
    return bool(_LOCATION_SHAPE_RE.search(text)) or "remote" in text.lower()


def _classify_divs(texts: list[str]) -> tuple[str, str | None, str | None]:
    """Classify a card's ``<div>`` texts into ``(location, employment_type_raw, department)``.

    TBE (CwsV2) card markup is NOT positionally stable across tenants: some render one div
    (location only), others render two or three (location + employment type + department), and
    the order of the extra divs differs per tenant (inventory-D: Caltech is
    location/employment_type/department; NVR Inc is department/location; Sullivan & Cromwell is
    blank/location). Trusting "div #1 is always location" silently mis-tags department text — or
    an empty placeholder div — as location on those tenants, so classification is by content
    instead of position:

      1. an employment-type div is recognized by a small controlled vocabulary;
      2. a location div is recognized by shape ("City, ST", "ST - City", or containing "remote");
      3. whatever single div is left over is the department.

    With exactly one non-blank div (the common/documented case) it is always the location — this
    preserves the legacy single-div behavior and also correctly handles tenants that pad the card
    with a blank placeholder div alongside the real location (e.g. Sullivan & Cromwell).
    """
    non_blank = [(i, t) for i, t in enumerate(texts) if t.strip()]
    if not non_blank:
        return "", None, None
    if len(non_blank) == 1:
        return non_blank[0][1].strip(), None, None

    remaining = dict(non_blank)  # index -> text, in document order; popped as fields are claimed

    et_idx = next((i for i, t in remaining.items() if _match_employment(t) is not None), None)
    employment_type_raw = remaining.pop(et_idx).strip() if et_idx is not None else None

    loc_idx = next((i for i, t in remaining.items() if _looks_like_location(t)), None)
    if loc_idx is None:
        # No div matched the location shape — fall back to the first remaining div rather than
        # dropping location entirely (mirrors the legacy "div #1" default for ambiguous cards).
        loc_idx = next(iter(remaining), None)
    location = remaining.pop(loc_idx).strip() if loc_idx is not None else ""

    department = next(iter(remaining.values())).strip() if remaining else None
    return location, employment_type_raw, department


def _parse_rows(html: str) -> list[tuple[str, str, str, str, str | None, str | None]]:
    """Pure parse: HTML -> ``(href, rid, title, location, employment_type_raw, department)``.

    Kept at module level (no I/O) so the div-classification logic can be exercised directly in
    tests without mocking the network.
    """
    rows: list[tuple[str, str, str, str, str | None, str | None]] = []
    for m in _ROW.finditer(html):
        href, rid, title = m.group(1), m.group(2), m.group(3)
        divs_blob = m.group("divs") or ""
        div_texts = [_clean(d) for d in _DIV_CELL.findall(divs_blob)]
        location, employment_type_raw, department = _classify_divs(div_texts)
        rows.append((href.strip(), rid, _clean(title), location, employment_type_raw, department))
    return rows


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
            for href, rid, title, location, employment_type_raw, department in _parse_rows(html):
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
                        url=_html.unescape(href),
                        payload={
                            "title": title,
                            "location": location,
                            "employment_type": employment_type_raw,
                            "department": department,
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
        employment_type_raw = p.get("employment_type")
        employment_type = (
            _match_employment(str(employment_type_raw)) or EmploymentType.UNKNOWN
            if employment_type_raw
            else EmploymentType.UNKNOWN
        )
        department = p.get("department") or None
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=department,
        )
