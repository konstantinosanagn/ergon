"""Tier-3 detail fetcher: BreezyProvider.fetch_detail (DRAIN-ONLY).

Breezy's ``/json`` list endpoint carries NO description; the per-position PAGE server-renders the
full JD. ``fetch_detail`` GETs the position page WITHOUT following redirects so the gone-signal
302-to-board-root is observable, extracts the ``#description`` / ``.position-description`` body,
and obeys the base contract (``None`` == GONE, raise == indeterminate).

Offline only -- a fake fetcher stands in for AsyncFetcher; no live network. Mirrors
``tests/test_phenom_fetch_detail.py``'s ``request()``-based fake (status/headers/text/url)."""

from __future__ import annotations

import anyio
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.breezy import BreezyProvider


class _FakeResponse:
    """Minimal stand-in for the ``httpx.Response`` that ``fetcher.request()`` returns."""

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
    """Serves a single canned ``request()`` response (or raises), recording requested URLs."""

    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._response = response
        self._raises = raises
        self.request_calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.request_calls.append((method, url))
        # The provider MUST fetch with redirects un-followed so the 302 gone-signal is observable.
        assert kw.get("follow_redirects") is False
        if self._raises is not None:
            raise self._raises
        assert self._response is not None, "test must set a response"
        return self._response


_APPLY_URL = "https://acme.breezy.hr/p/abc123def456-senior-engineer"

# A live position page: JD body wrapped in chrome (breadcrumb nav, apply button, %…% placeholder)
# that the extractor must strip so only the JD prose remains.
_ALIVE_HTML = (
    "<html><body>"
    '<div id="description">'
    '<nav class="breadcrumb">Home / Jobs / Engineering</nav>'
    '<button class="apply-now">Apply</button>'
    "%APPLY_NOW% "
    "<p>We are hiring a Senior Engineer. You will build the thing.</p>"
    "</div>"
    "</body></html>"
)

_BOARD_ROOT_HTML = "<html><body><h1>Acme Careers</h1><p>See our open roles.</p></body></html>"


def _ref(
    *,
    apply_url: str | None = _APPLY_URL,
    listing_url: str | None = None,
    token: str | None = "acme",
) -> DetailRef:
    return DetailRef(
        id="hash-1",
        source="breezy",
        token=token,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


# --- ALIVE -------------------------------------------------------------------------------------


def test_breezy_200_with_description_returns_cleaned_jd() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_ALIVE_HTML, url=_APPLY_URL))
    desc = anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))
    assert desc is not None
    assert "We are hiring a Senior Engineer" in desc
    # Breadcrumb, apply button, and the %…% i18n placeholder are all stripped.
    assert "Home" not in desc
    assert "Apply" not in desc
    assert "%" not in desc
    # apply_url is fetched verbatim, redirects un-followed (asserted inside the fake).
    assert fetcher.request_calls == [("GET", _APPLY_URL)]


def test_breezy_position_description_class_container() -> None:
    html = (
        "<html><body>"
        '<div class="position-description"><p>Full JD via the class container.</p></div>'
        "</body></html>"
    )
    fetcher = _FakeFetcher(_FakeResponse(200, text=html, url=_APPLY_URL))
    desc = anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))
    assert desc == "Full JD via the class container."


# --- GONE (None) -------------------------------------------------------------------------------


def test_breezy_302_to_board_root_returns_none() -> None:
    # A dead/removed position 302-redirects to the board root "/" -> GONE.
    fetcher = _FakeFetcher(_FakeResponse(302, headers={"location": "/"}))
    result = anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))
    assert result is None


def test_breezy_302_to_absolute_board_root_returns_none() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(302, headers={"location": "https://acme.breezy.hr/"})
    )
    result = anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))
    assert result is None


def test_breezy_autofollowed_200_board_root_returns_none() -> None:
    # If the fetcher AUTO-followed the gone-redirect, we land on a 200 board-root page with no
    # #description; the final URL being the board root classifies it as GONE.
    fetcher = _FakeFetcher(
        _FakeResponse(200, text=_BOARD_ROOT_HTML, url="https://acme.breezy.hr/")
    )
    result = anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))
    assert result is None


def test_breezy_explicit_404_returns_none() -> None:
    fetcher = _FakeFetcher(_FakeResponse(404))
    result = anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))
    assert result is None


# --- INDETERMINATE (raise) ---------------------------------------------------------------------


def test_breezy_transient_fetch_error_raises() -> None:
    # 5xx / 429 / timeout surface as a raised error from the fetcher -> must propagate, never None.
    fetcher = _FakeFetcher(raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))


def test_breezy_200_without_description_non_root_raises() -> None:
    # A 200 with no #description that ISN'T the board root is indeterminate -> RAISE (not None).
    fetcher = _FakeFetcher(
        _FakeResponse(200, text="<html><body><p>huh</p></body></html>", url=_APPLY_URL)
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))


def test_breezy_empty_description_container_raises() -> None:
    # Container present but empty after cleaning -> unclassifiable -> RAISE.
    html = '<html><body><div id="description">%FN%</div></body></html>'
    fetcher = _FakeFetcher(_FakeResponse(200, text=html, url=_APPLY_URL))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))


def test_breezy_redirect_elsewhere_raises() -> None:
    # A redirect that is NOT to the board root is unexpected -> indeterminate, never guessed dead.
    fetcher = _FakeFetcher(
        _FakeResponse(302, headers={"location": "https://acme.breezy.hr/p/other-role"})
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))


def test_breezy_unexpected_status_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(403))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BreezyProvider().fetch_detail(_ref(), fetcher))


def test_breezy_no_derivable_url_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_ALIVE_HTML))
    ref = _ref(apply_url=None, listing_url=None)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BreezyProvider().fetch_detail(ref, fetcher))
    assert fetcher.request_calls == []  # nothing fetched when no URL is buildable


# --- reconstruct URL from token + id (apply_url absent) ----------------------------------------


def test_breezy_reconstructs_url_from_token_and_id() -> None:
    # apply_url absent: the canonical /p/{id} page is rebuilt from ref.token + the position id
    # parsed out of listing_url. ref.token WINS over the host token in listing_url.
    fetcher = _FakeFetcher(_FakeResponse(200, text=_ALIVE_HTML, url=""))
    ref = _ref(
        apply_url=None,
        listing_url="https://old-token.breezy.hr/p/abc123def456",
        token="newtoken",
    )
    desc = anyio.run(lambda: BreezyProvider().fetch_detail(ref, fetcher))
    assert desc is not None and "Senior Engineer" in desc
    assert fetcher.request_calls == [("GET", "https://newtoken.breezy.hr/p/abc123def456")]


# --- drain-only wiring -------------------------------------------------------------------------


def test_breezy_is_drain_only_not_liveness_confirm() -> None:
    from scripts.build_index import _TIER3_DETAIL_SOURCES

    from ergon_tracker.index.freshness import DETERMINISTIC_SOURCES
    from ergon_tracker.index.liveness import CONFIRM_VIA_DETAIL_SOURCES

    # Wired for the Tier-3 JD drain...
    assert "breezy" in _TIER3_DETAIL_SOURCES
    # ...but NOT for the liveness confirm path (its 302-to-root is a SOFT gone-signal)...
    assert "breezy" not in CONFIRM_VIA_DETAIL_SOURCES
    # ...because its liveness/freshness is handled by the deterministic bulk id-set relist.
    assert "breezy" in DETERMINISTIC_SOURCES


def test_base_fetch_detail_is_none() -> None:
    ref = _ref()
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher()))
    assert desc is None
