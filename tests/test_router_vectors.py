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


def test_serving_never_builds_VectorIndex(monkeypatch):
    import ergon_tracker.index.rich as rich

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError(
            "VectorIndex must never be built in the serving path (~2.26GB float32)"
        )

    monkeypatch.setattr(rich, "VectorIndex", boom)
    monkeypatch.setattr(router, "_rich_path", lambda: None)
    q = SearchQuery(keywords="python", semantic=True, limit=2)
    router._vector_rerank(q, [_job("a", "A", "python")], want=2)  # must not touch VectorIndex
