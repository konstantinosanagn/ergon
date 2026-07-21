"""Tier-3 detail fetcher: PeopleAdminProvider.fetch_detail.

The Atom feed's ``<content>`` is a ~340-char truncated summary with NO location; the
server-rendered ``/postings/{id}`` page carries the full requisition body AND the location.
``fetch_detail`` GETs that page WITHOUT following redirects so the gone-signal 302-to-``/postings``
is observable, and obeys the base contract (``None`` == GONE, raise == indeterminate).

Offline only -- a fake fetcher stands in for AsyncFetcher; no live network. Mirrors
``tests/test_breezy_fetch_detail.py``'s ``request()``-based fake (status/headers/text/url)."""

from __future__ import annotations

import anyio
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import DetailFetch
from ergon_tracker.providers.peopleadmin import PeopleAdminProvider


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
        url: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.url = url


class _FakeFetcher:
    def __init__(
        self, response: _FakeResponse | None = None, *, raises: BaseException | None = None
    ) -> None:
        self._response = response
        self._raises = raises
        self.request_calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.request_calls.append((method, url))
        assert kw.get("follow_redirects") is False  # gone-signal 302 must be observable
        if self._raises is not None:
            raise self._raises
        assert self._response is not None, "test must set a response"
        return self._response


_APPLY_URL = "https://unmc.peopleadmin.com/postings/99390"

_ALIVE_HTML = (
    "<html><body>"
    '<nav class="breadcrumb">Home / Postings</nav>'
    '<div id="content_inner">'
    '<div id="form_view">'
    "<h2>Requisition Details</h2>"
    '<table><tr><th>Working Title</th><td>Research Technician</td></tr>'
    "<tr><th>Location</th><td>Omaha, NE</td></tr></table>"
    '<button class="apply">Apply</button>'
    "<p>Position Summary: You will run assays and maintain the lab.</p>"
    "</div></div>"
    "</body></html>"
)


def _ref(
    *,
    apply_url: str | None = _APPLY_URL,
    listing_url: str | None = None,
    token: str | None = "unmc.peopleadmin.com",
) -> DetailRef:
    return DetailRef(
        id="99390",
        source="peopleadmin",
        token=token,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def test_peopleadmin_alive_returns_detailfetch_with_location() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_ALIVE_HTML, url=_APPLY_URL))
    res = anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), fetcher))
    assert isinstance(res, DetailFetch)
    assert "Position Summary" in res.text
    assert "Requisition Details" in res.text
    assert res.locations is not None
    assert [loc.raw for loc in res.locations] == ["Omaha, NE"]
    assert fetcher.request_calls == [("GET", _APPLY_URL)]


def test_peopleadmin_alive_without_location_returns_bare_str() -> None:
    html = '<html><body><div id="form_view"><p>Just a body, no location table.</p></div></body></html>'
    res = anyio.run(
        lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(_FakeResponse(200, text=html)))
    )
    assert res == "Just a body, no location table."


def test_peopleadmin_404_returns_none() -> None:
    res = anyio.run(
        lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(_FakeResponse(404)))
    )
    assert res is None


def test_peopleadmin_410_returns_none() -> None:
    res = anyio.run(
        lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(_FakeResponse(410)))
    )
    assert res is None


def test_peopleadmin_302_to_search_root_returns_none() -> None:
    resp = _FakeResponse(302, headers={"location": "https://unmc.peopleadmin.com/postings"})
    res = anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(resp)))
    assert res is None


def test_peopleadmin_302_relative_search_root_returns_none() -> None:
    resp = _FakeResponse(302, headers={"location": "/postings"})
    res = anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(resp)))
    assert res is None


def test_peopleadmin_302_elsewhere_raises() -> None:
    # A redirect that is NOT the search root is unexpected -> indeterminate, never a death signal.
    resp = _FakeResponse(302, headers={"location": "https://unmc.peopleadmin.com/postings/99391"})
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(resp)))


def test_peopleadmin_auto_followed_redirect_to_root_returns_none() -> None:
    # Fetcher auto-followed the gone-redirect: 200 on the /postings root with no body -> GONE.
    resp = _FakeResponse(
        200, text="<html><body><h1>Search</h1></body></html>", url="https://unmc.peopleadmin.com/postings"
    )
    res = anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(resp)))
    assert res is None


def test_peopleadmin_200_no_body_not_root_raises() -> None:
    resp = _FakeResponse(200, text="<html><body><p>x</p></body></html>", url=_APPLY_URL)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(resp)))


def test_peopleadmin_5xx_status_raises() -> None:
    with pytest.raises(RuntimeError):
        anyio.run(
            lambda: PeopleAdminProvider().fetch_detail(_ref(), _FakeFetcher(_FakeResponse(503)))
        )


def test_peopleadmin_transient_fetch_error_propagates() -> None:
    fetcher = _FakeFetcher(raises=RuntimeError("boom (timeout)"))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PeopleAdminProvider().fetch_detail(_ref(), fetcher))


def test_peopleadmin_reconstructs_url_from_token_and_listing() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_ALIVE_HTML))
    ref = _ref(
        apply_url=None,
        listing_url="https://unmc.peopleadmin.com/postings/99390?foo=bar",
        token="unmc.peopleadmin.com",
    )
    res = anyio.run(lambda: PeopleAdminProvider().fetch_detail(ref, fetcher))
    assert isinstance(res, DetailFetch)
    assert fetcher.request_calls == [("GET", "https://unmc.peopleadmin.com/postings/99390")]


def test_peopleadmin_bare_subdomain_token_reconstructs_host() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_ALIVE_HTML))
    ref = _ref(apply_url=None, listing_url="https://x/postings/42", token="unmc")
    anyio.run(lambda: PeopleAdminProvider().fetch_detail(ref, fetcher))
    assert fetcher.request_calls == [("GET", "https://unmc.peopleadmin.com/postings/42")]


def test_peopleadmin_unbuildable_ref_raises() -> None:
    ref = _ref(apply_url=None, listing_url=None, token=None)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PeopleAdminProvider().fetch_detail(ref, _FakeFetcher(_FakeResponse(200))))


# --- liveness-gap resolution: peopleadmin (a CONFIRM source, absent from the freshness sweep lists)
# is nonetheless swept + expired by the liveness pass, which selects EVERY active row and routes a
# board-list miss through the detail-confirm because peopleadmin is in CONFIRM_VIA_DETAIL_SOURCES.


def test_peopleadmin_departed_row_expired_by_liveness_confirm(tmp_path) -> None:
    import sqlite3

    from ergon_tracker.index.db import fresh_db
    from ergon_tracker.index.liveness import CONFIRM_VIA_DETAIL_SOURCES, reconcile_liveness_tier

    assert "peopleadmin" in CONFIRM_VIA_DETAIL_SOURCES

    idx = tmp_path / "index.sqlite"
    fresh_db(idx)
    con = sqlite3.connect(idx)
    ts = "2026-07-01T00:00:00+00:00"
    con.execute(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token, "
        "apply_url) VALUES (?, ?, 'peopleadmin', 'UNMC', 'Tech', 'unknown', 'mid', 'full_time', "
        "'active', ?, ?, ?, 'b0', 'unmc.peopleadmin.com', ?)",
        ("pa-1", "ch-1", ts, ts, ts, "https://unmc.peopleadmin.com/postings/99390"),
    )
    con.commit()
    con.close()

    async def fetch_board(source: str, token: str) -> set[str]:
        return set()  # the posting has left the board's fresh list -> candidate

    async def fetch_detail(ref):  # route to the REAL provider against a 302-to-root (GONE) fetcher
        resp = _FakeResponse(302, headers={"location": "https://unmc.peopleadmin.com/postings"})
        return await PeopleAdminProvider().fetch_detail(ref, _FakeFetcher(resp))

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            str(tmp_path / "liveness.sqlite"),
            str(idx),
            fetch_board=fetch_board,
            fetch_detail=fetch_detail,
            now=lambda: ts,
        )
    )
    assert stats["flipped_dead"] == 1  # confirmed dead via the 302-to-root detail signal

    con = sqlite3.connect(idx)
    status, reason = con.execute(
        "SELECT status, expiry_reason FROM jobs WHERE id = 'pa-1'"
    ).fetchone()
    con.close()
    assert status == "expired" and reason == "dead_link"
