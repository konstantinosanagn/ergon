"""Tests for the ergon_tracker MCP server (skipped if the optional `mcp` extra isn't installed)."""

from __future__ import annotations

import httpx
import pytest
import respx

pytest.importorskip("mcp", reason="install ergon_tracker[mcp] to test the MCP server")

from ergon_tracker import mcp_server as srv  # noqa: E402

pytestmark = pytest.mark.anyio


async def test_tools_registered() -> None:
    tools = await srv.mcp.list_tools()
    assert sorted(t.name for t in tools) == [
        "list_h1b_sponsors",
        "list_sources",
        "resolve_company",
        "search_jobs",
    ]


async def test_every_tool_has_description_and_schema() -> None:
    tools = await srv.mcp.list_tools()
    for tool in tools:
        assert tool.description
        assert tool.inputSchema  # FastMCP derives JSON Schema from the signature


def test_list_sources_reports_providers_and_registry() -> None:
    out = srv.list_sources()
    assert "greenhouse" in out["providers"]
    assert out["registry_companies"] >= 200


def test_resolve_company_url_and_domain() -> None:
    assert srv.resolve_company("jobs.lever.co/spotify")["ats"] == "lever"
    seed_hit = srv.resolve_company("stripe.com")
    assert seed_hit["matched"] and seed_hit["ats"] == "greenhouse"
    assert srv.resolve_company("unknown.example")["matched"] is False


async def test_search_jobs_returns_compact_jobs_and_health() -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Backend Engineer",
                "absolute_url": "https://boards.greenhouse.io/stripe/jobs/1",
                "updated_at": "2026-06-01T00:00:00Z",
                "location": {"name": "Berlin"},
                "content": "x",
                "departments": [{"name": "Engineering"}],
                "offices": [{"name": "Berlin", "location": "Berlin"}],
                "metadata": [],
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(200, json=payload)
        )
        out = await srv.search_jobs(keywords="backend", companies=["stripe.com"], limit=5)

    assert out["count"] == 1
    job = out["jobs"][0]
    assert job["company"] and job["title"] == "Senior Backend Engineer"
    assert job["apply_url"]
    assert "raw" not in job  # compact view, no payload bloat
    health = {h["source"]: h for h in out["health"]}
    assert health["greenhouse"]["ok"]


async def test_search_jobs_broad_uses_index_not_live(monkeypatch) -> None:
    # A broad search (no companies/sources) must be served by the prebuilt index, NOT fanned out
    # to live aggregators/registry. Regression for the agent-safety guard that used to force
    # sources=aggregators and thereby bypass the index entirely.
    import ergon_tracker.index.router as router
    from ergon_tracker.models import JobPosting

    fake = [
        JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="ML Eng")
    ]
    monkeypatch.setattr(router, "try_index", lambda q: fake)

    out = await srv.search_jobs(keywords="ml engineer", sector="AI/ML", remote=True, limit=5)
    assert out["count"] == 1
    assert out["health"][0]["source"] == "index"  # served from index, zero ATS calls
    assert out["jobs"][0]["company"] == "Acme"


async def test_search_jobs_broad_falls_back_to_aggregators_when_index_down(monkeypatch) -> None:
    # If the index is unavailable, a broad search falls back to aggregators (fast/safe), NEVER a
    # live fan-out across the whole registry.
    import ergon_tracker.index.router as router

    monkeypatch.setattr(router, "try_index", lambda q: None)
    captured = {}

    class _FakeJS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def search(self, query):
            captured["sources"] = query.sources
            from ergon_tracker.models import SearchResult

            return SearchResult(jobs=[], health=[])

    monkeypatch.setattr(srv, "AsyncErgonTracker", lambda *a, **k: _FakeJS())
    await srv.search_jobs(keywords="nurse", limit=5)
    assert set(captured["sources"]) == set(srv.AGGREGATOR_PROVIDERS)  # aggregators, not 46k boards
