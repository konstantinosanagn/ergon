"""Tier-3 per-posting JD recovery: ApiCaptureProvider.fetch_detail (DRAIN-ONLY).

The captured LIST APIs omit the JD; each of 5 giants exposes it one plain HTTP hop away, described
by a ``detail`` block in ``registry/data/apicapture.json`` and dispatched on ``detail["kind"]``:
goldmansachs (graphql), meta (relay_json), google (html_sections), lululemon/baincompany (css).

Offline only -- a fake fetcher stands in for AsyncFetcher (canned ``request()`` responses; a canned
200 never escalates, so curl_cffi is never touched). Mirrors ``tests/test_breezy_fetch_detail.py``.
Per giant we assert: (a) success -> JD text (chrome stripped); (b) the gone-signal -> None;
(c) a transient -> RAISES; (d) the request was built correctly (URL / body / id / client-tier)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.apicapture import (
    ApiCaptureProvider,
    _build_detail_request,
    _load_specs,
)


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
    """Serves a canned ``request()`` response (or raises), recording each call's args/kwargs."""

    def __init__(
        self, response: _FakeResponse | None = None, *, raises: BaseException | None = None
    ) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kw: Any) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kw})
        if self._raises is not None:
            raise self._raises
        assert self._response is not None, "test must set a response"
        return self._response


def _ref(
    *,
    token: str,
    id: str = "1",
    apply_url: str | None = None,
    listing_url: str | None = None,
) -> DetailRef:
    return DetailRef(
        id=id,
        source="apicapture",
        token=token,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def _run(ref: DetailRef, fetcher: _FakeFetcher) -> str | None:
    return anyio.run(lambda: ApiCaptureProvider().fetch_detail(ref, fetcher))


# --- the 5 specs actually carry a detail block ------------------------------------------------


def test_all_five_giants_have_a_detail_block() -> None:
    specs = _load_specs()
    for token in ("goldmansachs", "meta", "google", "lululemon", "baincompany"):
        detail = specs[token].get("detail")
        assert detail and detail["kind"] in {"graphql", "relay_json", "css", "html_sections"}


# --- goldmansachs: graphql --------------------------------------------------------------------

_GS_REF = _ref(token="goldmansachs", id="179309_GS_MID_CAREER")
_GS_ALIVE = '{"data":{"role":{"descriptionHtml":"<p>Lead <strong>Market Risk</strong> in HK.</p>","status":"OPEN"}}}'


def test_goldman_request_built_correctly() -> None:
    detail = _load_specs()["goldmansachs"]["detail"]
    req = _build_detail_request(detail, _GS_REF)
    assert req is not None
    assert req.method == "POST"
    assert req.url == "https://api-higher.gs.com/gateway/api/v1/graphql"
    assert req.tier == "plain"
    # the NUMERIC prefix of the roleId is substituted as externalSourceId
    assert req.json_body["variables"]["externalSourceId"] == "179309"
    assert req.json_body["operationName"] == "GetRoleById"


def test_goldman_alive_returns_description_html() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_GS_ALIVE))
    desc = _run(_GS_REF, fetcher)
    assert desc is not None and "Market Risk" in desc
    call = fetcher.calls[0]
    assert call["method"] == "POST"
    assert call["json"]["variables"]["externalSourceId"] == "179309"


def test_goldman_null_role_is_gone() -> None:
    # invalid/removed id -> BAD_REQUEST validation + data.role == null -> GONE.
    fetcher = _FakeFetcher(_FakeResponse(200, text='{"errors":[{"message":"x"}],"data":{"role":null}}'))
    assert _run(_GS_REF, fetcher) is None


def test_goldman_transient_5xx_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(503, text=""))
    with pytest.raises(RuntimeError):
        _run(_GS_REF, fetcher)


def test_goldman_unparseable_200_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text="<html>not json</html>"))
    with pytest.raises(RuntimeError):
        _run(_GS_REF, fetcher)


# --- meta: relay_json (tls) -------------------------------------------------------------------

_META_REF = _ref(token="meta", id="1526484645064212")
# The inline Relay blob: description is {"__html": "<span>…"}, the others are [{"item": …}] lists.
_META_ALIVE = (
    '<html><script>{"data":{"xcp_requisition_job_description":{'
    '"id":"1526484645064212","title":"Research Engineer",'
    '"description":"{\\"__html\\":\\"<span>Meta is seeking Research Engineers.</span>\\"}",'
    '"responsibilities":[{"item":"Curate benchmarks"},{"item":"Direct capabilities"}],'
    '"minimum_qualifications":[{"item":"Bachelor degree"}],'
    '"preferred_qualifications":[{"item":"Publications at NeurIPS"}]'
    "}}}</script></html>"
)


def test_meta_request_built_correctly() -> None:
    detail = _load_specs()["meta"]["detail"]
    req = _build_detail_request(detail, _META_REF)
    assert req is not None
    assert req.method == "GET"
    assert req.url == "https://www.metacareers.com/jobs/1526484645064212/"
    assert req.tier == "tls"
    assert req.follow_redirects is True


def test_meta_alive_concatenates_keys() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(200, text=_META_ALIVE, url="https://www.metacareers.com/profile/job_details/1526484645064212/")
    )
    desc = _run(_META_REF, fetcher)
    assert desc is not None
    assert "Meta is seeking Research Engineers" in desc  # __html unwrapped + tags stripped
    assert "Curate benchmarks" in desc  # responsibilities list flattened
    assert "Bachelor degree" in desc
    assert "Publications at NeurIPS" in desc
    assert "<span>" not in desc  # HTML chrome stripped
    # client-tier: request issued as GET with redirects followed (fetcher records the kwargs).
    assert fetcher.calls[0]["follow_redirects"] is True


def test_meta_followed_to_position_not_available_is_gone() -> None:
    # A removed job 301s to /jobs/position-not-available/ (final status 404) -> GONE.
    fetcher = _FakeFetcher(
        _FakeResponse(404, text="<html>gone</html>", url="https://www.metacareers.com/jobs/position-not-available/")
    )
    assert _run(_META_REF, fetcher) is None


def test_meta_404_is_gone() -> None:
    fetcher = _FakeFetcher(_FakeResponse(404, text=""))
    assert _run(_META_REF, fetcher) is None


def test_meta_transient_5xx_raises() -> None:
    # 503 is NOT a bot-wall block (would-be escalation), so no curl_cffi -> classified transient.
    fetcher = _FakeFetcher(_FakeResponse(503, text=""))
    with pytest.raises(RuntimeError):
        _run(_META_REF, fetcher)


def test_meta_200_without_keys_raises() -> None:
    # A 200 job page that is NOT the gone page but carries no JD keys is indeterminate -> RAISE.
    fetcher = _FakeFetcher(
        _FakeResponse(200, text="<html><p>nothing here</p></html>", url="https://www.metacareers.com/profile/job_details/1/")
    )
    with pytest.raises(RuntimeError):
        _run(_META_REF, fetcher)


# --- google: html_sections (tls) --------------------------------------------------------------

_GOOGLE_REF = _ref(token="google", id="85183114006930118")
_GOOGLE_ALIVE = (
    "<html><body>"
    '<div class="aG5W3"><h3>About the job</h3><p>Work cross-functionally on products.</p></div>'
    '<div class="KwJkGe"><h3>Minimum qualifications:</h3><p>Bachelor degree or equivalent.</p></div>'
    '<div class="KwJkGe2"><h3>Preferred qualifications:</h3><p>Experience with taxonomy.</p></div>'
    '<div class="BDNOWe"><h3>Responsibilities</h3><p>Own the roadmap for admin tools.</p></div>'
    "</body></html>"
)
_GOOGLE_GONE = "<html><body><h3>Jobs search results</h3><p>Browse roles.</p></body></html>"


def test_google_request_built_correctly() -> None:
    detail = _load_specs()["google"]["detail"]
    req = _build_detail_request(detail, _GOOGLE_REF)
    assert req is not None
    assert req.method == "GET"
    assert req.url == "https://www.google.com/about/careers/applications/jobs/results/85183114006930118"
    assert req.tier == "tls"


def test_google_alive_extracts_sections() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_GOOGLE_ALIVE, url=_GOOGLE_REF.apply_url or ""))
    desc = _run(_GOOGLE_REF, fetcher)
    assert desc is not None
    assert "Work cross-functionally" in desc  # About the job
    assert "Bachelor degree or equivalent" in desc  # Minimum qualifications
    assert "Experience with taxonomy" in desc  # Preferred qualifications
    assert "Own the roadmap" in desc  # Responsibilities


def test_google_absent_sections_soft_gone() -> None:
    # A bad id soft-200s with no JD sections -> the spec's soft gone-signal -> None.
    fetcher = _FakeFetcher(_FakeResponse(200, text=_GOOGLE_GONE))
    assert _run(_GOOGLE_REF, fetcher) is None


def test_google_transient_5xx_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(500, text=""))
    with pytest.raises(RuntimeError):
        _run(_GOOGLE_REF, fetcher)


# --- lululemon: css ---------------------------------------------------------------------------

_LULU_URL = "https://careers.lululemon.com/en_US/careers/JobDetail/Visual-Merch/61522"
_LULU_REF = _ref(token="lululemon", id="61522", apply_url=_LULU_URL)
_LULU_ALIVE = (
    "<html><body>"
    '<nav class="breadcrumb">Home / Jobs</nav>'
    '<div class="article__content article__content--rich-text">'
    "<h2>Who We Are</h2><p>lululemon is a performance apparel company.</p></div>"
    '<div class="article__content">Facebook X Email</div>'
    "</body></html>"
)


def test_lululemon_request_built_from_apply_url() -> None:
    detail = _load_specs()["lululemon"]["detail"]
    req = _build_detail_request(detail, _LULU_REF)
    assert req is not None
    assert req.method == "GET"
    assert req.url == _LULU_URL  # the captured JobDetail url, used verbatim
    # tls tier: the shared HTTP/2 fetcher gets a StreamReset from lululemon (its LIST spec already
    # uses tls_impersonate), so detail escalates to the reused curl_cffi session on that block.
    assert req.tier == "tls"
    assert req.follow_redirects is False  # so the 302 -> /Error gone-signal is observable


def test_lululemon_alive_extracts_rich_text() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_LULU_ALIVE, url=_LULU_URL))
    desc = _run(_LULU_REF, fetcher)
    assert desc is not None
    assert "performance apparel company" in desc
    assert "Facebook" not in desc  # only the rich-text container, not the share bar
    assert fetcher.calls[0]["url"] == _LULU_URL
    assert fetcher.calls[0]["follow_redirects"] is False


def test_lululemon_redirect_to_error_is_gone() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(302, headers={"location": "https://careers.lululemon.com/en_US/careers/Error"})
    )
    assert _run(_LULU_REF, fetcher) is None


def test_lululemon_unclassifiable_redirect_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(302, headers={"location": "https://careers.lululemon.com/somewhere-else"}))
    with pytest.raises(RuntimeError):
        _run(_LULU_REF, fetcher)


def test_lululemon_200_without_container_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text="<html><body><p>huh</p></body></html>", url=_LULU_URL))
    with pytest.raises(RuntimeError):
        _run(_LULU_REF, fetcher)


def test_lululemon_no_url_raises_and_fetches_nothing() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_LULU_ALIVE))
    ref = _ref(token="lululemon", id="61522", apply_url=None, listing_url=None)
    with pytest.raises(RuntimeError):
        _run(ref, fetcher)
    assert fetcher.calls == []


# --- baincompany: css (largest block) ---------------------------------------------------------

_BAIN_URL = "https://careers.bain.com/jobs/FolderDetail/Analyst-French/107399"
_BAIN_REF = _ref(token="baincompany", id="107399", apply_url=_BAIN_URL)
_BAIN_ALIVE = (
    "<html><body>"
    '<div class="article__content">Job Title Analyst Job ID 107399</div>'
    '<div class="article__content">WHAT MAKES US GREAT We are proud to be recognized. '
    "You will analyze revenue end to end and build models for clients.</div>"
    '<div class="article__content">Apply</div>'
    "</body></html>"
)


def test_bain_request_built_from_apply_url() -> None:
    detail = _load_specs()["baincompany"]["detail"]
    req = _build_detail_request(detail, _BAIN_REF)
    assert req is not None
    assert req.url == _BAIN_URL
    assert req.tier == "plain"
    assert req.follow_redirects is False


def test_bain_alive_extracts_largest_block() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_BAIN_ALIVE, url=_BAIN_URL))
    desc = _run(_BAIN_REF, fetcher)
    assert desc is not None
    assert "analyze revenue end to end" in desc  # the largest .article__content (the JD)
    assert "Job Title Analyst" not in desc  # not the tiny header block
    assert desc.strip() != "Apply"


def test_bain_redirect_to_error_is_gone() -> None:
    fetcher = _FakeFetcher(_FakeResponse(302, headers={"location": "https://careers.bain.com/jobs/Error"}))
    assert _run(_BAIN_REF, fetcher) is None


def test_bain_transient_5xx_raises() -> None:
    fetcher = _FakeFetcher(_FakeResponse(500, text=""))
    with pytest.raises(RuntimeError):
        _run(_BAIN_REF, fetcher)


# --- unknown / non-detail token ---------------------------------------------------------------


def test_token_without_detail_block_raises() -> None:
    # A never-configured token has no detail block -> indeterminate (never a false gone).
    fetcher = _FakeFetcher(_FakeResponse(200, text="{}"))
    ref = _ref(token="some_other_token", id="1")
    with pytest.raises(RuntimeError):
        _run(ref, fetcher)
    assert fetcher.calls == []


# --- drain-only wiring ------------------------------------------------------------------------


def test_apicapture_is_drain_only_not_liveness_confirm() -> None:
    from scripts.build_index import _TIER3_DETAIL_SOURCES

    from ergon_tracker.index.freshness import DETERMINISTIC_SOURCES
    from ergon_tracker.index.liveness import CONFIRM_VIA_DETAIL_SOURCES

    # Wired for the Tier-3 JD drain...
    assert "apicapture" in _TIER3_DETAIL_SOURCES
    # ...but NOT for the liveness confirm path (soft gone-signals; its list reshuffles/caps)...
    assert "apicapture" not in CONFIRM_VIA_DETAIL_SOURCES
    # ...and NOT in the freshness deterministic relist either (barred from every confirm path).
    assert "apicapture" not in DETERMINISTIC_SOURCES
