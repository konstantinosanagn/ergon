"""Tests for the match_resume MCP tool (semantic-fit ranking; reranker + index monkeypatched)."""

from __future__ import annotations

from ergon_tracker import mcp_server
from ergon_tracker.models import JobPosting, Location, RemoteType


def _job(title):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=title,
        company="Co",
        title=title,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
    )


class _FakeReranker:
    def __init__(self, fit):
        self.fit = fit

    def rerank(self, query, jobs):
        return [self.fit.get(j.title, 0.0) for j in jobs]


def test_ranks_by_semantic_fit(monkeypatch):
    pool = [_job("Backend Engineer"), _job("ML Engineer"), _job("Sales Rep")]
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: list(pool))
    monkeypatch.setattr(
        "ergon_tracker.semantic.get_semantic_reranker",
        lambda *a, **k: _FakeReranker(
            {"ML Engineer": 0.91, "Backend Engineer": 0.5, "Sales Rep": 0.1}
        ),
    )
    res = mcp_server.match_resume(resume="I build ML pipelines in PyTorch", limit=2)
    assert res["ranked_by"] == "semantic_fit" and res["count"] == 2
    assert [j["title"] for j in res["jobs"]] == ["ML Engineer", "Backend Engineer"]  # by fit desc
    assert res["jobs"][0]["fit_score"] == 0.91


def test_degrades_to_lexical_without_semantic_extra(monkeypatch):
    pool = [_job("Backend Engineer"), _job("Marketing Lead")]
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: list(pool))

    def boom(*a, **k):
        raise ImportError("fastembed not installed")

    monkeypatch.setattr("ergon_tracker.semantic.get_semantic_reranker", boom)
    res = mcp_server.match_resume(
        resume="senior backend engineer", keywords="backend engineer", limit=5
    )
    assert "lexical" in res["ranked_by"]
    assert res["jobs"][0]["title"] == "Backend Engineer"  # lexical fallback still ranks sensibly


def test_index_unavailable_is_graceful(monkeypatch):
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: None)
    res = mcp_server.match_resume(resume="anything")
    assert res["count"] == 0 and "index unavailable" in res["note"]


def test_empty_pool_and_empty_resume(monkeypatch):
    monkeypatch.setattr("ergon_tracker.index.router.try_index", lambda q: [])
    assert "loosen" in mcp_server.match_resume(resume="x")["note"]
    assert "provide" in mcp_server.match_resume(resume="   ")["note"]  # short-circuits before index
