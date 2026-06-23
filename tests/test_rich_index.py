"""Rich index tier: full-JD FTS + pre-stored int8 embeddings + the cascade reconcile + stress.

A fake 384-dim embedder keeps tests fast/deterministic (no ONNX model). The first dims encode a small
vocab so vector ranking is checkable; the rest are deterministic padding so dim/quantization/matmul are
exercised at realistic width."""

from __future__ import annotations

import hashlib
import time

from ergon_tracker.index.build import build_index
from ergon_tracker.index.rich import (
    VectorIndex,
    build_rich_tier,
    dequantize_int8,
    fulltext_search,
    open_rich,
    quantize_int8,
    reconcile_rich_tier,
    rich_meta,
    vector_search,
)
from ergon_tracker.models import JobPosting, Location, RemoteType

_VOCAB = ["python", "kubernetes", "sales", "nurse", "finance"]
_DIM = 384


class FakeReranker:
    """Deterministic 384-dim embedder: vocab-count dims (dominant) + hashed padding (realistic width)."""

    model_name = "fake-384"

    def _vec(self, text: str) -> list[float]:
        t = (text or "").lower()
        base = [t.count(w) * 5.0 + 0.01 for w in _VOCAB]
        h = hashlib.sha1((text or "").encode()).digest()
        pad = [(h[i % len(h)] / 255.0) * 0.1 for i in range(_DIM - len(base))]
        return base + pad

    def _text(self, j: JobPosting) -> str:
        return f"{j.title or ''} {j.description_text or ''}"

    def embed_jobs_iter(self, jobs, *, batch_size=256, parallel=None):
        for j in jobs:
            yield j, self._vec(self._text(j))

    def embed_jobs(self, jobs, *, batch_size=256, parallel=None):
        return [self._vec(self._text(j)) for j in jobs]

    def embed_query(self, q: str) -> list[float]:
        return self._vec(q)


FAKE = FakeReranker()


def _job(sid, title, desc=""):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company="Co",
        title=title,
        description_text=desc,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
    )


def _build_rich(tmp_path, jobs, name="rich.sqlite"):
    p = tmp_path / name
    build_rich_tier(jobs, p, build_id="b1", reranker=FAKE)
    return p


def test_quantize_roundtrip_preserves_direction():
    from ergon_tracker.semantic import _cosine

    vec = FAKE._vec("python kubernetes senior engineer")
    scale, blob = quantize_int8(vec)
    back = dequantize_int8(scale, blob)
    assert len(back) == _DIM and _cosine(vec, back) > 0.999  # int8 holds cosine fidelity


def test_fulltext_matches_beyond_snippet(tmp_path):
    far = "x " * 400 + "supercalifragilistic_keyword"  # keyword sits well past char 300
    j1, j2 = _job("1", "Engineer", desc=far), _job("2", "Analyst", desc="ordinary")
    con = open_rich(_build_rich(tmp_path, [j1, j2]))
    assert fulltext_search(con, "supercalifragilistic_keyword", limit=10) == [
        j1.id
    ]  # via FULL desc
    assert len(far) > 300  # sanity: the 300-char snippet would NOT contain it


def test_vector_search_ranks_and_restricts(tmp_path):
    py = _job("py", "Python Engineer", "python kubernetes")
    sa, nu = _job("sa", "Sales Rep", "sales quota"), _job("nu", "Nurse", "nurse clinical")
    con = open_rich(_build_rich(tmp_path, [py, sa, nu]))
    q = FAKE.embed_query("python kubernetes backend")
    assert vector_search(con, q, limit=3)[0][0] == py.id  # most similar
    assert VectorIndex(con).search(q, limit=3)[0][0] == py.id  # preloaded agrees with one-shot
    restricted = vector_search(con, q, limit=3, candidate_ids=[sa.id, nu.id])
    assert {i for i, _ in restricted} == {sa.id, nu.id}  # py excluded by the candidate filter


def test_reconcile_cascades_with_main_index(tmp_path):
    a, b, c0 = (
        _job("a", "A role", "alpha"),
        _job("b", "B role", "beta"),
        _job("c", "C role", "orig gamma"),
    )
    rich = _build_rich(tmp_path, [a, b, c0])  # rich starts with A, B, C(original)

    c1 = _job("c", "C role", "REWRITTEN gamma description")  # same id, new content_hash
    d = _job("d", "D role", "delta")
    assert c1.id == c0.id  # same id; description changed in place -> the cascade must re-embed it
    main = tmp_path / "main.sqlite"
    build_index([a, c1, d], main, build_id="b2")  # main: A kept, B dropped, C changed, D added

    stats = reconcile_rich_tier(
        rich, main, [c1, d], build_id="b2", reranker=FAKE
    )  # fresh = crawled
    assert stats == {"pruned": 1, "embedded": 2, "missing": 0}  # B pruned; C+D (re)embedded

    con = open_rich(rich)
    assert {r[0] for r in con.execute("SELECT id FROM job_text")} == {
        a.id,
        c1.id,
        d.id,
    }  # B gone, D in
    assert (
        con.execute("SELECT count(*) FROM job_vectors").fetchone()[0] == 3
    )  # vectors cascaded too
    desc = con.execute("SELECT description FROM job_text WHERE id=?", (c0.id,)).fetchone()[0]
    assert "REWRITTEN" in desc  # C re-embedded with new content
    assert fulltext_search(con, "REWRITTEN", limit=5) == [c0.id]  # FTS re-synced


def test_reconcile_first_run_builds(tmp_path):
    main = tmp_path / "main.sqlite"
    build_index([_job("a", "A", "alpha")], main, build_id="b1")
    rich = tmp_path / "rich.sqlite"  # does not exist yet
    stats = reconcile_rich_tier(rich, main, [_job("a", "A", "alpha")], build_id="b1", reranker=FAKE)
    assert stats["embedded"] == 1 and rich.exists()


def test_stress_build_and_search(tmp_path):
    n = 3000
    jobs = [
        _job(str(i), f"Engineer {i}", ("python " if i % 3 == 0 else "sales ") * 60)
        for i in range(n)
    ]
    id_to_i = {j.id: i for i, j in enumerate(jobs)}
    t0 = time.perf_counter()
    built = build_rich_tier(jobs, tmp_path / "big.sqlite", build_id="b1", reranker=FAKE)
    build_s = time.perf_counter() - t0
    assert built == n
    con = open_rich(tmp_path / "big.sqlite")
    assert rich_meta(con)["dim"] == str(_DIM)
    vi = VectorIndex(con)  # preloaded matrix → repeated search is a bare matmul
    t0 = time.perf_counter()
    res = vi.search(FAKE.embed_query("python kubernetes"), limit=10)
    search_s = time.perf_counter() - t0
    assert len(res) == 10 and all(i in id_to_i for i, _ in res)
    assert any(id_to_i[i] % 3 == 0 for i, _ in res[:3])  # python-heavy jobs top the ranking
    print(
        f"\n[stress] build {n}: {build_s:.2f}s | preloaded vector search: {search_s * 1000:.1f}ms"
    )
    assert build_s < 30 and search_s < 1.0
