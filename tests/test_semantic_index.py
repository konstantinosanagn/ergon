"""Semantic reranking is applied to index-served (broad) queries, not just live ones."""

from __future__ import annotations

import anyio

from ergon_tracker.engine import run_search
from ergon_tracker.models import JobPosting, SearchQuery


def test_semantic_reranks_index_results(monkeypatch):
    import ergon_tracker.index.router as router
    import ergon_tracker.semantic as semantic

    jobs = [
        JobPosting.create(source="greenhouse", source_job_id=str(i), company="C", title=t)
        for i, t in enumerate(["Alpha", "Bravo", "Charlie"])
    ]
    monkeypatch.setattr(router, "try_index", lambda q: list(jobs))
    # Isolate the QUERY-TIME rerank fallback: force no vectors sidecar so _vector_rerank returns
    # None and try_index_ranked falls through to rank() with the patched reranker below. Without
    # this, _vector_rerank downloads the real published sidecar and embeds with the real model
    # (its module-top get_semantic_reranker isn't the one patched here). The sidecar/vector path
    # has its own coverage in tests/test_router_vectors.py.
    monkeypatch.setattr(router, "_rich_path", lambda: None)

    class _FakeRR:  # scores ascending -> rank() orders desc -> reverses input order
        def rerank(self, query, js):
            return [float(i) for i in range(len(js))]

    monkeypatch.setattr(semantic, "get_semantic_reranker", lambda *a, **k: _FakeRR())

    res = anyio.run(run_search, SearchQuery(keywords="x", semantic=True, limit=10), None)
    assert res.health[0].source == "index"
    assert [j.title for j in res.jobs] == ["Charlie", "Bravo", "Alpha"]  # semantically reranked


def test_index_lexical_when_not_semantic(monkeypatch):
    import ergon_tracker.index.router as router

    jobs = [JobPosting.create(source="greenhouse", source_job_id="1", company="C", title="Alpha")]
    monkeypatch.setattr(router, "try_index", lambda q: list(jobs))
    res = anyio.run(run_search, SearchQuery(keywords="x", limit=10), None)
    assert res.health[0].source == "index" and [j.title for j in res.jobs] == ["Alpha"]
