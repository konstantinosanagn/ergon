"""PeopleClick / PeopleFluent candidate-portal provider (careers.peopleclick.com).

A few large orgs (MIT) run their public careers site on PeopleClick. Its job list comes from a
JSON endpoint reachable with a plain HTTP cookie-primed session — NO browser:

1. GET  /careerscp/{client}/external/search/search.html        # sets JSESSIONID
2. POST /careerscp/{client}/external/results/searchResult.html # establishes the results context
3. GET  /careerscp/api/{client}/external/site/getJobs          # -> {totalHits, jobList:[...]}

Each job: ``jobPostId`` (id) and an ``attributes`` map carrying ``FLD_JP_POSTING_TITLE`` (title),
``JPM_LOCATION`` (e.g. "Cambridge, MA"), ``FLD_JP_DEPARTMENT``, plus the metadata the same map
already carries and normalize() maps here: ``JPM_DURATION`` ("Full-time (Hybrid)" — employment
type AND workplace), ``FLD_JPM_HIRING_RANGE_MIN``/``_MAX`` ("$127,500"), ``FLD_JPM_PAY_GRADE``,
``JPM_EXPERIENCE``, ``JPM_EDUCATION``, ``JP_POSTEDON`` ("Sep 11, 2026") and the full-JD
``JPM_DESCRIPTION``. PARTIAL by design: the API returns
only the first page (``hitsPerPage`` 50) and pagination is JS/session-driven server-side (no
plain-HTTP page param works), so we capture the first 50 of ``totalHits`` — entity-clean and far
better than the aggregator fallback, but not the whole board.

Token: ``"{client}"`` or ``"{client}|{Company Name}"`` (e.g. ``"client_mit|MIT"``). ``client`` is
the path segment (``client_mit``); per-job payload has no employer field, so the name is in the token.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..exceptions import ProviderError
from ..extract.base import ExtractInput
from ..extract.degree import degree_from_ats_vocab
from ..extract.level import level_from_ats_vocab
from ..extract.yoe import YoeExtractor
from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType, Salary
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PeopleClickProvider"]

_BASE = "https://careers.peopleclick.com/careerscp"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_YOE = YoeExtractor()
# JPM_DURATION is free text combining duration + workplace (e.g. "Full-time (Hybrid)"), so a
# substring search rather than a whole-value dict.
_EMPLOYMENT_PATTERNS: tuple[tuple[re.Pattern[str], EmploymentType], ...] = (
    (re.compile(r"\bfull[\s-]?time\b", re.I), EmploymentType.FULL_TIME),
    (re.compile(r"\bpart[\s-]?time\b", re.I), EmploymentType.PART_TIME),
    (re.compile(r"\bcontract(or)?\b", re.I), EmploymentType.CONTRACT),
    (re.compile(r"\btemp(orary)?\b", re.I), EmploymentType.TEMPORARY),
    (re.compile(r"\bseasonal\b", re.I), EmploymentType.TEMPORARY),
    (re.compile(r"\bintern(ship)?\b", re.I), EmploymentType.INTERNSHIP),
)
_CCY_SYMBOLS: tuple[tuple[str, str], ...] = (("$", "USD"), ("£", "GBP"), ("€", "EUR"))
# The same JPM_DURATION string also carries the workplace in parentheses ("Full-time (Hybrid)").
_WORKPLACE_PATTERNS: tuple[tuple[re.Pattern[str], RemoteType], ...] = (
    (re.compile(r"\bhybrid\b", re.I), RemoteType.HYBRID),
    (re.compile(r"\bremote\b|\bwork\s+from\s+home\b", re.I), RemoteType.REMOTE),
    (re.compile(r"\bon[\s-]?site\b|\bin[\s-]?person\b", re.I), RemoteType.ONSITE),
)


def _employment(text: str | None) -> EmploymentType:
    if not text:
        return EmploymentType.UNKNOWN
    for pattern, value in _EMPLOYMENT_PATTERNS:
        if pattern.search(text):
            return value
    return EmploymentType.UNKNOWN


def _workplace(text: str | None) -> RemoteType:
    if not text:
        return RemoteType.UNKNOWN
    for pattern, value in _WORKPLACE_PATTERNS:
        if pattern.search(text):
            return value
    return RemoteType.UNKNOWN


def _money(value: str | None) -> tuple[float | None, str | None]:
    if not value:
        return None, None
    currency = next((code for sym, code in _CCY_SYMBOLS if sym in value), None)
    digits = re.sub(r"[^\d.]", "", value)
    if not digits:
        return None, currency
    try:
        amount = float(digits)
    except ValueError:
        return None, currency
    return (amount if amount > 0 else None), currency


def _posted_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%b %d, %Y")
    except ValueError:
        return None


@register("peopleclick")
class PeopleClickProvider(BaseProvider):
    name = "peopleclick"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (niche ATS); avoid auto-claiming

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        return parts[0], (parts[1] if len(parts) > 1 and parts[1] else None)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        client, company = self._parse(token)
        if not client:
            return []
        search = f"{_BASE}/{client}/external/search/search.html"
        hdr = {"User-Agent": _UA, "Referer": search}
        try:
            # Prime the session, establish the results context, then read the jobs JSON.
            await fetcher.get_text(search, headers=hdr)
            await fetcher.request(
                "POST",
                f"{_BASE}/{client}/external/results/searchResult.html",
                data={"keyword": ""},
                headers=hdr,
            )
            data = await fetcher.get_json(
                f"{_BASE}/api/{client}/external/site/getJobs",
                headers={**hdr, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
        except Exception as exc:
            # never []: an empty list reads as "board is empty" and expires live rows.
            raise ProviderError("peopleclick", f"the board fetch failed for {token!r}") from exc
        jobs = data.get("jobList") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return []
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        for j in jobs:
            jid = str(j.get("jobPostId") or (j.get("identity") or {}).get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            attrs = j.get("attributes") or {}
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=company or client.replace("client_", ""),
                    token=token,
                    url=f"{_BASE}/{client}/external/jobdetails/{jid}",
                    payload=attrs,
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        a = raw.payload
        duration = self._clean(a.get("JPM_DURATION"))
        loc = self._clean(a.get("JPM_LOCATION"))
        locations: list[Location] = []
        remote = _workplace(duration)  # structured-ish field first, location text only as fallback
        if loc:
            is_remote = "remote" in loc.lower()
            locations.append(Location(raw=loc, is_remote=is_remote))
            if is_remote and remote is RemoteType.UNKNOWN:
                remote = RemoteType.REMOTE

        lo, lo_ccy = _money(self._clean(a.get("FLD_JPM_HIRING_RANGE_MIN")))
        hi, hi_ccy = _money(self._clean(a.get("FLD_JPM_HIRING_RANGE_MAX")))
        salary = (
            Salary(min_amount=lo, max_amount=hi, currency=lo_ccy or hi_ccy) if (lo or hi) else None
        )

        years_min, years_max = _YOE.extract(
            ExtractInput(title="", description_text=self._clean(a.get("JPM_EXPERIENCE")))
        )

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=self._clean(a.get("FLD_JP_POSTING_TITLE"))
            or self._clean(a.get("JPM_TITLE"))
            or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=_employment(duration),
            level=level_from_ats_vocab(self._clean(a.get("FLD_JPM_PAY_GRADE"))),
            department=self._clean(a.get("FLD_JP_DEPARTMENT")),
            salary=salary,
            years_experience_min=years_min,
            years_experience_max=years_max,
            degree_min=degree_from_ats_vocab(self._clean(a.get("JPM_EDUCATION"))),
            posted_at=_posted_at(self._clean(a.get("JP_POSTEDON"))),
            description_html=self._clean(a.get("JPM_DESCRIPTION")),
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None
