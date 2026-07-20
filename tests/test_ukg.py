"""Unit tests for the UKG Pro / UltiPro Recruiting provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import SearchQuery, make_job_id
from ergon_tracker.providers.base import BaseProvider
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


# --- board_count: cheap Top:1 totalCount change-CANDIDATE signal ---------------------------------


async def test_board_count_reads_total_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        d = json.loads(request.content)
        assert d["opportunitySearch"]["Top"] == 1
        assert d["opportunitySearch"]["Skip"] == 0
        return httpx.Response(200, json={"opportunities": [_rec(0)], "totalCount": 61})

    with respx.mock as m:
        m.post(URL).mock(side_effect=handler)
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await UKGProvider().board_count(TOKEN, f)
    assert count == 61


async def test_board_count_unparseable_token_returns_none() -> None:
    async with AsyncFetcher(per_host_rate=100) as f:
        count = await UKGProvider().board_count("", f)
    assert count is None


async def test_board_count_404_returns_none() -> None:
    with respx.mock as m:
        m.post(URL).mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await UKGProvider().board_count(TOKEN, f)
    assert count is None


async def test_board_count_transient_error_raises() -> None:
    with respx.mock as m:
        m.post(URL).mock(return_value=httpx.Response(503))
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            with pytest.raises(TransientHTTPError):
                await UKGProvider().board_count(TOKEN, f)


async def test_board_count_missing_total_count_raises() -> None:
    with respx.mock as m:
        m.post(URL).mock(return_value=httpx.Response(200, json={"opportunities": []}))
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(RuntimeError):
                await UKGProvider().board_count(TOKEN, f)


async def test_base_provider_board_count_is_none() -> None:
    assert await BaseProvider().board_count(TOKEN, None) is None  # type: ignore[arg-type]


# --- board_count: live gate (ERGON_LIVE_TESTS=1) --------------------------------------------------

_PROBE_FILE = Path(
    "/private/tmp/claude-501/-Users-kanagn-Desktop-job-researcher/"
    "d20c6e7c-0b7f-4b04-a828-a75251378b9c/scratchpad/probe_targets.json"
)
_SEED_FILE = Path(__file__).resolve().parents[1] / "src/ergon_tracker/registry/data/seed.json"


def _live_tokens(ats: str, n: int = 3) -> list[str]:
    """Token sample for the ``board_count`` live gate: prefer the investigator's
    ``probe_targets.json`` (pre-verified live boards, if present), else fall back to the registry
    seed filtered by ``ats`` -- mirrors ``tests/live``'s own ``_tokens`` helper."""
    if _PROBE_FILE.exists():
        try:
            data = json.loads(_PROBE_FILE.read_text())
            toks = [e["token"] for e in data.get(ats, []) if e.get("token")]
            if toks:
                return toks[:n]
        except Exception:
            pass
    with open(_SEED_FILE) as f:
        seed = json.load(f)["companies"]
    return [
        e["token"]
        for e in seed.values()
        if isinstance(e, dict) and e.get("ats") == ats and e.get("token")
    ][:n]


@pytest.mark.live
async def test_board_count_live_positive_and_consistent_with_sampled_fetch() -> None:
    tokens = _live_tokens("ukg", 5)
    assert tokens, "no ukg tokens available (neither probe_targets.json nor seed.json)"
    checked = positive = 0
    async with AsyncFetcher(per_host_rate=5, retries=2) as f:
        for token in tokens:
            try:
                count = await UKGProvider().board_count(token, f)
            except Exception:
                continue
            if count is None:
                continue
            assert count >= 0, f"{token}: board_count returned negative {count}"
            sampled = await UKGProvider().fetch(token, SearchQuery(limit=20), f)
            assert count >= len(sampled), (
                f"{token}: board_count {count} < sampled fetch {len(sampled)}"
            )
            checked += 1
            positive += count > 0
    assert checked >= 1, "no live ukg board yielded a usable board_count"
    assert positive >= 1, "no live ukg board yielded a POSITIVE board_count"
