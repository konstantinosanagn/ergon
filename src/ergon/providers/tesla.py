"""Tesla careers provider (the bespoke ``tesla.com`` careers API).

Tesla runs its own careers SPA, not a standard ATS. The whole board (~6.8k reqs) is served in a
single denormalized JSON document::

    GET https://www.tesla.com/cua-api/apps/careers/state

The endpoint sits behind a WAF: a cold request 403s, and rapid repeats earn a JS-challenge 429.
The runtime fetch therefore (1) impersonates Chrome's TLS fingerprint via curl_cffi and (2) primes
the session by GETting ``/careers/search/`` first (sets the clearance cookies) before the API call.
One paced call per run from a clean IP succeeds; this is a single-shot full-board pull, so we never
hammer it.

The payload is denormalized against lookup tables::

    {"lookup": {"locations": {"<id>": "Palo Alto, California", …},
                "departments": {"<id>": "Tesla AI", …},
                "types": {"1": "fulltime", "2": "parttime", "3": "intern", …}},
     "listings": [{"id", "t" (title), "dp" (dept id), "l" (location id), "y" (type id), …}, …]}

Each listing's ``l``/``dp``/``y`` are resolved against ``lookup`` at fetch time, so ``normalize`` is
trivial. Token is the sentinel ``"tesla"`` (seed-only; the board has no per-tenant token).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["TeslaProvider"]

_STATE_URL = "https://www.tesla.com/cua-api/apps/careers/state"
_PRIME_URL = "https://www.tesla.com/careers/search/"
_JOB_URL = "https://www.tesla.com/careers/search/job/{id}"
_EMPLOYMENT = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
}


@register("tesla")
class TeslaProvider(BaseProvider):
    name = "tesla"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        u = url_or_host.lower()
        if "tesla.com" in u and (
            "career" in u or "cua-api" in u or u.rstrip("/").endswith("tesla.com")
        ):
            return "tesla"
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome124", verify=False, timeout=45) as s:
            try:
                # Prime: the careers page sets the WAF-clearance cookies the API call needs.
                await s.get(
                    _PRIME_URL, headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"}
                )
                resp = await s.get(
                    _STATE_URL,
                    headers={
                        "Accept": "*/*",
                        "Referer": _PRIME_URL,
                        "x-requested-with": "XMLHttpRequest",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
            except Exception:
                return []
        return self._raws_from_state(data, token, query.limit)

    @staticmethod
    def _raws_from_state(data: Any, token: str, limit: int | None) -> list[RawJob]:
        if not isinstance(data, dict):
            return []
        listings = data.get("listings") or []
        lookup = data.get("lookup") or {}
        locs = lookup.get("locations") or {}
        deps = lookup.get("departments") or {}
        types = lookup.get("types") or {}
        raws: list[RawJob] = []
        seen: set[str] = set()
        for rec in listings:
            jid = str(rec.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            raws.append(
                RawJob(
                    source="tesla",
                    source_job_id=jid,
                    company="Tesla",
                    token=token,
                    url=_JOB_URL.format(id=jid),
                    payload={
                        "title": rec.get("t") or "",
                        "location": locs.get(str(rec.get("l")))
                        if rec.get("l") is not None
                        else None,
                        "department": deps.get(str(rec.get("dp")))
                        if rec.get("dp") is not None
                        else None,
                        "type": types.get(str(rec.get("y"))) if rec.get("y") is not None else None,
                    },
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p: dict[str, Any] = raw.payload
        loc_raw = (p.get("location") or "").strip()
        locations = []
        if loc_raw:
            locations.append(Location(raw=loc_raw, is_remote="remote" in loc_raw.lower()))
        remote = RemoteType.REMOTE if (locations and locations[0].is_remote) else RemoteType.UNKNOWN
        employment = _EMPLOYMENT.get((p.get("type") or "").strip().lower(), EmploymentType.UNKNOWN)
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            employment_type=employment,
            department=p.get("department"),
            salary=None,
            posted_at=None,
            updated_at=None,
            description_html=None,
            description_text=None,
            raw=raw.payload,
        )
