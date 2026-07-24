"""Workable job-board provider.

Workable exposes a free, unauthenticated public widget endpoint:
``GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true`` returning
``{name, description, jobs: [...]}`` in a single call (no pagination). :meth:`fetch` passes
``details=true`` so EVERY job in that one response carries its full ``description`` (HTML) inline
— folding what used to be a separate per-posting Tier-3 JD drain into the bulk call we already
make (live-measured same latency, no extra rate pressure; see :meth:`fetch`). Each job also
carries a ``shortcode`` (the stable id), ``title``, ``employment_type`` label, structured
``locations`` plus flat ``country``/``city``/``state`` fields, a ``telecommuting`` remote
flag, ``department`` and an apply ``url``. There is no server-side filtering, so
:meth:`fetch` returns the whole board and the orchestrator applies
``SearchQuery.matches`` client-side.

Tier-3 detail recovery (now a fallback + liveness-confirm path)
---------------------------------------------------------------
Because :meth:`fetch` now requests ``details=true``, a freshly-crawled Workable board already
captures every JD in bulk, so :meth:`fetch_detail` is no longer the primary JD source — it is
retained as (a) the liveness gone-signal confirmer (``CONFIRM_VIA_DETAIL_SOURCES``: a board-fetch
failure during a crawl would otherwise make every posting on that board look list-missing, a false
positive) and (b) a JD fallback for any residual no-JD row (a carry-forward from before this
change, or a board whose bulk fetch failed). Workable is therefore dropped from the Tier-3 JD
drain (``build_index._TIER3_DETAIL_SOURCES``) but kept in the liveness confirm set.

Live-verified: ``GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true``
(the SAME bulk widget endpoint :meth:`fetch` already calls, plus ``details=true``) returns
EVERY job on that board WITH a full ``description`` (HTML) in ONE unauthenticated call
(probed: 1,100 jobs in a single response). That means per-posting detail recovery doesn't need
a per-job round trip at all — one board fetch answers for every posting on that board.

:meth:`fetch_detail` exploits this with a per-process memo cache (:data:`_desc_by_shortcode`):
the account/board "slug" for a shortcode is resolved the same way as before — a bare
``https://apply.workable.com/j/{shortcode}`` shortlink doesn't embed it, so it takes ONE
redirect hop (``GET .../j/{shortcode}`` with redirects disabled → the ``Location`` header's
``/{slug}/j/{shortcode}`` reveals it; ``ref.token``, when the index has it, skips this hop) —
but instead of then hitting a per-job resource, we fetch the WHOLE board once and cache
every sibling's description by shortcode. So the first posting fetched from a given board pays
the redirect + board fetch; every other posting on that board (order-independent) is a pure
cache hit — zero network calls. At ~24 postings/board on average this cuts ~67k per-posting
2-hop calls down to ~2,800 board fetches for the whole crawl.

This was chosen over the authenticated ``spi/v3`` REST API (requires a per-account Bearer
token we don't have), the per-job ``api/v1/accounts/{slug}/jobs/{shortcode}`` resource (works,
but throws away the "whole board in one call" win), and the unofficial
``/{slug}/jobs/view/{shortcode}.md`` LLM-crawler markdown surface (undocumented, narrower
"cleanliness" than a JSON resource in the same public API family already used by :meth:`fetch`).
See :meth:`WorkableProvider.fetch_detail`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio

from ..extract.degree import degree_from_ats_vocab
from ..extract.level import level_from_ats_vocab
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

__all__ = ["WorkableProvider", "_reset_workable_cache"]

_API = "https://apply.workable.com/api/v1/widget/accounts/{token}"
_SHORTLINK_HOST = "apply.workable.com"

# --- per-run detail memo cache (module-level: persists across a reconcile run's single
# process) -----------------------------------------------------------------------------------
# Board-bulk memoization (Tier-3 JD recovery, see module docstring): every posting's description
# gets cached by shortcode the first time ANY posting from its board is fetched, so siblings on
# the same board are pure cache hits (zero network). ``_desc_by_shortcode`` holds the concatenated
# description (or ``None`` when the board response had no usable description for that shortcode);
# key presence means "already resolved", not "has a description". ``_fetched_board_slugs`` tracks
# which boards have already been bulk-fetched so a shortcode that's absent from its board's
# response (e.g. since removed/relisted) doesn't trigger a repeat board fetch.
_desc_by_shortcode: dict[str, str | None] = {}
_fetched_board_slugs: set[str] = set()

# Bounded re-attempt tracking for a board whose bulk fetch FAILS (network error / malformed
# payload): the slug is deliberately NOT added to ``_fetched_board_slugs`` on failure (see
# ``_fetch_board``), so a later posting on the same board -- or a reconcile retry -- gets a
# chance to recover instead of being permanently poisoned (the bug this fixes: one dropped
# board fetch used to leave EVERY sibling posting on that board returning ``None`` for the whole
# run). But an unbounded retry would let a genuinely-dead board be re-hammered by every one of
# its (possibly hundreds of) sibling postings. ``_board_fail_counts`` counts failed attempts per
# slug; once a slug hits ``_MAX_BOARD_FETCH_ATTEMPTS`` it is treated as known-failed and
# ``_fetch_board`` returns fast without re-fetching.
_board_fail_counts: dict[str, int] = {}
_MAX_BOARD_FETCH_ATTEMPTS = 3

# Per-slug async once-gate (fixes a check-then-act RACE): concurrent sibling coroutines for
# DIFFERENT postings on the SAME board must AWAIT one board fetch rather than each racing to
# start (or skip) it. ``_board_locks`` holds one ``anyio.Lock`` per slug, created lazily; a
# single ``_board_locks_guard`` lock serializes creation of those per-slug locks (a bare
# ``dict.setdefault``/``defaultdict`` is NOT concurrency-safe to populate — two coroutines could
# both decide "no lock yet" and hand out two different lock objects for the same slug, which
# defeats the whole point). Once a per-slug lock object exists, holding *that* lock (not the
# guard) around the double-checked ``_fetched_board_slugs`` read + bulk fetch is what actually
# serializes siblings: the first coroutine in does the fetch; the others block on the lock and,
# on acquiring it, find the board already populated and return immediately.
_board_locks: dict[str, anyio.Lock] = {}
_board_locks_guard = anyio.Lock()


async def _lock_for_slug(slug: str) -> anyio.Lock:
    """Return the (lazily-created) per-slug lock for ``slug``, safe under concurrent callers."""
    async with _board_locks_guard:
        lock = _board_locks.get(slug)
        if lock is None:
            lock = anyio.Lock()
            _board_locks[slug] = lock
        return lock


def _reset_workable_cache() -> None:
    """Clear the module-level detail memo cache. Test-only: isolates tests from each other and
    from prior process state; production code never needs to call this (the cache is meant to
    live for the whole reconcile run)."""
    _desc_by_shortcode.clear()
    _fetched_board_slugs.clear()
    _board_fail_counts.clear()
    _board_locks.clear()


# Path pieces around a shortlink URL: {..., "j", shortcode, ...}. Used both on the canonical
# bare shortlink (``apply.workable.com/j/{shortcode}``) and the post-redirect/full shape
# (``apply.workable.com/{token}/j/{shortcode}``) and its legacy host cousin
# (``{token}.workable.com/j/{shortcode}``).
_LOCATION_TOKEN_RE = re.compile(r"/([^/?#]+)/j/([^/?#]+)", re.I)

# Hosts we recognise, each capturing the board token as group 1.
_HOST_PATTERNS = (
    re.compile(r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([^/?#]+)", re.I),
    re.compile(r"([^./?#]+)\.workable\.com", re.I),
)

# Workable ``employment_type`` labels → canonical EmploymentType.
_EMPLOYMENT_BY_LABEL = {
    "full-time": EmploymentType.FULL_TIME,
    "full time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "internship": EmploymentType.INTERNSHIP,
    "intern": EmploymentType.INTERNSHIP,
}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


@register("workable")
class WorkableProvider(BaseProvider):
    name = "workable"

    @classmethod
    def matches(cls, url_or_host: str) -> str | None:
        for pattern in _HOST_PATTERNS:
            m = pattern.search(url_or_host)
            if m:
                token = m.group(1).strip("/")
                if token and token not in ("apply", "www", "api"):
                    return token
        return None

    async def fetch(self, token: str, query: SearchQuery, fetcher: AsyncFetcher) -> list[RawJob]:
        # Single call: Workable returns the whole board (no server-side filters). ``details=true``
        # makes that SAME bulk call carry every job's full ``description`` (HTML) inline (see the
        # module docstring's live probe: 1,100 jobs WITH descriptions in one response), so
        # :meth:`normalize` populates the JD directly — no per-posting Tier-3 drain needed. It's the
        # same endpoint at the same cost (live-measured: same latency, no extra rate pressure); the
        # whole board's descriptions come back in the one response, so there is nothing to dedup per
        # posting here (the shortcode memo in :meth:`fetch_detail` only matters for the per-posting
        # recovery path, which this bulk capture makes redundant for a freshly-crawled board).
        url = _API.format(token=token)
        data = await fetcher.get_json(url, params={"details": "true"})
        account = data.get("name") or token if isinstance(data, dict) else token
        jobs: list[dict[str, Any]] = data.get("jobs", []) if isinstance(data, dict) else []

        raws: list[RawJob] = []
        for job in jobs:
            raws.append(
                RawJob(
                    source=self.name,
                    source_job_id=str(job.get("shortcode", "")),
                    company=account,
                    token=token,
                    url=job.get("url") or job.get("shortlink"),
                    payload=job,
                )
            )
        return raws

    # --- detail (Tier-3 JD recovery) -----------------------------------------

    @staticmethod
    def _full_shortlink(url: str) -> tuple[str, str] | None:
        """Return ``(token, shortcode)`` when ``url`` already embeds both.

        Handles the post-redirect/full public shape
        ``https://apply.workable.com/{token}/j/{shortcode}`` and the legacy host cousin
        ``https://{token}.workable.com/j/{shortcode}``. Never raises."""
        try:
            parts = urlsplit(url if "://" in url else f"https://{url}")
        except Exception:
            return None
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        if not host:
            return None
        if host == _SHORTLINK_HOST:
            m = _LOCATION_TOKEN_RE.search(parts.path)
            if m:
                return m.group(1), m.group(2)
            return None
        m = re.match(r"^([^.]+)\.workable\.com$", host)
        if not m:
            return None
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) >= 2 and segments[0].lower() == "j":
            return m.group(1), segments[1]
        return None

    @staticmethod
    def _bare_shortcode(url: str) -> str | None:
        """Return the shortcode from the BARE shortlink shape
        ``https://apply.workable.com/j/{shortcode}`` (no token embedded) — the shape the bulk
        widget endpoint actually returns and what the index stores as ``apply_url`` today.
        Never raises."""
        try:
            parts = urlsplit(url if "://" in url else f"https://{url}")
        except Exception:
            return None
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        if host != _SHORTLINK_HOST:
            return None
        segments = [seg for seg in parts.path.split("/") if seg]
        if len(segments) >= 2 and segments[0].lower() == "j":
            return segments[1]
        return None

    async def _resolve_token(self, shortcode: str, fetcher: AsyncFetcher) -> str | None:
        """Resolve the account token for a bare shortlink via ONE redirect hop: request
        ``/j/{shortcode}`` with redirects disabled and read the token out of the ``Location``
        header's path (``/{token}/j/{shortcode}``). Non-raising: any fetch failure, missing
        header, or unrecognisable shape returns ``None``."""
        url = f"https://{_SHORTLINK_HOST}/j/{shortcode}"
        try:
            resp = await fetcher.request("GET", url, follow_redirects=False)
        except Exception:
            return None
        location = resp.headers.get("location")
        if not isinstance(location, str) or not location:
            return None
        m = _LOCATION_TOKEN_RE.search(location)
        if not m:
            return None
        return m.group(1)

    async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
        """Fetch one posting's full JD via the board-bulk memo cache (Tier-3 recovery, see
        module docstring).

        The shortcode is parsed from ``ref.apply_url`` (falling back to ``ref.listing_url``).
        A cache hit (this shortcode already resolved by an earlier call — either directly, or as
        a sibling on a board already bulk-fetched this run) returns immediately with NO network
        call. On a miss, the board slug is taken from ``ref.token`` when present, else resolved
        via one redirect hop (see :meth:`_resolve_token`); the whole board is then bulk-fetched
        ONCE (see :meth:`_fetch_board`), priming the cache for every sibling posting on it before
        this posting's (now-cached) description is returned. Concurrent siblings for OTHER
        postings on the SAME board AWAIT that one in-flight fetch via a per-slug ``anyio.Lock``
        (see :data:`_board_locks`) instead of racing it — without the lock, a sibling could see
        the slug already "claimed" and read the cache before the fetch that claimed it had
        actually populated anything, permanently returning ``None`` for the rest of the run.

        Non-raising: any unparseable URL, failed hop, or failed/malformed board fetch returns
        ``None``, never an exception."""
        slug: str | None = None
        shortcode: str | None = None
        for url in (ref.apply_url, ref.listing_url):
            if not url:
                continue
            full = self._full_shortlink(url)
            if full is not None:
                slug, shortcode = full
                break
        if shortcode is None:
            for url in (ref.apply_url, ref.listing_url):
                if not url:
                    continue
                shortcode = self._bare_shortcode(url)
                if shortcode:
                    break
        if not shortcode:
            return None

        if shortcode in _desc_by_shortcode:
            return _desc_by_shortcode[shortcode]

        if not slug:
            slug = ref.token or await self._resolve_token(shortcode, fetcher)
        if not slug:
            return None

        # Concurrent siblings for OTHER postings on this SAME board must AWAIT the in-flight
        # fetch rather than each independently deciding to start (or skip) one -- a bare
        # ``if slug not in _fetched_board_slugs`` check-then-act here is exactly the race this
        # lock closes: without it, two coroutines can both see "not fetched yet", both proceed,
        # or (worse) one marks it fetched and returns before the fetch actually populates the
        # cache, so the other reads ``_desc_by_shortcode`` while it's still empty.
        lock = await _lock_for_slug(slug)
        async with lock:
            # Double-checked: another sibling may have finished populating the board -- or
            # exhausted its bounded retry budget -- while we were waiting for the lock.
            if (
                slug not in _fetched_board_slugs
                and _board_fail_counts.get(slug, 0) < _MAX_BOARD_FETCH_ATTEMPTS
            ):
                await self._fetch_board(slug, fetcher)

        return _desc_by_shortcode.get(shortcode)

    @staticmethod
    async def _fetch_board(slug: str, fetcher: AsyncFetcher) -> None:
        """Bulk-fetch every job on ``slug``'s board (with descriptions) and prime
        :data:`_desc_by_shortcode` for all of them in one shot.

        Marks ``slug`` as fetched ONLY on a full, well-formed success -- a failed or malformed
        response instead increments :data:`_board_fail_counts` for ``slug`` (never adding it to
        :data:`_fetched_board_slugs`), so a later posting on the SAME board, or a reconcile
        retry, gets a chance to recover it rather than being permanently poisoned by one dropped
        request (previously: the slug was marked "fetched" unconditionally before the fetch even
        started, so a single failed board fetch returned ``None`` for every sibling posting on
        that board for the rest of the run). The caller (:meth:`fetch_detail`) bounds re-attempts
        via :data:`_MAX_BOARD_FETCH_ATTEMPTS` so a genuinely-dead board isn't re-hammered by
        every one of its sibling postings forever. Always called while holding ``slug``'s
        per-slug lock (see :func:`_lock_for_slug`), so there is never more than one call in
        flight per slug. Non-raising."""
        url = _API.format(token=slug)
        try:
            data = await fetcher.get_json(url, params={"details": "true"})
        except Exception:
            _board_fail_counts[slug] = _board_fail_counts.get(slug, 0) + 1
            return
        if not isinstance(data, dict):
            _board_fail_counts[slug] = _board_fail_counts.get(slug, 0) + 1
            return
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            _board_fail_counts[slug] = _board_fail_counts.get(slug, 0) + 1
            return
        _fetched_board_slugs.add(slug)
        for job in jobs:
            if not isinstance(job, dict):
                continue
            code = job.get("shortcode")
            if not isinstance(code, str) or not code:
                continue
            _desc_by_shortcode[code] = WorkableProvider._extract_description(job)

    @staticmethod
    def _extract_description(job: dict[str, Any]) -> str | None:
        """Concatenate ``description`` + ``requirements`` + ``benefits`` (whichever are
        present) from one job record of a board-bulk response, matching the field shape of the
        (now-retired) per-job resource. ``None`` when there's no usable description text."""
        description = job.get("description")
        if not isinstance(description, str) or not description.strip():
            return None
        parts = [description]
        for key in ("requirements", "benefits"):
            extra = job.get(key)
            if isinstance(extra, str) and extra.strip():
                parts.append(extra)
        return "\n".join(parts)

    def normalize(self, raw: RawJob) -> JobPosting:
        p = raw.payload

        locations = self._locations(p)
        telecommuting = bool(p.get("telecommuting"))
        remote = RemoteType.REMOTE if telecommuting else self._remote(locations)
        employment_type = self._employment_type(p.get("employment_type"))
        degree_min = degree_from_ats_vocab(p.get("education"))
        # Workable's "education" field is the ATS's own structured minimum-education setting for
        # the requisition, not free text — so a recognised value IS the posting's stated
        # requirement (never "preferred"). Setting degree_required=True here lets the enrich degree
        # guard (`degree_min is None and degree_required is None`) skip the text extractor only when
        # we actually have a mapped value; when education is absent/unrecognized both stay None so
        # the extractor still gets a chance to find a requirement in the description.
        degree_required = True if degree_min is not None else None

        # The bulk ``?details=true`` widget call (see :meth:`fetch`) carries every job's full JD
        # inline, so the description is present WITHOUT any per-posting fetch. Reuse
        # :meth:`_extract_description` (description + requirements + benefits) so bulk capture and
        # the Tier-3 fallback (:meth:`fetch_detail`) yield byte-identical JD text.
        description_html = self._extract_description(p)
        description_text = self._to_text(description_html)

        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=p.get("title") or "",
            fetched_at=raw.fetched_at,
            apply_url=p.get("url") or p.get("application_url") or p.get("shortlink"),
            locations=locations,
            remote=remote,
            employment_type=employment_type,
            department=p.get("department") or None,
            level=level_from_ats_vocab(p.get("experience")),
            degree_min=degree_min,
            degree_required=degree_required,
            salary=None,  # not exposed by the widget endpoint
            posted_at=_parse_date(p.get("published_on")),
            description_html=description_html,
            description_text=description_text,
            raw=raw.payload,
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _to_text(html: str | None) -> str | None:
        """Flatten JD HTML to plain text (mirrors greenhouse/breezy ``_to_text``)."""
        if not html:
            return None
        from selectolax.parser import HTMLParser

        text = HTMLParser(html).text(separator=" ", strip=True)
        return text or None

    @staticmethod
    def _locations(p: dict[str, Any]) -> list[Location]:
        telecommuting = bool(p.get("telecommuting"))
        structured = p.get("locations")
        if isinstance(structured, list) and structured:
            out: list[Location] = []
            for loc in structured:
                if not isinstance(loc, dict):
                    continue
                out.append(
                    Location(
                        city=loc.get("city") or None,
                        region=loc.get("region") or None,
                        country=loc.get("country") or None,
                        raw=WorkableProvider._raw_text(
                            loc.get("city"), loc.get("region"), loc.get("country")
                        ),
                        is_remote=telecommuting,
                    )
                )
            if out:
                return out

        # Fall back to the flat country/city/state fields.
        city = p.get("city")
        region = p.get("state")
        country = p.get("country")
        raw = WorkableProvider._raw_text(city, region, country)
        if not any((city, region, country, telecommuting)):
            return []
        return [
            Location(
                city=city or None,
                region=region or None,
                country=country or None,
                raw=raw,
                is_remote=telecommuting,
            )
        ]

    @staticmethod
    def _raw_text(*parts: Any) -> str | None:
        text = ", ".join(str(p) for p in parts if p)
        return text or None

    @staticmethod
    def _remote(locations: list[Location]) -> RemoteType:
        if any(loc.is_remote for loc in locations):
            return RemoteType.REMOTE
        if locations:
            return RemoteType.ONSITE
        return RemoteType.UNKNOWN

    @staticmethod
    def _employment_type(label: str | None) -> EmploymentType:
        key = str(label or "").strip().lower()
        if not key:
            return EmploymentType.UNKNOWN
        return _EMPLOYMENT_BY_LABEL.get(key, EmploymentType.OTHER)
