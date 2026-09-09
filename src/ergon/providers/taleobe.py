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

import httpx

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef
    from ..models import SearchQuery

__all__ = ["TaleoBEProvider"]

_PER_PAGE = 10
_MAX_PAGES = 100
_BASE = "https://{hostpath}/ats/careers/v2/searchResults"
_DETAIL_BASE = "https://{hostpath}/ats/careers/v2/viewRequisition"
# One regex over the consistent CwsV2 markup: (url, rid, title) then ALL of the card's trailing
# <div> cells (0-3 of them, tenant-dependent) — classified by _classify_divs, not by position.
_ROW = re.compile(
    r'<h4 class="oracletaleocwsv2-head-title">\s*<a href="([^"]*?rid=(\d+)[^"]*?)"[^>]*>(.*?)</a>'
    r"\s*</h4>\s*(?P<divs>(?:<div[^>]*>.*?</div>\s*)*)",
    re.S | re.I,
)
_DIV_CELL = re.compile(r"<div[^>]*>(.*?)</div>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

# --- detail (Tier-3 JD recovery / freshness confirm) ---------------------------------------------
# A ``viewRequisition`` detail page carries a ``rid=`` query param -- reused to validate/normalize
# ref.apply_url/listing_url before refetching it.
_RID_RE = re.compile(r"[?&]rid=(\d+)", re.IGNORECASE)
# Verified live (fabricated rid on phg.tbe.taleo.net/phg01, HTTP 200): a removed/nonexistent
# requisition renders NO ``application/ld+json`` JobPosting block and instead embeds this fixed,
# provider-authored message (page ``document.title`` is also set to "Job Not Available"). Used
# ONLY alongside a failed JSON-LD parse -- never on its own -- a live JD would already have been
# returned by the JSON-LD branch first.
_GONE_RE = re.compile(r"no longer available|job not available", re.IGNORECASE)

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

# Single token alternation (same vocabulary as _EMPLOYMENT_PATTERNS, minus the \b anchors) used to
# require a WHOLE-VALUE match: a div only counts as the employment-type cell if its ENTIRE trimmed
# text is composed of one or more of these tokens (e.g. "Fulltime Regular", "Part-Time"). A
# substring match alone is not enough — otherwise a DEPARTMENT div such as "Contract
# Administration" or "Temp Staffing" would be misclassified as employment_type, starving the real
# department/location assignment (Stage-1 review finding).
_EMPLOYMENT_TOKEN = (
    r"(?:full[\s-]?time|part[\s-]?time|contract(?:or)?|temp(?:orary)?|seasonal"
    r"|intern(?:ship)?|casual|regular)"
)
_EMPLOYMENT_WHOLE_RE = re.compile(rf"^{_EMPLOYMENT_TOKEN}(?:[\s/,&-]+{_EMPLOYMENT_TOKEN})*$", re.I)


def _clean(text: str) -> str:
    return _html.unescape(_TAG.sub("", text)).strip()


def _match_employment(text: str) -> EmploymentType | None:
    """Map a TBE employment-type div's free text (e.g. "Fulltime Regular") to the taxonomy.

    Requires a WHOLE-VALUE match: every word in the (trimmed) text must itself be one of the
    controlled employment-vocabulary tokens (``_EMPLOYMENT_WHOLE_RE``). A text like "Fulltime
    Regular" is entirely made of employment tokens and matches; "Contract Administration" has a
    trailing word ("Administration") that is NOT an employment token, so the whole-value check
    fails and it is correctly left unclassified (letting it fall through to department/location
    classification instead of being misread as an employment-type div).
    """
    norm = text.strip()
    if not norm or not _EMPLOYMENT_WHOLE_RE.fullmatch(norm):
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

    If, after location (and employment-type, when present) are claimed, MORE THAN ONE div is
    still left over (e.g. a 3-div card where none of the divs matched the employment vocabulary,
    so only location was claimed and 2 candidates remain) no div is silently dropped: all
    remaining divs are preserved in the card's original document order and concatenated into
    ``department`` with " / " as the separator. This is the least-surprising fallback — we have
    no signal for picking one over the other, so both survive into the single department field
    rather than one being discarded.
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

    # remaining.values() is still in original document order (pop() doesn't reorder the dict) —
    # join ALL leftover divs rather than keeping only next(iter(...)), so a second/third
    # department-like div is never silently discarded (Stage-1 review finding).
    department = " / ".join(t.strip() for t in remaining.values()) if remaining else None
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

    # --- detail (Tier-3 JD recovery / freshness confirm) ---------------------------------------

    @classmethod
    def _detail_url(cls, ref: DetailRef) -> str | None:
        """Derive the per-requisition ``viewRequisition`` URL for ``ref``.

        ``fetch``'s ``_parse_rows`` already sets ``RawJob.url`` to the exact scraped ``<a href>``
        (unescaped), so ``ref.apply_url``/``ref.listing_url`` normally IS the detail URL already --
        used as-is once validated (carries a ``rid=`` param). Falls back to rebuilding from
        ``ref.token`` (``"{hostpath}|{org}|{cws}[|{company}]"``) + ``ref.id`` when no usable URL
        survives. Returns ``None`` (never raises) when nothing recognisable is derivable."""
        for url in (ref.apply_url, ref.listing_url):
            if url and _RID_RE.search(url):
                return url
        hostpath, org, cws, _company = cls._parse(ref.token or "")
        if hostpath and org and cws and ref.id:
            return f"{_DETAIL_BASE.format(hostpath=hostpath)}?org={org}&cws={cws}&rid={ref.id}"
        return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one requisition's full JD via its own ``viewRequisition`` detail page (Tier-3
        recovery / freshness-sweep confirm).

        Live-verified (NVR Inc / ``phg.tbe.taleo.net``): the detail page carries a
        ``application/ld+json`` ``JobPosting`` block (reusing
        :meth:`BaseProvider.extract_jsonld_jobs`) whose ``description`` is the full JD HTML. A
        removed/nonexistent ``rid`` does **NOT** 404 -- it's a soft-shell HTTP 200 page with NO
        JSON-LD block and a fixed "no longer available" / "Job Not Available" marker instead
        (:data:`_GONE_RE`). The confirmed-gone path is therefore the VERIFIED-soft-404 branch the
        contract allows, gated on BOTH a failed JSON-LD parse AND the marker text (never the
        marker alone), so a live JD whose own prose says "no longer available" is never misread as
        gone -- the JSON-LD branch above would already have returned it. A real HTTP 404/410 is
        still handled defensively (returns ``None``) in case some tenant genuinely 404s, though
        none observed live did. Anything else -- unbuildable detail URL, 5xx/429/timeout, or a 200
        with neither JSON-LD nor a gone-marker -- RAISES (indeterminate), per contract."""
        detail_url = self._detail_url(ref)
        if detail_url is None:
            raise RuntimeError(f"taleobe detail: no derivable detail URL for {ref!s}")
        try:
            html_text = await fetcher.get_text(detail_url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        for job in self.extract_jsonld_jobs(html_text):
            description = job.get("description")
            if isinstance(description, str) and description.strip():
                return description
        if _GONE_RE.search(html_text):
            return None
        raise RuntimeError(f"taleobe detail: no JobPosting JSON-LD and no gone-signal for {ref!s}")

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
            _match_employment(str(employment_type_raw or "")) or EmploymentType.UNKNOWN
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
