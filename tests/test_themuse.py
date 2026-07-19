"""The Muse provider unit tests (offline, respx-mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from ergon_tracker import RemoteType
from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import EmploymentType, SearchQuery
from ergon_tracker.providers.themuse import TheMuseProvider

pytestmark = pytest.mark.anyio

API = "https://www.themuse.com/api/public/jobs"


def _provider() -> TheMuseProvider:
    return TheMuseProvider()


def _job(i: int, **over: object) -> dict[str, object]:
    job: dict[str, object] = {
        "id": 1000 + i,
        "name": f"Engineer {i}",
        "company": {"name": f"Company {i}"},
        "locations": [{"name": "New York, NY"}],
        "levels": [{"name": "Senior Level", "short_name": "senior"}],
        "type": "external",
        "refs": {"landing_page": f"https://www.themuse.com/jobs/company/engineer-{i}"},
        "publication_date": "2026-06-11T00:12:35Z",
        "contents": f"<p>Job {i} description</p>",
        "categories": [{"name": "Engineering"}],
    }
    job.update(over)
    return job


def _paged_handler(total: int) -> tuple[list[int], object]:
    """Serve ``total`` jobs, PAGE_SIZE per page, keyed off the 1-indexed ``page`` param."""
    pages_seen: list[int] = []
    size = TheMuseProvider.PAGE_SIZE

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages_seen.append(page)
        start = (page - 1) * size
        page_jobs = [_job(i) for i in range(start, min(start + size, total))]
        page_count = max(1, -(-total // size))
        return httpx.Response(
            200,
            json={
                "page": page,
                "page_count": page_count,
                "items_per_page": size,
                "total": total,
                "results": page_jobs,
            },
        )

    return pages_seen, handler


# --- matches ---------------------------------------------------------------


def test_matches_always_none_aggregator() -> None:
    assert TheMuseProvider.matches("themuse.com") is None
    assert TheMuseProvider.matches("https://www.themuse.com/api/public/jobs") is None


# --- fetch / concurrent pagination -----------------------------------------


async def test_fetch_paginates_concurrently_to_satisfy_limit() -> None:
    pages_seen, handler = _paged_handler(total=200)
    with respx.mock:
        route = respx.get(API)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=45), f)

    # limit=45 -> ceil(45/20)=3 pages fetched (1,2,3), then sliced to 45.
    assert route.call_count == 3
    assert sorted(pages_seen) == [1, 2, 3]
    assert len(raws) == 45
    # Page order preserved across the concurrent fan-out.
    assert raws[0].payload["name"] == "Engineer 0"
    assert raws[-1].payload["name"] == "Engineer 44"
    assert all(r.source == "themuse" and r.token is None for r in raws)


async def test_fetch_caps_at_max_pages_without_limit() -> None:
    _, handler = _paged_handler(total=10_000)
    with respx.mock:
        route = respx.get(API)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=200) as f:
            raws = await _provider().fetch("", SearchQuery(), f)

    assert route.call_count == TheMuseProvider.MAX_PAGES
    assert len(raws) == TheMuseProvider.MAX_PAGES * TheMuseProvider.PAGE_SIZE


async def test_fetch_single_page_for_small_limit() -> None:
    pages_seen, handler = _paged_handler(total=100)
    with respx.mock:
        route = respx.get(API)
        route.side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(limit=5), f)

    # ceil(5/20)=1 page; sliced to 5.
    assert route.call_count == 1
    assert pages_seen == [1]
    assert len(raws) == 5


async def test_fetch_passes_location_filter() -> None:
    seen_params: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(request.url.params.get("location"))
        return httpx.Response(200, json={"results": [_job(0)]})

    with respx.mock:
        respx.get(API).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            await _provider().fetch("", SearchQuery(location="Flexible / Remote", limit=1), f)

    assert seen_params == ["Flexible / Remote"]


# --- normalize -------------------------------------------------------------


async def test_normalize_full_field_mapping() -> None:
    pages_seen, handler = _paged_handler(total=1)
    with respx.mock:
        respx.get(API).side_effect = handler
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await _provider().fetch("", SearchQuery(), f)

    job = _provider().normalize(raws[0])
    assert job.source == "themuse"
    assert job.source_job_id == "1000"
    assert job.company == "Company 0"
    assert job.title == "Engineer 0"
    assert job.locations[0].raw == "New York, NY"
    assert job.locations[0].is_remote is False
    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.UNKNOWN  # "external" is not an employment type
    assert job.department == "Engineering"
    assert job.apply_url == "https://www.themuse.com/jobs/company/engineer-0"
    assert job.description_html == "<p>Job 0 description</p>"
    assert job.posted_at == datetime(2026, 6, 11, 0, 12, 35, tzinfo=timezone.utc)
    assert job.raw == raws[0].payload


def test_normalize_detects_remote_location() -> None:
    raw = _provider()._to_raw(_job(0, locations=[{"name": "Flexible / Remote"}]))
    job = _provider().normalize(raw)
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].is_remote is True


def test_normalize_maps_employment_type() -> None:
    raw = _provider()._to_raw(_job(0, type="Full-Time"))
    assert _provider().normalize(raw).employment_type is EmploymentType.FULL_TIME

    raw = _provider()._to_raw(_job(0, type="Internship"))
    assert _provider().normalize(raw).employment_type is EmploymentType.INTERNSHIP


def test_normalize_missing_fields_are_safe() -> None:
    raw = _provider()._to_raw({"id": 7})
    job = _provider().normalize(raw)
    assert job.source_job_id == "7"
    assert job.company == ""
    assert job.title == ""
    assert job.locations == []
    assert job.remote is RemoteType.UNKNOWN
    assert job.department is None
    assert job.apply_url is None
    assert job.posted_at is None


# --- fixture sanity --------------------------------------------------------


def test_real_fixture_normalizes() -> None:
    data = json.loads(load_fixture("themuse_sample.json"))
    assert data["page"] == 1
    assert data["results"]
    raws = [_provider()._to_raw(j) for j in data["results"]]
    jobs = [_provider().normalize(r) for r in raws]
    first = jobs[0]
    assert first.source == "themuse"
    assert first.title  # real title present
    assert first.company  # real company present
    assert first.apply_url and first.apply_url.startswith("https://www.themuse.com")


# --- fetch_detail: 404-vs-transient hardening contract ----------------------
#
# NOTE (see fetch_detail's docstring for full evidence): live probing found the landing page's
# own 404 is NOT a verified gone-signal for this source (a real board -- IBM -- returns 404 on
# this page for postings still alive per The Muse's own authoritative per-job API), so unlike
# every other hardened provider, fetch_detail deliberately RAISES on 404/410 here instead of
# returning None. These tests assert that deliberate deviation.

DETAIL_URL = "https://www.themuse.com/jobs/crh/machine-operator-5f28d4"

_MAIN_HTML = (
    "<html><body><main><h1>Machine Operator</h1><p>"
    + ("Handle assignments in a repetitive and sequential order. " * 20)
    + "</p></main></body></html>"
)


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
        id="21897374", source="themuse", token=None, apply_url=apply_url,
        listing_url=None, content_sig="s",
    )


async def test_fetch_detail_alive_returns_text() -> None:
    fetcher = _FakeFetcher(html=_MAIN_HTML)
    res = await TheMuseProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert isinstance(res, str)
    assert "Handle assignments" in res


async def test_fetch_detail_404_raises_not_none() -> None:
    """Deliberate deviation from the standard contract: 404 is NOT trusted as gone here."""
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    with pytest.raises(RuntimeError):
        await TheMuseProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_410_raises_not_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    with pytest.raises(RuntimeError):
        await TheMuseProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await TheMuseProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await TheMuseProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_missing_url_raises() -> None:
    fetcher = _FakeFetcher(html="<html></html>")
    with pytest.raises(RuntimeError):
        await TheMuseProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)


async def test_fetch_detail_too_short_raises() -> None:
    fetcher = _FakeFetcher(html="<html><body><main>too short</main></body></html>")
    with pytest.raises(RuntimeError):
        await TheMuseProvider().fetch_detail(_detail_ref(), fetcher)
