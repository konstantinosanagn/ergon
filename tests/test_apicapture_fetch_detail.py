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
from ergon_tracker.models import DetailFetch, SalaryInterval
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


def _run(ref: DetailRef, fetcher: _FakeFetcher) -> str | DetailFetch | None:
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


# structured salary: compensation{minSalary,maxSalary,currency} rides the same GraphQL response.
_GS_ALIVE_PAY = (
    '{"data":{"role":{"descriptionHtml":"<p>Lead <strong>Market Risk</strong> in NYC.</p>",'
    '"compensation":{"minSalary":85000,"maxSalary":140000,"currency":"USD"},"status":"OPEN"}}}'
)
_GS_ALIVE_NULL_PAY = (
    '{"data":{"role":{"descriptionHtml":"<p>Lead Market Risk in HK.</p>",'
    '"compensation":{"minSalary":null,"maxSalary":null,"currency":null},"status":"OPEN"}}}'
)


def test_goldman_alive_with_compensation_returns_detailfetch() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text=_GS_ALIVE_PAY))
    got = _run(_GS_REF, fetcher)
    assert isinstance(got, DetailFetch)
    assert "Market Risk" in got.text
    assert got.salary is not None
    assert got.salary.min_amount == 85000
    assert got.salary.max_amount == 140000
    assert got.salary.currency == "USD"
    assert got.salary.interval is SalaryInterval.YEAR


def test_goldman_null_compensation_falls_back_to_bare_str() -> None:
    # Non-US geos null both bounds -> no structured salary -> bare JD str (not a DetailFetch).
    fetcher = _FakeFetcher(_FakeResponse(200, text=_GS_ALIVE_NULL_PAY))
    got = _run(_GS_REF, fetcher)
    assert isinstance(got, str)
    assert "Market Risk" in got


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


# structured salary: public_compensation[0] carries min/max as "$132,000/year" strings.
_META_ALIVE_PAY = (
    '<html><script>{"data":{"xcp_requisition_job_description":{'
    '"id":"1526484645064212","title":"Research Engineer",'
    '"description":"{\\"__html\\":\\"<span>Meta is seeking Research Engineers.</span>\\"}",'
    '"public_compensation":[{"compensation_amount_minimum":"$132,000/year",'
    '"compensation_amount_maximum":"$189,000/year"}]'
    "}}}</script></html>"
)
_META_ALIVE_NO_PAY = (
    '<html><script>{"data":{"xcp_requisition_job_description":{'
    '"id":"1526484645064212","title":"Research Engineer",'
    '"description":"{\\"__html\\":\\"<span>Meta is seeking Research Engineers.</span>\\"}",'
    '"public_compensation":[]'
    "}}}</script></html>"
)


def test_meta_alive_with_compensation_returns_detailfetch() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(200, text=_META_ALIVE_PAY, url="https://www.metacareers.com/profile/job_details/1526484645064212/")
    )
    got = _run(_META_REF, fetcher)
    assert isinstance(got, DetailFetch)
    assert "Meta is seeking Research Engineers" in got.text
    assert got.salary is not None
    assert got.salary.min_amount == 132000  # "$132,000/year" -> $ and /year stripped, , coerced
    assert got.salary.max_amount == 189000
    assert got.salary.currency == "USD"  # spec literal
    assert got.salary.interval is SalaryInterval.YEAR  # trailing /year


def test_meta_empty_compensation_falls_back_to_bare_str() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(200, text=_META_ALIVE_NO_PAY, url="https://www.metacareers.com/profile/job_details/1526484645064212/")
    )
    got = _run(_META_REF, fetcher)
    assert isinstance(got, str)
    assert "Meta is seeking Research Engineers" in got


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
    assert isinstance(desc, str)  # no detail.salary block -> bare str, salary branch skipped
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
    assert isinstance(desc, str)  # no detail.salary block -> bare str, salary branch skipped
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
    assert isinstance(desc, str)  # no detail.salary block -> bare str, salary branch skipped
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


# ==============================================================================================
# Tail-source detail blocks (14 specs recovered via plain HTTP): json / json_ld / css kinds.
# Same offline contract: (a) alive -> JD str; (b) gone-signal -> None; (c) transient -> raise.
# ==============================================================================================

# --- json kind: Microsoft apply-v2 (url_template, hard-404 gone) ------------------------------

_MS_REF = _ref(token="microsoft", id="1970393556866423")
_MS_ALIVE = '{"id":"1970393556866423","job_description":"<b>Overview</b><p>Build ISD.</p>"}'


def test_microsoft_request_built_from_template() -> None:
    detail = _load_specs()["microsoft"]["detail"]
    req = _build_detail_request(detail, _MS_REF)
    assert req is not None
    assert req.method == "GET"
    assert req.tier == "tls"
    assert req.url == (
        "https://apply.careers.microsoft.com/api/apply/v2/jobs/1970393556866423"
        "?domain=microsoft.com&job_index=0"
    )


def test_microsoft_alive_extracts_job_description() -> None:
    desc = _run(_MS_REF, _FakeFetcher(_FakeResponse(200, text=_MS_ALIVE)))
    assert isinstance(desc, str) and "Overview" in desc and "Build ISD" in desc


def test_microsoft_404_is_gone() -> None:
    assert _run(_MS_REF, _FakeFetcher(_FakeResponse(404, text="not found"))) is None


def test_microsoft_transient_5xx_raises() -> None:
    with pytest.raises(RuntimeError):
        _run(_MS_REF, _FakeFetcher(_FakeResponse(503, text="")))


def test_microsoft_200_without_field_raises() -> None:
    # No gone.absent on Microsoft (its dead ids hard-404): a 200 missing the field is indeterminate.
    with pytest.raises(RuntimeError):
        _run(_MS_REF, _FakeFetcher(_FakeResponse(200, text='{"id":"x"}')))


# --- json kind: KPIT / Talentojo (plain client, nested job.description, hard-404 gone) ----------

_KPIT_REF = _ref(token="kpit", id="82842")
_KPIT_ALIVE = (
    '{"job":{"id":82842,"title":"AUTOSAR BSW Engineer",'
    '"description":"<b>Responsibilities:</b> Configure and integrate CAN NM module."},'
    '"organization":{},"linked_recruiters":[]}'
)


def test_kpit_request_built_from_template() -> None:
    detail = _load_specs()["kpit"]["detail"]
    req = _build_detail_request(detail, _KPIT_REF)
    assert req is not None
    assert req.method == "GET"
    assert req.tier == "plain"
    assert req.url == "https://talentojo.kpit.com/service/jobs/82842"


def test_kpit_alive_extracts_nested_description() -> None:
    desc = _run(_KPIT_REF, _FakeFetcher(_FakeResponse(200, text=_KPIT_ALIVE)))
    assert isinstance(desc, str) and "Responsibilities" in desc and "CAN NM module" in desc


def test_kpit_404_is_gone() -> None:
    assert _run(_KPIT_REF, _FakeFetcher(_FakeResponse(404, text="not found"))) is None


def test_kpit_transient_5xx_raises() -> None:
    with pytest.raises(RuntimeError):
        _run(_KPIT_REF, _FakeFetcher(_FakeResponse(503, text="")))


# --- json kind: TriNet (internal id regexed out of the apply-URL) -----------------------------

# The list stores displayJobId as the posting id, but apply-v2 needs the INTERNAL id, which only
# appears inside positionUrl (`/careers/job/{internal}`) -> threaded into apply_url, regexed here.
_TRINET_REF = _ref(token="trinetusa", id="3003661", apply_url="/careers/job/42077819")


def test_trinet_resolves_internal_id_from_apply_url() -> None:
    detail = _load_specs()["trinetusa"]["detail"]
    req = _build_detail_request(detail, _TRINET_REF)
    assert req is not None
    assert req.url == (
        "https://jobs.trinet.com/api/apply/v2/jobs/42077819?domain=trinet.com&job_index=0"
    )
    assert req.tier == "plain"


def test_trinet_alive_extracts_jd() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text='{"job_description":"<p>TriNet role.</p>"}'))
    desc = _run(_TRINET_REF, fetcher)
    assert isinstance(desc, str) and "TriNet role" in desc


def test_trinet_no_apply_url_raises_and_fetches_nothing() -> None:
    # An older row indexed before the positionUrl remap has no apply_url -> unbuildable -> RAISE
    # (indeterminate, never a false gone), and no request is issued.
    fetcher = _FakeFetcher(_FakeResponse(200, text="{}"))
    ref = _ref(token="trinetusa", id="3003661", apply_url=None, listing_url=None)
    with pytest.raises(RuntimeError):
        _run(ref, fetcher)
    assert fetcher.calls == []


# --- json kind: ADP (Fuyao) — soft-404 gone via gone.absent -----------------------------------

_ADP_REF = _ref(token="fuyaoglassamerica", id="9201259006853_1")


def test_adp_alive_extracts_requisition_description() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text='{"requisitionDescription":"<p>Fuyao JD.</p>"}'))
    desc = _run(_ADP_REF, fetcher)
    assert isinstance(desc, str) and "Fuyao JD" in desc


def test_adp_soft404_absent_field_is_gone() -> None:
    # A dead itemID soft-200s with the field simply absent -> gone.absent -> None (not a raise).
    fetcher = _FakeFetcher(_FakeResponse(200, text='{"itemID":"x","postingInstructions":[]}'))
    assert _run(_ADP_REF, fetcher) is None


def test_adp_transient_5xx_raises() -> None:
    with pytest.raises(RuntimeError):
        _run(_ADP_REF, _FakeFetcher(_FakeResponse(500, text="")))


# --- json kind: Upstart (Greenhouse) — double-escaped content unescaped -----------------------

_UPSTART_REF = _ref(token="upstartnetwork", id="8056112")


def test_upstart_unescapes_double_escaped_content() -> None:
    # Greenhouse serves ``content`` as double-escaped HTML: &lt;p&gt;... -> one unescape -> <p>...
    fetcher = _FakeFetcher(
        _FakeResponse(200, text='{"content":"&lt;p&gt;About &amp; Upstart&lt;/p&gt;"}')
    )
    desc = _run(_UPSTART_REF, fetcher)
    assert desc == "<p>About & Upstart</p>"


def test_upstart_404_is_gone() -> None:
    assert _run(_UPSTART_REF, _FakeFetcher(_FakeResponse(404, text=""))) is None


# --- json kind: UVA (Workday cxs URL derived from applyUrl) -----------------------------------

_UVA_APPLY = (
    "https://uva.wd1.myworkdayjobs.com/UVAJobs/job/Charlottesville-VA/Assistant_R0083231/apply"
)
_UVA_REF = _ref(token="universityofvirginia", id="R0083231", apply_url=_UVA_APPLY)


def test_uva_derives_cxs_url_stripping_apply_suffix() -> None:
    detail = _load_specs()["universityofvirginia"]["detail"]
    req = _build_detail_request(detail, _UVA_REF)
    assert req is not None
    # /wday/cxs/{tenant}/{site}/job/... with the widgets feed's trailing /apply dropped.
    assert req.url == (
        "https://uva.wd1.myworkdayjobs.com/wday/cxs/uva/UVAJobs/job/Charlottesville-VA/Assistant_R0083231"
    )


def test_uva_alive_extracts_job_description() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(200, text='{"jobPostingInfo":{"jobDescription":"<p>Greenhouse Asst.</p>"}}')
    )
    desc = _run(_UVA_REF, fetcher)
    assert isinstance(desc, str) and "Greenhouse Asst" in desc


def test_uva_404_is_gone() -> None:
    assert _run(_UVA_REF, _FakeFetcher(_FakeResponse(404, text=""))) is None


# --- json_ld kind: Talemetry/TTC (Parker) — tolerant description decode, redirect gone ---------

_PARKER_REF = _ref(token="parkerhannifin", id="18015548")
# The ld+json JobPosting.description carries INVALID JSON escapes (\$) and would break json.loads;
# the tolerant decoder recovers it anyway (the HTML is then stripped to text).
_PARKER_ALIVE = (
    '<html><head><script type="application/ld+json">'
    '{"@type":"JobPosting","title":"Buyer II",'
    '"description":"<p>Position Summary<\\/p> <ul><li>Pay: \\$84915 to \\$141320<\\/li><\\/ul>"}'
    "</script></head><body>...</body></html>"
)


def test_parker_request_built_and_no_follow() -> None:
    detail = _load_specs()["parkerhannifin"]["detail"]
    req = _build_detail_request(detail, _PARKER_REF)
    assert req is not None
    assert req.url == "https://parkercareers.ttcportals.com/jobs/18015548"
    assert req.tier == "tls"
    assert req.follow_redirects is False  # so the 301 -> job_not_found gone-signal is observable


def test_parker_alive_extracts_jsonld_description() -> None:
    desc = _run(_PARKER_REF, _FakeFetcher(_FakeResponse(200, text=_PARKER_ALIVE)))
    assert isinstance(desc, str)
    assert "Position Summary" in desc
    assert "$84915 to $141320" in desc  # invalid \$ escapes recovered, HTML stripped
    assert "<li>" not in desc


def test_parker_redirect_to_job_not_found_is_gone() -> None:
    fetcher = _FakeFetcher(
        _FakeResponse(
            301, headers={"location": "https://parkercareers.ttcportals.com/jobs?job_not_found=true"}
        )
    )
    assert _run(_PARKER_REF, fetcher) is None


def test_parker_transient_5xx_raises() -> None:
    with pytest.raises(RuntimeError):
        _run(_PARKER_REF, _FakeFetcher(_FakeResponse(502, text="")))


def test_parker_200_without_jobposting_raises() -> None:
    # A 200 that is not the gone redirect and carries no JobPosting ld+json is indeterminate.
    with pytest.raises(RuntimeError):
        _run(_PARKER_REF, _FakeFetcher(_FakeResponse(200, text="<html><body>nope</body></html>")))


# --- css kind: EOG (select=all concat) + soft-404 gone.absent ---------------------------------

_EOG_REF = _ref(token="eogresources", id="11228")
_EOG_ALIVE = (
    "<html><body>"
    '<section id="dvJobDescription">Record oil and gas revenue.</section>'
    '<section id="dvJobRequirements">Bachelor degree in Accounting.</section>'
    "</body></html>"
)


def test_eog_concatenates_all_sections() -> None:
    desc = _run(_EOG_REF, _FakeFetcher(_FakeResponse(200, text=_EOG_ALIVE)))
    assert isinstance(desc, str)
    assert "Record oil and gas revenue" in desc  # #dvJobDescription
    assert "Bachelor degree in Accounting" in desc  # #dvJobRequirements concatenated


def test_eog_absent_sections_soft_gone() -> None:
    fetcher = _FakeFetcher(_FakeResponse(200, text="<html><body><p>dead</p></body></html>"))
    assert _run(_EOG_REF, fetcher) is None


# --- css kind: Boston University — text_marker gone -------------------------------------------

_BU_REF = _ref(token="bostonuniversity", id="316939")


def test_boston_alive_extracts_description() -> None:
    html = '<html><body><section class="sr-job-detail__description">Manage BU athletics ops.</section></body></html>'
    desc = _run(_BU_REF, _FakeFetcher(_FakeResponse(200, text=html)))
    assert isinstance(desc, str) and "Manage BU athletics ops" in desc


def test_boston_placeholder_text_marker_is_gone() -> None:
    # A removed posting soft-200s with the SAME container holding a "cannot find this position"
    # placeholder -> text_marker gone-signal -> None.
    html = (
        '<html><body><section class="sr-job-detail__description">'
        "We apologize, but we cannot find this position. Please view our current openings."
        "</section></body></html>"
    )
    assert _run(_BU_REF, _FakeFetcher(_FakeResponse(200, text=html))) is None


# --- css kind: Artifint — soft-404 gone.absent + Kirkland redirect gone ------------------------


def test_artifint_absent_container_soft_gone() -> None:
    ref = _ref(token="artifinttechnologies", id="36827")
    fetcher = _FakeFetcher(_FakeResponse(200, text="<html><body><p>no job</p></body></html>"))
    assert _run(ref, fetcher) is None


def test_kirkland_alive_and_redirect_gone() -> None:
    ref = _ref(token="kirklandandellis", id="18013281")
    html = '<html><body><div class="job-description-text">About Kirkland role.</div></body></html>'
    assert "About Kirkland role" in (_run(ref, _FakeFetcher(_FakeResponse(200, text=html))) or "")
    gone = _FakeFetcher(
        _FakeResponse(
            301, headers={"location": "https://staffjobsus.kirkland.com/jobs?job_not_found=true"}
        )
    )
    assert _run(ref, gone) is None


# --- all new kinds are registered -------------------------------------------------------------


def test_new_detail_kinds_registered() -> None:
    from ergon_tracker.providers.apicapture import _DETAIL_EXTRACTORS

    for kind in ("json", "json_ld", "css", "html_sections", "graphql", "relay_json"):
        assert kind in _DETAIL_EXTRACTORS


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
