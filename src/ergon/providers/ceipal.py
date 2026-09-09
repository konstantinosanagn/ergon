"""Ceipal career-portal provider (careerapi.ceipal.com).

Ceipal is the dominant ATS for US/Indian IT-staffing firms — many of them are H-1B sponsors. Their
public career-portal widget (``jobsapi.ceipal.com/APISource/widget.js``, embedded on the firm's
site via ``data-ceipal-api-key`` / ``data-ceipal-career-portal-id``) is fetchable over plain HTTP
with NO browser, once two non-obvious requirements are met:

* The job-data endpoint ``POST careerapi.ceipal.com/{api_key}/CareerPortalJobPostings/?page=N`` is
  **referer-gated** — it 400s "not allowed from outside the Career Portal" unless the request carries
  ``Referer``/``Origin`` of **``https://jobsapi.ceipal.com/``** (the widget's own iframe host, NOT
  the firm's domain).
* The body is **multipart/form-data** carrying ``api_key``, ``cp_id``, ``method`` =
  ``CareerPortalJobPostings``, ``from_career_portal`` = 1, ``page``, plus empty search fields. A JSON
  body 500s.

Response: ``{count, num_pages, host, results:[{job_id, position_title, public_job_title, city,
state, country, created, client, ...}]}`` (20/page). Per-job ``client`` is the firm, so the name is
carried in the token.

Token: ``"{api_key}|{cp_id}|{Company}"`` — the two public widget keys (base64-ish strings read off
the firm's careers page) plus the firm label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["CeipalProvider"]

_BASE = "https://careerapi.ceipal.com"
_REFERER = "https://jobsapi.ceipal.com/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@register("ceipal")
class CeipalProvider(BaseProvider):
    name = "ceipal"

    MAX_PAGES = 200  # bound full pulls (=4000 jobs) when no limit is given

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        return None  # seed-only (needs the two public keys + name); never auto-claims

    @staticmethod
    def _parse(token: str) -> tuple[str, str, str | None]:
        parts = [p.strip() for p in token.split("|")]
        api_key = parts[0] if parts else ""
        cp_id = parts[1] if len(parts) > 1 else ""
        company = parts[2] if len(parts) > 2 and parts[2] else None
        return api_key, cp_id, company

    def _form(self, api_key: str, cp_id: str, page: int) -> dict[str, tuple[None, str]]:
        fields = {
            "from_chatbot": "0",
            "page": str(page),
            "api_key": api_key,
            "method": "CareerPortalJobPostings",
            "cp_id": cp_id,
            "from_career_portal": "1",
            "searchkey": "",
            "country": "",
            "state": "",
            "city": "",
        }
        return {k: (None, v) for k, v in fields.items()}

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        api_key, cp_id, company = self._parse(token)
        if not api_key or not cp_id:
            return []
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": _REFERER,
            "Origin": "https://jobsapi.ceipal.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        url = f"{_BASE}/{api_key}/CareerPortalJobPostings/"
        limit = query.limit
        seen: set[str] = set()
        raws: list[RawJob] = []
        num_pages: int | None = None
        for page in range(1, self.MAX_PAGES + 1):
            try:
                resp = await fetcher.request(
                    "POST",
                    url,
                    params={"page": page},
                    files=self._form(api_key, cp_id, page),
                    headers=headers,
                )
                data = resp.json()
            except Exception:
                break
            if not isinstance(data, dict):
                break
            if num_pages is None and isinstance(data.get("num_pages"), int):
                num_pages = data["num_pages"]
            results = data.get("results")
            if not isinstance(results, list) or not results:
                break
            host = data.get("host") or _BASE
            new = 0
            for j in results:
                if not isinstance(j, dict):
                    continue
                jid = str(j.get("job_id") or j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=company or j.get("client") or "",
                        token=token,
                        url=f"{host.rstrip('/')}/job/{jid}",
                        payload=j,
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws
            if new == 0 or (num_pages is not None and page >= num_pages):
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        j = raw.payload
        parts = [self._clean(j.get(k)) for k in ("city", "state", "country")]
        loc = ", ".join(p for p in parts if p)
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        # remote_opportunities: "1" = remote per Ceipal's enum; also honor a "remote" location.
        is_remote = str(j.get("remote_opportunities") or "") == "1" or "remote" in loc.lower()
        if loc:
            locations.append(Location(raw=loc, is_remote=is_remote))
        if is_remote:
            remote = RemoteType.REMOTE
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=self._clean(j.get("position_title"))
            or self._clean(j.get("public_job_title"))
            or "",
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            department=self._clean(j.get("job_code")),
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None
