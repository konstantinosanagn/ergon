"""Unit tests for the Taleo Business Edition (TBE/CwsV2) provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.taleobe import TaleoBEProvider

pytestmark = pytest.mark.anyio

HOST = "phf.tbe.taleo.net/phf03"
BASE = f"https://{HOST}/ats/careers/v2/searchResults"


def _row(rid: str, title: str, loc: str) -> str:
    return (
        f'<h4 class="oracletaleocwsv2-head-title"><a href="https://{HOST}/ats/careers/v2/'
        f'viewRequisition?org=CALTECH&cws=37&rid={rid}" class="viewJobLink">{title}</a></h4>'
        f'<div tabindex="0">{loc}</div>'
    )


def _mock(respx_mock: respx.MockRouter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rf = request.url.params.get("rowFrom")
        if not rf:  # page 0
            body = _row("101", "Administrative Assistant", "Pasadena, CA") + _row(
                "102", "Research Scientist (Remote)", "Remote - US"
            )
            return httpx.Response(200, text=f"<html>23Jobs{body}</html>")
        return httpx.Response(200, text="<html>no more</html>")

    respx_mock.get(url__startswith=BASE).mock(side_effect=handler)


def test_matches_host() -> None:
    p = TaleoBEProvider
    assert (
        p.matches("https://phf.tbe.taleo.net/phf03/ats/careers/v2/searchResults")
        == "phf.tbe.taleo.net"
    )
    assert p.matches("https://boards.greenhouse.io/x") is None


def test_parse_token() -> None:
    assert TaleoBEProvider._parse("phf.tbe.taleo.net/phf03|CALTECH|37") == (
        "phf.tbe.taleo.net/phf03",
        "CALTECH",
        "37",
        None,
    )
    assert TaleoBEProvider._parse("phf.tbe.taleo.net/phf03|CALTECH|37|Caltech")[3] == "Caltech"


async def test_fetch_and_normalize() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoBEProvider().fetch(f"{HOST}|CALTECH|37|Caltech", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"Caltech"}
    j0 = TaleoBEProvider().normalize(raws[0])
    assert j0.id == make_job_id("taleobe", "101")
    assert j0.title == "Administrative Assistant"
    assert j0.locations[0].raw == "Pasadena, CA"
    assert "rid=101" in j0.apply_url

    remote = TaleoBEProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoBEProvider().fetch(f"{HOST}|CALTECH|37", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_degrades_on_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=BASE).mock(return_value=httpx.Response(500))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await TaleoBEProvider().fetch(f"{HOST}|CALTECH|37", SearchQuery(), f)
    assert raws == []


# --- fetch_detail: 404-vs-soft-404-vs-transient hardening contract ------------------------------

TOKEN = f"{HOST}|CALTECH|37|Caltech"
DETAIL_URL = f"https://{HOST}/ats/careers/v2/viewRequisition?org=CALTECH&cws=37&rid=101"


def _jsonld_html(description: str = "<p>Full JD text for the role.</p>") -> str:
    return (
        "<html><head><script type=\"application/ld+json\">"
        f'{{"@type": "JobPosting", "title": "Administrative Assistant", '
        f'"description": "{description}"}}'
        "</script></head><body></body></html>"
    )


def _gone_html() -> str:
    return (
        "<html><head><script>document.title = \"Job Not Available\";</script></head>"
        "<body><span>This job has moved or is no longer available. Please search our "
        "current job openings.</span></body></html>"
    )


def _malformed_html() -> str:
    """A 200 with neither JSON-LD nor the gone-marker -- truly indeterminate."""
    return "<html><body>Unexpected page shape.</body></html>"


class _FakeFetcher:
    """Stands in for AsyncFetcher: returns a fixed body, or raises a fixed exception."""

    def __init__(self, *, html: str | None = None, exc: BaseException | None = None) -> None:
        self._html = html
        self._exc = exc
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        assert self._html is not None
        return self._html


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", DETAIL_URL)
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


def _detail_ref(apply_url: str | None = DETAIL_URL, token: str | None = TOKEN) -> DetailRef:
    return DetailRef(
        id="101", source="taleobe", token=token, apply_url=apply_url, listing_url=None,
        content_sig="s",
    )


async def test_fetch_detail_alive_returns_jd_text() -> None:
    fetcher = _FakeFetcher(html=_jsonld_html())
    res = await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert isinstance(res, str) and "Full JD text" in res


async def test_fetch_detail_soft_404_returns_none() -> None:
    """A removed/nonexistent rid renders NO JSON-LD and a fixed 'no longer available' marker plus
    a 'Job Not Available' page title -- verified live (NVR Inc / phg.tbe.taleo.net, HTTP 200)."""
    fetcher = _FakeFetcher(html=_gone_html())
    res = await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_malformed_page_raises() -> None:
    """No JSON-LD AND no gone-marker -- indeterminate, must never be treated as gone."""
    fetcher = _FakeFetcher(html=_malformed_html())
    with pytest.raises(RuntimeError):
        await TaleoBEProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_missing_url_raises() -> None:
    fetcher = _FakeFetcher(html=_jsonld_html())
    with pytest.raises(RuntimeError):
        await TaleoBEProvider().fetch_detail(_detail_ref(apply_url=None, token=None), fetcher)


async def test_fetch_detail_rebuilds_url_from_token_when_apply_url_missing() -> None:
    """No apply_url/listing_url -> derive the detail URL from token + ref.id instead of failing."""
    fetcher = _FakeFetcher(html=_jsonld_html())
    res = await TaleoBEProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert isinstance(res, str) and "Full JD text" in res
