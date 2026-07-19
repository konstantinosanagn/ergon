"""Unit tests for the Radancy/TalentBrew provider (respx-mocked, offline)."""

from __future__ import annotations

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import SearchQuery, make_job_id
from ergon_tracker.providers.radancy import RadancyProvider

pytestmark = pytest.mark.anyio

RESULTS = "https://jobs.acme.com/search-jobs/results"


def _card(jid: str, title: str, city: str, loc: str, cat: str) -> str:
    return (
        f'<li><a href="/job/{city}/{title.lower().replace(" ", "-")}/9/{jid}" data-job-id="{jid}">'
        f"<h2>{title}</h2>"
        f'<span class="job-location">{loc}</span>'
        f'<span class="job-category">{cat}</span></a></li>'
    )


def _page(cards: list[str]) -> dict:
    return {"results": "<ul>" + "".join(cards) + "</ul>" if cards else "", "hasJobs": bool(cards)}


def _mock(respx_mock: respx.MockRouter, pages: list[list[str]]) -> None:
    def _resp(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("CurrentPage", "1"))
        cards = pages[page - 1] if 1 <= page <= len(pages) else []
        return httpx.Response(200, json=_page(cards))

    respx_mock.get(RESULTS).mock(side_effect=_resp)


def test_parse_token() -> None:
    assert RadancyProvider._parse("jobs.acme.com|Acme") == ("jobs.acme.com", "Acme", None)
    assert RadancyProvider._parse("https://jobs.acme.com/|Acme") == ("jobs.acme.com", "Acme", None)
    assert RadancyProvider._parse("jobs.acme.com") == ("jobs.acme.com", None, None)
    assert RadancyProvider._parse("jobs.acme.com|Acme|brand-facet__optum") == (
        "jobs.acme.com",
        "Acme",
        "brand-facet__optum",
    )


async def test_fetch_paginates_and_normalizes() -> None:
    pages = [
        [
            _card("111", "Senior Data Engineer", "phoenix", "Phoenix, AZ", "Engineering"),
            _card("222", "Remote Marketing Lead", "remote", "Remote", "Marketing"),
        ],
        [_card("333", "Analyst, Finance", "miami", "Miami, FL", "Finance")],
    ]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch("jobs.acme.com|Acme", SearchQuery(), f)

    assert len(raws) == 3
    assert {r.company for r in raws} == {"Acme"}
    j0 = RadancyProvider().normalize(raws[0])
    assert j0.id == make_job_id("radancy", "111")
    assert j0.title == "Senior Data Engineer"
    assert j0.locations[0].raw == "Phoenix, AZ"
    assert j0.department == "Engineering"
    assert j0.apply_url == "https://jobs.acme.com/job/phoenix/senior-data-engineer/9/111"
    jr = RadancyProvider().normalize(raws[1])
    assert jr.remote.value == "remote"


async def test_fetch_respects_limit() -> None:
    pages = [[_card(str(i), f"Role {i}", "nyc", "New York, NY", "Ops") for i in range(100)]]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch("jobs.acme.com|Acme", SearchQuery(limit=4), f)
    assert len(raws) == 4


def _brand_card(jid: str, title: str, brand: str) -> str:
    return (
        f'<li><a href="/job/nyc/{jid}/9/{jid}" data-job-id="{jid}" '
        f'class="brand-facet brand-facet__{brand}"><h2>{title}</h2>'
        f'<span class="job-location">New York, NY</span></a></li>'
    )


async def test_brand_facet_filter() -> None:
    # A mixed-brand board: only the optum-tagged cards should survive the filter, but UHC cards on
    # a later page must NOT trip the end-of-results break.
    pages = [
        [_brand_card("1", "Optum RN", "optum"), _brand_card("2", "UHC Analyst", "uhc")],
        [_brand_card("3", "Optum Coder", "optum"), _brand_card("4", "UHG Clerk", "uhg")],
    ]
    with respx.mock as respx_mock:
        _mock(respx_mock, pages)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch(
                "jobs.acme.com|Optum|brand-facet__optum", SearchQuery(), f
            )
    assert {r.source_job_id for r in raws} == {"1", "3"}  # both optum jobs, across both pages
    assert all("Optum" in RadancyProvider().normalize(r).title for r in raws)


async def test_empty_page_stops() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock, [[]])  # first page already empty
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await RadancyProvider().fetch("jobs.acme.com|Acme", SearchQuery(), f)
    assert raws == []


# --- fetch_detail: 404-vs-transient hardening contract ----------------------

DETAIL_URL = "https://jobs.acme.com/job/phoenix/senior-data-engineer/9/111"


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


def _detail_ref(apply_url: str | None = DETAIL_URL) -> DetailRef:
    return DetailRef(
        id="111", source="radancy", token="jobs.acme.com|Acme", apply_url=apply_url,
        listing_url=None, content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await RadancyProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await RadancyProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await RadancyProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await RadancyProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_content() -> None:
    body = "x" * 500  # clears _DETAIL_MIN_LEN
    html = f'<html><body><main><p>{body}</p></main></body></html>'
    fetcher = _FakeFetcher(html=html)
    res = await RadancyProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert isinstance(res, str) and body in res


async def test_fetch_detail_missing_url_raises() -> None:
    fetcher = _FakeFetcher(html="<html></html>")
    with pytest.raises(RuntimeError):
        await RadancyProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)
