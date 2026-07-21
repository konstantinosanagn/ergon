"""PeopleAdmin provider — the dominant higher-ed / public-sector ATS (universities, school
districts, state agencies).

Every PeopleAdmin tenant exposes its ENTIRE posting list as a single Atom feed at
``https://{host}/postings/search.atom`` (e.g. ``unmc.peopleadmin.com`` → 215 entries in one
document, no pagination — ``?page=N`` just repeats the same feed). Each ``<entry>`` carries the
posting id, title, apply link, HTML description (``<content>``) and the hiring unit
(``<author><name>``). Location isn't in the feed (it lives on the detail page, which we don't
bulk-fetch), so it normalizes to ``None`` — never invented.

Token: the PeopleAdmin host (``"unmc.peopleadmin.com"``) or bare subdomain (``"unmc"`` → expanded
to ``{sub}.peopleadmin.com``). ``matches()`` resolves any ``*.peopleadmin.com`` careers URL.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from ..models import DetailFetch, JobPosting, Location, RawJob, RemoteType
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef
    from ..models import SearchQuery

__all__ = ["PeopleAdminProvider"]

_FEED = "https://{host}/postings/search.atom"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S | re.I)
_ID = re.compile(r"<id>\s*https?://[^<]*?/postings/(\d+)\s*</id>", re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_LINK = re.compile(r'<link[^>]*rel="alternate"[^>]*href="([^"]+)"', re.I)
_CONTENT = re.compile(r"<content[^>]*>(.*?)</content>", re.S | re.I)
_AUTHOR = re.compile(r"<author>\s*<name>(.*?)</name>", re.S | re.I)
_PUBLISHED = re.compile(r"<published>(.*?)</published>", re.S | re.I)

_POSTING = "https://{host}/postings/{id}"
# The posting id embedded in a ``.../postings/{id}`` URL (a real posting always has an id segment;
# the bare ``/postings`` search root -- where a removed posting redirects -- never does).
_POSTING_ID_RE = re.compile(r"/postings/(\d+)", re.IGNORECASE)
# HTTP statuses that signal a redirect (a removed posting 302s to the ``/postings`` search root).
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# The requisition-body container on a server-rendered posting page, tried in order. ``#form_view`` /
# ``#content_inner`` isolate the posting fields; ``#content .mainContent`` / ``#content`` are the
# broader wrappers used as a fallback for tenants that white-label the markup.
_JD_SELECTOR = "#form_view, #content_inner, #content .mainContent, #content, .job-details"
# Page chrome to strip from the container so only the requisition prose remains.
_JD_NOISE_SELECTOR = (
    "script, style, nav, header, footer, button, form input, [class*=breadcrumb], "
    "a[class*=apply], a[class*=button]"
)
# Summary-table row labels that carry the posting's location (the Atom feed omits location).
_LOCATION_LABEL_RE = re.compile(r"\b(location|campus|city|work\s+location|position\s+location)\b", re.I)


@register("peopleadmin")
class PeopleAdminProvider(BaseProvider):
    name = "peopleadmin"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        candidate = url_or_host if "//" in url_or_host else "//" + url_or_host
        host = urlsplit(candidate).netloc.split("@")[-1].split(":")[0].lower()
        return host if host.endswith(".peopleadmin.com") else None

    @staticmethod
    def _host(token: str) -> str:
        # Bare subdomain ("unmc") -> {sub}.peopleadmin.com. A full host with a dot
        # ("jobs.rutgers.edu", "unmc.peopleadmin.com") is used as-is — many universities
        # white-label the same PeopleAdmin Atom feed on their own domain.
        h = token.strip().lower().rstrip("/")
        return h if "." in h else f"{h}.peopleadmin.com"

    @staticmethod
    def _company(host: str) -> str:
        # "unmc.peopleadmin.com" -> "unmc"; a custom host "jobs.rutgers.edu" -> "rutgers"
        # (the registrable label, not the "jobs"/"careers" service prefix).
        parts = host.split(".")
        if host.endswith(".peopleadmin.com"):
            return parts[0]
        return parts[-2] if len(parts) >= 2 else parts[0]

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        host = self._host(token)
        try:
            text = await fetcher.get_text(_FEED.format(host=host))
        except Exception:
            return []
        limit = query.limit
        raws: list[RawJob] = []
        seen: set[str] = set()
        for block in _ENTRY.findall(text):
            id_m = _ID.search(block)
            if not id_m:
                continue
            jid = id_m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            link_m = _LINK.search(block)
            url = link_m.group(1).strip() if link_m else f"https://{host}/postings/{jid}"
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=jid,
                    company=self._company(host),
                    token=host,
                    url=url,
                    payload={
                        "title": self._text(_TITLE, block),
                        "department": self._text(_AUTHOR, block),
                        "description": self._text(_CONTENT, block),
                        "published": self._text(_PUBLISHED, block),
                    },
                )
            )
            if limit is not None and len(raws) >= limit:
                break
        return raws

    @staticmethod
    def _text(pat: re.Pattern[str], block: str) -> str:
        m = pat.search(block)
        return _html.unescape(m.group(1).strip()) if m else ""

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | DetailFetch | None:
        """Tier-3 JD recovery + liveness confirm: the Atom feed's ``<content>`` is a ~340-char
        truncated summary with NO location, so fetch the server-rendered posting PAGE and extract
        the full requisition body (and the location, absent from the feed).

        THE URL: GET ``ref.apply_url`` (already the ``https://{host}/postings/{id}`` page), falling
        back to a reconstruction from ``ref.token`` (the host) + the posting id parsed out of
        ``listing_url``. No derivable URL -> the ref is unbuildable (indeterminate, never death) ->
        RAISE.

        Contract (see ``providers/base.py`` -- ``None`` == GONE, raise == indeterminate). The page is
        fetched with redirects NOT auto-followed so the gone-signal 302 is observable:
          - an explicit 404/410 -> ``None`` (GONE).
          - a 302 to the ``/postings`` search ROOT (no id) -> ``None`` (a removed posting redirects
            there); a redirect ELSEWHERE is unexpected -> RAISE.
          - 200 with a non-empty requisition body -> the JD text, wrapped in a ``DetailFetch`` with
            the page's location when one is cleanly extractable (ALIVE).
          - a 200 that (after an auto-followed redirect) landed on the ``/postings`` root with no
            body -> ``None``; a 200 without a body that ISN'T the root, or any other status -> RAISE
            (indeterminate; NEVER ``None``).

        peopleadmin is in ``liveness.CONFIRM_VIA_DETAIL_SOURCES`` (its clean 302-to-root / 404 IS a
        definitive gone-signal), so this both recovers JD/location AND provides its liveness confirm.
        """
        url = self._detail_url(ref)
        if not url:
            raise RuntimeError(f"peopleadmin detail: no derivable detail URL for {ref!s}")
        resp = await fetcher.request("GET", url, follow_redirects=False)
        status = resp.status_code

        if status in (404, 410):
            return None
        if status in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if location and self._is_search_root(urljoin(url, location)):
                return None
            raise RuntimeError(f"peopleadmin detail: unclassifiable redirect {status} for {ref!s}")
        if status != 200:
            raise RuntimeError(f"peopleadmin detail: unexpected status {status} for {ref!s}")

        text, locations = self._extract(resp.text)
        if text:
            if locations:
                return DetailFetch(text=text, locations=locations)
            return text
        # 200 with no usable body. If the fetcher auto-FOLLOWED the gone-redirect we're now on the
        # search root -> GONE. Otherwise it's an indeterminate 200 body -> RAISE.
        final_url = str(getattr(resp, "url", "") or url)
        if self._is_search_root(final_url):
            return None
        raise RuntimeError(f"peopleadmin detail: 200 without a requisition body for {ref!s}")

    @classmethod
    def _detail_url(cls, ref: DetailRef) -> str | None:
        """The posting-page URL: ``ref.apply_url`` verbatim when present, else the canonical
        ``https://{host}/postings/{id}`` reconstructed from ``ref.token`` (the host) + the id parsed
        from ``listing_url``. ``None`` when no id is derivable."""
        if ref.apply_url:
            return ref.apply_url
        if ref.listing_url and ref.token:
            m = _POSTING_ID_RE.search(ref.listing_url)
            if m:
                return _POSTING.format(host=cls._host(ref.token), id=m.group(1))
        return None

    @staticmethod
    def _is_search_root(url: str) -> bool:
        """True when ``url`` is a peopleadmin postings SEARCH ROOT (path ``/postings`` with no id) --
        where a removed posting redirects. Any real posting lives at ``/postings/{id}``."""
        path = urlsplit(url).path.strip("/")
        return path in ("postings", "")

    @classmethod
    def _extract(cls, html: str | None) -> tuple[str | None, list[Location]]:
        """Extract ``(jd_text, locations)`` from a posting page: the requisition-body container
        (chrome stripped, whitespace collapsed) plus any location read from the summary table's
        ``<th>Location</th> -> <td>`` rows. Returns ``(None, [])`` when no body container is found."""
        if not html:
            return None, []
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        locations = cls._locations(tree)
        node = tree.css_first(_JD_SELECTOR)
        if node is None:
            return None, locations
        for noise in node.css(_JD_NOISE_SELECTOR):
            noise.decompose()
        text = node.text(separator=" ", strip=True)
        if not text:
            return None, locations
        text = re.sub(r"\s+", " ", text).strip()
        return (text or None), locations

    @staticmethod
    def _locations(tree: Any) -> list[Location]:
        """Location from the posting's summary table: the ``<td>`` value of the first ``<tr>`` whose
        ``<th>`` label reads Location/Campus/City. Best-effort -- returns ``[]`` when absent."""
        for row in tree.css("tr"):
            th = row.css_first("th")
            td = row.css_first("td")
            if th is None or td is None:
                continue
            label = re.sub(r"\s+", " ", th.text(strip=True) or "")
            if not _LOCATION_LABEL_RE.search(label):
                continue
            value = re.sub(r"\s+", " ", td.text(separator=" ", strip=True) or "").strip()
            if value:
                return [Location(raw=value)]
        return []

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=str(p.get("title") or ""),
            fetched_at=raw.fetched_at,
            apply_url=raw.url,
            locations=[],  # not in the Atom feed; never invented
            remote=RemoteType.UNKNOWN,
            department=self._clean(p.get("department")),
            posted_at=self._date(p.get("published")),
            description_html=self._clean(p.get("description")),
        )

    @staticmethod
    def _clean(v: object) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _date(v: object) -> datetime | None:
        if not isinstance(v, str) or not v.strip():
            return None
        try:
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
