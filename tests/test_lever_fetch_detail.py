"""Tier-3 detail fetcher: LeverProvider.fetch_detail.

Offline only -- a FakeFetcher stands in for AsyncFetcher; no live network. Mirrors
``rippling.fetch_detail``'s clean-404 contract (``providers/base.py``): returns ``None`` ONLY on a
real HTTP 404/410 (Lever's textbook ``{"ok":false,"error":"Document not found"}``); every
indeterminate/transient condition -- an unbuildable ref, a fetch exception, a non-404 HTTP status,
a non-dict payload, or an empty/missing description -- RAISES instead, so the liveness sweep (lever
is in ``CONFIRM_VIA_DETAIL_SOURCES`` with a confirmed-streak threshold of 1) never expires a
still-live posting on an ambiguous signal."""

from __future__ import annotations

import anyio
import httpx
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.lever import LeverProvider


class _FakeFetcher:
    def __init__(self, payload: object) -> None:
        self._p = payload
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        return self._p


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.lever.co/v0/postings/acme/uuid?mode=json")
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


class _RaisingStatusFetcher:
    def __init__(self, status: int) -> None:
        self._exc = _http_status_error(status)
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        raise self._exc


def _ref(
    *,
    id: str = "abc-123",
    token: str | None = "acme",
    apply_url: str | None = None,
    listing_url: str | None = None,
) -> DetailRef:
    return DetailRef(
        id=id,
        source="lever",
        token=token,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def test_lever_fetch_detail_returns_description_plain() -> None:
    fetcher = _FakeFetcher({"descriptionPlain": "Build the thing.", "text": "Engineer"})
    res = anyio.run(lambda: LeverProvider().fetch_detail(_ref(), fetcher))
    assert res == "Build the thing."
    assert fetcher.calls == ["https://api.lever.co/v0/postings/acme/abc-123?mode=json"]


def test_lever_fetch_detail_eu_token_routes_to_eu_host() -> None:
    fetcher = _FakeFetcher({"descriptionPlain": "EU role."})
    res = anyio.run(lambda: LeverProvider().fetch_detail(_ref(token="cirrus|eu"), fetcher))
    assert res == "EU role."
    assert fetcher.calls == ["https://api.eu.lever.co/v0/postings/cirrus/abc-123?mode=json"]


def test_lever_fetch_detail_concats_sections_when_no_plain() -> None:
    payload = {
        "opening": "About us.",
        "descriptionBody": "The role.",
        "description": "<p>ignored-when-plainless? no, joined</p>",
    }
    res = anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _FakeFetcher(payload)))
    assert res == "About us.\nThe role.\n<p>ignored-when-plainless? no, joined</p>"


def test_lever_fetch_detail_falls_back_to_apply_url() -> None:
    # No token -> parse (token, id) out of the public apply URL (note the trailing /apply segment).
    fetcher = _FakeFetcher({"descriptionPlain": "From URL."})
    ref = _ref(token=None, apply_url="https://jobs.lever.co/acme/xy-9/apply")
    res = anyio.run(lambda: LeverProvider().fetch_detail(ref, fetcher))
    assert res == "From URL."
    assert fetcher.calls == ["https://api.lever.co/v0/postings/acme/xy-9?mode=json"]


def test_lever_fetch_detail_404_returns_none() -> None:
    res = anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _RaisingStatusFetcher(404)))
    assert res is None


def test_lever_fetch_detail_410_returns_none() -> None:
    res = anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _RaisingStatusFetcher(410)))
    assert res is None


def test_lever_fetch_detail_5xx_raises_not_none() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _RaisingStatusFetcher(503)))


def test_lever_fetch_detail_429_raises_not_none() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _RaisingStatusFetcher(429)))


def test_lever_fetch_detail_non_dict_payload_raises() -> None:
    with pytest.raises(RuntimeError):
        anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _FakeFetcher(["not", "a", "dict"])))


def test_lever_fetch_detail_empty_description_raises() -> None:
    with pytest.raises(RuntimeError):
        anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _FakeFetcher({"descriptionPlain": "  "})))


def test_lever_fetch_detail_missing_description_raises() -> None:
    with pytest.raises(RuntimeError):
        anyio.run(lambda: LeverProvider().fetch_detail(_ref(), _FakeFetcher({"text": "title only"})))


def test_lever_fetch_detail_unbuildable_ref_raises() -> None:
    # No token AND no lever-shaped URL -> unbuildable (indeterminate), never a death signal.
    ref = _ref(token=None, apply_url="https://example.com/not-lever")
    with pytest.raises(RuntimeError):
        anyio.run(lambda: LeverProvider().fetch_detail(ref, _FakeFetcher({"descriptionPlain": "x"})))
