"""Unit tests for the BambooHR provider (respx-mocked, offline).

Fixture ``bamboohr_jobs.json`` is a trimmed capture of the live
``https://aca.bamboohr.com/careers/list`` response (token "aca"); the third entry is
edited to exercise the ``isRemote`` mapping path.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.bamboohr import BambooHRProvider
from ergon_tracker.providers.base import BaseProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
BOARD_URL = "https://aca.bamboohr.com/careers/list"


def _fixture() -> dict:
    return json.loads((FIXTURES / "bamboohr_jobs.json").read_text())


def test_matches_recognizes_hosts() -> None:
    p = BambooHRProvider
    assert p.matches("https://aca.bamboohr.com/careers/list") == "aca"
    assert p.matches("https://acme.bamboohr.com") == "acme"
    assert p.matches("acme.bamboohr.com/careers/42") == "acme"
    assert p.matches("https://www.bamboohr.com/careers") is None
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

        assert str(route.calls.last.request.url) == BOARD_URL

    assert len(raws) == 3
    r0 = raws[0]
    assert r0.source == "bamboohr"
    assert r0.source_job_id == "39"
    assert r0.company == "aca"
    assert r0.token == "aca"
    assert r0.url == "https://aca.bamboohr.com/careers/39"
    assert r0.payload["jobOpeningName"] == "Aircraft Maintenance Engineer"


async def test_normalize_legacy_location() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

    job = BambooHRProvider().normalize(raws[0])

    assert job.id == make_job_id("bamboohr", "39")
    assert job.source == "bamboohr"
    assert job.source_job_id == "39"
    assert job.title == "Aircraft Maintenance Engineer"
    assert job.company == "aca"
    assert job.apply_url == "https://aca.bamboohr.com/careers/39"

    assert len(job.locations) == 1
    loc = job.locations[0]
    assert loc.city == "Edmonton International Airport"
    assert loc.region == "Alberta"
    assert loc.country is None
    assert loc.is_remote is False

    assert job.remote is RemoteType.UNKNOWN
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.department == "Maintenance"
    assert job.salary is None
    assert job.posted_at is None
    assert job.description_html is None
    assert job.description_text is None
    assert job.raw == raws[0].payload


async def test_normalize_structured_ats_location() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

    job = BambooHRProvider().normalize(raws[1])
    assert job.title == "Advanced Care Paramedic - Flights"
    # "Part-Time" -> PART_TIME
    assert job.employment_type is EmploymentType.PART_TIME
    loc = job.locations[0]
    assert loc.city == "Edmonton"
    assert loc.region == "Alberta"
    assert loc.country == "Canada"
    assert loc.raw == "Edmonton, Alberta, Canada"
    assert job.remote is RemoteType.UNKNOWN


async def test_normalize_remote_and_internship() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)

    job = BambooHRProvider().normalize(raws[2])
    assert job.title == "Remote Dispatch Coordinator"
    # isRemote=true -> REMOTE, with a remote-flagged (otherwise empty) location
    assert job.remote is RemoteType.REMOTE
    assert job.locations and job.locations[0].is_remote is True
    # "Internship" -> INTERNSHIP
    assert job.employment_type is EmploymentType.INTERNSHIP


async def test_fetch_empty_or_missing_result() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"meta": {}}))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await BambooHRProvider().fetch("aca", SearchQuery(), f)
    assert raws == []


# --- fetch_detail: 404-vs-transient hardening contract ----------------------

DETAIL_APPLY_URL = "https://aca.bamboohr.com/careers/109"
DETAIL_URL = "https://aca.bamboohr.com/careers/109/detail"


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
        id="109",
        source="bamboohr",
        token="aca",
        apply_url=apply_url,
        listing_url=None,
        content_sig="s",
    )


async def test_fetch_detail_404_returns_none() -> None:
    # Live-verified (2026-07-19, aca.bamboohr.com): a nonexistent job id 404s with
    # {"type":"not_found","title":"Resource not found.",...} -- a real HTTP 404, not a 200
    # soft-404 body.
    fetcher = _FakeFetcher(exc=_http_status_error(404))
    res = await BambooHRProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_410_returns_none() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(410))
    res = await BambooHRProvider().fetch_detail(_detail_ref(), fetcher)
    assert res is None


async def test_fetch_detail_transient_error_raises() -> None:
    fetcher = _FakeFetcher(exc=TransientHTTPError("503 from x"))
    with pytest.raises(TransientHTTPError):
        await BambooHRProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_503_status_raises() -> None:
    fetcher = _FakeFetcher(exc=_http_status_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await BambooHRProvider().fetch_detail(_detail_ref(), fetcher)


async def test_fetch_detail_alive_returns_description() -> None:
    payload = {"result": {"jobOpening": {"description": "<p>Build historic restorations.</p>"}}}
    fetcher = _FakeFetcher(payload=payload)
    res = await BambooHRProvider().fetch_detail(_detail_ref(), fetcher)
    assert fetcher.calls == [DETAIL_URL]
    assert res == "<p>Build historic restorations.</p>"


async def test_fetch_detail_unparseable_ref_raises() -> None:
    # No apply_url/listing_url/token+id from which (token, id) can be derived is NOT evidence
    # of death -> raise.
    fetcher = _FakeFetcher(payload={})
    ref = DetailRef(
        id="", source="bamboohr", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    with pytest.raises(RuntimeError):
        await BambooHRProvider().fetch_detail(ref, fetcher)


async def test_fetch_detail_missing_description_raises() -> None:
    # A 200 with no usable description is indeterminate, not a verified soft-404 -> raise.
    fetcher = _FakeFetcher(payload={"result": {"jobOpening": {}}})
    with pytest.raises(RuntimeError):
        await BambooHRProvider().fetch_detail(_detail_ref(), fetcher)


# --- board_count: cheap meta.totalCount change-CANDIDATE signal ----------------------------------


async def test_board_count_reads_meta_total_count() -> None:
    with respx.mock:
        route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_fixture()))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await BambooHRProvider().board_count("aca", f)
    assert count == 3  # bamboohr_jobs.json fixture's meta.totalCount
    assert route.call_count == 1  # exactly ONE request -- the same call fetch() makes


async def test_board_count_404_returns_none() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await BambooHRProvider().board_count("aca", f)
    assert count is None


async def test_board_count_transient_error_raises() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(503))
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            with pytest.raises(TransientHTTPError):
                await BambooHRProvider().board_count("aca", f)


async def test_board_count_missing_total_count_raises() -> None:
    with respx.mock:
        respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"meta": {}}))
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(RuntimeError):
                await BambooHRProvider().board_count("aca", f)


async def test_base_provider_board_count_is_none() -> None:
    assert await BaseProvider().board_count("aca", None) is None  # type: ignore[arg-type]


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
    tokens = _live_tokens("bamboohr", 5)
    assert tokens, "no bamboohr tokens available (neither probe_targets.json nor seed.json)"
    checked = positive = 0
    async with AsyncFetcher(per_host_rate=5, retries=2) as f:
        for token in tokens:
            try:
                count = await BambooHRProvider().board_count(token, f)
            except Exception:
                continue
            if count is None:
                continue
            assert count >= 0, f"{token}: board_count returned negative {count}"
            sampled = await BambooHRProvider().fetch(token, SearchQuery(), f)
            assert count >= len(sampled), (
                f"{token}: board_count {count} < sampled fetch {len(sampled)}"
            )
            checked += 1
            positive += count > 0
    assert checked >= 1, "no live bamboohr board yielded a usable board_count"
    assert positive >= 1, "no live bamboohr board yielded a POSITIVE board_count"
