"""UKG Pro Recruiting (formerly UltiPro) careers provider.

UKG Pro is a top-tier ATS used by thousands of US employers (UDR, Welltower, …). Each tenant's
public job board is a SPA at ``https://{host}/{code}/JobBoard/{guid}/`` (``host`` is
``recruiting.ultipro.com`` or ``recruiting2.ultipro.com``). The board fetches jobs from a public,
no-auth JSON endpoint with NO browser::

    POST https://{host}/{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults
    Content-Type: application/json
    {"opportunitySearch": {"Top": 50, "Skip": {N}, "QueryString": "", "OrderBy": [], "Filters": []},
     "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                       "Skills": [], "hasNoLicenses": false, "SkippedSkills": []}}

Response: ``{"opportunities": [ {record}, ... ], "totalCount": N}``. Paginate ``Skip`` by ``Top``
until ``Skip >= totalCount``. Each record carries ``Id`` (guid), ``Title``, ``RequisitionNumber``,
``FullTime``, ``JobCategoryName``, ``Locations`` (list of ``{Address:{City,State:{Code}}}``),
``PostedDate``, ``BriefDescription``. The apply/detail page is
``https://{host}/{code}/JobBoard/{guid}/OpportunityDetail?opportunityId={Id}``.

Token: ``"{host}|{code}|{guid}|{Company}"``. ``Company`` is optional (defaults to ``code``); a
2-field ``"{code}|{guid}"`` token defaults ``host`` to ``recruiting.ultipro.com``. Example:
``"recruiting2.ultipro.com|UNI1027UDRT|6ccb8fd4-4950-43e4-9978-4bcc85c6f5e1|UDR"``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from ..models import DetailFetch, EmploymentType, JobPosting, Location, RawJob, RemoteType, Salary
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef
    from ..models import SearchQuery

# The OpportunityDetail SPA page embeds the full JD as a JSON `"Description":"…"` string (escaped
# HTML). The list feed only carries a short `BriefDescription` teaser.
_DETAIL_DESC_RE = re.compile(r'"Description"\s*:\s*("(?:[^"\\]|\\.)*")')

# The same OpportunityDetail JSON also carries a structured pay-range field, almost always gated
# off (PayRangeVisible=false) -- read directly when a tenant DOES expose it, instead of relying
# solely on regex-mining the JD prose for pay-transparency-law text.
_PAY_VISIBLE_RE = re.compile(r'"PayRangeVisible"\s*:\s*(true|false)')
_PAY_MIN_RE = re.compile(r'"PayRangeMinimum"\s*:\s*(-?\d+(?:\.\d+)?|null)')
_PAY_MAX_RE = re.compile(r'"PayRangeMaximum"\s*:\s*(-?\d+(?:\.\d+)?|null)')
_PAY_CURRENCY_RE = re.compile(r'"PayRangeCurrencyCode"\s*:\s*("(?:[^"\\]|\\.)*"|null)')

__all__ = ["UKGProvider"]

_DEFAULT_HOST = "recruiting.ultipro.com"
_URL = "https://{host}/{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults"
_VIEW = "https://{host}/{code}/JobBoard/{guid}/OpportunityDetail?opportunityId={jid}"
# Recognise a UKG Pro board URL: /{code}/JobBoard/{guid}
_BOARD_RE = re.compile(r"/([A-Za-z0-9]{6,})/JobBoard/([0-9a-fA-F-]{36})")
_PAGE = 50


def _json_scalar(raw: str, pattern: re.Pattern[str]) -> Any:
    """Decode one regex-captured JSON scalar (bool/number/string/null); None on no match."""
    m = pattern.search(raw)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _json_array(raw: str, key: str) -> list[Any] | None:
    """Extract the JSON array value of a top-level ``"key": [...]`` from an embedded JS/JSON blob,
    via bracket-balance scanning (handles nesting; a plain regex can't). None when the key is
    absent or its value fails to decode."""
    m = re.search(rf'"{re.escape(key)}"\s*:\s*\[', raw)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(raw[start : i + 1])
                except (ValueError, TypeError):
                    return None
                return value if isinstance(value, list) else None
    return None


def _flatten_criteria_text(items: list[Any]) -> str:
    """Flatten a criteria array (shape not documented -- strings, or dicts of free-text/labelled
    fields) into one readable sentence for the existing yoe/degree text extractors to parse."""
    parts: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            v = value.strip()
            if v:
                parts.append(v)
        elif isinstance(value, (int, float)):
            parts.append(str(value))
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for v in value:
                _walk(v)

    _walk(items)
    return "; ".join(parts)


def _pay_range(raw: str) -> Salary | None:
    """UKG's structured pay-range field, gated by PayRangeVisible -- read directly when a tenant
    exposes it (``PayRangeVisible=true``); None when gated off/absent/no usable amount."""
    if _json_scalar(raw, _PAY_VISIBLE_RE) is not True:
        return None
    min_amount = _json_scalar(raw, _PAY_MIN_RE)
    max_amount = _json_scalar(raw, _PAY_MAX_RE)
    min_amount = float(min_amount) if isinstance(min_amount, (int, float)) else None
    max_amount = float(max_amount) if isinstance(max_amount, (int, float)) else None
    if min_amount is None and max_amount is None:
        return None
    currency = _json_scalar(raw, _PAY_CURRENCY_RE)
    currency = currency.strip() if isinstance(currency, str) and currency.strip() else None
    return Salary(min_amount=min_amount, max_amount=max_amount, currency=currency)


@register("ukg")
class UKGProvider(BaseProvider):
    name = "ukg"

    MAX_PAGES = 200  # bound full pulls (=10k jobs)

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        """Recognise an UltiPro board URL -> ``"{host}|{code}|{guid}"`` token, else None."""
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        parts = urlsplit(candidate)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        # UKG Pro serves boards on the legacy *.ultipro.com hosts and the newer
        # *.rec.pro.ukg.net hosts (same /{code}/JobBoard/{guid} API). Accept both.
        if not (host.endswith("ultipro.com") or host.endswith("rec.pro.ukg.net")):
            return None
        m = _BOARD_RE.search(parts.path)
        if not m:
            return None
        return f"{host}|{m.group(1)}|{m.group(2)}"

    @staticmethod
    def _parse(token: str) -> tuple[str, str, str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        if len(parts) == 2:  # "{code}|{guid}"
            return _DEFAULT_HOST, parts[0], parts[1], None
        host = parts[0].replace("https://", "").replace("http://", "").strip("/")
        code = parts[1] if len(parts) > 1 else ""
        guid = parts[2] if len(parts) > 2 else ""
        company = parts[3] if len(parts) > 3 and parts[3] else None
        return host, code, guid, company

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host, code, guid, company = self._parse(token)
        if not (host and code and guid):
            return []
        url = _URL.format(host=host, code=code, guid=guid)
        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        total: int | None = None
        skip = 0  # advance by the ACTUAL returned count, never a fixed stride: if the server caps
        # Top below _PAGE on a big board, fixed-stride skipping would silently drop jobs.
        for _ in range(self.MAX_PAGES):
            body = {
                "opportunitySearch": {
                    "Top": _PAGE,
                    "Skip": skip,
                    "QueryString": "",
                    "OrderBy": [],
                    "Filters": [],
                },
                "matchCriteria": {
                    "PreferredJobs": [],
                    "Educations": [],
                    "LicenseAndCertifications": [],
                    "Skills": [],
                    "hasNoLicenses": False,
                    "SkippedSkills": [],
                },
            }
            try:
                data = await fetcher.post_json(url, json=body)
            except Exception:
                break
            opps = data.get("opportunities") if isinstance(data, dict) else None
            if not isinstance(opps, list) or not opps:
                break
            if total is None and isinstance(data.get("totalCount"), int):
                total = data["totalCount"]
            new = 0
            for rec in opps:
                if not isinstance(rec, dict):
                    continue
                jid = str(rec.get("Id") or rec.get("RequisitionNumber") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                raws.append(self._to_raw(rec, host, code, guid, company, jid))
                if limit is not None and len(raws) >= limit:
                    return raws
            skip += len(opps)  # actual stride, so a server-side Top cap can't create gaps
            if new == 0 or (total is not None and skip >= total):
                break
        return raws

    async def board_count(self, token: str, fetcher: AsyncFetcher) -> int | None:
        """Cheap change-CANDIDATE signal: ``totalCount`` from a ``Top:1`` search (see
        ``BaseProvider.board_count``).

        Issues ONE minimal POST to the same ``LoadSearchResults`` endpoint ``fetch`` uses
        (``Top=1, Skip=0``, no filters) and reads ``totalCount``. Returns ``None`` ONLY on a
        confirmed-gone signal (404/410, or an unparseable token); everything else indeterminate/
        transient RAISES (mirrors ``fetch_detail``'s 404-vs-transient contract)."""
        host, code, guid, _company = self._parse(token)
        if not (host and code and guid):
            return None
        url = _URL.format(host=host, code=code, guid=guid)
        body = {
            "opportunitySearch": {
                "Top": 1,
                "Skip": 0,
                "QueryString": "",
                "OrderBy": [],
                "Filters": [],
            },
            "matchCriteria": {
                "PreferredJobs": [],
                "Educations": [],
                "LicenseAndCertifications": [],
                "Skills": [],
                "hasNoLicenses": False,
                "SkippedSkills": [],
            },
        }
        try:
            data = await fetcher.post_json(url, json=body)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        if not isinstance(data, dict):
            raise RuntimeError(f"ukg board_count: non-dict payload for token {token!r}")
        total = data.get("totalCount")
        if not isinstance(total, int):
            raise RuntimeError(f"ukg board_count: missing/non-int totalCount for token {token!r}")
        return total

    def _to_raw(
        self, rec: dict[str, Any], host: str, code: str, guid: str, company: str | None, jid: str
    ) -> RawJob:
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=company or code,
            token=f"{host}|{code}|{guid}",
            url=_VIEW.format(host=host, code=code, guid=guid, jid=jid),
            payload=rec,
        )

    @staticmethod
    def _location(rec: dict[str, Any]) -> Location | None:
        locs = rec.get("Locations")
        item = locs[0] if isinstance(locs, list) and locs else None
        if not isinstance(item, dict):
            return None
        addr = item.get("Address")
        addr = addr if isinstance(addr, dict) else {}
        city = (addr.get("City") or "").strip()
        state = ""
        st = addr.get("State")
        if isinstance(st, dict):
            state = (st.get("Code") or st.get("Name") or "").strip()
        label = ", ".join(x for x in (city, state) if x) or (
            str(item.get("LocalizedName") or "").strip()
        )
        if not label:
            return None
        return Location(
            city=city or None,
            region=state or None,
            raw=label,
            is_remote="remote" in label.lower(),
        )

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(value.strip()[:10], "%Y-%m-%d")
            except ValueError:
                return None

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Fetch one posting's FULL JD (Tier-3 recovery).

        UKG's list feed only carries a short ``BriefDescription`` teaser; the full JD lives on the
        ``OpportunityDetail`` page (which IS ``ref.apply_url``), embedded as a JSON ``"Description"``
        string. Recovering it matters because UKG's structured pay field is almost always gated off
        (``PayRangeVisible=false``), yet ~40% of postings state the salary in the JD BODY (pay-
        transparency-law text) -- which the enrich extractor mines once we capture it. When a
        tenant DOES expose the gated pay range, or the ``WorkExperienceCriteria``/
        ``EducationCriteria`` arrays (present in the schema, empty on every sample seen so far),
        this returns a :class:`DetailFetch` instead so the reconcile seeds the structured salary
        directly and folds the criteria text into the JD body for the same yoe/degree text
        extractors to mine (their shape is undocumented and unobserved non-empty, so we don't
        invent a structured mapping for them).

        Returns ``None`` ONLY on a confirmed-gone signal: a real HTTP 404/410 re-fetching the
        ``OpportunityDetail`` page. A missing/unbuildable URL is NOT evidence of death, and every
        other indeterminate/transient condition -- other HTTP statuses, timeouts, rate limits, an
        empty body, or a 200 whose page doesn't embed the expected ``"Description":"..."`` JSON
        (regex miss or JSON-decode failure) -- RAISES instead, so the freshness sweep never
        expires a still-live posting on an ambiguous signal (a regex/JSON miss on a 200 is NOT a
        verified soft-404 marker for this provider).
        """
        url = ref.apply_url or ref.listing_url
        if not url or "OpportunityDetail" not in url:
            raise RuntimeError(f"ukg detail: no OpportunityDetail URL for {ref!s}")
        try:
            raw = await fetcher.get_text(url)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (404, 410):
                return None
            raise
        if not raw:
            raise RuntimeError(f"ukg detail: empty page body for {ref!s}")
        m = _DETAIL_DESC_RE.search(raw)
        if not m:
            raise RuntimeError(f"ukg detail: no Description JSON found for {ref!s}")
        try:
            desc = json.loads(m.group(1))  # decodes \uXXXX / \" / \\ correctly
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"ukg detail: Description JSON decode failed for {ref!s}") from e
        if not (isinstance(desc, str) and desc.strip()):
            raise RuntimeError(f"ukg detail: empty Description for {ref!s}")

        salary = _pay_range(raw)
        extra: list[str] = []
        for key, label in (
            ("WorkExperienceCriteria", "Experience required"),
            ("EducationCriteria", "Education required"),
        ):
            items = _json_array(raw, key)
            if items:
                text = _flatten_criteria_text(items)
                if text:
                    extra.append(f"{label}: {text}.")
        if not (salary or extra):
            return desc
        body = desc + ("\n\n" + " ".join(extra) if extra else "")
        return DetailFetch(text=body, salary=salary)

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = self._location(p)
        remote = RemoteType.REMOTE if (loc and loc.is_remote) else RemoteType.UNKNOWN
        employment = (
            EmploymentType.FULL_TIME if p.get("FullTime") is True else EmploymentType.UNKNOWN
        )
        desc = p.get("BriefDescription")
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("Title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            employment_type=employment,
            department=str(p.get("JobCategoryName") or "") or None,
            posted_at=self._date(p.get("PostedDate")),
            description_html=desc if isinstance(desc, str) and desc.strip() else None,
        )
