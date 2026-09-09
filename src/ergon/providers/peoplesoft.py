"""PeopleSoft "Candidate Gateway" / Fluid Recruiting career-site provider.

Many large universities/health systems (U. Missouri, Florida State, Augusta, the ND University
System, …) run PeopleSoft HCM Recruiting. The candidate-facing job search is a stateful Fluid app
behind the ``HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL`` component — there is NO JSON API, NO sitemap, and NO
JobPosting JSON-LD. But the search-results GRID is fully reachable with NO browser via PeopleSoft's
own ICAction postback protocol (replayable headlessly):

1. **Bootstrap GET** (persistent session, Chrome-TLS impersonation) the search URL. PeopleSoft
   auto-provisions an anonymous applicant session (cookies). Parse ``ICSID`` + ``ICStateNum`` from
   the HTML.
2. Two tenant shapes:
   * **A** — the 50-row grid renders on the GET itself (``?PAGE=HRS_APP_SCHJOB_FL``).
   * **B** — the GET returns a search splash; one ``ICAction=NAV_PB$0`` ("View All Jobs") POST
     returns the grid (``?FOCUS=Applicant&SiteId={n}``).
3. **Lazy-scroll pagination**: POST ``ICAction=HRS_AGNT_RSLT_I$hdown$0`` repeatedly; each response
   is CUMULATIVE (50→100→150…). Re-read ``ICStateNum`` from each response (it increments); ``ICSID``
   stays constant. ``ICAJAX=0`` is mandatory — ``ICAJAX=1`` returns a tiny state-only XML with no
   jobs. Loop until the rendered row count stops growing.

The grid carries everything: job-opening id, title, location, department, business unit, posted date.

Token: ``"{host}|{site}|{node}|{siteid}|{shape}|{Company}|{bu_filter}"``. ``siteid`` may be empty
(shape A). ``shape`` is ``A`` or ``B``. ``bu_filter`` is OPTIONAL — when the board is a shared
system (e.g. the ND University System hosts NDSU + Minot + Bismarck on one board), only rows whose
Business Unit contains this substring are kept, scoping the capture to one institution. Example:
``"erecruit.umsystem.edu|tamext|COLUM|6|B|University of Missouri System"``.
"""

from __future__ import annotations

import contextlib
import html as _htmlmod
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["PeopleSoftProvider"]

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Grid field id -> payload key. Title is parsed separately (it contains nested markup).
_GRID_FIELDS = {
    "id": "HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID",
    "location": "LOCATION",
    "department": "HRS_APP_JBSCH_I_HRS_DEPT_DESCR",
    "business_unit": "HRS_BU_DESCR",
    "posted": "SCH_OPENED",
}


def _ic(html: str, name: str) -> str | None:
    m = re.search(rf"id=['\"]{name}['\"]\s+value=['\"]([^'\"]*)['\"]", html) or re.search(
        rf"name=['\"]{name}['\"]\s+value=['\"]([^'\"]*)['\"]", html
    )
    return m.group(1) if m else None


@register("peoplesoft")
class PeopleSoftProvider(BaseProvider):
    name = "peoplesoft"

    MAX_SCROLLS = 60  # cap lazy-scroll postbacks (=3000 jobs) as a safety bound

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (needs host+site+node+siteid+shape); never auto-claims

    @staticmethod
    def _parse_token(token: str) -> dict[str, str]:
        p = [x.strip() for x in token.split("|")]
        p += [""] * (7 - len(p))
        return {
            "host": p[0],
            "site": p[1],
            "node": p[2] or "EMPLOYEE",
            "siteid": p[3],
            "shape": (p[4] or "A").upper(),
            "company": p[5],
            "bu_filter": p[6],
        }

    @staticmethod
    def _parse_grid(html: str) -> list[dict[str, Any]]:
        """Extract job rows from the PeopleSoft results grid. Returns one dict per row index."""
        rows: dict[str, dict[str, Any]] = {}
        for m in re.finditer(r"id='SCH_JOB_TITLE\$(\d+)'[^>]*>(.*?)</span>", html, re.S):
            idx, inner = m.group(1), m.group(2)
            title = _htmlmod.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
            if title:
                rows.setdefault(idx, {})["title"] = title
        for key, field in _GRID_FIELDS.items():
            for m in re.finditer(rf"id='{field}\$(\d+)'[^>]*>([^<]*)<", html):
                idx, val = m.group(1), _htmlmod.unescape(m.group(2)).strip()
                if idx in rows and val:
                    rows[idx][key] = val
        return [
            rows[i] for i in sorted(rows, key=int) if rows[i].get("title") and rows[i].get("id")
        ]

    def _post_body(self, html: str, action: str) -> dict[str, str]:
        return {
            "ICAJAX": "0",
            "ICType": "Panel",
            "ICElementNum": "0",
            "ICNAVTYPEDROPDOWN": "0",
            "ICStateNum": _ic(html, "ICStateNum") or "1",
            "ICSID": _ic(html, "ICSID") or "",
            "ICAction": action,
            "ICModelCancel": "0",
            "ICXPos": "0",
            "ICYPos": "0",
            "ResponsetoDiffFrame": "-1",
            "TargetFrameName": "None",
            "FacetPath": "None",
            "ICFocus": "",
            "ICSaveWarningFilter": "0",
            "ICChanged": "-1",
            "ICResubmit": "0",
        }

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        t = self._parse_token(token)
        if not (t["host"] and t["site"]):
            return []
        from curl_cffi.requests import AsyncSession

        psc = (
            f"https://{t['host']}/psc/{t['site']}/{t['node']}/HRMS/c/"
            "HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
        )
        hdr = {"User-Agent": _UA}
        async with AsyncSession(impersonate="chrome124", verify=False, timeout=45) as s:
            try:
                # Portal warm-up seeds session cookies (required by some tenants, harmless elsewhere).
                with contextlib.suppress(Exception):
                    await s.get(
                        f"https://{t['host']}/psp/{t['site']}/{t['node']}/HRMS/h/?tab=DEFAULT",
                        headers=hdr,
                    )
                if t["shape"] == "A":
                    get_url = psc + "?PAGE=HRS_APP_SCHJOB_FL"
                    html = (await s.get(get_url, headers=hdr)).text
                else:
                    get_url = psc + f"?FOCUS=Applicant&SiteId={t['siteid']}"
                    html = (await s.get(get_url, headers=hdr)).text
                    if not self._parse_grid(html):  # splash -> "View All Jobs"
                        html = await self._post(s, psc, html, "NAV_PB$0", get_url, hdr)
                rows = self._parse_grid(html)
                for _ in range(self.MAX_SCROLLS):
                    nxt = await self._post(s, psc, html, "HRS_AGNT_RSLT_I$hdown$0", get_url, hdr)
                    nrows = self._parse_grid(nxt)
                    if len(nrows) <= len(rows):
                        break
                    html, rows = nxt, nrows
            except Exception:
                return []

        bu = t["bu_filter"].lower()
        out: list[RawJob] = []
        seen: set[str] = set()
        for rec in rows:
            if bu and bu not in (rec.get("business_unit") or "").lower():
                continue
            jid = str(rec.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            out.append(self._to_raw(rec, t, jid, token))
            if query.limit is not None and len(out) >= query.limit:
                break
        return out

    async def _post(
        self, s: Any, url: str, html: str, action: str, referer: str, hdr: dict[str, str]
    ) -> str:
        h = {**hdr, "Content-Type": "application/x-www-form-urlencoded", "Referer": referer}
        resp = await s.post(url, data=self._post_body(html, action), headers=h)
        return str(resp.text)

    def _to_raw(self, rec: dict[str, Any], t: dict[str, str], jid: str, token: str) -> RawJob:
        apply_url = (
            f"https://{t['host']}/psc/{t['site']}/{t['node']}/HRMS/c/"
            f"HRS_HRAM_FL.HRS_APP_JBPST_FL.GBL?Page=HRS_APP_JBPST_FL&Action=U"
            f"&SiteId={t['siteid']}&JobOpeningId={jid}"
        )
        return RawJob(
            source=self.name,
            source_job_id=jid,
            company=t["company"] or t["host"],
            token=token,
            url=apply_url,
            payload=rec,
        )

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc_raw = str(p.get("location") or "").strip()
        locations: list[Location] = []
        if loc_raw:
            locations.append(Location(raw=loc_raw, is_remote="remote" in loc_raw.lower()))
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
            department=p.get("department") or None,
            posted_at=self._date(p.get("posted")),
            raw=raw.payload,
        )

    @staticmethod
    def _date(raw: Any) -> datetime | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
        return None
