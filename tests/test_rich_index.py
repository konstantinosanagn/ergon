"""Rich index tier: vectors-only sidecar (pre-stored int8 embeddings) + the cascade reconcile + stress.

A fake 384-dim embedder keeps tests fast/deterministic (no ONNX model). The first dims encode a small
vocab so vector ranking is checkable; the rest are deterministic padding so dim/quantization/matmul are
exercised at realistic width."""

from __future__ import annotations

import hashlib
import time

import pytest

from ergon_tracker.index.build import build_index
from ergon_tracker.index.rich import (
    VectorIndex,
    _sig,
    build_rich_tier,
    dequantize_int8,
    migrate_legacy_rich,
    open_rich,
    quantize_int8,
    reconcile_rich_tier,
    reconcile_rich_tier_from_fresh,
    rich_meta,
    vector_search,
    write_fresh_rich,
)
from ergon_tracker.models import JobPosting, Location, RemoteType

_VOCAB = ["python", "kubernetes", "sales", "nurse", "finance"]
_DIM = 384


class FakeReranker:
    """Deterministic 384-dim embedder: vocab-count dims (dominant) + hashed padding (realistic width).

    Records every ``parallel`` value it is called with (``seen_parallel``) so tests can assert the
    reconcile path stays single-process (``parallel=None`` — never fastembed worker processes)."""

    model_name = "fake-384"

    def __init__(self) -> None:
        self.seen_parallel: list[int | None] = []

    def _vec(self, text: str) -> list[float]:
        t = (text or "").lower()
        base = [t.count(w) * 5.0 + 0.01 for w in _VOCAB]
        h = hashlib.sha1((text or "").encode()).digest()
        pad = [(h[i % len(h)] / 255.0) * 0.1 for i in range(_DIM - len(base))]
        return base + pad

    def _text(self, j: JobPosting) -> str:
        return f"{j.title or ''} {j.description_text or ''}"

    def embed_jobs_iter(self, jobs, *, batch_size=256, parallel=None):
        self.seen_parallel.append(parallel)
        for j in jobs:
            yield j, self._vec(self._text(j))

    def embed_jobs(self, jobs, *, batch_size=256, parallel=None):
        return [self._vec(self._text(j)) for j in jobs]

    def embed_texts_iter(self, texts, *, batch_size=256, parallel=None):
        self.seen_parallel.append(parallel)
        for t in texts:
            yield self._vec(t)

    def embed_query(self, q: str) -> list[float]:
        return self._vec(q)


FAKE = FakeReranker()


def _job(sid, title, desc="", company="Co"):
    # `company` matters for bulk synthetic sets: the main index fuzzy-dedups near-identical titles
    # within one company, so mass-generated "Engineer {i}" jobs must spread across companies to
    # all stay live.
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company=company,
        title=title,
        description_text=desc,
        locations=[Location(raw="Remote", is_remote=True)],
        remote=RemoteType.REMOTE,
    )


def _build_rich(tmp_path, jobs, name="rich.sqlite"):
    p = tmp_path / name
    build_rich_tier(jobs, p, build_id="b1", reranker=FAKE)
    return p


def _main_and_fresh(tmp_path, jobs, tag):
    """Build a main index + a matching ``fresh_rich`` capture DB from the same job list, tagged so
    repeated calls within one test don't collide on filenames. Returns ``(main_path, fresh_path)``."""
    import sqlite3

    main = tmp_path / f"main{tag}.sqlite"
    build_index(jobs, main, build_id=f"b{tag}")
    fresh = tmp_path / f"fresh{tag}.sqlite"
    con = sqlite3.connect(fresh)
    write_fresh_rich(con, jobs)
    con.commit()
    con.close()
    return main, fresh


def test_quantize_roundtrip_preserves_direction():
    from ergon_tracker.semantic import _cosine

    vec = FAKE._vec("python kubernetes senior engineer")
    scale, blob = quantize_int8(vec)
    back = dequantize_int8(scale, blob)
    assert len(back) == _DIM and _cosine(vec, back) > 0.999  # int8 holds cosine fidelity


def test_schema_is_vectors_only(tmp_path):
    py = _job("py", "Python Engineer", "python kubernetes")
    con = open_rich(_build_rich(tmp_path, [py]))
    names = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")
    }
    assert "job_vectors" in names and "meta" in names
    assert "job_text" not in names and "job_text_fts" not in names  # FTS gone => no O(rows) rebuild
    row = con.execute("SELECT id, sig, scale, vec FROM job_vectors").fetchone()
    assert row[0] == py.id and row[1]  # sig now lives on the vectors row
    assert len(row[3]) == 384  # int8 blob


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
    assert {r[0] for r in con.execute("SELECT id FROM job_vectors")} == {
        a.id,
        c1.id,
        d.id,
    }  # B gone, D in
    assert (
        con.execute("SELECT count(*) FROM job_vectors").fetchone()[0] == 3
    )  # vectors cascaded too
    sig = con.execute("SELECT sig FROM job_vectors WHERE id=?", (c0.id,)).fetchone()[0]
    assert sig == _sig(c1)  # C re-embedded with new content (sig reflects REWRITTEN description)


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


# --- real-model path (gated on the `semantic` extra, like test_semantic; skips without it) ---------
try:
    import fastembed  # noqa: F401

    _HAS_FASTEMBED = True
except ImportError:
    _HAS_FASTEMBED = False


@pytest.mark.skipif(not _HAS_FASTEMBED, reason="real-model path needs the `semantic` extra")
def test_real_embedding_end_to_end(tmp_path):
    """The REAL bge-small model: build + vector-rank + quantize fidelity on real text (no fake)."""
    from ergon_tracker.semantic import _cosine, get_semantic_reranker

    jobs = [
        _job(
            "ml",
            "Machine Learning Engineer",
            "design and train deep learning models, pytorch, GPUs",
        ),
        _job("ar", "AI Researcher", "publish research on large language models and transformers"),
        _job("ac", "Staff Accountant", "reconcile ledgers, prepare tax filings, audit invoices"),
        _job(
            "nu",
            "Registered Nurse",
            "patient care, clinical assessments, medication administration",
        ),
    ]
    r = get_semantic_reranker()  # real ONNX model
    build_rich_tier(jobs, tmp_path / "real.sqlite", build_id="r", reranker=r)
    con = open_rich(tmp_path / "real.sqlite")
    assert rich_meta(con)["dim"] == "384"  # real model dimensionality

    top = vector_search(con, r.embed_query("deep learning / neural network engineer"), limit=4)
    by_id = {j.id: j for j in jobs}
    assert by_id[top[0][0]].title in {"Machine Learning Engineer", "AI Researcher"}  # ML/AI on top
    assert by_id[top[-1][0]].title in {
        "Staff Accountant",
        "Registered Nurse",
    }  # unrelated at bottom

    real_vec = r.embed_jobs(jobs[:1])[0]
    scale, blob = quantize_int8(real_vec)
    assert len(blob) == 384  # int8 → 1 byte/dim
    assert (
        _cosine(real_vec, dequantize_int8(scale, blob)) > 0.999
    )  # quant fidelity on a REAL embedding


# --- incremental cron path: capture fresh full-text on disk, reconcile from it (carry-forward) -----
def test_reconcile_from_fresh_cold_then_carryforward(tmp_path):
    import sqlite3

    from ergon_tracker.index.rich import (
        reconcile_rich_tier_from_fresh,
        vector_search,
        write_fresh_rich,
    )

    a = _job("a", "Python Engineer", "python kubernetes")
    b = _job("b", "Sales Rep", "sales quota")
    c0 = _job("c", "Nurse", "nurse clinical")

    # round 1 (cold start): the crawl window had A,B,C -> fresh_rich; main index also A,B,C
    fresh1 = tmp_path / "fresh1.sqlite"
    con = sqlite3.connect(fresh1)
    write_fresh_rich(con, [a, b, c0])
    con.commit()
    con.close()
    main1 = tmp_path / "main1.sqlite"
    build_index([a, b, c0], main1, build_id="b1")
    rich = tmp_path / "rich.sqlite"
    s1 = reconcile_rich_tier_from_fresh(rich, main1, fresh1, build_id="b1", reranker=FAKE)
    assert s1 == {"pruned": 0, "embedded": 3, "missing": 0}
    con = open_rich(rich)
    assert {r[0] for r in con.execute("SELECT id FROM job_vectors")} == {a.id, b.id, c0.id}

    # round 2: B dropped from main, C's description changed, D added. The crawl window this run only
    # touched C and D (A and B were NOT re-crawled) -> fresh_rich has ONLY C',D.
    c1 = _job("c", "Nurse", "nurse clinical REWRITTEN")
    d = _job("d", "Data Engineer", "python spark")
    fresh2 = tmp_path / "fresh2.sqlite"
    con = sqlite3.connect(fresh2)
    write_fresh_rich(con, [c1, d])
    con.commit()
    con.close()
    main2 = tmp_path / "main2.sqlite"
    build_index([a, c1, d], main2, build_id="b2")  # A carried forward, B gone
    s2 = reconcile_rich_tier_from_fresh(rich, main2, fresh2, build_id="b2", reranker=FAKE)
    assert s2 == {"pruned": 1, "embedded": 2, "missing": 0}  # B pruned; C'+D embedded

    con = open_rich(rich)
    ids = {r[0] for r in con.execute("SELECT id FROM job_vectors")}
    assert ids == {a.id, c1.id, d.id}  # B pruned, D added, A CARRIED FORWARD (not re-crawled)
    assert con.execute("SELECT count(*) FROM job_vectors").fetchone()[0] == 3
    sig = con.execute("SELECT sig FROM job_vectors WHERE id=?", (c0.id,)).fetchone()[0]
    assert sig == _sig(c1)  # C re-embedded with the new description (sig moved onto job_vectors)
    # A's vector survived the carry-forward (never re-embedded) — still searchable
    top = vector_search(con, FAKE.embed_query("python kubernetes"), limit=3)
    assert a.id in {i for i, _ in top}


def test_reconcile_from_fresh_carries_sig_and_skips_unchanged(tmp_path):
    a = _job("a", "Python Engineer", "python kubernetes")
    main1, fresh1 = _main_and_fresh(tmp_path, [a], "1")
    rich = tmp_path / "rich.sqlite"
    s1 = reconcile_rich_tier_from_fresh(rich, main1, fresh1, build_id="b1", reranker=FAKE)
    assert s1 == {"pruned": 0, "embedded": 1, "missing": 0}
    # same content again -> sig matches -> nothing re-embedded
    main2, fresh2 = _main_and_fresh(tmp_path, [a], "2")
    s2 = reconcile_rich_tier_from_fresh(rich, main2, fresh2, build_id="b2", reranker=FAKE)
    assert s2 == {"pruned": 0, "embedded": 0, "missing": 0}


# --- memory-safety rework: chunked streaming reconcile + bounded ramp + single-process ------------
def _write_fresh(path, jobs):
    import sqlite3

    from ergon_tracker.index.rich import write_fresh_rich

    con = sqlite3.connect(path)
    write_fresh_rich(con, jobs)
    con.commit()
    con.close()


def _dump_rich(path):
    """Full comparable state: vector rows (id, sig, exact vec bytes) and meta dim/model."""
    con = open_rich(path)
    vecs = sorted(
        (r[0], r[1], r[2], bytes(r[3]))
        for r in con.execute("SELECT id, sig, scale, vec FROM job_vectors")
    )
    meta = {k: v for k, v in rich_meta(con).items() if k in ("dim", "model", "quant")}
    con.close()
    return vecs, meta


def test_reconcile_from_fresh_chunk_boundaries_match_single_fetch(tmp_path):
    """chunk_size=3 over a 10-row fresh set (carry-forward + unchanged-recrawl + changed + orphaned
    + new, deliberately straddling chunk boundaries) must produce stats AND final DB state identical
    to one big fetch (chunk_size=10_000)."""
    import shutil

    from ergon_tracker.index.rich import reconcile_rich_tier_from_fresh

    seed = [_job(f"s{i}", f"Role {i}", f"orig desc {i} python") for i in range(10)]
    fresh0 = tmp_path / "fresh0.sqlite"
    _write_fresh(fresh0, seed)
    main0 = tmp_path / "main0.sqlite"
    build_index(seed, main0, build_id="b0")
    rich_a = tmp_path / "rich_a.sqlite"
    s0 = reconcile_rich_tier_from_fresh(rich_a, main0, fresh0, build_id="b0", reranker=FAKE)
    assert s0 == {"pruned": 0, "embedded": 10, "missing": 0}

    # round 2: s0,s1 orphaned; s2..s5 changed; s6,s7 carried forward (not re-crawled);
    # s8,s9 re-crawled UNCHANGED (same sig -> must skip); n0..n3 brand new.
    changed = [_job(f"s{i}", f"Role {i}", f"REWRITTEN desc {i} kubernetes") for i in range(2, 6)]
    unchanged = [_job(f"s{i}", f"Role {i}", f"orig desc {i} python") for i in (8, 9)]
    new = [
        _job(f"n{i}", f"New Role {i}", f"new desc {i} sales", company=f"NewCo{i}") for i in range(4)
    ]  # distinct companies: same-company "New Role {i}" titles fuzzy-dedup away in the main index
    live = [seed[6], seed[7], *changed, *unchanged, *new]
    fresh1 = tmp_path / "fresh1.sqlite"
    _write_fresh(fresh1, [*changed, *unchanged, *new])  # 10 rows -> chunks of 3,3,3,1
    main1 = tmp_path / "main1.sqlite"
    build_index(live, main1, build_id="b1")

    rich_b = tmp_path / "rich_b.sqlite"
    shutil.copy(rich_a, rich_b)  # identical starting sidecars
    sa = reconcile_rich_tier_from_fresh(
        rich_a, main1, fresh1, build_id="b1", reranker=FAKE, chunk_size=3
    )
    sb = reconcile_rich_tier_from_fresh(
        rich_b, main1, fresh1, build_id="b1", reranker=FAKE, chunk_size=10_000
    )
    assert (
        sa == sb == {"pruned": 2, "embedded": 8, "missing": 0}
    )  # 4 changed + 4 new; unchanged skip
    assert _dump_rich(rich_a) == _dump_rich(rich_b)  # byte-identical vectors+sig+meta


def test_reconcile_from_fresh_ramp_cap_converges(tmp_path, capsys):
    """Changed-set > cap: run1 embeds exactly the cap (missing = the deferred rest — including
    stale-changed rows whose old text stays), run2 picks up the remainder via sig comparison, and
    the converged state equals a single uncapped run."""
    import shutil

    from ergon_tracker.index.rich import reconcile_rich_tier_from_fresh

    x0_old = _job("x0", "X0", "old zero")
    x1_old = _job("x1", "X1", "old one")
    fresh0 = tmp_path / "fresh0.sqlite"
    _write_fresh(fresh0, [x0_old, x1_old])
    main0 = tmp_path / "main0.sqlite"
    build_index([x0_old, x1_old], main0, build_id="b0")
    rich = tmp_path / "rich.sqlite"
    reconcile_rich_tier_from_fresh(rich, main0, fresh0, build_id="b0", reranker=FAKE)

    # 6 new/changed rows, cap 3. New rows first in fresh_rich so the CHANGED x0',x1' get deferred
    # -> their stale rows must count into `missing`, not pass as represented.
    x0_new, x1_new = _job("x0", "X0", "NEW zero"), _job("x1", "X1", "NEW one")
    new = [_job(f"y{i}", f"Y{i}", f"fresh {i}") for i in range(4)]
    live = [*new, x0_new, x1_new]
    fresh1 = tmp_path / "fresh1.sqlite"
    _write_fresh(fresh1, [*new, x0_new, x1_new])
    main1 = tmp_path / "main1.sqlite"
    build_index(live, main1, build_id="b1")

    rich_ref = tmp_path / "rich_ref.sqlite"
    shutil.copy(rich, rich_ref)

    s1 = reconcile_rich_tier_from_fresh(
        rich, main1, fresh1, build_id="b1", reranker=FAKE, chunk_size=2, max_embed_per_run=3
    )
    assert s1 == {"pruned": 0, "embedded": 3, "missing": 3}  # y3 + stale x0,x1 deferred
    assert "rich ramp: embedded 3, deferred 3 to next run" in capsys.readouterr().out
    con = open_rich(rich)
    sig = con.execute("SELECT sig FROM job_vectors WHERE id=?", (x0_old.id,)).fetchone()[0]
    assert sig == _sig(x0_old)  # deferred stale row keeps its old sig until its ramp turn

    s2 = reconcile_rich_tier_from_fresh(
        rich, main1, fresh1, build_id="b2", reranker=FAKE, chunk_size=2, max_embed_per_run=3
    )
    assert s2 == {"pruned": 0, "embedded": 3, "missing": 0}  # remainder picked up via sig diff

    s_ref = reconcile_rich_tier_from_fresh(
        rich_ref, main1, fresh1, build_id="b1", reranker=FAKE, max_embed_per_run=None
    )
    assert s_ref == {"pruned": 0, "embedded": 6, "missing": 0}
    assert _dump_rich(rich) == _dump_rich(rich_ref)  # capped ramp converges to the uncapped state


def test_reconcile_from_fresh_always_single_process(tmp_path, monkeypatch):
    """The reconcile path must NEVER spawn fastembed worker processes: even on CI with a batch large
    enough that _auto_parallel would fan out (>= _PARALLEL_MIN), every embed call gets parallel=None."""
    from ergon_tracker.index.rich import _PARALLEL_MIN, reconcile_rich_tier_from_fresh

    monkeypatch.setenv("CI", "true")  # the env where _auto_parallel would return 0 (all cores)
    n = _PARALLEL_MIN + 100
    jobs = [_job(str(i), f"Engineer {i}", f"python {i}", company=f"Co{i}") for i in range(n)]
    fresh = tmp_path / "fresh.sqlite"
    _write_fresh(fresh, jobs)
    main = tmp_path / "main.sqlite"
    build_index(jobs, main, build_id="b1")
    rec = FakeReranker()
    stats = reconcile_rich_tier_from_fresh(
        tmp_path / "rich.sqlite", main, fresh, build_id="b1", reranker=rec, max_embed_per_run=None
    )
    assert stats["embedded"] == n
    assert len(rec.seen_parallel) >= 1
    assert all(p is None for p in rec.seen_parallel)  # single_process=True hardwires parallel=None


def test_ramp_cap_env_and_ci_defaults(tmp_path, monkeypatch):
    """max_embed_per_run='auto' resolves ERGON_RICH_MAX_EMBED, else 120k on CI, else unlimited."""
    from ergon_tracker.index.rich import (
        _RAMP_DEFAULT_CI,
        _resolve_max_embed,
        reconcile_rich_tier_from_fresh,
    )

    monkeypatch.delenv("ERGON_RICH_MAX_EMBED", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert _resolve_max_embed() is None  # local default: unlimited
    monkeypatch.setenv("CI", "true")
    assert _resolve_max_embed() == _RAMP_DEFAULT_CI  # CI default: bounded cold start
    monkeypatch.setenv("ERGON_RICH_MAX_EMBED", "2")
    assert _resolve_max_embed() == 2  # env wins over the CI default

    jobs = [_job(str(i), f"R{i}", f"d{i}") for i in range(3)]
    fresh = tmp_path / "fresh.sqlite"
    _write_fresh(fresh, jobs)
    main = tmp_path / "main.sqlite"
    build_index(jobs, main, build_id="b1")
    stats = reconcile_rich_tier_from_fresh(
        tmp_path / "rich.sqlite", main, fresh, build_id="b1", reranker=FAKE
    )  # default 'auto' -> env cap of 2 applies end-to-end
    assert stats == {"pruned": 0, "embedded": 2, "missing": 1}


def test_reconcile_from_fresh_handles_missing_capture(tmp_path):
    # fresh DB without a fresh_rich table (capture was off) -> prune-only, no crash
    import sqlite3

    from ergon_tracker.index.rich import reconcile_rich_tier_from_fresh

    a = _job("a", "Eng", "x")
    fresh = tmp_path / "fresh.sqlite"
    sqlite3.connect(fresh).close()  # empty: no fresh_rich
    main = tmp_path / "main.sqlite"
    build_index([a], main, build_id="b1")
    stats = reconcile_rich_tier_from_fresh(
        tmp_path / "rich.sqlite", main, fresh, build_id="b1", reranker=FAKE
    )
    assert (
        stats["embedded"] == 0 and stats["missing"] == 1
    )  # a is live but uncaptured -> ramps in later


# --- legacy-schema migration (Task 2): existing pre-vectors-only sidecars upgrade in place ----------
def test_migrate_legacy_preserves_vectors_and_is_idempotent(tmp_path):
    import sqlite3

    p = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE job_text (rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, sig TEXT, description TEXT);"
        "CREATE VIRTUAL TABLE job_text_fts USING fts5(description, content='job_text', content_rowid='rowid');"
        "CREATE TABLE job_vectors (id TEXT PRIMARY KEY, scale REAL NOT NULL, vec BLOB NOT NULL);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    con.execute("INSERT INTO job_text(id, sig, description) VALUES('x','sig-x','hello')")
    con.execute("INSERT INTO job_vectors(id, scale, vec) VALUES('x', 0.5, ?)", (b"\x01" * 384,))
    con.commit()

    moved = migrate_legacy_rich(con)
    assert moved == 1
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_text" not in names and "job_text_fts" not in names
    row = con.execute("SELECT id, sig, scale, vec FROM job_vectors").fetchone()
    assert row == ("x", "sig-x", 0.5, b"\x01" * 384)  # vector + sig preserved, nothing recomputed
    assert migrate_legacy_rich(con) == 0  # idempotent


def test_reconcile_from_fresh_migrates_legacy_sidecar_preserves_vectors(tmp_path):
    """Critical regression: reconcile_rich_tier_from_fresh must not crash on a legacy (pre-Task-1)
    sidecar — sig-less job_vectors alongside job_text + job_text_fts — and must carry the
    already-computed vector forward byte-for-byte (never re-embedded) while dropping the legacy
    tables. This is the end-to-end path a real CI runner hits on its first run after the schema
    change, not just migrate_legacy_rich() in isolation."""
    import sqlite3

    a = _job("a", "Python Engineer", "python kubernetes")
    main = tmp_path / "main.sqlite"
    build_index([a], main, build_id="b1")

    # hand-build a LEGACY sidecar: old 3-column job_vectors (no sig), job_text, job_text_fts
    rich = tmp_path / "legacy_rich.sqlite"
    con = sqlite3.connect(rich)
    con.executescript(
        "CREATE TABLE job_text (rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, sig TEXT, description TEXT);"
        "CREATE VIRTUAL TABLE job_text_fts USING fts5(description, content='job_text', content_rowid='rowid');"
        "CREATE TABLE job_vectors (id TEXT PRIMARY KEY, scale REAL NOT NULL, vec BLOB NOT NULL);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    legacy_sig = _sig(a)
    legacy_vec = b"\x02" * 384
    con.execute(
        "INSERT INTO job_text(id, sig, description) VALUES(?, ?, ?)",
        (a.id, legacy_sig, a.description_text),
    )
    con.execute("INSERT INTO job_vectors(id, scale, vec) VALUES(?, ?, ?)", (a.id, 0.5, legacy_vec))
    con.commit()
    con.close()

    # fresh capture this run: same job, unchanged sig -> nothing should be re-embedded
    fresh = tmp_path / "fresh.sqlite"
    con = sqlite3.connect(fresh)
    write_fresh_rich(con, [a])
    con.commit()
    con.close()

    stats = reconcile_rich_tier_from_fresh(rich, main, fresh, build_id="b2", reranker=FAKE)
    assert stats == {"pruned": 0, "embedded": 0, "missing": 0}  # carried forward, not re-embedded

    con = open_rich(rich)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_text" not in names and "job_text_fts" not in names  # legacy tables dropped
    row = con.execute("SELECT id, sig, scale, vec FROM job_vectors").fetchone()
    assert tuple(row) == (a.id, legacy_sig, 0.5, legacy_vec)  # vector preserved byte-for-byte


def test_backfill_from_index_embeds_unvectored_backlog(tmp_path):
    """Coverage accelerator: rows live in the main index but NOT in this run's crawl window
    (fresh_rich) get embedded directly from the index, so the tail reaches 100% without waiting for
    board rotation. Also covers idempotency + budget + the sig self-upgrade on a later real crawl."""
    import sqlite3

    from ergon_tracker.index.rich import open_rich, reconcile_rich_tier_from_fresh, write_fresh_rich

    jobs = [
        _job("a", "Alpha Engineer", "alpha description here", company="C1"),
        _job("b", "Beta Engineer", "beta " + "x" * 500, company="C2"),  # long: snippet<full desc
        _job("c", "Gamma Engineer", "gamma description here", company="C3"),
    ]
    ida, idb, idc = jobs[0].id, jobs[1].id, jobs[2].id
    main = tmp_path / "main.sqlite"
    build_index(jobs, main, build_id="b1")
    # crawl window this run = only job a
    fresh = tmp_path / "fresh.sqlite"
    con = sqlite3.connect(fresh)
    write_fresh_rich(con, [jobs[0]])
    con.commit()
    con.close()
    rich = tmp_path / "rich.sqlite"

    # WITHOUT backfill: only the crawl-window row embeds; b, c stay missing.
    s = reconcile_rich_tier_from_fresh(
        rich, main, fresh, build_id="b1", reranker=FAKE, backfill_from_index=False
    )
    assert s["embedded"] == 1 and s["missing"] == 2

    # WITH backfill: b, c embed straight from the index -> full coverage.
    s2 = reconcile_rich_tier_from_fresh(
        rich, main, fresh, build_id="b2", reranker=FAKE, backfill_from_index=True
    )
    assert s2["embedded"] == 2 and s2["missing"] == 0  # a already vectored, b+c backfilled
    rc = open_rich(rich)
    got = dict(rc.execute("SELECT id, sig FROM job_vectors"))
    rc.close()
    assert set(got) == {ida, idb, idc}
    bsig_backfill = got[idb]  # sha1(content_hash | snippet)

    # IDEMPOTENT: re-run backfill -> nothing new (sigs already match).
    s3 = reconcile_rich_tier_from_fresh(
        rich, main, fresh, build_id="b3", reranker=FAKE, backfill_from_index=True
    )
    assert s3["embedded"] == 0 and s3["missing"] == 0

    # SELF-UPGRADE: when b's board is later crawled (full description in fresh_rich), its full-desc
    # sig differs from the snippet-backfill sig -> it re-embeds (quality upgrade).
    fresh2 = tmp_path / "fresh2.sqlite"
    con = sqlite3.connect(fresh2)
    write_fresh_rich(con, [jobs[1]])  # b, now via the crawl path (full description_text)
    con.commit()
    con.close()
    s4 = reconcile_rich_tier_from_fresh(
        rich, main, fresh2, build_id="b4", reranker=FAKE, backfill_from_index=False
    )
    assert s4["embedded"] == 1  # b re-embedded (upgraded)
    rc = open_rich(rich)
    assert rc.execute("SELECT sig FROM job_vectors WHERE id=?", (idb,)).fetchone()[0] != bsig_backfill
    rc.close()


def test_backfill_respects_embed_budget(tmp_path):
    import sqlite3

    from ergon_tracker.index.rich import reconcile_rich_tier_from_fresh, write_fresh_rich

    jobs = [_job(str(i), f"Engineer {i}", f"desc {i}", company=f"C{i}") for i in range(6)]
    main = tmp_path / "m.sqlite"
    build_index(jobs, main, build_id="b1")
    fresh = tmp_path / "f.sqlite"  # empty crawl window -> all 6 are backlog
    con = sqlite3.connect(fresh)
    write_fresh_rich(con, [])
    con.commit()
    con.close()
    rich = tmp_path / "r.sqlite"
    s = reconcile_rich_tier_from_fresh(
        rich, main, fresh, build_id="b1", reranker=FAKE, backfill_from_index=True, max_embed_per_run=4
    )
    assert s["embedded"] == 4 and s["missing"] == 2  # budget capped; rest next run
