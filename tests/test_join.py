"""Unit tests for the join.com provider (respx-mocked, offline).

Fixture ``join_page.html`` is a trimmed careers page whose ``__NEXT_DATA__`` blob mirrors the
real ``https://join.com/companies/{token}`` shape (company + jobs.items + pagination).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from ergon_tracker.exceptions import TransientHTTPError
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.models import EmploymentType, RemoteType, SearchQuery, make_job_id
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.join import JoinProvider

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://join.com/companies/onetwosocial"


def _html() -> str:
    return (FIXTURES / "join_page.html").read_text()


def test_matches_recognizes_company_urls() -> None:
    p = JoinProvider
    assert p.matches("https://join.com/companies/onetwosocial") == "onetwosocial"
    assert p.matches("join.com/companies/acme-gmbh/jobs/123-role") == "acme-gmbh"
    assert p.matches("https://jobs.lever.co/spotify") is None
    assert p.matches("https://example.com/careers") is None


async def test_fetch_builds_rawjobs() -> None:
    with respx.mock:
        route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)
        assert route.called

    assert len(raws) == 2
    r0 = raws[0]
    assert r0.source == "join"
    assert r0.source_job_id == "16084660"
    assert r0.company == "OneTwoSocial"
    assert r0.token == "onetwosocial"
    assert r0.url == (
        "https://join.com/companies/onetwosocial/jobs/16084660-social-motion-designer"
    )


async def test_normalize_first_job() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)

    job = JoinProvider().normalize(raws[0])
    assert job.id == make_job_id("join", "16084660")
    assert job.title == "Social Motion Designer"
    assert job.company == "OneTwoSocial"
    assert job.apply_url.endswith("/jobs/16084660-social-motion-designer")
    assert job.locations and job.locations[0].city == "Munich"
    assert job.locations[0].country == "Germany"
    assert job.remote is RemoteType.ONSITE
    assert job.employment_type is EmploymentType.FULL_TIME  # "Employee"
    assert job.department == "Design"
    assert job.salary is None
    assert job.description_text is None
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert (job.posted_at.year, job.posted_at.month) == (2026, 4)


async def test_normalize_second_job_intern_remote() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)

    job = JoinProvider().normalize(raws[1])
    assert job.title == "Marketing Intern"
    assert job.employment_type is EmploymentType.INTERNSHIP  # "Intern"
    assert job.remote is RemoteType.REMOTE
    assert job.locations[0].city == "Berlin"


async def test_fetch_no_next_data_returns_empty() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html>no data</html>"))
        async with AsyncFetcher(per_host_rate=100) as f:
            raws = await JoinProvider().fetch("onetwosocial", SearchQuery(), f)
    assert raws == []


# --- board_count: cheap page-1 total change-CANDIDATE signal -----------------------------------


async def test_board_count_reads_pagination_total() -> None:
    with respx.mock:
        route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=_html()))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await JoinProvider().board_count("onetwosocial", f)
    assert count == 2  # join_page.html fixture's pagination.total
    assert route.call_count == 1  # exactly ONE request


async def test_board_count_404_returns_none() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(404))
        async with AsyncFetcher(per_host_rate=100) as f:
            count = await JoinProvider().board_count("onetwosocial", f)
    assert count is None


async def test_board_count_transient_error_raises() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(503))
        async with AsyncFetcher(per_host_rate=100, retries=1) as f:
            with pytest.raises(TransientHTTPError):
                await JoinProvider().board_count("onetwosocial", f)


async def test_board_count_missing_total_raises() -> None:
    with respx.mock:
        respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<html>no data</html>"))
        async with AsyncFetcher(per_host_rate=100) as f:
            with pytest.raises(RuntimeError):
                await JoinProvider().board_count("onetwosocial", f)


async def test_base_provider_board_count_is_none() -> None:
    assert await BaseProvider().board_count("onetwosocial", None) is None  # type: ignore[arg-type]


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
    tokens = _live_tokens("join", 15)  # many join boards are currently empty; widen the sample
    assert tokens, "no join tokens available (neither probe_targets.json nor seed.json)"
    checked = positive = 0
    async with AsyncFetcher(per_host_rate=5, retries=2) as f:
        for token in tokens:
            try:
                count = await JoinProvider().board_count(token, f)
            except Exception:
                continue
            if count is None:
                continue
            assert count >= 0, f"{token}: board_count returned negative {count}"
            sampled = await JoinProvider().fetch(token, SearchQuery(limit=5), f)
            assert count >= len(sampled), (
                f"{token}: board_count {count} < sampled fetch {len(sampled)}"
            )
            checked += 1
            positive += count > 0
    assert checked >= 1, "no live join board yielded a usable board_count"
    assert positive >= 1, "no live join board yielded a POSITIVE board_count"
