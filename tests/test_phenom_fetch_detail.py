"""Tier-3 detail fetcher: PhenomProvider.fetch_detail RE-ROUTES the 96.6% of phenom rows whose
``apply_url`` points at Workday/SuccessFactors to those providers' own ``fetch_detail`` --
11,414 of 11,831 phenom rows. The remaining ~400 GENUINE-phenom rows (canonical ``/job/{seq}``
on a phenom-native career host, no re-route target) get a phenom-NATIVE per-posting check
(``_fetch_native``): fetch the phenom detail page itself and parse its ``JobPosting`` JSON-LD.

Offline only -- a fake fetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``'s fake-fetcher style, but serves the Workday cxs JSON
shape (``get_json``), the SuccessFactors HTML shape (``get_text``), AND the native path's raw
``request()`` (bare ``/job/{seq}`` probe, optionally followed by a ``get_text`` on the
locale-resolved URL) so one fetcher backs all three."""

from __future__ import annotations

import anyio
import httpx
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.phenom import PhenomProvider


class _FakeResponse:
    """Minimal stand-in for the ``httpx.Response`` ``fetcher.request()`` returns."""

    def __init__(
        self, status_code: int, headers: dict[str, str] | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class _FakeFetcher:
    """Serves Workday's cxs JSON (get_json), SuccessFactors' HTML (get_text), and the native
    path's bare-URL probe (request) + locale-resolved follow-up (get_text)."""

    def __init__(
        self,
        json_payload: object | None = None,
        html_payload: str | None = None,
        raise_on: str | None = None,
        request_response: _FakeResponse | None = None,
        request_raises: BaseException | None = None,
        get_text_raises: BaseException | None = None,
    ) -> None:
        self._json = json_payload
        self._html = html_payload
        self._raise_on = raise_on
        self._request_response = request_response
        self._request_raises = request_raises
        self._get_text_raises = get_text_raises
        self.get_json_calls: list[str] = []
        self.get_text_calls: list[str] = []
        self.request_calls: list[tuple[str, str]] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.get_json_calls.append(url)
        if self._raise_on == "get_json":
            raise RuntimeError("boom")
        return self._json

    async def get_text(self, url: str, **kw: object) -> str:
        self.get_text_calls.append(url)
        if self._get_text_raises is not None:
            raise self._get_text_raises
        if self._raise_on == "get_text":
            raise RuntimeError("boom")
        return self._html or ""

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.request_calls.append((method, url))
        if self._request_raises is not None:
            raise self._request_raises
        assert self._request_response is not None, "test must set request_response"
        return self._request_response


_WD_JSON = {"jobPostingInfo": {"jobDescription": "<p>WD JD</p>"}}
_SF_HTML = '<div id="jobdescription"><p>SF JD</p></div>'


def _jsonld_html(description: str) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"@type": "JobPosting", "description": "{description}"}}'
        "</script></head><body></body></html>"
    )


def _http_404(url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", url)
    resp = httpx.Response(404, request=req)
    return httpx.HTTPStatusError("404", request=req, response=resp)


def test_phenom_workday_apply_url_strips_trailing_apply_and_delegates() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON)
    ref = DetailRef(
        id="1",
        source="phenom",
        token="careers.example.com",
        apply_url=(
            "https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100/apply"
        ),
        listing_url="https://careers.example.com/job/12345",
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>WD JD</p>"
    # Proves the "/apply" suffix was stripped BEFORE building the cxs URL.
    assert fetcher.get_json_calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_careers/job/"
        "USA-Remote/Engineer_R-100"
    ]


def test_phenom_workday_apply_url_without_trailing_apply_still_works() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON)
    ref = DetailRef(
        id="2",
        source="phenom",
        token="careers.example.com",
        apply_url="https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>WD JD</p>"
    assert fetcher.get_json_calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_careers/job/"
        "USA-Remote/Engineer_R-100"
    ]


def test_phenom_workday_falls_back_to_listing_url() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON)
    ref = DetailRef(
        id="3",
        source="phenom",
        token="careers.example.com",
        apply_url=None,
        listing_url=(
            "https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100/apply"
        ),
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "<p>WD JD</p>"
    assert fetcher.get_json_calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/acme_careers/job/"
        "USA-Remote/Engineer_R-100"
    ]


def test_phenom_successfactors_apply_url_delegates() -> None:
    fetcher = _FakeFetcher(html_payload=_SF_HTML)
    ref = DetailRef(
        id="4",
        source="phenom",
        token="careers.example.com",
        apply_url="https://career8.successfactors.com/career?company=acme&job=12345678",
        listing_url="https://careers.example.com/job/999",
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == _SF_HTML
    # SF re-GETs the stored apply_url verbatim -- no URL rewriting for this path.
    assert fetcher.get_text_calls == [
        "https://career8.successfactors.com/career?company=acme&job=12345678"
    ]


def test_phenom_sapsf_host_delegates_to_successfactors() -> None:
    fetcher = _FakeFetcher(html_payload=_SF_HTML)
    ref = DetailRef(
        id="5",
        source="phenom",
        token="careers.example.com",
        apply_url="https://performancemanager.sapsf.com/sap/job/Analyst/12345678/",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == _SF_HTML


def test_phenom_genuine_phenom_no_derivable_seq_raises() -> None:
    # No re-route target AND no ``/job/{seq}`` shape anywhere in apply/listing url -- genuinely
    # unbuildable, so it's indeterminate and must RAISE (keep), never return None (which the
    # liveness confirm would read as dead). No native probe is even attempted.
    fetcher = _FakeFetcher(json_payload=_WD_JSON, html_payload=_SF_HTML)
    ref = DetailRef(
        id="6",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/about-us",
        listing_url="https://careers.molsoncoors.com/about-us",
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert fetcher.get_json_calls == []
    assert fetcher.get_text_calls == []
    assert fetcher.request_calls == []


# --- native (genuine-phenom, no re-route target) -------------------------------------------


def test_phenom_native_direct_200_returns_jd_text() -> None:
    # Bare /job/{seq} resolves directly (no locale-prefix redirect quirk on this tenant).
    fetcher = _FakeFetcher(
        request_response=_FakeResponse(200, text=_jsonld_html("Native JD text"))
    )
    ref = DetailRef(
        id="11",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/job/123",
        listing_url="https://careers.molsoncoors.com/job/123",
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "Native JD text"
    assert fetcher.request_calls == [("GET", "https://careers.molsoncoors.com/job/123")]
    assert fetcher.get_text_calls == []


def test_phenom_native_redirect_then_alive() -> None:
    # LIVE-verified www.hhccareers.org shape: bare /job/{seq} 303s to a locale root regardless
    # of validity; the real per-posting resource is {locale_root}/job/{seq}.
    fetcher = _FakeFetcher(
        request_response=_FakeResponse(303, headers={"location": "/us/en"}),
        html_payload=_jsonld_html("Rheumatologist JD"),
    )
    ref = DetailRef(
        id="12",
        source="phenom",
        token="www.hhccareers.org",
        apply_url="https://www.hhccareers.org/job/HHKHHEUS25160096EXTERNALENUS",
        listing_url=None,
        content_sig="s",
    )
    desc = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert desc == "Rheumatologist JD"
    assert fetcher.request_calls == [
        ("GET", "https://www.hhccareers.org/job/HHKHHEUS25160096EXTERNALENUS")
    ]
    assert fetcher.get_text_calls == [
        "https://www.hhccareers.org/us/en/job/HHKHHEUS25160096EXTERNALENUS"
    ]


def test_phenom_native_direct_404_returns_none() -> None:
    fetcher = _FakeFetcher(request_response=_FakeResponse(404))
    ref = DetailRef(
        id="13",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/job/999999",
        listing_url=None,
        content_sig="s",
    )
    result = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert result is None


def test_phenom_native_redirect_then_410_returns_none() -> None:
    # LIVE-verified: a fabricated id on www.hhccareers.org 303s to the SAME locale root as a real
    # id, then the locale-resolved detail URL 410s -- the real gone-signal for this tenant.
    fetcher = _FakeFetcher(
        request_response=_FakeResponse(303, headers={"location": "/us/en"}),
        get_text_raises=_http_404(
            "https://www.hhccareers.org/us/en/job/HHKHHEUS99999999FAKEFAKEFAKE"
        ),
    )
    ref = DetailRef(
        id="14",
        source="phenom",
        token="www.hhccareers.org",
        apply_url="https://www.hhccareers.org/job/HHKHHEUS99999999FAKEFAKEFAKE",
        listing_url=None,
        content_sig="s",
    )
    result = anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert result is None


def test_phenom_native_redirect_without_location_raises() -> None:
    fetcher = _FakeFetcher(request_response=_FakeResponse(303, headers={}))
    ref = DetailRef(
        id="15",
        source="phenom",
        token="www.hhccareers.org",
        apply_url="https://www.hhccareers.org/job/ABC123",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_phenom_native_unexpected_status_raises() -> None:
    # A non-404/410, non-200, non-3xx status is indeterminate, never treated as gone.
    fetcher = _FakeFetcher(request_response=_FakeResponse(403))
    ref = DetailRef(
        id="16",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/job/123",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_phenom_native_no_jsonld_raises() -> None:
    fetcher = _FakeFetcher(request_response=_FakeResponse(200, text="<html><body/></html>"))
    ref = DetailRef(
        id="17",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/job/123",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_phenom_native_request_transient_raises() -> None:
    # A transient error on the bare-URL probe (timeout/5xx from the retry layer) must propagate,
    # never be swallowed to None.
    fetcher = _FakeFetcher(request_raises=RuntimeError("boom"))
    ref = DetailRef(
        id="18",
        source="phenom",
        token="careers.molsoncoors.com",
        apply_url="https://careers.molsoncoors.com/job/123",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_phenom_native_get_text_transient_raises() -> None:
    # A transient error on the locale-resolved follow-up (after a redirect) must also propagate.
    fetcher = _FakeFetcher(
        request_response=_FakeResponse(303, headers={"location": "/us/en"}),
        get_text_raises=RuntimeError("boom"),
    )
    ref = DetailRef(
        id="19",
        source="phenom",
        token="www.hhccareers.org",
        apply_url="https://www.hhccareers.org/job/ABC123",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_phenom_both_urls_none_raises() -> None:
    fetcher = _FakeFetcher(json_payload=_WD_JSON, html_payload=_SF_HTML)
    ref = DetailRef(
        id="7",
        source="phenom",
        token="careers.example.com",
        apply_url=None,
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))
    assert fetcher.get_json_calls == []
    assert fetcher.get_text_calls == []


def test_phenom_workday_delegate_raise_propagates() -> None:
    # A delegated Workday transient (5xx/timeout) must PROPAGATE, not be swallowed to None -- else
    # the liveness confirm expires a still-live posting on a transient blip.
    fetcher = _FakeFetcher(raise_on="get_json")
    ref = DetailRef(
        id="8",
        source="phenom",
        token="careers.example.com",
        apply_url=(
            "https://acme.wd5.myworkdayjobs.com/acme_careers/job/USA-Remote/Engineer_R-100/apply"
        ),
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_phenom_successfactors_delegate_raise_propagates() -> None:
    fetcher = _FakeFetcher(raise_on="get_text")
    ref = DetailRef(
        id="9",
        source="phenom",
        token="careers.example.com",
        apply_url="https://career8.successfactors.com/career?company=acme&job=12345678",
        listing_url=None,
        content_sig="s",
    )
    with pytest.raises(RuntimeError):
        anyio.run(lambda: PhenomProvider().fetch_detail(ref, fetcher))


def test_base_fetch_detail_is_none() -> None:
    """The generic BaseProvider default (no per-posting detail endpoint) -- mirrors
    ``tests/test_workday_fetch_detail.py``'s equivalent check."""
    ref = DetailRef(
        id="10", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher()))
    assert desc is None
