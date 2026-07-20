"""Tier-3 detail fetcher: RipplingProvider.fetch_detail.

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``providers/smartrecruiters.py``'s hardened 404-vs-transient contract (``providers/base.py``):
returns ``None`` ONLY on a real HTTP 404/410 (confirmed-gone); every indeterminate/transient
condition -- an unbuildable ref, a fetch exception, a non-404 HTTP status, a non-dict payload, or
an empty/missing/malformed ``description`` -- RAISES instead, so the liveness sweep (rippling is in
``CONFIRM_VIA_DETAIL_SOURCES`` with a confirmed-streak threshold of 1) never expires a still-live
posting on an ambiguous signal."""

from __future__ import annotations

import anyio
import httpx
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.rippling import RipplingProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._p


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "GET", "https://api.rippling.com/platform/api/ats/v1/board/acme-co/jobs/uuid"
    )
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


class _RaisingStatusFetcher:
    """Raises a fixed httpx.HTTPStatusError (a real HTTP status) on every get_json."""

    def __init__(self, status: int) -> None:
        self._exc = _http_status_error(status)
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        raise self._exc


def test_rippling_fetch_detail_concatenates_description_dict_shape_1() -> None:
    payload = {"description": {"company": "<p>A</p>", "role": "<p>B</p>"}}
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="1",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/11fs-group-ltd/jobs/3c36-uuid-1",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>A</p>\n<p>B</p>"
    assert fetcher.calls == [
        "https://api.rippling.com/platform/api/ats/v1/board/11fs-group-ltd/jobs/3c36-uuid-1"
    ]


def test_rippling_fetch_detail_concatenates_description_dict_shape_2() -> None:
    payload = {"description": {"company": "<p>C</p>", "requirements": "<p>D</p>"}}
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="2",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/1nhealth/jobs/9f21-uuid-2",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>C</p>\n<p>D</p>"
    assert fetcher.calls == [
        "https://api.rippling.com/platform/api/ats/v1/board/1nhealth/jobs/9f21-uuid-2"
    ]


def test_rippling_fetch_detail_falls_back_to_listing_url() -> None:
    payload = {"description": {"role": "<p>Fallback JD</p>"}}
    fetcher = _FakeFetcher(payload)
    ref = DetailRef(
        id="3",
        source="rippling",
        token=None,
        apply_url=None,
        listing_url="https://ats.rippling.com/acme-co/jobs/uuid-3",
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>Fallback JD</p>"
    assert fetcher.calls == [
        "https://api.rippling.com/platform/api/ats/v1/board/acme-co/jobs/uuid-3"
    ]


def test_rippling_fetch_detail_plain_string_description_returned_directly() -> None:
    payload = {"description": "<p>Plain string JD</p>"}
    ref = DetailRef(
        id="4",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-4",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc == "<p>Plain string JD</p>"


def test_rippling_fetch_detail_missing_description_raises() -> None:
    # A 200 with no ``description`` is an unclassifiable shape, NOT a confirmed-gone signal -> raise.
    payload: dict = {"someOtherKey": {}}
    ref = DetailRef(
        id="5",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-5",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_empty_description_dict_raises() -> None:
    payload = {"description": {}}
    ref = DetailRef(
        id="6",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-6",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_empty_string_description_raises() -> None:
    payload = {"description": "   "}
    ref = DetailRef(
        id="7",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-7",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_non_dict_non_str_description_raises() -> None:
    payload = {"description": ["not", "a", "dict-or-str"]}
    ref = DetailRef(
        id="8",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-8",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_truthy_non_dict_payload_raises() -> None:
    # ``data`` itself truthy but not a dict is an unclassifiable shape -> raise (never a death signal).
    payload = "oops-a-string"
    ref = DetailRef(
        id="9",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-9",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_unparseable_urls_raises() -> None:
    # An unbuildable detail URL is NOT evidence of death -> raise (mirrors smartrecruiters).
    payload = {"description": {"role": "<p>Should never be fetched</p>"}}
    ref = DetailRef(
        id="10",
        source="rippling",
        token=None,
        apply_url="https://example.com/not-a-rippling-url",
        listing_url="https://example.com/also-not-rippling",
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_no_urls_no_token_raises() -> None:
    payload = {"description": {"role": "<p>Should never be fetched</p>"}}
    ref = DetailRef(
        id="11",
        source="rippling",
        token=None,
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _FakeFetcher(payload)))


def test_rippling_fetch_detail_transient_fetch_error_propagates() -> None:
    # A bare (non-HTTPStatusError) fetch exception is indeterminate -> propagates, never None.
    class _RaisingFetcher:
        async def get_json(self, url: str, **kw: object) -> object:
            raise RuntimeError("boom (e.g. timeout / connection reset)")

    ref = DetailRef(
        id="12",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-12",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _RaisingFetcher()))


def test_rippling_fetch_detail_5xx_status_raises_not_none() -> None:
    # (R4 a) A transient HTTP 503 MUST raise, never collapse to None -- else the liveness sweep
    # (threshold 1) would expire a live posting on a single blip.
    ref = DetailRef(
        id="13",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-13",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _RaisingStatusFetcher(503)))


def test_rippling_fetch_detail_429_status_raises_not_none() -> None:
    ref = DetailRef(
        id="14",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-14",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(lambda: RipplingProvider().fetch_detail(ref, _RaisingStatusFetcher(429)))


def test_rippling_fetch_detail_404_returns_none() -> None:
    # (R4 b) A real HTTP 404 is the ONLY confirmed-gone signal -> returns None (expire the row).
    ref = DetailRef(
        id="15",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-15",
        listing_url=None,
        content_sig="s",
    )
    res = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _RaisingStatusFetcher(404)))
    assert res is None


def test_rippling_fetch_detail_410_returns_none() -> None:
    ref = DetailRef(
        id="16",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/acme-co/jobs/uuid-16",
        listing_url=None,
        content_sig="s",
    )
    res = anyio.run(lambda: RipplingProvider().fetch_detail(ref, _RaisingStatusFetcher(410)))
    assert res is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(
        id="1", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({})))
    assert desc is None


# --- structured pay (payRangeDetails -> DetailFetch.salary) ------------------------------------


def _ref() -> DetailRef:
    return DetailRef(
        id="1",
        source="rippling",
        token=None,
        apply_url="https://ats.rippling.com/covenant-house-new-york/jobs/uuid-1",
        listing_url=None,
        content_sig="s",
    )


def test_fetch_detail_returns_detailfetch_with_structured_salary() -> None:
    from ergon_tracker.models import DetailFetch, SalaryInterval

    payload = {
        "description": {"role": "<p>Do the thing.</p>"},
        "payRangeDetails": [
            {"currency": "USD", "frequency": "YEAR", "rangeStart": 55000.0, "rangeEnd": 65000.0}
        ],
    }
    res = anyio.run(lambda: RipplingProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert isinstance(res, DetailFetch)
    assert res.text == "<p>Do the thing.</p>"
    assert res.salary is not None
    assert res.salary.min_amount == 55000 and res.salary.max_amount == 65000
    assert res.salary.currency == "USD" and res.salary.interval is SalaryInterval.YEAR


def test_fetch_detail_returns_bare_str_when_no_payrange() -> None:
    # No payRangeDetails -> unchanged historical contract (bare str), so nothing downstream shifts.
    payload = {"description": "<p>Body only.</p>"}
    res = anyio.run(lambda: RipplingProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert res == "<p>Body only.</p>"


def test_fetch_detail_empty_payrange_is_bare_str() -> None:
    payload = {"description": "<p>Body.</p>", "payRangeDetails": []}
    res = anyio.run(lambda: RipplingProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert res == "<p>Body.</p>"


def test_salary_from_payrange_edge_cases() -> None:
    from ergon_tracker.models import SalaryInterval

    P = RipplingProvider._salary_from_payrange
    assert P(None) is None and P([]) is None and P("nope") is None
    assert P([{"frequency": "YEAR"}]) is None  # no amounts
    assert P([{"rangeStart": None, "rangeEnd": None, "currency": "USD"}]) is None
    # hourly equal bounds are a valid single-point figure
    hr = P([{"currency": "USD", "frequency": "HOUR", "rangeStart": 24.28, "rangeEnd": 24.28}])
    assert hr.min_amount == 24.28 and hr.max_amount == 24.28 and hr.interval is SalaryInterval.HOUR
    # multi geo-tier, same currency -> span (min start, max end)
    span = P(
        [
            {"currency": "USD", "frequency": "YEAR", "rangeStart": 97200, "rangeEnd": 170100},
            {"currency": "USD", "frequency": "YEAR", "rangeStart": 91800, "rangeEnd": 160650},
        ]
    )
    assert span.min_amount == 91800 and span.max_amount == 170100
    # multi-currency -> headline (first) currency only, CAD never merged in
    mc = P(
        [
            {"currency": "USD", "frequency": "YEAR", "rangeStart": 156000, "rangeEnd": 260000},
            {"currency": "CAD", "frequency": "YEAR", "rangeStart": 128000, "rangeEnd": 160000},
        ]
    )
    assert mc.currency == "USD" and mc.min_amount == 156000 and mc.max_amount == 260000
    # unknown frequency -> keep amounts, interval unset (never guessed)
    unk = P([{"currency": "USD", "frequency": "FORTNIGHT", "rangeStart": 2000, "rangeEnd": 2500}])
    assert unk.min_amount == 2000 and unk.interval is None


def test_fetch_detail_recovers_worklocations_strings() -> None:
    from ergon_tracker.models import DetailFetch

    payload = {"description": "<p>Role.</p>", "workLocations": ["London, United Kingdom", ""]}
    res = anyio.run(lambda: RipplingProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert isinstance(res, DetailFetch)
    assert [loc.raw for loc in res.locations] == ["London, United Kingdom"]  # empties skipped


# --- (R4 c) liveness classify_row treats a RAISED rippling confirm as KEEP, never flipped_dead --


def test_liveness_keeps_row_when_rippling_confirm_raises_transient(tmp_path) -> None:
    """Integration guard: route the REAL RipplingProvider.fetch_detail (against a 503-raising
    fetcher) through reconcile_liveness_tier for a rippling row that has left its board's fresh
    list. Because rippling is in CONFIRM_VIA_DETAIL_SOURCES (confirmed-streak threshold 1), the OLD
    None-on-transient behavior would have expired this live posting on a single blip. The hardened
    provider RAISES instead, and the liveness pass classifies a raise as confirm_errored -> KEEP."""
    import sqlite3

    from ergon_tracker.index.db import fresh_db
    from ergon_tracker.index.liveness import CONFIRM_VIA_DETAIL_SOURCES, reconcile_liveness_tier

    assert "rippling" in CONFIRM_VIA_DETAIL_SOURCES

    idx = tmp_path / "index.sqlite"
    fresh_db(idx)
    con = sqlite3.connect(idx)
    ts = "2026-07-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token, "
        "apply_url) VALUES (?, ?, 'rippling', 'Acme', 'Engineer', 'unknown', 'mid', 'full_time', "
        "'active', ?, ?, ?, 'b0', 'acme-co', ?)",
        ("rp-1", "ch-1", ts, ts, ts, "https://ats.rippling.com/acme-co/jobs/uuid-rp-1"),
    )
    con.commit()
    con.close()

    liv = str(tmp_path / "liveness.sqlite")

    async def fetch_board(source: str, token: str) -> set[str]:
        return set()  # list-miss: the posting left the fresh board list

    async def fetch_detail(ref):  # dispatch to the REAL hardened provider, transient 503
        return await RipplingProvider().fetch_detail(ref, _RaisingStatusFetcher(503))

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, str(idx), fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: ts
        )
    )
    assert stats["flipped_dead"] == 0  # NEVER expired on a transient confirm error
    assert stats["confirm_errored"] == 1

    con = sqlite3.connect(idx)
    status, reason = con.execute(
        "SELECT status, expiry_reason FROM jobs WHERE id = 'rp-1'"
    ).fetchone()
    con.close()
    assert status == "active" and reason is None
