from __future__ import annotations

import gzip
import hashlib
import json

from tests.test_rich_index import FAKE, _build_rich, _job

from ergon_tracker.index import router
from ergon_tracker.index.rich import RICH_SCHEMA_VERSION
from ergon_tracker.models import SearchQuery


def _publish(remote, tmp_path, jobs):
    src = _build_rich(tmp_path, jobs)
    raw = src.read_bytes()
    (remote / "index-vectors.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest-vectors.json").write_text(
        json.dumps(
            {
                "build_id": "b1",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "schema_version": RICH_SCHEMA_VERSION,
            }
        )
    )


def test_vector_rerank_orders_by_cosine(tmp_path, monkeypatch):
    from ergon_tracker.index.cache import RichCache

    py = _job("py", "Python Engineer", "python kubernetes")
    nu = _job("nu", "Nurse", "nurse clinical")
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path, [py, nu])
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "c")
    monkeypatch.setattr(router, "_rich_path", cache.ensure_fresh)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: FAKE)
    q = SearchQuery(keywords="python kubernetes", semantic=True, limit=2)
    out = router._vector_rerank(q, [nu, py], want=2)
    assert [j.id for j in out] == [py.id, nu.id]  # cosine puts the python job first


def test_vector_rerank_hybrid_scores_uncovered_pool_members(tmp_path, monkeypatch):
    """Sidecar covers only `py`; `nu` is absent from job_vectors and must still be scored+kept."""
    from ergon_tracker.index.cache import RichCache

    py = _job("py", "Python Engineer", "python kubernetes")
    nu = _job("nu", "Nurse", "nurse clinical")
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path, [py])  # only py has a stored vector
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "c")
    monkeypatch.setattr(router, "_rich_path", cache.ensure_fresh)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: FAKE)
    q = SearchQuery(keywords="python kubernetes", semantic=True, limit=2)
    out = router._vector_rerank(q, [nu, py], want=2)
    assert {j.id for j in out} == {py.id, nu.id}  # uncovered job NOT dropped
    assert out[0].id == py.id  # covered + more similar still ranks first


def test_vector_rerank_returns_none_without_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "_rich_path", lambda: None)
    q = SearchQuery(keywords="python", semantic=True, limit=2)
    assert router._vector_rerank(q, [_job("a", "A", "python")], want=2) is None


class _CountingReranker:
    """Wraps FAKE and counts how many jobs are embedded at QUERY TIME (embed_jobs), so a test can
    assert the uncovered-embed cap is honoured."""

    def __init__(self) -> None:
        self.embedded = 0

    def embed_query(self, q):  # noqa: ANN001, ANN201
        return FAKE.embed_query(q)

    def embed_jobs(self, jobs, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.embedded += len(jobs)
        return FAKE.embed_jobs(jobs, **kwargs)


class _RerankFake:
    """Query embed via FAKE; ``rerank`` forces the PICKME job to the top so a test can prove the
    query-time ``rank()`` fallback actually ran (bare lexical order would leave PICKME last)."""

    def embed_query(self, q):  # noqa: ANN001, ANN201
        return FAKE.embed_query(q)

    def embed_jobs(self, jobs, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return FAKE.embed_jobs(jobs, **kwargs)

    def rerank(self, query, jobs):  # noqa: ANN001, ANN201
        return [1.0 if j.title == "PICKME" else 0.0 for j in jobs]


def test_vector_rerank_caps_query_time_embeddings(tmp_path, monkeypatch):
    """150 uncovered pool members, sidecar covers none of them -> exactly _UNCOVERED_EMBED_CAP (100)
    query-time embeddings, never all 150 (no regression vs. the pre-change ~100-doc rerank)."""
    from ergon_tracker.index.cache import RichCache

    cov = _job("cov", "Covered", "python")
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path, [cov])  # sidecar covers only `cov`, none of the pool
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "c")
    counter = _CountingReranker()
    monkeypatch.setattr(router, "_rich_path", cache.ensure_fresh)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: counter)
    pool = [_job(f"u{i}", f"Role {i}", "python kubernetes", company=f"Co{i}") for i in range(150)]
    q = SearchQuery(keywords="python kubernetes", semantic=True, limit=150)
    out = router._vector_rerank(q, pool, want=150)
    assert counter.embedded == 100  # capped at _UNCOVERED_EMBED_CAP, not all 150 uncovered
    assert out is not None and len(out) == 150  # nothing dropped


def test_vector_rerank_keeps_beyond_cap_uncovered_in_lexical_order(tmp_path, monkeypatch):
    """The 50 uncovered jobs past the cap are NOT dropped and NOT interleaved: they appear after all
    cosine-scored jobs, in their incoming lexical (BM25) order."""
    from ergon_tracker.index.cache import RichCache

    cov = _job("cov", "Covered", "python")
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path, [cov])
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "c")
    monkeypatch.setattr(router, "_rich_path", cache.ensure_fresh)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: FAKE)
    pool = [_job(f"u{i}", f"Role {i}", "python kubernetes", company=f"Co{i}") for i in range(150)]
    q = SearchQuery(keywords="python kubernetes", semantic=True, limit=150)
    out = router._vector_rerank(q, pool, want=150)
    assert out is not None and len(out) == 150
    beyond = [j.id for j in out[100:]]  # last 50 = the beyond-cap uncovered overflow
    assert beyond == [j.id for j in pool[100:]]  # preserved lexical order, appended after scored


def test_serving_never_builds_VectorIndex(tmp_path, monkeypatch):
    """Guard on the LIVE path: a POPULATED sidecar so open_rich/vector_search genuinely execute, with
    VectorIndex patched to raise — the serving path must never construct it (~2.26GB float32)."""
    import ergon_tracker.index.rich as rich
    from ergon_tracker.index.cache import RichCache

    py = _job("py", "Python Engineer", "python kubernetes")
    nu = _job("nu", "Nurse", "nurse clinical")
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, tmp_path, [py, nu])
    cache = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "c")
    monkeypatch.setattr(router, "_rich_path", cache.ensure_fresh)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: FAKE)

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError(
            "VectorIndex must never be built in the serving path (~2.26GB float32)"
        )

    monkeypatch.setattr(rich, "VectorIndex", boom)
    q = SearchQuery(keywords="python kubernetes", semantic=True, limit=2)
    out = router._vector_rerank(q, [nu, py], want=2)  # runs the real vector path
    assert out is not None and [j.id for j in out] == [py.id, nu.id]  # served, VectorIndex untouched


def test_try_index_ranked_falls_back_to_query_rerank_when_vector_path_raises(tmp_path, monkeypatch):
    """Sidecar PRESENT but the vector path raises -> _vector_rerank returns None (never raises), so the
    query-time rank() rung runs (proven by the reranker reordering PICKME to the top), NOT bare
    lexical order (which would leave PICKME — a description-only keyword hit — last)."""
    import ergon_tracker.index.rich as rich
    from ergon_tracker.index.backend import SqliteIndexBackend
    from ergon_tracker.index.build import build_index

    jobs = [_job(str(i), f"ML Engineer {i}", "ml", company=f"Co{i}") for i in range(4)]
    jobs.append(_job("p", "PICKME", "ml engineer", company="CoP"))  # keyword only in description
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    monkeypatch.setattr(router, "_load_sharded", lambda q: None)
    monkeypatch.setattr(router, "_load_slim", lambda: None)
    monkeypatch.setattr(router, "_load_backend", lambda: SqliteIndexBackend(p))

    rich_path = _build_rich(tmp_path, [_job("x", "X", "x")])  # a real, openable sidecar
    fake = _RerankFake()
    monkeypatch.setattr(router, "_rich_path", lambda: rich_path)
    monkeypatch.setattr(router, "get_semantic_reranker", lambda: fake)  # synthetic query embed
    monkeypatch.setattr("ergon_tracker.semantic.get_semantic_reranker", lambda: fake)  # fallback rank

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("vector_search exploded")

    monkeypatch.setattr(rich, "vector_search", boom)  # the vector path fails mid-flight
    out = router.try_index_ranked(SearchQuery(keywords="engineer", semantic=True, limit=10))
    assert out is not None
    assert out[0].title == "PICKME"  # query-time rank() ran; bare lexical would rank PICKME last
