"""Unit tests for the DirectEmployers/dejobs provider (respx-mocked, offline)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.dejobs import DEJobsProvider

pytestmark = pytest.mark.anyio

API = "https://prod-search-api.jobsyn.org/api/v1/solr/search"


def _job(guid: str, title: str, loc: str) -> dict:
    return {
        "guid": guid,
        "title_exact": title,
        "company_exact": "American Airlines",
        "location_exact": loc,
        "title_slug": title.lower().replace(" ", "-"),
        "date_added": "2026-06-01T00:00:00Z",
        "description": f"<p>{title}</p>",
    }


def _mock(respx_mock: respx.MockRouter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        _job("G1", "Staff Assistant", "Los Angeles, CA"),
                        _job("G2", "Flight Attendant (Remote)", "Remote, US"),
                    ],
                    "pagination": {"total": 2, "page": 1, "page_size": 15, "has_more_pages": False},
                },
            )
        return httpx.Response(200, json={"jobs": [], "pagination": {"has_more_pages": False}})

    respx_mock.get(url__startswith=API).mock(side_effect=handler)


def test_matches_is_seed_only() -> None:
    # Aggregator: never auto-claims a host.
    assert DEJobsProvider.matches("https://dejobs.org/x") is None


async def test_fetch_and_normalize() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await DEJobsProvider().fetch("american-airlines", SearchQuery(), f)

    assert len(raws) == 2
    assert {r.company for r in raws} == {"American Airlines"}
    j0 = DEJobsProvider().normalize(raws[0])
    assert j0.id == make_job_id("dejobs", "G1")
    assert j0.title == "Staff Assistant"
    assert j0.locations[0].raw == "Los Angeles, CA"
    assert "G1" in j0.apply_url
    assert j0.posted_at is not None

    remote = DEJobsProvider().normalize(raws[1])
    assert remote.remote is RemoteType.REMOTE


async def test_fetch_respects_limit() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await DEJobsProvider().fetch("american-airlines", SearchQuery(limit=1), f)
    assert len(raws) == 1


async def test_fetch_degrades_on_error() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=API).mock(return_value=httpx.Response(403))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await DEJobsProvider().fetch("american-airlines", SearchQuery(), f)
    assert raws == []


# --- board_count: cheap page-1 total change-CANDIDATE signal -----------------------------------


async def test_board_count_reads_pagination_total() -> None:
    with respx.mock as respx_mock:
        _mock(respx_mock)
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await DEJobsProvider().board_count("american-airlines", f)
    assert count == 2  # _mock's page=1 pagination.total


async def test_board_count_empty_token_returns_none() -> None:
    async with AsyncFetcher(per_host_rate=100) as f:
        count = await DEJobsProvider().board_count("  ", f)
    assert count is None


async def test_board_count_404_returns_none() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=API).mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await DEJobsProvider().board_count("american-airlines", f)
    assert count is None


async def test_board_count_transient_error_raises() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=API).mock(return_value=httpx.Response(503))
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            with pytest.raises(TransientHTTPError):
                await DEJobsProvider().board_count("american-airlines", f)


async def test_board_count_missing_total_raises() -> None:
    with respx.mock as respx_mock:
        respx_mock.get(url__startswith=API).mock(
            return_value=httpx.Response(200, json={"jobs": [], "pagination": {}})
        )
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(RuntimeError):
                await DEJobsProvider().board_count("american-airlines", f)


async def test_base_provider_board_count_is_none() -> None:
    assert (
        await BaseProvider().board_count("american-airlines", None) is None  # type: ignore[arg-type]
    )


# --- board_count: live gate (ERGON_LIVE_TESTS=1) ------------------------------------------------

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
    tokens = _live_tokens("dejobs", 10)
    assert tokens, "no dejobs tokens available (neither probe_targets.json nor seed.json)"
    checked = positive = 0
    async with AsyncFetcher(per_host_rate=5, retries=2) as f:
        for token in tokens:
            try:
                count = await DEJobsProvider().board_count(token, f)
            except Exception:
                continue
            if count is None:
                continue
            assert count >= 0, f"{token}: board_count returned negative {count}"
            sampled = await DEJobsProvider().fetch(token, SearchQuery(limit=10), f)
            assert count >= len(sampled), (
                f"{token}: board_count {count} < sampled fetch {len(sampled)}"
            )
            checked += 1
            positive += count > 0
    assert checked >= 1, "no live dejobs board yielded a usable board_count"
    assert positive >= 1, "no live dejobs board yielded a POSITIVE board_count"
