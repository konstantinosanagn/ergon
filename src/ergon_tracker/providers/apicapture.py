"""Captured-API replay provider — for "proxied" giants that have no reachable ATS host but DO
expose a public, no-auth JSON/GraphQL job API on their own domain (Goldman -> api-higher.gs.com).

A Playwright capture pass records the SPA's job-data request verbatim — URL, method, body — plus
dot-paths into the response (records, total) and a per-field map. We replay that request exactly,
mutating only the page field, and extract jobs generically. Specs live in
``registry/data/apicapture.json`` keyed by token; the provider is OPT-IN (``matches()`` only
resolves an explicit ``apicapture:`` scheme) so it never auto-claims a host.

Token: a spec key (e.g. ``"goldmansachs"``). Fields absent from the capture normalize to ``None``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher

__all__ = ["ApiCaptureProvider"]

_SCHEME_RE = re.compile(r"^api(?:capture)?:", re.IGNORECASE)
_EMPLOYMENT = {
    "full_time": EmploymentType.FULL_TIME,
    "full-time": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.TEMPORARY,
}


def _load_specs() -> dict[str, dict[str, Any]]:
    try:
        text = (files("ergon_tracker.registry.data") / "apicapture.json").read_text("utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _extract_embed(html: str, script_id: str) -> Any:
    """Parse the JSON embedded in a ``<script id="{script_id}">…</script>`` tag (Next.js
    ``__NEXT_DATA__`` and similar server-rendered data islands). Returns ``{}`` if not found."""
    m = re.search(
        r'<script[^>]*\bid="' + re.escape(script_id) + r'"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        return {}
    try:
        return json.loads(m.group(1).strip())
    except ValueError:
        return {}


def _dig(obj: Any, path: list[Any]) -> Any:
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _set(obj: Any, path: list[Any], value: Any) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def _set_query(url: str, param: str, value: int) -> str:
    """Return ``url`` with query ``param`` set to ``value`` (for GET pagination)."""
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    qs[param] = [str(value)]
    return urlunsplit(parts._replace(query=urlencode(qs, doseq=True)))


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class _FetcherCaller:
    """Default request path: the shared AsyncFetcher (HTTP/2, rate-limited, retried)."""

    def __init__(self, fetcher: AsyncFetcher) -> None:
        self._f = fetcher

    async def open(self) -> None: ...
    async def close(self) -> None: ...

    async def post_json(self, url: str, body: Any, headers: dict[str, str] | None) -> Any:
        return await self._f.post_json(url, json=body, headers=headers)

    async def get_json(self, url: str, headers: dict[str, str] | None) -> Any:
        return await self._f.get_json(url, headers=headers)

    async def get_text(self, url: str, headers: dict[str, str] | None) -> str:
        return await self._f.get_text(url, headers=headers)


class _BrowserCaller:
    """Dedicated HTTP/1.1 + browser-UA client for own-domain APIs behind bot-management that
    rejects the shared fetcher's HTTP/2 fingerprint (TikTok USDS). Cookie-warms ``warm_url``."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._client: Any = None

    async def open(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, http2=False, headers={"User-Agent": _BROWSER_UA}
        )
        warm = self._spec.get("warm_url")
        if warm:
            with contextlib.suppress(Exception):
                await self._client.get(warm)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def post_json(self, url: str, body: Any, headers: dict[str, str] | None) -> Any:
        r = await self._client.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    async def get_json(self, url: str, headers: dict[str, str] | None) -> Any:
        r = await self._client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

    async def get_text(self, url: str, headers: dict[str, str] | None) -> str:
        r = await self._client.get(url, headers=headers)
        r.raise_for_status()
        text: str = r.text
        return text


@register("apicapture")
class ApiCaptureProvider(BaseProvider):
    name = "apicapture"

    MAX_PAGES = 200

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        m = _SCHEME_RE.match(url_or_host.strip())
        if not m:
            return None
        tok = url_or_host.strip()[m.end() :].strip()
        return tok or None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        token = _SCHEME_RE.sub("", token.strip()).strip()
        spec = _load_specs().get(token)
        if not spec:
            return []
        url, method = spec["url"], spec.get("method", "POST").upper()
        page_path = spec.get("page_path") or []
        page_param = spec.get("page_param")  # GET: pagination via this query param
        page_start = int(spec.get("page_start", 0))
        page_step = int(spec.get("page_step", 1))  # add page*step to the page field (offset-style)
        size = int(spec.get("size", 50))
        rec_path, tot_path = spec.get("records_path") or [], spec.get("total_path") or []
        unwrap = spec.get("record_unwrap") or []  # per-record inner path (e.g. ["data"] wrappers)
        # Server-rendered Next.js/Nuxt sites embed their data as JSON in a <script>; ``embed_script``
        # is that tag's id (e.g. "__NEXT_DATA__"). We fetch HTML and parse that blob as the response.
        embed = spec.get("embed_script")
        company = spec.get("company") or token
        headers = spec.get("headers") or None

        # Some own-domain APIs sit behind bot-management that rejects the shared fetcher's HTTP/2 +
        # bot-UA (TikTok USDS -> 405). A browser_http1 spec routes requests through a dedicated
        # HTTP/1.1 + browser-UA client instead (cookie-warmed if warm_url is set).
        client = _BrowserCaller(spec) if spec.get("browser_http1") else _FetcherCaller(fetcher)

        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        total: int | None = None
        await client.open()
        try:
            for page in range(self.MAX_PAGES):
                offset = page_start + page * page_step
                body = copy.deepcopy(spec.get("body"))
                if page_path:
                    _set(body, page_path, offset)
                if spec.get("size_path"):
                    _set(body, spec["size_path"], size)
                try:
                    if embed:
                        page_url = _set_query(url, page_param, offset) if page_param else url
                        html = await client.get_text(page_url, headers)
                        data = _extract_embed(html, embed)
                    elif method == "POST":
                        data = await client.post_json(url, body, headers)
                    elif page_param:
                        data = await client.get_json(_set_query(url, page_param, offset), headers)
                    else:
                        data = await client.get_json(url, headers)
                except Exception:
                    break
                if total is None:
                    t = _dig(data, tot_path)
                    total = t if isinstance(t, int) else None
                records = _dig(data, rec_path)
                if not isinstance(records, list) or not records:
                    break
                new = 0
                for rec in records:
                    if unwrap:
                        rec = _dig(rec, unwrap)
                    if not isinstance(rec, dict):
                        continue
                    jid = str(_dig(rec, [spec["fields"].get("id", "id")]) or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    new += 1
                    raws.append(
                        RawJob(
                            source=self.name,
                            source_job_id=jid,
                            company=company,
                            token=token,
                            url=self._field(rec, spec, "url"),
                            payload={**rec, "_spec": spec["fields"]},
                        )
                    )
                    if limit is not None and len(raws) >= limit:
                        return raws[:limit]
                if new == 0 or (total is not None and len(seen) >= total):
                    break
        finally:
            await client.close()
        return raws

    @staticmethod
    def _field(rec: dict[str, Any], spec: dict[str, Any], name: str) -> str | None:
        key = spec["fields"].get(name)
        if not key:
            return None
        val = rec.get(key)
        return val if isinstance(val, str) and val.strip() else None

    @staticmethod
    def _fget(p: dict[str, Any], key: str) -> Any:
        # Field keys may be a dotted path into nested records ("Locations.0.Address.City");
        # numeric segments index lists. A plain key (no dot) is a direct lookup.
        if not key:
            return None
        if "." not in key:
            return p.get(key)
        path: list[Any] = [int(s) if s.lstrip("-").isdigit() else s for s in key.split(".")]
        return _dig(p, path)

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        fmap = p.get("_spec", {})

        title = self._clean_title(str(self._fget(p, fmap.get("title", "title")) or ""))
        department = self._clean(self._fget(p, fmap.get("department", "")))
        loc = self._location(self._fget(p, fmap.get("location", "")))
        remote = RemoteType.REMOTE if (loc and loc.is_remote) else RemoteType.UNKNOWN
        emp_raw = (
            str(self._fget(p, fmap.get("employment_type", "")) or "")
            .strip()
            .lower()
            .replace(" ", "_")
        )
        employment = _EMPLOYMENT.get(emp_raw, EmploymentType.UNKNOWN)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=title,
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[loc] if loc else [],
            remote=remote,
            employment_type=employment,
            department=department,
            salary=None,
            posted_at=self._date(self._fget(p, fmap.get("posted_at", ""))),
            updated_at=None,
            description_html=self._clean(self._fget(p, fmap.get("description", ""))),
            description_text=None,
            raw=raw.payload,
        )

    @staticmethod
    def _clean(v: Any) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _clean_title(t: str) -> str:
        # Some feeds leak a code-fence/markdown marker into the title ("plaintext\nData Architect",
        # "```\nTitle"). Strip a leading fence/lang marker line; leave clean titles untouched.
        t = t.strip()
        t = re.sub(r"^(?:```+\s*\w*|plaintext|markdown|text)\s*[\r\n]+", "", t, flags=re.I)
        return t.strip("`").strip()

    @staticmethod
    def _date(v: Any) -> datetime | None:
        if not isinstance(v, str) or not v.strip():
            return None
        try:
            return datetime.fromisoformat(v.strip()[:10])
        except ValueError:
            return None

    @staticmethod
    def _location(v: Any) -> Location | None:
        """Build a Location from a string, or a list/dict of {city,state,country,name}."""
        item = v[0] if isinstance(v, list) and v else v
        if isinstance(item, str) and item.strip():
            label = item.strip()
        elif isinstance(item, dict):
            # Case-insensitive key view so PascalCase APIs (UltiPro City/State/Country) work too.
            ci = {str(k).lower(): val for k, val in item.items()}
            # Prefer an explicit English label when present (TikTok's city_info carries both an
            # en_name and a localized name); otherwise compose from city/name/state/country.
            if ci.get("en_name") and str(ci["en_name"]).strip():
                label = str(ci["en_name"]).strip()
            else:
                # A nested {state:{name:..}, country:{name:..}} (EPAM/UltiPro) -> use inner ``name``.
                parts: list[str] = []
                for k in ("city", "name", "state", "country"):
                    val = ci.get(k)
                    if isinstance(val, dict):
                        val = val.get("name") or val.get("Name") or val.get("Code")
                    if val and str(val).strip():
                        parts.append(str(val).strip())
                label = ", ".join(dict.fromkeys(parts))
        else:
            return None
        if not label:
            return None
        return Location(raw=label, is_remote="remote" in label.lower())
