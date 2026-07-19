"""Unit tests for the UKG Pro / UltiPro Recruiting provider (respx-mocked, offline)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import SearchQuery, make_job_id
from ergon_tracker.providers.ukg import UKGProvider

pytestmark = pytest.mark.anyio

URL = "https://recruiting.ultipro.com/ACME01/JobBoard/g-1/JobBoardView/LoadSearchResults"
TOKEN = "recruiting.ultipro.com|ACME01|g-1|Acme"


def _rec(i: int) -> dict:
    return {
        "Id": f"id-{i}",
        "Title": f"Job {i}",
        "RequisitionNumber": f"REQ{i}",
        "JobCategoryName": "Engineering",
        "FullTime": True,
        "PostedDate": "2026-06-18T21:37:28.111Z",
        "BriefDescription": f"<p>Role {i}</p>",
        "Locations": [{"Address": {"City": "Alexandria", "State": {"Code": "VA"}}}],
    }


def _mock(respx_mock: respx.MockRouter, total: int, server_cap: int = 50) -> None:
    """Mock LoadSearchResults paging; ``server_cap`` = max records the server returns per call
    regardless of requested Top (simulates a server-side Top cap)."""

    def handler(request: httpx.Request) -> httpx.Response:
        d = json.loads(request.content)
        top = d["opportunitySearch"]["Top"]
        skip = d["opportunitySearch"]["Skip"]
        eff = min(top, server_cap)
        opps = [_rec(i) for i in range(skip, min(skip + eff, total))]
        return httpx.Response(200, json={"opportunities": opps, "totalCount": total})

    respx_mock.post(URL).mock(side_effect=handler)


def test_parse_token() -> None:
    assert UKGProvider._parse("recruiting2.ultipro.com|C|g|Acme") == (
        "recruiting2.ultipro.com",
        "C",
        "g",
        "Acme",
    )
    assert UKGProvider._parse("recruiting.ultipro.com|C|g") == (
        "recruiting.ultipro.com",
        "C",
        "g",
        None,
    )
    assert UKGProvider._parse("C|g") == ("recruiting.ultipro.com", "C", "g", None)  # host defaulted


def test_matches_board_url() -> None:
    assert (
        UKGProvider.matches(
            "https://recruiting2.ultipro.com/UNI1027UDRT/JobBoard/6ccb8fd4-4950-43e4-9978-4bcc85c6f5e1/"
        )
        == "recruiting2.ultipro.com|UNI1027UDRT|6ccb8fd4-4950-43e4-9978-4bcc85c6f5e1"
    )
    # UKG's newer rec.pro.ukg.net host (same JobBoard API)
    assert (
        UKGProvider.matches(
            "https://biolifesolution.rec.pro.ukg.net/BIO1501BLSI/JobBoard/4d900524-48eb-4343-a232-4c2b27be9029/"
        )
        == "biolifesolution.rec.pro.ukg.net|BIO1501BLSI|4d900524-48eb-4343-a232-4c2b27be9029"
    )
    assert UKGProvider.matches("https://careers.example.com/jobs") is None  # not ultipro


async def test_fetch_paginates_and_normalizes() -> None:
    with respx.mock as m:
        _mock(m, total=130)
        async with AsyncFetcher(per_host_rate=1000) as f:
            raws = await UKGProvider().fetch(TOKEN, SearchQuery(), f)
    assert len({r.source_job_id for r in raws}) == 130  # all jobs, deduped
    assert {r.company for r in raws} == {"Acme"}
    j = UKGProvider().normalize(raws[0])
    assert j.id == make_job_id("ukg", "id-0")
    assert j.title == "Job 0"
    assert j.locations[0].raw == "Alexandria, VA"
    assert j.department == "Engineering"
    assert j.employment_type.value == "full_time"
    assert "opportunityId=id-0" in j.apply_url
    assert j.posted_at is not None and j.posted_at.year == 2026


async def test_fetch_complete_when_server_caps_top_below_page() -> None:
    # Server returns at most 17/call regardless of requested Top; actual-stride paging must still
    # reach every job (the silent-gap regression this guards against).
    with respx.mock as m:
        _mock(m, total=500, server_cap=17)
        async with AsyncFetcher(per_host_rate=1000) as f:
            raws = await UKGProvider().fetch(TOKEN, SearchQuery(), f)
    assert len({r.source_job_id for r in raws}) == 500


async def test_fetch_respects_limit() -> None:
    with respx.mock as m:
        _mock(m, total=500)
        async with AsyncFetcher(per_host_rate=1000) as f:
            raws = await UKGProvider().fetch(TOKEN, SearchQuery(limit=12), f)
    assert len(raws) == 12


async def test_fetch_empty_board() -> None:
    with respx.mock as m:
        _mock(m, total=0)
        async with AsyncFetcher(per_host_rate=1000) as f:
            raws = await UKGProvider().fetch(TOKEN, SearchQuery(), f)
    assert raws == []


# --- fetch_detail: 404-vs-transient hardening contract ----------------------

DETAIL_URL = "https://recruiting.ultipro.com/ACME01/JobBoard/g-1/OpportunityDetail?opportunityId=id-0"


class _FakeFetcher:
    """Stands in for AsyncFetcher: returns a fixed body, or raises a fixed exception."""

    def __init__(self, *, text: str | None = None, exc: BaseException | None = None) -> None:
        self._text = text
        self._exc = exc
        self.calls: list[str] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        assert self._text is not None
        return self._text


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
        id="id-0", source="ukg", token=TOKEN, apply_url=apply_url,
        listing_url=None, content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await UKGProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await UKGProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await UKGProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await UKGProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_description() -> None:
    html = '<script>var o = {"Description":"<p>Full JD body.<\\/p>"};</script>'
    fetcher = _FakeFetcher(text=html)
    res = await UKGProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert res == "<p>Full JD body.</p>"


async def test_fetch_detail_missing_url_raises() -> None:
    fetcher = _FakeFetcher(text="")
    with pytest.raises(RuntimeError):
        await UKGProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)


async def test_fetch_detail_no_description_json_raises() -> None:
    # A 200 with no embedded Description JSON is indeterminate, not a verified soft-404 -> raise.
    fetcher = _FakeFetcher(text="<html><body>Not Found or some other page</body></html>")
    with pytest.raises(RuntimeError):
        await UKGProvider().fetch_detail(_detail_ref(), fetcher)
