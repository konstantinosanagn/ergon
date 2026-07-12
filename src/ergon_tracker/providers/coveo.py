"""Coveo-for-Sitecore provider — for enterprise career sites that index jobs in Coveo and expose
a SAME-ORIGIN search proxy (e.g. SLB -> ``careers.slb.com/coveo/rest/search/v2``).

These sites are fully JS-rendered (no ATS host in static HTML, no own-domain JSON API), but their
Coveo-for-Sitecore integration proxies the Coveo Search API through the careers domain itself and
injects the search token server-side — so the job index is reachable with **plain HTTP, no browser
and no captured token**. We POST to ``https://{host}/coveo/rest/search/v2`` filtering to the jobs
source via an advanced query (``aq=@source==...``) and paginate with ``firstResult``.

Token: ``"{host}"`` (the job source is auto-detected from a probe query) or, more reliably,
``"{host}|{source}"`` (e.g. ``"careers.slb.com|ATS_Jobs_Source - Prod"``) when the Coveo source
name is known. The provider is OPT-IN (``matches()`` resolves only an explicit ``coveo:`` scheme)
so it never auto-claims a host.

DIRECT mode (``"direct:{key}"``): some sites (UST) don't proxy Coveo — they call the DIRECT Coveo
cloud host (``{org}.org.coveo.com``) with a short-lived search token minted from a same-origin
endpoint, and scope jobs via a ``searchHub`` (not a source filter). Such a board is configured in
``registry/data/coveo_direct.json`` keyed by ``{key}``: ``{org, mint_url, warm_url, search_hub}``.
We mint the token (cookie-warm + the page's Referer) and paginate the direct host. NOTE: these
sites front Cloudflare bot-management that rejects HTTP/2 + non-browser UAs, so this path uses a
dedicated HTTP/1.1 client with a browser User-Agent (the shared fetcher is HTTP/2).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from ..models import JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..models import SearchQuery

__all__ = ["CoveoProvider"]

_SCHEME_RE = re.compile(r"^coveo:", re.IGNORECASE)
_DIRECT_RE = re.compile(r"^direct:", re.IGNORECASE)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_PER_PAGE = 50
# A Coveo result is a JOB (not a site page) when its source/uri looks job-ish. Used to auto-detect
# the jobs source when the token doesn't name one.
_JOB_HINT = re.compile(r"job|ats|career|requis|vacanc", re.I)


def _search_url(host: str) -> str:
    return f"https://{host.rstrip('/')}/coveo/rest/search/v2"


@register("coveo")
class CoveoProvider(BaseProvider):
    name = "coveo"

    MAX_PAGES = 60  # 60 * 50 = 3000 jobs ceiling

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        m = _SCHEME_RE.match(url_or_host.strip())
        if not m:
            return None
        tok = url_or_host.strip()[m.end() :].strip()
        return tok or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        token = _SCHEME_RE.sub("", token.strip()).strip()
        if not token:
            return []
        if _DIRECT_RE.match(token):
            return await self._fetch_direct(_DIRECT_RE.sub("", token).strip(), query)
        host, _, source = token.partition("|")
        host, source = host.strip().lower(), source.strip()
        url = _search_url(host)

        if not source:
            source = await self._detect_source(url, fetcher)
            if not source:
                return []
        aq = f'@source=="{source}"'

        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        for page in range(self.MAX_PAGES):
            body = {
                "q": query.keywords or "",
                "aq": aq,
                "numberOfResults": _PER_PAGE,
                "firstResult": page * _PER_PAGE,
            }
            try:
                data = await fetcher.post_json(url, json=body)
            except Exception:
                break
            results = data.get("results", []) if isinstance(data, dict) else []
            if not results:
                break
            new = 0
            for it in results:
                if not isinstance(it, dict):
                    continue
                raw = it.get("raw") or {}
                jid = str(raw.get("permanentid") or raw.get("rowid") or it.get("uniqueId") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                new += 1
                raws.append(
                    RawJob(
                        source=self.name,
                        source_job_id=jid,
                        company=host.split(".")[1] if host.count(".") >= 1 else host,
                        token=token,
                        url=it.get("clickUri") or raw.get("clickableuri") or raw.get("uri"),
                        payload={**raw, "_title": it.get("title") or raw.get("title")},
                    )
                )
                if limit is not None and len(raws) >= limit:
                    return raws[:limit]
            if new == 0:
                break
        return raws

    async def _detect_source(self, url: str, fetcher: AsyncFetcher) -> str:
        """Find the Coveo source carrying jobs: the one whose results have job-ish uris/sources."""
        try:
            data = await fetcher.post_json(
                url, json={"q": "", "numberOfResults": _PER_PAGE, "firstResult": 0}
            )
        except Exception:
            return ""
        tally: dict[str, int] = {}
        for it in data.get("results", []) if isinstance(data, dict) else []:
            raw = it.get("raw") or {}
            src = raw.get("source") or it.get("source") or ""
            uri = str(it.get("clickUri") or raw.get("uri") or "")
            if src and (_JOB_HINT.search(src) or _JOB_HINT.search(uri)):
                tally[src] = tally.get(src, 0) + 1
        return max(tally, key=lambda s: tally[s]) if tally else ""

    @staticmethod
    def _direct_specs() -> dict[str, dict[str, Any]]:
        try:
            text = (files("ergon_tracker.registry.data") / "coveo_direct.json").read_text("utf-8")
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    async def _fetch_direct(self, key: str, query: SearchQuery) -> list[RawJob]:
        """Direct Coveo cloud host with a minted search token + searchHub scope (UST). Uses a
        dedicated HTTP/1.1 + browser-UA client because the mint endpoint sits behind Cloudflare
        bot-management that rejects the shared fetcher's HTTP/2 fingerprint."""
        spec = self._direct_specs().get(key)
        if not spec:
            return []
        import httpx

        org = spec["org"]
        company = spec.get("company") or key
        search_hub = spec.get("search_hub")
        search_url = f"https://{org}.org.coveo.com/rest/search/v2?organizationId={org}"
        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        try:
            async with httpx.AsyncClient(
                timeout=25.0,
                follow_redirects=True,
                http2=False,
                headers={"User-Agent": _BROWSER_UA},
            ) as c:
                if spec.get("warm_url"):
                    await c.get(spec["warm_url"])
                mint = await c.get(
                    spec["mint_url"],
                    params={spec.get("mint_param", "currentDate"): "1"},
                    headers={
                        "Referer": spec.get("warm_url", ""),
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/plain, */*",
                    },
                )
                token = mint.json().get("token")
                if not token:
                    return []
                auth = {"authorization": f"Bearer {token}"}
                for page in range(self.MAX_PAGES):
                    body: dict[str, Any] = {
                        "q": query.keywords or "",
                        "numberOfResults": _PER_PAGE,
                        "firstResult": page * _PER_PAGE,
                    }
                    if search_hub:
                        body["searchHub"] = search_hub
                    resp = await c.post(search_url, json=body, headers=auth)
                    results = resp.json().get("results", []) if resp.status_code == 200 else []
                    if not results:
                        break
                    new = 0
                    for it in results:
                        raw = it.get("raw") or {}
                        jid = str(
                            raw.get("jobid") or raw.get("permanentid") or it.get("uniqueId") or ""
                        )
                        if not jid or jid in seen:
                            continue
                        seen.add(jid)
                        new += 1
                        raws.append(
                            RawJob(
                                source=self.name,
                                source_job_id=jid,
                                company=company,
                                token=f"direct:{key}",
                                url=it.get("clickUri") or raw.get("uri"),
                                payload={**raw, "_title": it.get("title") or raw.get("title")},
                            )
                        )
                        if limit is not None and len(raws) >= limit:
                            return raws[:limit]
                    if new == 0:
                        break
        except Exception:
            return raws
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        title = str(p.get("_title") or p.get("title") or "")
        city = self._scalar(p.get("city"))
        country = self._scalar(p.get("country"))
        loc_label = ", ".join(x for x in (city, country) if x and x.lower() != "multi-location")
        locations: list[Location] = []
        remote = RemoteType.UNKNOWN
        if loc_label or city:
            label = loc_label or city
            is_remote = "remote" in label.lower()
            locations.append(Location(raw=label, is_remote=is_remote))
            if is_remote:
                remote = RemoteType.REMOTE

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=locations,
            remote=remote,
            # proxy mode keys these "category"/"description"; direct mode (UST-style boards)
            # keys them "obu"/"data" instead — read proxy first, direct as fallback so both
            # modes populate department/description_html from this one shared normalize().
            department=self._clean(p.get("category") or p.get("obu")),
            posted_at=self._date(p.get("date")),
            description_html=self._clean(p.get("description") or p.get("data")),
            raw=raw.payload,
        )

    @staticmethod
    def _scalar(v: Any) -> str:
        """Coveo multi-value fields arrive as lists; collapse to a single trimmed string."""
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        return str(v).strip() if v is not None else ""

    @staticmethod
    def _clean(v: Any) -> str | None:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        return v.strip() if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _date(v: Any) -> datetime | None:
        # Coveo dates are epoch millis or ISO strings.
        if isinstance(v, (int, float)) and v > 0:
            try:
                return datetime.utcfromtimestamp(v / 1000)
            except (ValueError, OverflowError, OSError):
                return None
        if isinstance(v, str) and v.strip():
            try:
                return datetime.fromisoformat(v.strip()[:19].replace("Z", ""))
            except ValueError:
                return None
        return None
