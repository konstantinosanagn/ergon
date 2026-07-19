"""Unit tests for the Eightfold provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.eightfold import EightfoldProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://fcx.eightfold.ai/api/apply/v2/jobs"


def _fixture() -> dict:
    return json.loads((FIXTURES / "eightfold_jobs.json").read_text())


def _mock(respx_mock: respx.MockRouter) -> None:
    """Discovery GET (no params) returns the config dict (with domain + positions);
    a second page returns empty positions so pagination terminates."""
    payload = _fixture()
    config = {"domain": payload["domain"]}  # no-param discovery response
    empty = {"domain": payload["domain"], "count": payload["count"], "positions": []}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "domain" not in params:
            return httpx.Response(200, json=config)
        if params.get("start") == "0":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=empty)

    respx_mock.get(url__startswith=API).mock(side_effect=handler)


def test_matches_recognizes_host() -> None:
    p = EightfoldProvider
    assert p.matches("https://fcx.eightfold.ai/careers") == "fcx"
    assert p.matches("fcx.eightfold.ai/api/apply/v2/jobs") == "fcx"
    assert p.matches("https://whirlpool.eightfold.ai") == "whirlpool"
    assert p.matches("https://www.eightfold.ai") is None
    assert p.matches("https://app.eightfold.ai") is None
    assert p.matches("https://boards.greenhouse.io/airbnb") is None
    assert p.matches("https://example.com") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(), f)

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "eightfold"
    assert r0.source_job_id == "42478672"
    assert r0.company == "fcx"
    assert r0.token == "fcx"
    assert r0.url == "https://talent.fmjobs.com/careers/job/42478672"
    assert r0.payload["name"] == "Manager Engineering - Electrical"


async def test_normalize_onsite_job() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(), f)

    job = EightfoldProvider().normalize(raws[0])

    assert job.id == make_job_id("eightfold", "42478672")
    assert job.title == "Manager Engineering - Electrical"
    assert job.company == "fcx"
    assert job.department == "Engineering Services"
    assert job.apply_url == "https://talent.fmjobs.com/careers/job/42478672"
    assert job.remote is RemoteType.ONSITE
    assert job.employment_type is EmploymentType.UNKNOWN
    assert job.salary is None
    assert job.description_html is None
    assert job.description_text is None
    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.raw == "New Orleans, LA USA 70112"
    assert loc.is_remote is False
    # t_create = 1781660892 epoch seconds
    assert job.posted_at is not None
    assert job.posted_at.tzinfo is not None
    assert int(job.posted_at.astimezone(timezone.utc).timestamp()) == 1781660892
    assert job.raw == raws[0].payload


async def test_normalize_second_job_department_and_url() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(), f)

    job = EightfoldProvider().normalize(raws[1])
    assert job.title == "Chief Biodiversity and Wildlife"
    assert job.department == "Environmental"
    assert job.apply_url == "https://talent.fmjobs.com/careers/job/42477103"
    assert job.locations[0].raw == "Phoenix, AZ USA 85040"


async def test_fetch_respects_query_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("fcx", SearchQuery(limit=2), f)
    assert len(raws) == 2


_PCSX_POSITIONS = [
    {
        "id": 481077513632,
        "displayJobId": "260026021",
        "name": "barista - Store# 06447",
        "locations": ["890 E Alosta Ave, Azusa, California, United States"],
        "standardizedLocations": ["Azusa, CA, US"],
        "postedTs": 1774929600,
        "creationTs": 1775051037,
        "department": "Barista",
        "workLocationOption": "onsite",
        "atsJobId": "260026021",
        "positionUrl": "/careers/job/481077513632",
    },
    {
        "id": 481077513999,
        "displayJobId": "260026099",
        "name": "shift supervisor",
        "locations": ["Remote - United States"],
        "postedTs": 1774930000,
        "department": "Retail",
        "workLocationOption": "remote",
        "positionUrl": "/careers/job/481077513999",
    },
]


def _mock_pcsx(respx_mock: respx.MockRouter, tenant: str) -> None:
    """apply/v2 is locked (200 {"message": ...}); PCSX search returns 2 positions."""
    locked = {"message": "Not authorized for PCSX"}
    respx_mock.get(url__startswith=f"https://{tenant}.eightfold.ai/api/apply/v2/jobs").mock(
        return_value=httpx.Response(200, json=locked)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start")
        positions = _PCSX_POSITIONS if start == "0" else []
        return httpx.Response(
            200, json={"status": 200, "data": {"positions": positions, "count": 2}}
        )

    respx_mock.get(url__startswith=f"https://{tenant}.eightfold.ai/api/pcsx/search").mock(
        side_effect=handler
    )


async def test_pcsx_fallback_unlocks_locked_tenant() -> None:
    """apply/v2-locked tenant falls back to PCSX and parses its camelCase records."""
    with respx.mock as respx_mock:
        _mock_pcsx(respx_mock, "starbucks")
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("starbucks", SearchQuery(), f)

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source_job_id == "481077513632"
    assert r0.company == "starbucks"
    # relative positionUrl -> absolute on the tenant host
    assert r0.url == "https://starbucks.eightfold.ai/careers/job/481077513632"

    job0 = EightfoldProvider().normalize(r0)
    assert job0.title == "barista - Store# 06447"
    assert job0.department == "Barista"
    assert job0.remote is RemoteType.ONSITE
    # postedTs -> posted_at (epoch seconds)
    assert int(job0.posted_at.astimezone(timezone.utc).timestamp()) == 1774929600

    job1 = EightfoldProvider().normalize(raws[1])
    assert job1.remote is RemoteType.REMOTE


async def test_pcsx_fallback_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock_pcsx(respx_mock, "starbucks")
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("starbucks", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fully_closed_tenant_returns_empty() -> None:
    """Tenant where BOTH apply/v2 and PCSX are disabled (e.g. EY) -> []."""
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith="https://ey.eightfold.ai/api/apply/v2/jobs").mock(
            return_value=httpx.Response(403, json={"message": "Not authorized for PCSX"})
        )
        respx_mock.get(url__startswith="https://ey.eightfold.ai/api/pcsx/search").mock(
            return_value=httpx.Response(403, json={"message": "PCSX is not enabled for this user."})
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await EightfoldProvider().fetch("ey", SearchQuery(), f)
    assert raws == []


# --- fetch_detail: 404-vs-transient hardening contract ----------------------

DETAIL_APPLY_URL = "https://acme.eightfold.ai/careers/job/42478672"
DETAIL_URL = "https://acme.eightfold.ai/api/apply/v2/jobs/42478672"


class _FakeFetcher:
    """Stands in for AsyncFetcher: returns a fixed payload, or raises a fixed exception."""

    def __init__(self, *, payload: object = None, exc: BaseException | None = None) -> None:
        self._payload = payload
        self._exc = exc
        self.calls: list[str] = []

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._payload


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", DETAIL_URL)
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")  # pragma: no cover


def _detail_ref(apply_url: str | None = DETAIL_APPLY_URL) -> DetailRef:
    return DetailRef(
        id="42478672",
        source="eightfold",
        token=None,
        apply_url=apply_url,
        listing_url=None,
        content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await EightfoldProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await EightfoldProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await EightfoldProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await EightfoldProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_description() -> None:
    payload = {"job_description": "<p>Build chips.</p>"}
    fetcher = _FakeFetcher(payload=payload)
    res = await EightfoldProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert res == "<p>Build chips.</p>"


async def test_fetch_detail_no_id_raises() -> None:
    # No apply_url/listing_url from which a job id can be parsed is NOT evidence of death -> raise.
    fetcher = _FakeFetcher(payload={})
    with pytest.raises(RuntimeError):
        await EightfoldProvider().fetch_detail(_detail_ref(apply_url=None), fetcher)


async def test_fetch_detail_missing_job_description_raises() -> None:
    # A 200 with no usable job_description is indeterminate, not a verified soft-404 -> raise.
    fetcher = _FakeFetcher(payload={"someOtherKey": "x"})
    with pytest.raises(RuntimeError):
        await EightfoldProvider().fetch_detail(_detail_ref(), fetcher)
