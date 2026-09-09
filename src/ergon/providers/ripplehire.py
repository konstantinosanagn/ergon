"""RippleHire provider — the candidate-career-site ATS used by several large IT-services firms
(Mphasis, CitiusTech, …). Each firm runs a public career site at ``{firm}.ripplehire.com`` whose
job list comes from a single unauthenticated endpoint we replicate at runtime with plain HTTP
(NO browser):

    POST https://{firm}.ripplehire.com/candidate/candidatejobsearch
    Content-Type: application/x-www-form-urlencoded
    body: careerSiteUrlParams={"page":N,"search":"*:*","token":"{rh_token}","source":"CAREERSITE","pagesize":50}&lang=en

The endpoint content-negotiates: with an ``Accept: application/json`` header (which AsyncFetcher
sends) it returns JSON ``{"totalJobCount": N, "jobVoList": [...]}`` (one object per job: ``jobSeq``
id, ``jobTitle``, ``locations`` string, ``jobReqExp``, ``numOfOpening``, ``jobCode`` = the end
client). Pagination is ``page`` 0,1,2…; we walk pages until we've collected ``totalJobCount`` ids.

The ``{rh_token}`` is a public per-firm site token embedded in the firm's career page URL (one-time
discovery). The per-job ``jobCode`` is the END CLIENT (e.g. CitiusTech reqs labeled "Novartis"), so
the firm name is carried in the token, not read from the payload.

Token: ``"{firm}|{rh_token}|{Company Display Name}"`` (e.g. ``"mphasis|ty4DfyWddnOrtpclQeia|Mphasis"``).
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["RippleHireProvider"]

_URL = "https://{firm}.ripplehire.com/candidate/candidatejobsearch"
_PAGE = 50
_MAX_PAGES = 200


@register("ripplehire")
class RippleHireProvider(BaseProvider):
    name = "ripplehire"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower()
        return host if host.endswith(".ripplehire.com") else None

    @staticmethod
    def _parse(token: str) -> tuple[str, str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        firm = parts[0].split(".")[0].lower() if parts else ""
        rh = parts[1] if len(parts) > 1 else ""
        company = parts[2] if len(parts) > 2 and parts[2] else None
        return firm, rh, company

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        firm, rh, company = self._parse(token)
        if not firm or not rh:
            return []
        url = _URL.format(firm=firm)
        hdr = {"Content-Type": "application/x-www-form-urlencoded"}
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        total: int | None = None
        for page in range(_MAX_PAGES):
            params = {
                "page": page,
                "search": "*:*",
                "token": rh,
                "source": "CAREERSITE",
                "pagesize": _PAGE,
            }
            body = f"careerSiteUrlParams={_json.dumps(params)}&lang=en"
            try:
                resp = await fetcher.request("POST", url, content=body, headers=hdr)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                break
            if not isinstance(data, dict):
                break
            if total is None:
                tc = data.get("totalJobCount")
                total = int(tc) if isinstance(tc, (int, str)) and str(tc).isdigit() else None
            items = data.get("jobVoList") or []
            if not isinstance(items, list) or not items:
                break
            grew = False
            for it in items:
                jid = str(it.get("jobSeq") or "").strip()
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                grew = True
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=company or firm,
                        token=token,
                        url=f"https://{firm}.ripplehire.com/candidate/job/{jid}",
                        payload={
                            "title": str(it.get("jobTitle") or "").strip(),
                            "location": str(it.get("locations") or "").strip(),
                            "experience": str(it.get("jobReqExp") or "").strip(),
                            "client": str(it.get("jobCode") or "").strip(),
                        },
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws
            if not grew or (total is not None and len(seen) >= total):
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
