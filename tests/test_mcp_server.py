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
        "assess_fit",
        "h1b_jobs",
        "list_companies",
        "list_h1b_sponsors",
        "list_sources",
        "match_resume",
        "resolve_company",
        "search_jobs",
        "whats_new",
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


async def test_search_jobs_forwards_new_filters_to_index(monkeypatch) -> None:
    # The years/employment/currency/recency filters must reach the SearchQuery the index sees,
    # so a friend can use them via MCP (the years filter is the "0-2 yrs / new grad" use case).
    from ergon_tracker.index import router
    from ergon_tracker.models import EmploymentType

    captured = {}

    def fake_try_index(q):
        captured["q"] = q
        return []  # serve from index, empty result

    monkeypatch.setattr(router, "try_index", fake_try_index)
    out = await srv.search_jobs(
        keywords="engineer",
        min_years=0,
        max_years=2,
        include_unknown_years=False,
        employment_type="full_time",
        salary_min=140000,
        salary_currency="USD",
        posted_within_days=30,
    )
    assert out["health"][0]["source"] == "index"  # served from index, throttle-proof
    q = captured["q"]
    assert q.min_years == 0 and q.max_years == 2 and q.include_unknown_years is False
    assert q.employment_type == EmploymentType.FULL_TIME
    assert q.salary_min == 140000 and q.salary_currency == "USD"
    assert q.posted_after is not None  # posted_within_days -> cutoff datetime


async def test_search_jobs_forwards_degree_filter_to_index(monkeypatch) -> None:
    # The education filter (no competitor API has one) must reach the SearchQuery the index
    # sees — the "new grad: exclude M.D./Ph.D.-gated roles" use case.
    from ergon_tracker.index import router

    captured = {}

    def fake_try_index(q):
        captured["q"] = q
        return []

    monkeypatch.setattr(router, "try_index", fake_try_index)
    out = await srv.search_jobs(
        keywords="research associate",
        max_degree="bachelor",
        include_unknown_degree=False,
        max_years=2,
    )
    assert out["health"][0]["source"] == "index"
    q = captured["q"]
    assert q.max_degree == "bachelor" and q.include_unknown_degree is False
    assert q.max_years == 2


async def test_job_dict_includes_degree_fields() -> None:
    # Consumers must see BOTH the level and the required/preferred nuance on every result.
    from ergon_tracker.models import JobPosting

    j = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="William Blair",
        title="Equity Research Associate - BioTech",
        degree_min="phd_md",
        degree_required=False,  # "strongly preferred" — excluded by max_degree, shown as nuance
    )
    d = srv._job_to_dict(j)
    assert d["degree_min"] == "phd_md" and d["degree_required"] is False


async def test_job_dict_includes_years_and_core_fields() -> None:
    # "fetch information": the agent-facing dict must surface the rich fields, incl. the years a
    # role requires (so a years-filtered result can show why it matched).
    from ergon_tracker.models import JobPosting, Salary

    j = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Co",
        title="SWE",
        years_experience_min=0,
        years_experience_max=2,
        salary=Salary(min_amount=140000, max_amount=180000, currency="USD"),
    )
    d = srv._job_to_dict(j)
    assert d["years_min"] == 0 and d["years_max"] == 2
    assert d["salary"]["currency"] == "USD"
    for k in ("company", "title", "apply_url", "level", "sector", "posted_at", "visa_sponsor"):
        assert k in d


async def test_index_health_reports_freshness(monkeypatch) -> None:
    # The index health must carry as_of (which daily build served the query) so an agent can judge
    # data freshness.
    from ergon_tracker.index import router

    monkeypatch.setattr(router, "try_index_ranked", lambda q: [])
    monkeypatch.setattr(
        "ergon_tracker.index.cache.cached_index_build_id", lambda *a, **k: "build-2026-06-19-42"
    )
    out = await srv.search_jobs(keywords="engineer")
    assert out["health"][0]["source"] == "index"
    assert out["health"][0]["as_of"] == "build-2026-06-19-42"
