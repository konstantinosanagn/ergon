"""Breezy HR job-board provider.

Breezy HR exposes a free, unauthenticated public positions API:
``GET https://{token}.breezy.hr/json`` which returns a JSON *array* of every published
position for a company in one call. There is no server-side filtering, so :meth:`fetch`
returns the whole board and the orchestrator applies ``SearchQuery.matches`` client-side.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from ..extract.comp import parse_salary
from ..models import (
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    SearchQuery,
)
from .base import BaseProvider, register

if TYPE_CHECKING:
    from ..http import AsyncFetcher
    from ..index.detail import DetailRef

__all__ = ["BreezyProvider"]

_API = "https://{token}.breezy.hr/json"

# Per-position DETAIL page (Tier-3 JD recovery): the list ``/json`` endpoint carries NO
# description, but the server-rendered position page does. Canonical shape is
# ``https://{token}.breezy.hr/p/{position_id}-{slug}``; the slug is cosmetic -- a valid id with
# any/no slug still 200s -- so reconstruction only needs (token, position_id).
_POSITION = "https://{token}.breezy.hr/p/{position_id}"

# The JD body container on a breezy position page.
_DESCRIPTION_SELECTOR = "#description, .position-description"

# Chrome to strip out of the container so only the JD body remains: breadcrumb, apply button,
# scripts/styles. (Kept conservative -- only obvious page furniture, never JD prose.)
_JD_NOISE_SELECTOR = (
    "script, style, nav, header, footer, button, [class*=breadcrumb], a[class*=apply]"
)

# HTTP statuses that signal a redirect (a removed position 302s to the board root).
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# breezy i18n placeholder tokens (e.g. ``%APPLY_NOW%``, ``%FN%``) that leak into rendered text.
# Anchored to no-whitespace runs so a real "50% ... 30%" in JD prose is never eaten.
_PLACEHOLDER_RE = re.compile(r"%[A-Za-z0-9_.\-]*%")

# The breezy position id embedded in a ``.../p/{position_id}`` URL (id + optional ``-slug``).
_POSITION_ID_RE = re.compile(r"breezy\.hr/p/([^/?#]+)", re.IGNORECASE)

# Hosts we recognise, capturing the company token as group 1.
_HOST_PATTERNS = (re.compile(r"([^/.\s]+)\.breezy\.hr", re.I),)

# Breezy ``type.name`` values (e.g. "Full-Time", "Part-Time", "Contractor", "Intern").
_EMPLOYMENT_BY_NAME = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contractor": EmploymentType.CONTRACT,
    "contract": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
}


def _token_from_url(url: str) -> str | None:
    """The board token (``{token}.breezy.hr``) from a breezy URL, or ``None``."""
    for pattern in _HOST_PATTERNS:
        m = pattern.search(url)
        if m:
            token = m.group(1).strip("/")
            if token and token != "www":
                return token
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Breezy timestamps look like "2026-06-04T23:24:11.988Z".
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _employment(type_obj: Any) -> EmploymentType:
    name = type_obj.get("name") if isinstance(type_obj, dict) else type_obj
    if not name:
        return EmploymentType.UNKNOWN
    return _EMPLOYMENT_BY_NAME.get(str(name).strip().lower(), EmploymentType.UNKNOWN)


@register("breezy")
class BreezyProvider(BaseProvider):
    name = "breezy"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token != "www":
                    return token
        return None

    def conditional_url(self, token: str) -> str | None:
        # Whole board in one JSON response with a strong ETag (honors If-None-Match -> 304).
        return _API.format(token=token)

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Breezy has no server-side filtering: pull the whole board in one request.
        data = await fetcher.get_json(_API.format(token=token))
        return self._raws_from_data(data, token)

    def raws_from_body(self, token: str, body: bytes) -> list[RawJob]:
        """Parse an already-downloaded body (from a conditional 200), avoiding a refetch."""
        import json

        return self._raws_from_data(json.loads(body), token)

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """DRAIN-ONLY Tier-3 JD recovery: the list ``/json`` endpoint carries no description, so
        fetch the server-rendered position PAGE and extract the ``#description`` /
        ``.position-description`` body.

        THE URL: GET ``ref.apply_url`` (already the ``.../p/{id}-{slug}`` position page). When it's
        absent, reconstruct ``https://{token}.breezy.hr/p/{position_id}`` from ``ref.token`` + the
        position id -- the id is the only key (slug optional). The id lives only inside a breezy
        ``/p/`` URL, so it's parsed from ``listing_url``; if neither yields a URL the ref is
        UNBUILDABLE (indeterminate, never death) -> RAISE.

        CONTRACT (see ``base.py``'s ``fetch_detail`` -- ``None`` == GONE, raise == indeterminate).
        The page is fetched with redirects NOT auto-followed so the gone-signal 302 is observable:
          - 200 with a non-empty ``#description`` body -> return the JD text (ALIVE).
          - a 302 to the board root ``/`` -> ``None`` (a removed position redirects there); an
            explicit 404/410 -> ``None``. If the fetcher auto-FOLLOWED the redirect, we instead land
            on a 200 board-root page with NO ``#description`` -> also ``None`` (detected by the final
            URL being the board root).
          - a 200 without a ``#description`` that ISN'T the board root, a redirect ELSEWHERE, any
            other status (5xx/429/timeout surface as a raised transient from the fetcher), or an
            unparseable body -> RAISE (indeterminate; NEVER ``None``).

        NOTE the 302->root is a SOFT gone-signal, which is why breezy is wired DRAIN-ONLY
        (``_TIER3_DETAIL_SOURCES``) and deliberately NOT into ``liveness.CONFIRM_VIA_DETAIL_SOURCES``:
        in the drain a ``None`` merely fails to recover a JD (retried up to ``RETRY_CAP``), whereas
        in the liveness confirm path it would expire a live row. breezy's liveness/freshness is
        already handled by its deterministic bulk id-set relist (``DETERMINISTIC_SOURCES``).
        """
        url = self._detail_url(ref)
        if not url:
            raise RuntimeError(f"breezy detail: no derivable detail URL for {ref!s}")
        resp = await fetcher.request("GET", url, follow_redirects=False)
        status = resp.status_code

        if status in (404, 410):
            return None  # explicit not-found -> GONE
        if status in _REDIRECT_STATUSES:
            # A removed position 302s to the board root "/": that (and only that) is the gone-signal.
            # A redirect ELSEWHERE is unexpected -> indeterminate, never guessed as dead.
            location = resp.headers.get("location")
            if location and self._is_board_root(urljoin(url, location)):
                return None
            raise RuntimeError(f"breezy detail: unclassifiable redirect {status} for {ref!s}")
        if status != 200:
            raise RuntimeError(f"breezy detail: unexpected status {status} for {ref!s}")

        text = self._extract_jd(resp.text)
        if text:
            return text  # ALIVE
        # 200 with no usable #description. If the fetcher auto-FOLLOWED the gone-redirect we're now
        # on the board root -> GONE. Otherwise it's an indeterminate 200 body -> RAISE.
        final_url = str(getattr(resp, "url", "") or url)
        if self._is_board_root(final_url):
            return None
        raise RuntimeError(f"breezy detail: 200 without #description for {ref!s}")

    @staticmethod
    def _detail_url(ref: DetailRef) -> str | None:
        """The position-page URL to fetch: ``ref.apply_url`` verbatim when present, else the
        canonical ``.../p/{position_id}`` reconstructed from ``ref.token`` + the position id parsed
        out of ``listing_url``. Returns ``None`` when no id is derivable (unbuildable ref)."""
        if ref.apply_url:
            return ref.apply_url
        if ref.listing_url:
            m = _POSITION_ID_RE.search(ref.listing_url)
            token = ref.token or _token_from_url(ref.listing_url)
            if m and token:
                return _POSITION.format(token=token, position_id=m.group(1))
        return None

    @staticmethod
    def _is_board_root(url: str) -> bool:
        """True when ``url`` is a breezy board ROOT (``https://{token}.breezy.hr`` with no path) --
        where a removed position redirects. Any real position lives under ``/p/...``, never root."""
        parts = urlsplit(url)
        host = parts.netloc.split("@")[-1].split(":")[0].lower()
        return host.endswith(".breezy.hr") and parts.path.strip("/") == ""

    @staticmethod
    def _extract_jd(html: str | None) -> str | None:
        """Extract the JD body from the position page's ``#description`` /
        ``.position-description`` container: strip breadcrumb / apply-button / script chrome and
        ``%…%`` i18n placeholders, collapse whitespace. Returns ``None`` when the container is
        absent or empty after cleaning (the caller then classifies alive/gone/raise)."""
        if not html:
            return None
        from selectolax.parser import HTMLParser

        node = HTMLParser(html).css_first(_DESCRIPTION_SELECTOR)
        if node is None:
            return None
        for noise in node.css(_JD_NOISE_SELECTOR):
            noise.decompose()
        text = node.text(separator=" ", strip=True)
        if not text:
            return None
        text = _PLACEHOLDER_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    def _raws_from_data(self, data: Any, token: str) -> list[RawJob]:
        positions: list[dict[str, Any]] = data if isinstance(data, list) else []
        raws: list[RawJob] = []
        for pos in positions:
            company = pos.get("company")
            company_name = company.get("name") if isinstance(company, dict) else None
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(pos.get("id") or pos.get("_id") or ""),
                    company=company_name or token,
                    token=token,
                    url=pos.get("url"),
                    payload=pos,
                )
            )
        return raws

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        location = self._location(p)
        remote = (
            RemoteType.REMOTE
            if (location is not None and location.is_remote)
            else (RemoteType.UNKNOWN)
        )

        category = p.get("category")
        department = (
            (category.get("name") if isinstance(category, dict) else None)
            or (p.get("department") if isinstance(p.get("department"), str) else None)
            or None
        )

        description_html = p.get("description") or None
        description_text = self._to_text(description_html)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("name") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url"),
            locations=[location] if location else [],
            remote=remote,
            employment_type=_employment(p.get("type")),
            department=department,
            salary=parse_salary(p.get("salary") if isinstance(p.get("salary"), str) else None),
            posted_at=_parse_dt(p.get("published_date") or p.get("creation_date")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    @staticmethod
    def _location(p: dict[str, Any]) -> Location | None:
        loc = p.get("location")
        if not isinstance(loc, dict):
            return None
        city = (loc.get("city") or "").strip() or None
        state = loc.get("state")
        region = (state.get("name") if isinstance(state, dict) else state) or None
        if isinstance(region, str):
            region = region.strip() or None
        country = loc.get("country")
        country = (country.get("name") if isinstance(country, dict) else country) or None
        if isinstance(country, str):
            country = country.strip() or None
        raw_loc = (loc.get("name") or "").strip() or None
        is_remote = bool(loc.get("is_remote")) or "remote" in (raw_loc or "").lower()
        if not any((city, region, country, raw_loc)) and not is_remote:
            return None
        return Location(
            city=city,
            region=region,
            country=country,
            raw=raw_loc,
            is_remote=is_remote,
        )

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None
