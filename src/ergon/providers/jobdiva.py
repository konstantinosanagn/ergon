"""JobDiva provider — the dominant ATS/VMS for IT-staffing and contract-recruiting firms
(many of them H-1B mega-sponsors: Axelon, Natsoft, Diaspark, etc.).

Each staffing firm runs a public *candidate portal* at ``www1.jobdiva.com/portal/?a={hash}``.
The portal is a JS SPA, but its data comes from a plain JSON API on ``ws.jobdiva.com`` that we
replicate at runtime with three same-origin HTTP calls — NO browser:

1. **teamid** — GET the portal page; its HTML embeds ``teamid=<N>`` (the firm's JobDiva company
   id). Can be supplied in the token to skip this hop.
2. **token mint** — ``GET ws.jobdiva.com/candPortal/rest/auth/a?a={hash}`` returns a short-lived
   session ``{"token": ...}``. (No real auth: the Basic header the browser sends is ignored.)
3. **search** — ``POST ws.jobdiva.com/candPortal/rest/job/searchjobsportal`` with header
   ``portalid: {teamid}`` + ``token`` and a form body carrying ``portalID={teamid}`` and a
   ``from``/``to`` window. The response is ``{"total": N, "data": [...]}``; the server returns
   rows ``1..to`` (``from`` is a no-op), so a single request with ``to=total`` yields everything.
   ``portalid`` MUST equal the portal's own teamid or the API 401s — the session is teamid-bound.

The per-job ``company`` field is almost always ``"Confidential"`` (the staffing firm hides its
end-client), so the firm name is carried in the token, not read from the payload.

Token: ``"{hash}"`` or ``"{hash}|{teamid}"`` or ``"{hash}|{teamid}|{Company Name}"``. The teamid
is auto-discovered from the portal page when the middle field is empty (``"{hash}||Acme Corp"``).
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["JobDivaProvider"]

_PORTAL = "https://www1.jobdiva.com/portal/?a={hash}"
_AUTH = "https://ws.jobdiva.com/candPortal/rest/auth/a"
_SEARCH = "https://ws.jobdiva.com/candPortal/rest/job/searchjobsportal"
_TEAMID = re.compile(r"teamid['\"=:\s]+(\d+)", re.I)
# The API returns cumulative rows ``1..to`` regardless of ``from``, but rejects (400) any window
# whose span ``to-from+1`` exceeds ~200. So a single call with a small window anchored at the top
# (``from = to-199, to = total``) returns the whole list in one shot.
_WINDOW = 200


def _body(teamid: str, frm: int, to: int) -> str:
    return (
        f"city=&country=&from={frm}&jobCategories=&jobDivisions=&jobTypes=&keywords="
        f"&miles=&onsiteFlex=&portalID={teamid}&qualifications=&states=&to={to}"
        f"&unit=mi&zipcode="
    )


@register("jobdiva")
class JobDivaProvider(BaseProvider):
    name = "jobdiva"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower()
        return host if host.endswith("jobdiva.com") else None

    @staticmethod
    def _parse(token: str) -> tuple[str, str | None, str | None]:
        parts = [p.strip() for p in token.split("|")]
        h = parts[0]
        teamid = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
        company = parts[2] if len(parts) > 2 and parts[2] else None
        return h, teamid, company

    async def _teamid(self, h: str, fetcher: AsyncFetcher) -> str | None:
        try:
            page = await fetcher.get_text(_PORTAL.format(hash=h))
        except Exception:
            return None
        m = _TEAMID.search(page)
        return m.group(1) if m else None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        h, teamid, company = self._parse(token)
        if not h:
            return []
        if not teamid:
            teamid = await self._teamid(h, fetcher)
        if not teamid:
            return []
        # Mint a session token (Basic header the browser sends is ignored server-side).
        try:
            auth = await fetcher.get_json(
                _AUTH, params={"a": h}, headers={"portalid": "1", "compid": "-1", "a": h}
            )
        except Exception:
            return []
        sess = auth.get("token") if isinstance(auth, dict) else None
        if not sess:
            return []
        hdr = {
            "portalid": teamid,
            "compid": "0",
            "token": sess,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        limit = query.limit

        async def _search(frm: int, to: int) -> dict[str, Any] | None:
            try:
                resp = await fetcher.request(
                    "POST", _SEARCH, content=_body(teamid, frm, to), headers=hdr
                )
                resp.raise_for_status()
                payload = resp.json()
                return payload if isinstance(payload, dict) else None
            except Exception:
                return None

        # Probe the total with a 1-row window, then pull everything in one top-anchored window.
        probe = await _search(1, 1)
        if probe is None:
            return []
        total = int(probe.get("total") or 0)
        if total <= 0:
            return []
        to = total if limit is None else min(total, limit)
        page = await _search(max(1, to - _WINDOW + 1), to)
        rows = (page or {}).get("data") or []

        seen: set[str] = set()
        raws: list[RawJob] = []
        for j in rows:
            jid = str(j.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=company or self._clean(j.get("company")) or h,
                    token=token,
                    url=f"https://www1.jobdiva.com/portal/?a={h}&compid=0&jobid={jid}",
                    payload=j,
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        loc = self._clean(p.get("location"))
        names: list[str] = []
        for x in [loc, *(p.get("otherLocations") or [])]:
            c = self._clean(x)
            if c and c not in names:
                names.append(c)
        locations = [Location(raw=n, is_remote="remote" in n.lower()) for n in names]
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=self._remote(p),
            posted_at=self._date(p.get("postDate")),
            description_html=self._clean(p.get("jobDescription")),
            employment_type=EmploymentType.UNKNOWN,
        )

    @staticmethod
    def _remote(p: dict[str, Any]) -> RemoteType:
        v = str(p.get("workingRemote") or "").lower()
        if "hybrid" in v:
            return RemoteType.HYBRID
        if "remote" in v or v in {"yes", "y", "true", "1"}:
            return RemoteType.REMOTE
        if "onsite" in v or "on-site" in v or v in {"no", "n", "false", "0"}:
            return RemoteType.ONSITE
        return RemoteType.UNKNOWN

    @staticmethod
    def _clean(v: object) -> str | None:
        if isinstance(v, str):
            s = _html.unescape(v).strip()
            return s or None
        return None

    @staticmethod
    def _date(v: object) -> datetime | None:
        if isinstance(v, (int, float)) and v > 0:
            try:
                return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                return None
        return None
