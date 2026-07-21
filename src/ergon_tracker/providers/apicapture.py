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
import html as _htmlmod
import json
import re
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from ..models import EmploymentType, JobPosting, Location, RawJob, RemoteType, SearchQuery
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

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


def _parse_html_table(html: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a server-rendered HTML ``<table>`` of jobs into record dicts.

    Some body-shops expose their board as a plain HTML table (no JSON API). The spec drives it:

      ``"html_table"``: ``{"row_css": "tbody tr", "skip_rows": 1}`` — CSS for the job rows and how
      many leading rows to drop (the header).
      ``"columns"``: per-field extraction, each either ``{"col": N}`` (0-based ``<td>`` text) or
      ``{"re": "pattern"}`` (first capture group of a regex over the row's inner HTML — handy for an
      id/url buried in an ``href``). Keys match the ``fields`` map values, so normalize reads them
      straight through.
    """
    from selectolax.parser import HTMLParser

    cfg = spec.get("html_table") or {}
    row_css = cfg.get("row_css", "tr")
    skip = int(cfg.get("skip_rows", 0))
    columns: dict[str, dict[str, Any]] = spec.get("columns") or {}
    tree = HTMLParser(html)
    rows = tree.css(row_css)[skip:]
    out: list[dict[str, Any]] = []
    for row in rows:
        cells = row.css("td")
        inner = row.html or ""
        rec: dict[str, Any] = {}
        ok = False
        for field, how in columns.items():
            val: str | None = None
            if "col" in how:
                i = int(how["col"])
                if 0 <= i < len(cells):
                    val = cells[i].text(strip=True)
            elif "re" in how:
                m = re.search(how["re"], inner, re.S)
                if m:
                    # Regex runs over raw HTML, so decode entities (hrefs carry &amp;) for clean
                    # ids/urls; cell text() is already entity-decoded by the parser.
                    val = _htmlmod.unescape((m.group(1) if m.groups() else m.group(0)).strip())
            if val:
                ok = True
            rec[field] = val
        if ok:
            out.append(rec)
    return out


_RSS_TAGS = ("title", "link", "guid", "pubDate", "description", "category")


def _parse_rss(text: str) -> list[dict[str, Any]]:
    """Parse an RSS/Atom careers feed (``<item>`` blocks) into record dicts.

    Many WordPress careers sites with no REST job CPT still expose a ``/feed/`` (or
    ``/careers/feed/``) RSS feed. We extract the standard item tags (CDATA-unwrapped,
    entity-decoded) keyed by tag name, so the ``fields`` map reads them straight through
    (e.g. ``"title"``, ``"link"``; use ``link`` as the id when there's no numeric guid).
    """
    out: list[dict[str, Any]] = []
    for block in re.findall(r"<item[ >](.*?)</item>", text, re.S | re.I):
        rec: dict[str, Any] = {}
        for tag in _RSS_TAGS:
            m = re.search(
                rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S | re.I
            )
            rec[tag] = _htmlmod.unescape(m.group(1).strip()) if m else None
        if rec.get("title") or rec.get("link"):
            out.append(rec)
    return out


def _recover_json(text: str) -> Any:
    """Extract the first valid top-level JSON array embedded in ``text`` that is otherwise junk.

    Some WordPress hosts prepend an adsense ``<script>`` and a PHP "headers already sent" warning
    before the REST array, so ``resp.json()`` chokes. We scan ``[`` positions and ``raw_decode``
    (ignores trailing junk), returning the first non-empty list — the job array. Prefer arrays over
    objects so a stray JS object literal in the junk prefix can't win."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", text):
        with contextlib.suppress(ValueError):
            obj, _ = dec.raw_decode(text, m.start())
            if isinstance(obj, list) and obj:
                return obj
    return None


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


def _set_query(url: str, param: str, value: int | str) -> str:
    """Return ``url`` with query ``param`` set to ``value`` (GET pagination offsets, or a
    token-inject string)."""
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    qs[param] = [str(value)]
    return urlunsplit(parts._replace(query=urlencode(qs, doseq=True)))


def apply_token_to_spec(spec: dict[str, Any], value: str) -> dict[str, Any]:
    """Inject a cached Tier-2 session token into a COPY of ``spec`` per ``spec['token_inject']``.

    Config is any of: ``{"header": name}`` (request header), ``{"body_path": [..]}`` (POST body field),
    ``{"query": param}`` (URL query), ``{"cookie": name}`` (Cookie header). The original spec is never
    mutated, so a stale token can never leak across calls. See :mod:`ergon_tracker.token_store`."""
    cfg = spec.get("token_inject") or {}
    s = copy.deepcopy(spec)
    if "header" in cfg:
        s.setdefault("headers", {})[cfg["header"]] = value
    if "body_path" in cfg:
        if not isinstance(s.get("body"), dict):
            s["body"] = {}
        _set(s["body"], cfg["body_path"], value)
    if "query" in cfg:
        s["url"] = _set_query(s["url"], cfg["query"], value)
    if "cookie" in cfg:
        hdrs = s.setdefault("headers", {})
        hdrs["Cookie"] = f"{hdrs.get('Cookie', '')}; {cfg['cookie']}={value}".lstrip("; ")
    return s


_TOKEN_STORE: Any = None


def _token_store() -> Any:
    """Lazily construct the Tier-2 TokenStore ($ERGON_TOKEN_STORE or runs/tier2_tokens.json)."""
    global _TOKEN_STORE
    if _TOKEN_STORE is None:
        import os

        from ..token_store import TokenStore

        default = Path(__file__).resolve().parents[3] / "runs" / "tier2_tokens.json"
        _TOKEN_STORE = TokenStore(os.environ.get("ERGON_TOKEN_STORE") or str(default))
    return _TOKEN_STORE


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

    async def post_form(self, url: str, body: Any, headers: dict[str, str] | None) -> Any:
        resp = await self._f.request("POST", url, data=body, headers=headers)
        return resp.json()

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

    async def post_form(self, url: str, body: Any, headers: dict[str, str] | None) -> Any:
        r = await self._client.post(url, data=body, headers=headers)
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


class _CurlCaller:
    """TLS-impersonation client (curl_cffi, Chrome fingerprint) for own-domain JSON APIs behind
    TLS-fingerprint bot walls (Talemetry/Akamai) that reject httpx outright — same no-browser
    lever schemaorg uses. Opt-in via spec ``"tls_impersonate": true``; never used in tests (no
    spec carries the flag), so curl_cffi never bypasses respx in the hermetic suite."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._s: Any = None

    async def open(self) -> None:
        from curl_cffi.requests import AsyncSession

        self._s = AsyncSession(impersonate="chrome124", verify=False, timeout=30)

    async def close(self) -> None:
        if self._s is not None:
            with contextlib.suppress(Exception):
                await self._s.close()

    async def post_json(self, url: str, body: Any, headers: dict[str, str] | None) -> Any:
        r = await self._s.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    async def post_form(self, url: str, body: Any, headers: dict[str, str] | None) -> Any:
        r = await self._s.post(url, data=body, headers=headers)
        r.raise_for_status()
        return r.json()

    async def get_json(self, url: str, headers: dict[str, str] | None) -> Any:
        r = await self._s.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

    async def get_text(self, url: str, headers: dict[str, str] | None) -> str:
        r = await self._s.get(url, headers=headers)
        r.raise_for_status()
        text: str = r.text
        return text


# --- Tier-3 per-posting JD recovery (DRAIN-ONLY) ------------------------------------------------
#
# The captured LIST APIs omit the JD, but each of a handful of giants exposes the full JD via ONE
# plain unauthenticated HTTP hop (no browser). A spec opts in with a ``detail`` block describing how
# to fetch+extract one posting; ``fetch_detail`` dispatches on ``detail["kind"]`` (graphql /
# relay_json / css / html_sections). Per-giant specifics (url/body/selector/json_path/gone-rule)
# live as DATA in the spec, never as code branches. See ``base.py``'s fetch_detail contract:
# ``None`` == confirmed-gone, raise == indeterminate/transient (NEVER None on a transient).

_JD_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
# Bot-wall statuses that a tls_impersonate spec escalates past via curl_cffi (schemaorg's blessed
# escalate-on-block lever). NOT death and NOT a normal transient -- a genuine "your fingerprint is
# blocked". 5xx/429 stay TransientHTTPError from the shared fetcher (retried), 404/410 are gone.
_JD_BLOCK_STATUS = frozenset({400, 401, 403, 406})

# Sentinel: an extractor determined DEFINITIVE gone from a 200 body (graphql null role / google's
# absent JD sections) -- distinct from ``None`` ("couldn't extract" -> classify decides raise).
_GONE: Any = object()


@dataclass(frozen=True)
class _DetailReq:
    method: str
    url: str
    tier: str  # "plain" | "tls"
    follow_redirects: bool
    headers: dict[str, str] | None
    json_body: Any | None


@dataclass(frozen=True)
class _DetailResp:
    status_code: int
    text: str
    url: str
    headers: dict[str, str]  # keys lower-cased so ``.get("location")`` is case-insensitive


def _num_prefix(value: str) -> str | None:
    m = re.match(r"\d+", value or "")
    return m.group(0) if m else None


def _detail_ctx(ref: DetailRef) -> dict[str, str]:
    """Placeholder context for url/body templates, from the DetailRef. ``num_id`` is the leading
    ``\\d+`` of the posting id (Goldman's numeric externalSourceId prefix of ``179309_GS_...``)."""
    ctx = {
        "id": ref.id or "",
        "apply_url": ref.apply_url or "",
        "listing_url": ref.listing_url or "",
    }
    num = _num_prefix(ref.id or "")
    if num:
        ctx["num_id"] = num
    return ctx


# A template placeholder is a bare ``{identifier}`` -- deliberately narrow so a GraphQL query's own
# ``{`` selection-set braces (``{\n  role(...`` -- spaces/newlines/parens inside) are never mistaken
# for one.
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _fill(template: str, ctx: dict[str, str]) -> str | None:
    """Substitute ``{name}`` placeholders from ``ctx``; None if any placeholder is missing/empty."""
    out = template
    for ph in _PLACEHOLDER_RE.findall(template):
        val = ctx.get(ph)
        if not val:
            return None
        out = out.replace("{" + ph + "}", val)
    return out


def _fill_body(node: Any, ctx: dict[str, str]) -> Any:
    """Deep-substitute placeholders in a JSON body template. Raises KeyError on an unresolved
    placeholder so the caller can treat the whole request as unbuildable."""
    if isinstance(node, str):
        if not _PLACEHOLDER_RE.search(node):
            return node
        filled = _fill(node, ctx)
        if filled is None:
            raise KeyError(node)
        return filled
    if isinstance(node, dict):
        return {k: _fill_body(v, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_body(v, ctx) for v in node]
    return node


def _build_detail_request(detail: dict[str, Any], ref: DetailRef) -> _DetailReq | None:
    """Build the one detail request from the spec's ``detail`` block + the DetailRef, or None when
    no URL/body is derivable (an unbuildable ref -> the caller RAISES; never guessed dead)."""
    ctx = _detail_ctx(ref)
    if detail.get("url"):
        url: str | None = detail["url"]
    elif detail.get("url_template"):
        url = _fill(detail["url_template"], ctx)
    elif detail.get("url_from"):
        url = ctx.get(detail["url_from"]) or ctx.get(detail.get("url_from_fallback", "")) or None
    else:
        url = None
    if not url:
        return None
    json_body = None
    if detail.get("body_template") is not None:
        try:
            json_body = _fill_body(detail["body_template"], ctx)
        except KeyError:
            return None
    return _DetailReq(
        method=detail.get("method", "GET").upper(),
        url=url,
        tier=detail.get("client", "plain"),
        follow_redirects=bool(detail.get("follow_redirects", False)),
        headers=detail.get("headers"),
        json_body=json_body,
    )


def _strip_html(value: str) -> str:
    if "<" in value and ">" in value:
        from selectolax.parser import HTMLParser

        return HTMLParser(value).text(separator=" ", strip=True)
    return value


def _relay_value_text(val: Any) -> str | None:
    """Flatten one Relay JSON value to text: a ``[{"item": ...}]`` bullet list, a plain string, or
    a ``{"__html": "<p>…"}``-wrapped HTML string (Meta wraps its description that way)."""
    if isinstance(val, list):
        items = [
            _strip_html(str(it.get("item") or "")) for it in val if isinstance(it, dict)
        ]
        return " ".join(p for p in items if p).strip() or None
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{") and "__html" in s:
            with contextlib.suppress(ValueError):
                inner = json.loads(s)
                if isinstance(inner, dict) and isinstance(inner.get("__html"), str):
                    s = inner["__html"]
        return _strip_html(s).strip() or None
    return None


def _extract_graphql(text: str, url: str, detail: dict[str, Any]) -> Any:
    try:
        data = json.loads(text)
    except ValueError:
        return None  # unparseable 200 -> indeterminate -> raise
    null_path = (detail.get("gone") or {}).get("json_null_path")
    if null_path is not None and _dig(data, null_path) is None:
        return _GONE  # e.g. Goldman: data.role == null (removed / invalid id)
    val = _dig(data, detail.get("json_path") or [])
    return val if isinstance(val, str) and val.strip() else None


def _extract_relay_json(text: str, url: str, detail: dict[str, Any]) -> Any:
    """Concat named JSON keys from the inline Relay blob. ``container_key`` anchors extraction to a
    single object (Meta's ``xcp_requisition_job_description``) so a same-named key elsewhere on the
    page can't win; otherwise each key is scanned for in the blob."""
    dec = json.JSONDecoder()
    container: Any = None
    ckey = detail.get("container_key")
    if ckey:
        i = text.find('"' + ckey + '":')
        if i != -1:
            with contextlib.suppress(ValueError):
                container, _ = dec.raw_decode(text, i + len('"' + ckey + '":'))
    parts: list[str] = []
    for key in detail.get("keys") or []:
        val: Any = None
        if isinstance(container, dict) and key in container:
            val = container[key]
        else:
            for m in re.finditer(r'"' + re.escape(key) + r'":', text):
                with contextlib.suppress(ValueError):
                    val, _ = dec.raw_decode(text, m.end())
                    break
        piece = _relay_value_text(val)
        if piece:
            parts.append(piece)
    return "\n\n".join(parts).strip() or None


def _extract_css(text: str, url: str, detail: dict[str, Any]) -> Any:
    """Text of a CSS-selected JD container. ``select: "largest"`` picks the biggest match (Bain's
    JD is the largest ``.article__content``); default takes the first."""
    from selectolax.parser import HTMLParser

    tree = HTMLParser(text)
    sel = detail["selector"]
    got: str | None
    if detail.get("select") == "largest":
        best: str | None = None
        best_len = 0
        for node in tree.css(sel):
            body = node.text(separator=" ", strip=True)
            if len(body) > best_len:
                best, best_len = body, len(body)
        got = best
    else:
        first = tree.css_first(sel)
        got = first.text(separator=" ", strip=True) if first else None
    if not got:
        return None
    return re.sub(r"\s+", " ", got).strip() or None


def _extract_html_sections(text: str, url: str, detail: dict[str, Any]) -> Any:
    """Concat named visible sections, located by their HEADING text (robust to rotating obfuscated
    class names). Absent sections -> ``_GONE`` when the spec marks the gone-signal soft (Google:
    a bad id soft-200s, so gone == JD sections absent), else None (indeterminate)."""
    from selectolax.parser import HTMLParser

    wanted = {s.strip().rstrip(":").lower() for s in detail.get("sections") or []}
    tree = HTMLParser(text)
    parts: list[str] = []
    seen: set[str] = set()  # body text, NOT node id -- two headings can share one parent container
    for node in tree.css("h1,h2,h3,h4,h5,h6"):
        label = node.text(strip=True).rstrip(":").strip().lower()
        if label not in wanted:
            continue
        parent = node.parent or node
        body = re.sub(r"\s+", " ", parent.text(separator=" ", strip=True)).strip()
        if body and body not in seen:
            seen.add(body)
            parts.append(body)
    if parts:
        return "\n\n".join(parts).strip() or None
    return _GONE if (detail.get("gone") or {}).get("absent") else None


_DETAIL_EXTRACTORS = {
    "graphql": _extract_graphql,
    "relay_json": _extract_relay_json,
    "css": _extract_css,
    "html_sections": _extract_html_sections,
}


def _resp_of(raw: Any) -> _DetailResp:
    """Normalize an httpx.Response (or the tests' fake) to a ``_DetailResp`` with lower-cased
    headers so redirect-``Location`` lookups are case-insensitive across httpx/curl_cffi/fake."""
    headers = {str(k).lower(): v for k, v in dict(getattr(raw, "headers", {}) or {}).items()}
    return _DetailResp(
        status_code=raw.status_code,
        text=getattr(raw, "text", "") or "",
        url=str(getattr(raw, "url", "") or ""),
        headers=headers,
    )


_TLS_SESSION: Any = None


def _tls_session() -> Any:
    """Lazily-created, PROCESS-REUSED curl_cffi Chrome-TLS session (never per-call: creating one is
    expensive). Constructor is sync with no await before assignment, so the single-threaded event
    loop can't hand out two. Same impersonation the list path (``_CurlCaller``) uses; never closed
    (process-lifetime singleton, like ``_TOKEN_STORE``). Never reached in tests (canned 200s never
    escalate), so curl_cffi stays off the hermetic path."""
    global _TLS_SESSION
    if _TLS_SESSION is None:
        from curl_cffi.requests import AsyncSession

        _TLS_SESSION = AsyncSession(impersonate="chrome124", verify=False, timeout=30)
    return _TLS_SESSION


async def _tls_request(req: _DetailReq) -> _DetailResp:
    """Send ``req`` through the reused curl_cffi session (tls-impersonate escalation for a bot-wall
    block). Awaits curl_cffi's async API -- no blocking I/O."""
    session = _tls_session()
    if req.method == "POST":
        r = await session.post(
            req.url, json=req.json_body, headers=req.headers, allow_redirects=req.follow_redirects
        )
    else:
        r = await session.get(
            req.url, headers=req.headers, allow_redirects=req.follow_redirects
        )
    return _resp_of(r)


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

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """DRAIN-ONLY Tier-3 JD recovery: the captured LIST API omits the JD, but the spec's
        ``detail`` block names one plain HTTP hop that carries it. Dispatch on ``detail["kind"]``,
        send via the shared ``fetcher`` (per-host rate-limit + retries + reused client apply), and
        classify per the base contract (``None`` == GONE, raise == indeterminate/transient).

        ``ref.token`` is the spec key (e.g. ``"goldmansachs"``); a token whose spec has no ``detail``
        block, or an unbuildable request, RAISES (indeterminate -- never a false gone). Since
        apicapture is barred from every freshness/liveness confirm path, a ``None`` here can only
        mean "JD not recovered" in the drain, never a liveness expiry."""
        spec = _load_specs().get(ref.token or "") or {}
        detail = spec.get("detail")
        if not detail or detail.get("kind") not in _DETAIL_EXTRACTORS:
            raise RuntimeError(f"apicapture detail: no usable detail block for token {ref.token!r}")
        req = _build_detail_request(detail, ref)
        if req is None:
            raise RuntimeError(f"apicapture detail: unbuildable request for {ref.id}")
        resp = await self._detail_send(fetcher, req)
        return self._classify(resp, detail, ref)

    @staticmethod
    async def _detail_send(fetcher: AsyncFetcher, req: _DetailReq) -> _DetailResp:
        """Send via the shared fetcher (NOT bypassed: keeps rate-limiting + retries + the reused
        HTTP/2 client). A ``tls`` spec escalates ONCE to the reused curl_cffi session on a bot-wall
        block (or a transport error) -- schemaorg's escalate-on-block lever; a canned 200 in tests
        never escalates, so the hermetic suite never touches curl_cffi."""
        kwargs: dict[str, Any] = {"follow_redirects": req.follow_redirects}
        if req.headers:
            kwargs["headers"] = req.headers
        if req.json_body is not None:
            kwargs["json"] = req.json_body
        try:
            raw = await fetcher.request(req.method, req.url, **kwargs)
        except Exception:
            if req.tier == "tls":
                return await _tls_request(req)
            raise
        resp = _resp_of(raw)
        if req.tier == "tls" and resp.status_code in _JD_BLOCK_STATUS:
            return await _tls_request(req)
        return resp

    @staticmethod
    def _classify(resp: _DetailResp, detail: dict[str, Any], ref: DetailRef) -> str | None:
        """The load-bearing alive/gone/raise decision, in ONE place. Transport-level gone
        (redirect-marker / final-url-marker / explicit gone status) is spec-declared here; body-level
        gone (graphql null role, absent sections) comes back as ``_GONE`` from the kind extractor."""
        gone = detail.get("gone") or {}
        status = resp.status_code
        if status in _JD_REDIRECT_STATUS:
            location = resp.headers.get("location", "")
            marker = gone.get("redirect_marker")
            if marker and marker in location:
                return None  # e.g. lululemon/bain 302 -> /Error
            raise RuntimeError(f"apicapture detail: unclassifiable redirect {status} for {ref.id}")
        if gone.get("final_url_marker") and gone["final_url_marker"] in resp.url:
            return None  # e.g. meta followed 301 -> /jobs/position-not-available/
        if status in set(gone.get("status") or ()):
            return None
        if status != 200:
            raise RuntimeError(f"apicapture detail: status {status} for {ref.id}")
        result = _DETAIL_EXTRACTORS[detail["kind"]](resp.text, resp.url, detail)
        if result is _GONE:
            return None
        if isinstance(result, str) and result.strip():
            return result
        raise RuntimeError(f"apicapture detail: no JD extracted ({detail['kind']}) for {ref.id}")

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        token = _SCHEME_RE.sub("", token.strip()).strip()
        spec = _load_specs().get(token)
        if not spec:
            return []
        # Tier-2: inject a cached, offline-minted session token (Akamai cookie / ADP-RM token / JWT)
        # before reading the request. No valid token -> replay proceeds tokenless and likely 401/403s,
        # which marks the token stale so the offline cron re-mints it (live-fetch then falls back to
        # the index). The browser is never on this path. See ergon_tracker.token_store.
        token_ref = spec.get("token_ref")
        store = _token_store() if token_ref else None
        if store is not None:
            cached = store.get(token_ref)
            if cached is not None:
                spec = apply_token_to_spec(spec, cached)
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
        if spec.get("tls_impersonate"):
            client: Any = _CurlCaller(spec)
        elif spec.get("browser_http1"):
            client = _BrowserCaller(spec)
        else:
            client = _FetcherCaller(fetcher)

        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        total: int | None = None
        stale_token = False  # set if a token_ref replay fails on page 0 (token likely expired)
        await client.open()
        try:
            # Salesforce Experience-Cloud guest Aura: the fwuid + loaded app-version in aura.context
            # rotate when the org is upgraded (a stale pair yields aura:expired). Re-scrape them from
            # a fresh GET of the page so the replay self-heals.
            aura_ctx = (
                await self._refresh_aura(client, spec, headers)
                if spec.get("aura_refresh")
                else None
            )
            for page in range(self.MAX_PAGES):
                offset = page_start + page * page_step
                body = copy.deepcopy(spec.get("body"))
                if aura_ctx and isinstance(body, dict):
                    body["aura.context"] = aura_ctx
                if page_path:
                    _set(body, page_path, offset)
                if spec.get("size_path"):
                    _set(body, spec["size_path"], size)
                try:
                    if spec.get("html_table"):
                        page_url = _set_query(url, page_param, offset) if page_param else url
                        html = await client.get_text(page_url, headers)
                        records = _parse_html_table(html, spec)
                    elif spec.get("rss"):
                        page_url = _set_query(url, page_param, offset) if page_param else url
                        records = _parse_rss(await client.get_text(page_url, headers))
                    elif embed:
                        page_url = _set_query(url, page_param, offset) if page_param else url
                        html = await client.get_text(page_url, headers)
                        data = _extract_embed(html, embed)
                        records = _dig(data, rec_path)
                    elif method == "POST":
                        if spec.get("form_encoded"):
                            data = await client.post_form(url, body, headers)
                        else:
                            data = await client.post_json(url, body, headers)
                        records = _dig(data, rec_path)
                    else:
                        get_url = _set_query(url, page_param, offset) if page_param else url
                        if spec.get("json_text_recover"):
                            data = _recover_json(await client.get_text(get_url, headers))
                        else:
                            data = await client.get_json(get_url, headers)
                        records = _dig(data, rec_path)
                except Exception:
                    # A token_ref replay that dies on the first page almost always means the cached
                    # session token expired — flag it for re-mint (handled after the loop).
                    if page == 0 and token_ref:
                        stale_token = True
                    break
                if total is None and not (spec.get("html_table") or spec.get("rss")):
                    t = _dig(data, tot_path)
                    total = t if isinstance(t, int) else None
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
                            url=self._job_url(rec, spec),
                            payload={**rec, "_spec": spec["fields"]},
                        )
                    )
                    if limit is not None and len(raws) >= limit:
                        return raws[:limit]
                if new == 0 or (total is not None and len(seen) >= total):
                    break
        finally:
            await client.close()
        if stale_token and store is not None:
            store.mark_stale(token_ref)  # next offline cron mint re-mints; this call falls back
        return raws

    @staticmethod
    async def _refresh_aura(
        client: Any, spec: dict[str, Any], headers: dict[str, str] | None
    ) -> str | None:
        """Re-scrape ``fwuid`` and the loaded app-version from the live page and patch them into the
        spec body's ``aura.context`` string (Salesforce rotates these on org upgrades). Falls back to
        the stored context on any failure so a transient page error doesn't break the pull."""
        ctx = (spec.get("body") or {}).get("aura.context")
        if not isinstance(ctx, str):
            return None
        try:
            page = await client.get_text(spec["aura_refresh"], headers)
        except Exception:
            return ctx
        fw = re.search(r'"fwuid":"([^"]+)"', page)
        if fw:
            ctx = re.sub(
                r'("fwuid":")[^"]*(")', lambda m: m.group(1) + fw.group(1) + m.group(2), ctx
            )
        ld = re.search(r'"(APPLICATION@markup://[^"]+)":"([^"]+)"', page)
        if ld:
            key, val = re.escape(ld.group(1)), ld.group(2)
            ctx = re.sub(
                r'("' + key + r'":")[^"]*(")', lambda m: m.group(1) + val + m.group(2), ctx
            )
        return ctx

    @staticmethod
    def _field(rec: dict[str, Any], spec: dict[str, Any], name: str) -> str | None:
        key = spec["fields"].get(name)
        if not key:
            return None
        val = rec.get(key)
        return val if isinstance(val, str) and val.strip() else None

    @classmethod
    def _job_url(cls, rec: dict[str, Any], spec: dict[str, Any]) -> str | None:
        """Per-job apply URL. A spec may give a direct ``url`` field OR a ``url_template`` with
        ``{name}`` placeholders resolved against the ``fields`` map (e.g. Meta has no per-job url
        field, but its job id -> ``metacareers.com/jobs/{id}/``). Unresolved placeholder -> None."""
        tmpl = spec.get("url_template")
        if isinstance(tmpl, str) and tmpl:
            out = tmpl
            for ph in re.findall(r"\{([^}]+)\}", tmpl):
                key = spec["fields"].get(ph, ph)
                val = rec.get(key)
                if not (isinstance(val, (str, int)) and str(val).strip()):
                    return None
                out = out.replace("{" + ph + "}", str(val))
            return out
        return cls._field(rec, spec, "url")

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
        # Unescape HTML entities (WordPress REST leaks "&#8211;" etc.), then strip a leaked
        # code-fence/markdown marker ("plaintext\nData Architect"); leave clean titles untouched.
        t = _htmlmod.unescape(t).strip()
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
