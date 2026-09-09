"""Oracle Taleo (Enterprise faceted-search) job-board provider.

Taleo is Oracle's *legacy* recruiting product (distinct from the modern Oracle Recruiting
Cloud handled by ``oracle.py``). Modern faceted-search Taleo career sites expose a PUBLIC,
unauthenticated JSON search endpoint that the SPA itself consumes — no token, no cookie, no
CSRF, no browser. The one non-obvious requirement is a ``tz`` request header (without it the
endpoint returns HTTP 500)::

    POST https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}
        Content-Type: application/json
        tz: GMT-05:00
        Body: {"fieldData":{"fields":{},"valid":true}, ... ,"pageNo":1}

``{host}`` is ``{tenant}.taleo.net``. Two ids are needed: ``{cs}`` (career-section CODE — numeric
like ``2`` or alpha like ``ex``) and ``{portal}`` (a 9-digit number embedded in the career-section
HTML). When the token carries only the host we discover both: GET the ``jobsearch.ftl`` page for a
handful of candidate ``cs`` codes until a large HTML page returns, then regex ``portal=(\\d+)``.

The response carries a tenant-configured, **self-describing** ``column`` array per requisition:
``linkedColumn`` indexes the title, ``locationsColumns`` indexes a JSON-encoded location string
(e.g. ``'["TX-Richmond"]'``), and the remaining (last) column is a free-text posting date. Stable
ids are ``jobId`` (→ ``jobdetail.ftl?job=``) and ``contestNo`` (the public req number).

Token shape: ``"{host}|{cs}|{portal}"`` (e.g. ``"drhorton.taleo.net|2|101430233"``). A bare
``"{host}"`` token — or one with a missing ``cs``/``portal`` — triggers discovery at fetch time.

The full description lives on the ``jobdetail.ftl`` HTML page (no JSON-LD), which we don't fetch in
bulk, so ``description_text``/``description_html`` are ``None`` here. Never invented.
"""

from __future__ import annotations

import html as _htmlmod
import json as _json
import re
from datetime import datetime, timezone
from math import ceil
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx

from ..models import JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["TaleoProvider"]

_SEARCH = "https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}"
_PAGE = "https://{host}/careersection/{cs}/jobsearch.ftl?lang=en"
_DETAIL = "https://{host}/careersection/{cs}/jobdetail.ftl?job={jid}&lang=en"

# Recognise a Taleo career URL/host. Tenant slug, sometimes prefixed ``tas-``.
_HOST_RE = re.compile(r"([a-z0-9-]+\.taleo\.net)", re.IGNORECASE)
# Career-section code from a ``/careersection/{cs}/`` path segment.
_CS_RE = re.compile(r"/careersection/([a-z0-9_]+)/", re.IGNORECASE)
# 9-digit portal id embedded in the career-section HTML.
_PORTAL_RE = re.compile(r"portal=(\d+)")
# Recognise a jobdetail.ftl URL and pull out (host, cs, jid) -- used to validate/normalize
# ref.apply_url/listing_url before refetching it in fetch_detail.
_DETAIL_URL_RE = re.compile(
    r"https?://([a-z0-9-]+\.taleo\.net)/careersection/([a-z0-9_]+)/jobdetail\.ftl\?job=(\d+)",
    re.IGNORECASE,
)

# --- jobdetail.ftl JD extraction (Tier-3 recovery / freshness confirm) --------------------------
# The full JD is NOT static HTML -- it's shipped as a JS array literal the page's own script feeds
# into ``requisitionDescriptionInterface`` via ``api.fillList(...)``. Each array item is a
# single-quoted JS string; the JD/qualifications fields are prefixed ``!*!`` and URL-encoded HTML
# (further double-encoded: literal ``"`` -> ``%22`` -> the entity ``&quot;`` -> ``%26quot;``), and
# each appears TWICE (a duplicate "print" copy) -- verified live across hyatt/hdr/drhorton.taleo.net.
_DETAIL_ARR_RE = re.compile(
    r"api\.fillList\('requisitionDescriptionInterface',\s*'descRequisition',\s*\[(.*?)\]\);", re.S
)
# One JS single-quoted string literal, honouring backslash-escapes (``\\.``) so an escaped quote
# inside a value never prematurely ends the match.
_DETAIL_STR_RE = re.compile(r"'((?:\\.|[^'\\])*)'")
# Verified live (fabricated req ids on hyatt/hdr/drhorton.taleo.net, all HTTP 200): a removed/
# nonexistent requisition's jobdetail.ftl renders NO ``!*!``-prefixed content items in the
# fillList array and instead embeds this fixed, provider-authored message. Used ONLY as a
# confirmatory signal alongside an empty extracted JD -- never on its own -- so a live JD that
# happens to mention "no longer available" in its own prose can never be mistaken for gone (the
# extraction above would already have returned it as alive).
_DETAIL_GONE_RE = re.compile(r"no longer available", re.IGNORECASE)

# --- Legacy "jobsearch.ajax" career sections (no modern REST endpoint; REST returns 0) -----------
# These embed the first results page directly in the jobsearch.ftl HTML as a "!|!"-delimited
# FTL-history stream. Each job record is the signature: id‖title‖id‖title‖id‖id‖id‖id‖id‖<columns…>
# where the tenant-configured columns (location / contestNo / dates) follow in a tenant-specific
# order — so we classify the post-id window by TYPE rather than fixed offset.
_FTL_SEP = "!|!"
_FTL_DATE = re.compile(r"^[A-Z][a-z]{2,8} \d{1,2}, \d{4}$")  # "Jun 19, 2026"
_FTL_CODE = re.compile(r"^[A-Z0-9]{5,10}$")  # contestNo (INF00EO / 01024483)
_FTL_LOC = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9].*[-,].*$")  # has a letter + hyphen/comma

# Candidate career-section codes probed during discovery (cheapest-first).
_CS_CANDIDATES = ("1", "2", "5", "ex", "external", "cb_external")
# A real career section returns a large HTML page; stubs are ~1.5KB.
_CS_MIN_LEN = 10_000

# The mandatory tz header (any valid GMT offset works; without it the API 500s).
_HEADERS = {"tz": "GMT-05:00", "Content-Type": "application/json"}

# Empty-selection search body = "all jobs". ``fieldData`` MUST be an object, not an array.
_BODY: dict[str, Any] = {
    "fieldData": {"fields": {}, "valid": True},
    "filterSelectionParam": {"searchFilterSelections": []},
    "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
    "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
    "pageNo": 1,
}

# Free-text posting-date formats seen in the trailing column (best-effort).
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d")


def _parse_date(value: Any) -> datetime | None:
    """Parse a free-text posting date (e.g. ``"Jun 17, 2026"``) to UTC, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _decode_location(value: Any) -> str | None:
    """Decode a location cell — a JSON-encoded string array like ``'["TX-Richmond"]'``.

    Falls back to the raw string if it isn't valid JSON; None when empty/unusable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = _json.loads(value)
    except ValueError:
        return value.strip() or None
    if isinstance(decoded, list):
        parts = [str(p).strip() for p in decoded if str(p).strip()]
        return ", ".join(parts) or None
    text = str(decoded).strip()
    return text or None


@register("taleo")
class TaleoProvider(BaseProvider):
    name = "taleo"

    PAGE_SIZE = 25  # server default; pagingData.pageSize is authoritative when present
    MAX_PAGES = 200  # bound full pulls (= ~5000 jobs)

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise a Taleo career URL/host -> ``"{host}|{cs}|"`` (portal discovered at fetch).

        ``cs`` is taken from the URL path when present; the portal is never in the URL, so it's
        left empty and resolved during ``fetch``. Non-Taleo URLs -> None.
        """
        host_m = _HOST_RE.search(url_or_host)
        if not host_m:
            return None
        host = host_m.group(1).lower()
        cs_m = _CS_RE.search(url_or_host)
        cs = cs_m.group(1) if cs_m else ""
        return f"{host}|{cs}|"

    @staticmethod
    def _split(token: str) -> tuple[str, str, str]:
        """Parse ``"{host}|{cs}|{portal}"`` (cs/portal optional) -> (host, cs, portal)."""
        parts = (token or "").split("|")
        host = parts[0].strip().lower() if parts else ""
        cs = parts[1].strip() if len(parts) > 1 else ""
        portal = parts[2].strip() if len(parts) > 2 else ""
        return host, cs, portal

    async def _discover(
        self, host: str, cs: str, fetcher: AsyncFetcher
    ) -> tuple[str, str, str] | None:
        """Resolve ``(cs, portal, page_html)`` for a host by probing career-section pages.

        Honours a caller-supplied ``cs`` (tried first), otherwise probes ``_CS_CANDIDATES``.
        Returns the first large career-section page as ``(cs, portal_or_empty, html)`` — ``portal``
        may be empty (legacy ``jobsearch.ajax`` sites), in which case ``fetch`` parses the embedded
        ``!|!`` stream from ``html``. The page HTML is returned so the legacy path can reuse it.
        """
        candidates = [cs] if cs else []
        candidates += [c for c in _CS_CANDIDATES if c != cs]
        for candidate in candidates:
            try:
                html = await fetcher.get_text(_PAGE.format(host=host, cs=candidate))
            except Exception:
                continue
            if len(html) < _CS_MIN_LEN:
                continue
            m = _PORTAL_RE.search(html)
            return candidate, (m.group(1) if m else ""), html
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, cs, portal = self._split(token)
        if not host:
            return []
        page_html: str | None = None
        if not (cs and portal):
            resolved = await self._discover(host, cs, fetcher)
            if resolved is None:
                return []
            cs, portal, page_html = resolved
        if not cs:
            return []

        # Modern faceted-search Taleo: the public REST endpoint. If it yields nothing (legacy
        # jobsearch.ajax sites return 0 here), fall back to parsing the jobsearch.ftl HTML stream.
        if portal:
            rest = await self._fetch_rest(host, cs, portal, query, fetcher)
            if rest:
                return rest
        if page_html is None:
            try:
                page_html = await fetcher.get_text(_PAGE.format(host=host, cs=cs))
            except Exception:
                return []
        return self._raws_from_ftl(page_html, host, cs, query.limit)

    async def _fetch_rest(
        self, host: str, cs: str, portal: str, query: SearchQuery, fetcher: AsyncFetcher
    ) -> list[RawJob]:
        url = _SEARCH.format(host=host, portal=portal)
        limit = query.limit
        raws: list[RawJob] = []
        total: int | None = None
        page_size = self.PAGE_SIZE

        for page in range(1, self.MAX_PAGES + 1):
            body = dict(_BODY, pageNo=page)
            try:
                data = await fetcher.post_json(url, json=body, headers=_HEADERS)
            except Exception:
                break  # network/HTTP failure — stop gracefully

            if not isinstance(data, dict) or data.get("careerSectionUnAvailable") is True:
                break
            batch = data.get("requisitionList")
            if not isinstance(batch, list) or not batch:
                break

            paging = data.get("pagingData")
            if isinstance(paging, dict):
                if isinstance(paging.get("pageSize"), int) and paging["pageSize"] > 0:
                    page_size = paging["pageSize"]
                if total is None and isinstance(paging.get("totalCount"), int):
                    total = paging["totalCount"]

            for req in batch:
                if isinstance(req, dict):
                    raws.append(self._to_raw(req, host, cs))
                    if limit is not None and len(raws) >= limit:
                        return raws[:limit]

            last_page = ceil(total / page_size) if total else page
            if page >= last_page:
                break
        return raws

    def _to_raw(self, req: dict[str, Any], host: str, cs: str) -> RawJob:
        jid = str(req.get("jobId") or "")
        url = _DETAIL.format(host=host, cs=cs, jid=jid) if jid else None
        return RawJob(
            source=self.name,
            source_job_id=jid or str(req.get("contestNo") or ""),
            company=host.split(".")[0],
            token=f"{host}|{cs}|",
            url=url,
            payload=req,
        )

    # --- detail (Tier-3 JD recovery / freshness confirm) -------------------------------------------

    @classmethod
    def _detail_url(cls, ref: DetailRef) -> str | None:
        """Derive the per-requisition ``jobdetail.ftl`` URL for ``ref``.

        Prefers ``ref.apply_url``/``ref.listing_url`` (both providers/``_to_raw``/``_raws_from_ftl``
        already populate ``RawJob.url`` with this exact shape) -- re-derived through
        :data:`_DETAIL_URL_RE` rather than trusted verbatim so a stray query-string difference
        never changes the URL we actually fetch. Falls back to rebuilding from ``ref.token``
        (``"{host}|{cs}|{portal}"`` -- ``cs`` is always populated once discovery ran, ``portal`` is
        not needed here) + ``ref.id`` when no usable URL survives. Returns ``None`` (never raises)
        when neither source yields a recognisable Taleo host/cs/job id.
        """
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            m = _DETAIL_URL_RE.match(url)
            if m:
                return _DETAIL.format(host=m.group(1).lower(), cs=m.group(2), jid=m.group(3))
        host, cs, _portal = cls._split(ref.token or "")
        if host and cs and ref.id:
            return _DETAIL.format(host=host, cs=cs, jid=ref.id)
        return None

    @staticmethod
    def _extract_jd(html_text: str) -> str | None:
        """Pull the JD (+ qualifications) HTML out of the ``requisitionDescriptionInterface``
        ``fillList`` array embedded in a ``jobdetail.ftl`` page -- see module-level regex
        docstrings. Each real content field is duplicated (print copy); de-duped here. Returns
        ``None`` when the array is missing/absent or carries no ``!*!``-prefixed content item
        (which is exactly the shape a removed/nonexistent requisition renders)."""
        m = _DETAIL_ARR_RE.search(html_text)
        if not m:
            return None
        seen: set[str] = set()
        parts: list[str] = []
        for item in _DETAIL_STR_RE.findall(m.group(1)):
            if not item.startswith("!*!"):
                continue
            decoded = _htmlmod.unescape(unquote(item[3:])).strip()
            if not decoded or decoded in seen:
                continue
            seen.add(decoded)
            parts.append(decoded)
        return "\n".join(parts) if parts else None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one requisition's full JD via its own ``jobdetail.ftl`` page (Tier-3 recovery /
        freshness-sweep confirm).

        Live-verified (hyatt/hdr/drhorton.taleo.net): a removed OR never-existed requisition id
        does **NOT** 404 -- ``jobdetail.ftl`` always answers HTTP 200, rendering a soft-shell page
        whose ``fillList`` array carries no JD content and instead embeds a fixed "no longer
        available" message (:data:`_DETAIL_GONE_RE`). So the confirmed-gone path here is the
        VERIFIED-soft-404 branch the contract allows, gated on BOTH an empty extraction AND the
        marker text (never the marker alone) so a live JD whose own prose happens to say "no longer
        available" is never misread as gone -- :meth:`_extract_jd` would already have returned it.
        A real HTTP 404/410 is still handled defensively (returns ``None``) in case some tenant
        genuinely 404s, though none observed live did. Anything else -- unbuildable detail URL,
        5xx/429/timeout, or a 200 whose ``fillList`` array is missing/unparseable AND carries no
        gone-marker -- RAISES (indeterminate), per contract."""
        detail_url = self._detail_url(ref)
        if detail_url is None:
            raise RuntimeError(f"taleo detail: no derivable detail URL for {ref!s}")
        try:
            html_text = await fetcher.get_text(detail_url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        text = self._extract_jd(html_text)
        if text:
            return text
        if _DETAIL_GONE_RE.search(html_text):
            return None
        raise RuntimeError(f"taleo detail: no JD content and no gone-signal for {ref!s}")

    # --- legacy jobsearch.ftl "!|!" stream -------------------------------------------------------

    def _raws_from_ftl(self, html_text: str, host: str, cs: str, limit: int | None) -> list[RawJob]:
        """Parse the first results page embedded in jobsearch.ftl as a ``!|!`` stream.

        Each job is the signature ``id‖title‖id‖title‖id‖id‖id‖id‖id‖<columns…>``; the
        tenant-configured columns (location / contestNo / dates) follow in a tenant-specific order,
        so we classify the post-id window by type. Only the embedded first page is available
        without the (fragile, CSRF-gated) ajax pagination — a partial but entity-clean capture.
        """
        f = [_htmlmod.unescape(x) for x in html_text.split(_FTL_SEP)]
        n = len(f)
        raws: list[RawJob] = []
        seen: set[str] = set()
        i = 0
        while i + 9 < n:
            jid = f[i]
            if (
                jid.isdigit()
                and len(jid) >= 5
                and f[i + 2] == jid
                and f[i + 4] == jid
                and f[i + 8] == jid
                and f[i + 1]
                and not f[i + 1].isdigit()
                and f[i + 3] == f[i + 1]
                and f[i + 1].strip().lower() not in ("apply", "re-apply")
            ):
                title = f[i + 1].strip()
                window = f[i + 9 : i + 20]
                loc = next(
                    (
                        w
                        for w in window
                        if _FTL_LOC.match(w) and not _FTL_DATE.match(w) and not _FTL_CODE.match(w)
                    ),
                    None,
                )
                posted = next((w for w in window if _FTL_DATE.match(w)), None)
                if jid not in seen:
                    seen.add(jid)
                    raws.append(
                        RawJob(
                            source=self.name,
                            source_job_id=jid,
                            company=host.split(".")[0],
                            token=f"{host}|{cs}|",
                            url=_DETAIL.format(host=host, cs=cs, jid=jid),
                            payload={
                                "_ftl": True,
                                "title": title,
                                "location": loc,
                                "posted": posted,
                            },
                        )
                    )
                    if limit is not None and len(raws) >= limit:
                        break
                i += 9
            else:
                i += 1
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        if p.get("_ftl"):  # legacy jobsearch.ftl stream record
            loc_label = p.get("location")
            locations = (
                [Location(raw=loc_label, is_remote="remote" in loc_label.lower())]
                if loc_label
                else []
            )
            return JobPosting.create(
                source=self.name,
                source_job_id=raw.source_job_id,
                company=raw.company,
                title=str(p.get("title") or ""),
                fetched_at=raw.fetched_at,
                apply_url=raw.url,
                locations=locations,
                remote=RemoteType.REMOTE
                if (locations and locations[0].is_remote)
                else RemoteType.UNKNOWN,
                department=None,
                salary=None,
                posted_at=_parse_date(p.get("posted")),
                updated_at=None,
                description_html=None,
                description_text=None,
                raw=raw.payload,
            )
        column = p.get("column")
        if not isinstance(column, list):
            column = []

        def _cell(idx: Any) -> Any:
            return column[idx] if isinstance(idx, int) and 0 <= idx < len(column) else None

        title = str(_cell(p.get("linkedColumn")) or "").strip()

        loc_idxs = p.get("locationsColumns")
        loc_label = None
        if isinstance(loc_idxs, list) and loc_idxs:
            loc_label = _decode_location(_cell(loc_idxs[0]))

        locations = []
        if loc_label:
            locations.append(Location(raw=loc_label, is_remote="remote" in loc_label.lower()))

        remote = RemoteType.UNKNOWN
        if any(loc.is_remote for loc in locations):
            remote = RemoteType.REMOTE

        # Posting date = the trailing (last) free-text column.
        posted_at = _parse_date(column[-1]) if column else None

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=None,
            salary=None,
            posted_at=posted_at,
            updated_at=None,
            description_html=None,  # full text only on jobdetail.ftl (not fetched in bulk)
            description_text=None,
            raw=raw.payload,
        )
