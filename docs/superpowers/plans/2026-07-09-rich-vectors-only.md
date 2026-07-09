# Rich Vectors-Only + Wire Into Serving — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a vectors-only rich sidecar (no FTS ⇒ no O(total-rows) growth ⇒ the ramp finishes), migrate the 360k already-computed embeddings, and wire it into the serving path so users actually get pre-stored-vector semantic ranking.

**Architecture:** `rich.py` drops `job_text`/`job_text_fts` and moves `sig` onto `job_vectors`; the reconcile auto-migrates a legacy sidecar on the runner. A new `RichCache` (mirroring `SlimCache`) downloads `index-vectors.sqlite.gz`. `router.try_index_ranked` gains a hybrid vector path: cosine-rank pool ids covered by pre-stored vectors, embed only the uncovered remainder, fall back to today's behaviour when the sidecar or the `semantic` extra is absent.

**Tech Stack:** Python ≥3.10, stdlib + sqlite3 + numpy (already arrives with the `semantic` extra). No new dependency.

## Global Constraints

- **HARD RULE — the laptop is never occupied.** All embedding, migration, and stress runs execute on GitHub runners. Local execution is limited to **synthetic unit tests with the existing `FAKE = FakeReranker()`** (`tests/test_rich_index.py:70`). No real embedding fleet, no `parallel=` workers, no model downloads. A local `fastembed parallel=N` benchmark crashed the developer's machine (per-worker ONNX model copies + a `<stdin>` respawn storm).
- **Embedding stays `single_process=True` on the reconcile path.** This is the CI-OOM fix (`a44dca3`) and is not revisited.
- **Deliberate headroom, never "it fits":** release asset ≤ ~609 MB (cap 2 GiB); run duration target ≤ 270 min (timeout 330); ≤ 4 concurrent jobs (allowance ~20); Actions cache untouched.
- **`--rich` stays manual-only** through the ramp. No cron bursts, no API polling. The existing `concurrency: build-index` group keeps runs serialized.
- **Non-fatal:** the core index `_gated_publish`es first; a rich failure never blocks it (`build_index.py:823-838`).
- **Serving must never instantiate `VectorIndex`** — it loads every vector as float32 (~2.26 GB at 1.47M). A test guards this.
- **Absence is a non-event:** no sidecar, or no `semantic` extra ⇒ exactly today's behaviour (query-time rerank, else BM25 lexical order).
- ruff line-length 100, no semicolon one-liners (E701/E702); mypy strict on `src/`.

## Key Facts (verified)

- **Schema today** (`rich.py:43-52`): `job_text(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, sig, description)`, `job_text_fts` **external-content FTS5** (`content='job_text', content_rowid='rowid'`) with **no sync triggers**, `job_vectors(id TEXT PRIMARY KEY, scale REAL, vec BLOB)`, `meta`, `idx_job_text_sig`.
- **Three FTS-rebuild call sites** — `build_rich_tier:108-110`, `reconcile_rich_tier:170`, `reconcile_rich_tier_from_fresh:418-420`. The rebuild rescans all of `job_text`: the growth term.
- `reconcile_rich_tier_from_fresh` (`rich.py:322-438`): `have = dict(con.execute("SELECT id, sig FROM job_text"))` (:370); per-chunk filter `chunk = [r for r in rows if r[0] in live_ids and have.get(r[0]) != r[1]]` (:389); ramp cap via `_resolve_max_embed()` (:307-319, `_RAMP_DEFAULT_CI = 120_000`); returns `{pruned, embedded, missing}` with `final_ids = ((set(have) - set(orphans)) - deferred_ids) | rebuilt_ids` (:431).
- `_delete_ids` (`rich.py:495-500`) deletes from **both** `job_vectors` and `job_text`. `_upsert_text` (`:186-190`) writes `job_text` only.
- `_embed_rows_into(con, rows: list[tuple[str,str]], *, reranker, batch, single_process)` (`rich.py:265-304`) writes `INSERT OR REPLACE INTO job_vectors(id, scale, vec)`; `quantize_int8` → 384-byte blob.
- `write_fresh_rich` captures `(id, sig, description, embed_text)`; `FRESH_RICH_SCHEMA` at `rich.py:244-247`.
- `vector_search(con, query_vec, *, limit=50, candidate_ids=None) -> list[tuple[str, float]]` (`rich.py:529-568`) — **cosine, sorted desc**, `WHERE id IN (...)` when `candidate_ids` given; **ids absent from `job_vectors` are silently dropped**; does NOT preserve candidate order. `open_rich(path)` (`:504-507`) opens read-only.
- **`SlimCache`** (`cache.py:262-311`) is the template: `__init__(base_url=None, cache_dir=None, repo=_REPO, tag=_TAG)` then `ensure_fresh() -> Path | None`; fetches `manifest-slim.json` (build_id + sha256 + schema_version), compares `build_id`, `gzip.decompress`, sha256-verifies, atomic `tmp.replace`. Constants: `_REPO`, `_TAG="index-latest"`, `_DEFAULT_BASE`, `_default_cache_dir() -> ~/.cache/ergon-tracker` (`:28-34`). Caches are constructed **per call** (`SlimCache().ensure_fresh()` in `router._load_slim`, `:39`).
- **`router.try_index_ranked`** (`router.py:82-104`): `want = query.limit or 20`; `pool = try_index(query.model_copy(update={"limit": max(want * 10, 200)})) or indexed`; `indexed = rank(pool, query.keywords, reranker=get_semantic_reranker())[:want]`.
- `rank(jobs, query, *, reranker)` (`ranking.py:131-140`) expects `Reranker.rerank(query, jobs) -> list[float]` (`:89-93`) and only reranks the lexical top-100. `SemanticReranker.rerank` returns **cosine** scores; `embed_query(q) -> list[float]` (`semantic.py:155-159`). So `vector_search`'s cosine and `rerank`'s cosine are on the **same scale** — mergeable.
- **Cache tests use no network** (`tests/test_index_cache.py:33-40`): publish gz + manifest into a `tmp_path` dir, pass `base_url=remote.as_uri()` (`file://`), `cache_dir=tmp_path/"cache"`.
- Tests inject `FAKE = FakeReranker()` explicitly as `reranker=` (`tests/test_rich_index.py:32-70,88-91`); `_DIM = 384`.

## File Structure

**Modify:** `src/ergon_tracker/index/rich.py` (schema, reconcile, migration; delete `job_text`/FTS/`fulltext_search`), `src/ergon_tracker/index/cache.py` (add `RichCache`), `src/ergon_tracker/index/router.py` (hybrid vector rerank), `scripts/build_index.py` (publish `index-vectors.sqlite.gz` + `manifest-vectors.json`), `.github/workflows/build-index.yml` (download/upload names), `tests/test_rich_index.py` (adapt).
**Create:** `tests/test_rich_cache.py`, `tests/test_router_vectors.py`.

---

## Task 1: Vectors-only schema — drop `job_text` + FTS

**Files:** Modify `src/ergon_tracker/index/rich.py`; Modify `tests/test_rich_index.py`

**Interfaces:**
- Produces: `RICH_SCHEMA` (vectors-only), `RICH_SCHEMA_VERSION: int = 3`, `_embed_rows_into(con, rows: list[tuple[str, str, str]], *, reranker, batch, single_process=False) -> tuple[int, str]` where each row is `(id, sig, embed_text)`, `_delete_ids(con, ids)` (vectors only). **Removes:** `job_text`, `job_text_fts`, `idx_job_text_sig`, `_upsert_text`, `fulltext_search`, and all three FTS-rebuild call sites.

- [ ] **Step 1: Write the failing tests (adapt existing invariants)**

In `tests/test_rich_index.py`: delete `test_fulltext_matches_beyond_snippet`, and remove the `fulltext_search` import and its assertions from `test_reconcile_cascades_with_main_index`, `test_reconcile_from_fresh_cold_then_carryforward`, and `test_reconcile_from_fresh_chunk_boundaries_match_single_fetch`. Add:

```python
def test_schema_is_vectors_only(tmp_path):
    py = _job("py", "Python Engineer", "python kubernetes")
    con = open_rich(_build_rich(tmp_path, [py]))
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    assert "job_vectors" in names and "meta" in names
    assert "job_text" not in names and "job_text_fts" not in names  # FTS gone => no O(rows) rebuild
    row = con.execute("SELECT id, sig, scale, vec FROM job_vectors").fetchone()
    assert row[0] == py.id and row[1]  # sig now lives on the vectors row
    assert len(row[3]) == 384          # int8 blob


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
```

(`_main_and_fresh` is the existing helper in the file that builds a main index + a `fresh_rich` DB; reuse it verbatim.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_rich_index.py -x -q`
Expected: FAIL — `job_text` still present; `SELECT ... sig ... FROM job_vectors` errors.

- [ ] **Step 3: Implement the vectors-only schema**

Replace `RICH_SCHEMA` (`rich.py:43-52`) with:

```python
RICH_SCHEMA_VERSION = 3  # 3 = vectors-only (job_text/FTS removed); bump to reject stale assets
RICH_SCHEMA = """
CREATE TABLE job_vectors (id TEXT PRIMARY KEY, sig TEXT, scale REAL NOT NULL, vec BLOB NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
```

Rewrite `_delete_ids` (`rich.py:495-500`) to touch only `job_vectors`:

```python
def _delete_ids(con: sqlite3.Connection, ids: list[str]) -> None:
    for i in range(0, len(ids), 500):  # chunk to stay under SQLite's variable limit
        chunk = ids[i : i + 500]
        ph = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM job_vectors WHERE id IN ({ph})", chunk)  # noqa: S608 - ph is placeholders
```

Rewrite `_embed_rows_into` to carry `sig` (rows are `(id, sig, embed_text)`):

```python
def _embed_rows_into(
    con: sqlite3.Connection,
    rows: list[tuple[str, str, str]],
    *,
    reranker: SemanticReranker | None,
    batch: int,
    single_process: bool = False,
) -> tuple[int, str]:
    """Embed ``(id, sig, embed_text)`` rows (streamed) → upsert int8 vectors. Returns (dim, model).

    ``single_process=True`` hardwires ``parallel=None`` — no fastembed worker processes, ever (each
    worker loads its own ~67MB ONNX model). The reconcile path always sets it."""
    if not rows:
        return 0, getattr(reranker, "model_name", "?")
    if reranker is None:
        from ..semantic import get_semantic_reranker

        reranker = get_semantic_reranker()
    par = None if single_process else _auto_parallel(len(rows))
    texts = [r[2] for r in rows]
    dim = 0
    buf: list[tuple[str, str, float, bytes]] = []
    for (jid, sig, _), vec in zip(
        rows, reranker.embed_texts_iter(texts, batch_size=batch, parallel=par), strict=True
    ):
        scale, blob = quantize_int8(vec)
        dim = dim or len(vec)
        buf.append((jid, sig, scale, blob))
        if len(buf) >= 1000:
            con.executemany(
                "INSERT OR REPLACE INTO job_vectors(id, sig, scale, vec) VALUES(?, ?, ?, ?)", buf
            )
            buf = []
    if buf:
        con.executemany(
            "INSERT OR REPLACE INTO job_vectors(id, sig, scale, vec) VALUES(?, ?, ?, ?)", buf
        )
    return dim, getattr(reranker, "model_name", "?")
```

Then, in each of the three builders:
- **`build_rich_tier`**: delete the `_upsert_text(con, jobs)` call and the FTS rebuild (`:107-110`); call `_embed_rows_into(con, [(j.id, _sig(j), _job_text(j)) for j in jobs], ...)`.
- **`reconcile_rich_tier`**: delete `_upsert_text(con, rebuild)` and the FTS rebuild (`:168-170`); change `have = dict(con.execute("SELECT id, sig FROM job_vectors"))`; embed via the new row shape.
- **`reconcile_rich_tier_from_fresh`**: change `have` to read `job_vectors` (`:370`); delete the inlined `INSERT OR REPLACE INTO job_text(...)` (`:401-404`); delete the FTS rebuild (`:418-420`); call `_embed_rows_into(con, [(r[0], r[1], r[3]) for r in chunk], ...)`.

Delete `_upsert_text` and `fulltext_search` entirely (no callers remain outside the deleted tests). Slim `FRESH_RICH_SCHEMA` + `write_fresh_rich` to `(id, sig, embed_text)` — `description` was only ever FTS input, and dropping it shrinks the fresh DB's I/O per run.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_rich_index.py -q`
Expected: PASS. The preserved invariants — `test_reconcile_from_fresh_chunk_boundaries_match_single_fetch` (chunked == single-fetch, byte-identical), `test_reconcile_from_fresh_cold_then_carryforward`, `test_reconcile_from_fresh_ramp_cap_converges`, `test_reconcile_from_fresh_always_single_process` — must all still pass.

- [ ] **Step 5: Lint, type, commit**

```bash
.venv/bin/ruff check src/ergon_tracker/index/rich.py tests/test_rich_index.py
.venv/bin/ruff format src/ergon_tracker/index/rich.py tests/test_rich_index.py
.venv/bin/mypy
git add src/ergon_tracker/index/rich.py tests/test_rich_index.py
git commit -m "feat(rich): vectors-only sidecar — drop job_text + FTS (removes the O(total-rows) rebuild)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Legacy-schema migration (runs on the runner)

**Files:** Modify `src/ergon_tracker/index/rich.py`; Modify `tests/test_rich_index.py`

**Interfaces:**
- Consumes: `RICH_SCHEMA`, `RICH_SCHEMA_VERSION` (Task 1).
- Produces: `migrate_legacy_rich(con: sqlite3.Connection) -> int` — if a legacy `job_text` table exists, copy `(id, sig, scale, vec)` into the new `job_vectors`, drop the legacy tables, return the number of rows carried. Returns `0` when nothing to migrate (idempotent). Called from `_ensure_schema(con)` used by all three builders.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_rich_index.py
def test_migrate_legacy_preserves_vectors_and_is_idempotent(tmp_path):
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_rich_index.py -k migrate_legacy -q`
Expected: FAIL — `migrate_legacy_rich` not defined.

- [ ] **Step 3: Implement the migration**

```python
def migrate_legacy_rich(con: sqlite3.Connection) -> int:
    """Upgrade a legacy sidecar (job_text + FTS + sig-less job_vectors) to the vectors-only schema.

    Carries every already-computed embedding forward — sigs come from ``job_text`` — so a ramp that
    cost hours of CI embedding is never recomputed. Idempotent: returns 0 when already migrated.
    Runs on the CI runner as part of the reconcile; never on a developer machine."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "job_text" not in tables:
        return 0
    if "sig" not in {r[1] for r in con.execute("PRAGMA table_info(job_vectors)")}:
        con.execute("ALTER TABLE job_vectors ADD COLUMN sig TEXT")
    moved = con.execute(
        "UPDATE job_vectors SET sig = (SELECT t.sig FROM job_text t WHERE t.id = job_vectors.id) "
        "WHERE sig IS NULL"
    ).rowcount
    con.execute("DROP TABLE IF EXISTS job_text_fts")
    con.execute("DROP TABLE IF EXISTS job_text")
    con.execute("DROP INDEX IF EXISTS idx_job_text_sig")
    con.commit()
    con.execute("VACUUM")  # reclaim the ~87% the text+FTS occupied
    return moved
```

Add a shared `_ensure_schema(con)` that all three builders call in place of their current `executescript(RICH_SCHEMA)` guard:

```python
def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the vectors-only schema if absent, else migrate a legacy sidecar in place."""
    if not _has_schema(con):
        con.executescript(RICH_SCHEMA)
        return
    migrate_legacy_rich(con)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_rich_index.py -q`
Expected: PASS (all, including the new migration test).

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check src/ergon_tracker/index/rich.py tests/test_rich_index.py
.venv/bin/ruff format src/ergon_tracker/index/rich.py tests/test_rich_index.py
.venv/bin/mypy
git add src/ergon_tracker/index/rich.py tests/test_rich_index.py
git commit -m "feat(rich): in-place legacy->vectors-only migration (preserves the 360k embeddings)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `RichCache`

**Files:** Modify `src/ergon_tracker/index/cache.py`; Create `tests/test_rich_cache.py`

**Interfaces:**
- Produces: `class RichCache` with `__init__(self, base_url=None, cache_dir=None, repo=_REPO, tag=_TAG)` and `ensure_fresh(self) -> Path | None`. Assets: `manifest-vectors.json` + `index-vectors.sqlite.gz`; local `index-vectors.sqlite` + `manifest-vectors.json`.

- [ ] **Step 1: Write the failing test (no network — `file://` remote, like `test_index_cache.py`)**

```python
# tests/test_rich_cache.py
from __future__ import annotations

import gzip
import hashlib
import json

from ergon_tracker.index.cache import RichCache
from ergon_tracker.index.rich import RICH_SCHEMA_VERSION, open_rich, vector_search
from tests.test_rich_index import FAKE, _build_rich, _job


def _publish_rich(remote, tmp_path):
    src = _build_rich(tmp_path, [_job("py", "Python Engineer", "python kubernetes")])
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


def test_rich_cache_downloads_verifies_and_opens(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_rich(remote, tmp_path)
    path = RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh()
    assert path is not None and path.exists()
    con = open_rich(path)
    assert vector_search(con, FAKE.embed_query("python kubernetes"), limit=1)  # usable


def test_rich_cache_absent_asset_returns_none(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()  # nothing published
    assert RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh() is None


def test_rich_cache_rejects_corrupt_download(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish_rich(remote, tmp_path)
    m = json.loads((remote / "manifest-vectors.json").read_text())
    m["sha256"] = "0" * 64
    (remote / "manifest-vectors.json").write_text(json.dumps(m))
    assert RichCache(base_url=remote.as_uri(), cache_dir=tmp_path / "cache").ensure_fresh() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_rich_cache.py -q`
Expected: FAIL — `RichCache` not defined.

- [ ] **Step 3: Implement `RichCache` (mirror `SlimCache`)**

```python
class RichCache:
    """Download the vectors sidecar (``index-vectors.sqlite.gz``): pre-stored int8 job embeddings.

    Same shape as SlimCache. Absence is a non-event — the caller falls back to query-time reranking.
    """

    def __init__(
        self,
        base_url: str | None = None,
        cache_dir: Path | None = None,
        repo: str = _REPO,
        tag: str = _TAG,
    ) -> None:
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.cache_dir = Path(cache_dir or _default_cache_dir())
        self.repo = repo
        self.tag = tag
        self.db_path = self.cache_dir / "index-vectors.sqlite"
        self.local_manifest = self.cache_dir / "manifest-vectors.json"

    def ensure_fresh(self) -> Path | None:
        """Return a verified vectors-sidecar path, or None (caller reranks at query time)."""
        from .rich import RICH_SCHEMA_VERSION

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            fetch = _asset_fetcher(self.base_url, self.repo, self.tag)
            remote = json.loads(fetch("manifest-vectors.json"))
        except Exception as exc:  # noqa: BLE001 - no vectors published -> query-time rerank
            log.debug("no vectors manifest (%s); query-time rerank", exc)
            return None
        if int(remote.get("schema_version", -1)) != RICH_SCHEMA_VERSION:
            return None
        local = json.loads(self.local_manifest.read_text()) if self.local_manifest.exists() else {}
        if local.get("build_id") == remote.get("build_id") and self.db_path.exists():
            return self.db_path  # already current
        try:
            raw = gzip.decompress(fetch("index-vectors.sqlite.gz"))
        except Exception as exc:  # noqa: BLE001
            log.warning("vectors download failed (%s)", exc)
            return self.db_path if self.db_path.exists() else None
        if hashlib.sha256(raw).hexdigest() != remote.get("sha256"):
            log.warning("vectors sha256 mismatch; rejecting download")
            return None
        tmp = self.db_path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        tmp.replace(self.db_path)  # atomic
        self.local_manifest.write_text(json.dumps(remote))
        log.info("vectors sidecar updated to build %s (%d bytes)", remote.get("build_id"), len(raw))
        return self.db_path
```

Also add `"manifest-vectors.json"` to the tuple `cached_index_build_id` iterates (`cache.py:44`).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_rich_cache.py tests/test_index_cache.py -q`
Expected: PASS (3 new + existing cache tests).

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check src/ergon_tracker/index/cache.py tests/test_rich_cache.py
.venv/bin/ruff format src/ergon_tracker/index/cache.py tests/test_rich_cache.py
.venv/bin/mypy
git add src/ergon_tracker/index/cache.py tests/test_rich_cache.py
git commit -m "feat(rich): RichCache for the vectors sidecar (mirrors SlimCache; absence is a non-event)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the router — hybrid vector rerank

**Files:** Modify `src/ergon_tracker/index/router.py`; Create `tests/test_router_vectors.py`

**Interfaces:**
- Consumes: `RichCache.ensure_fresh()` (Task 3), `open_rich`, `vector_search` (`rich.py`), `get_semantic_reranker`, `SemanticReranker.embed_query/rerank`.
- Produces: `_vector_rerank(query: SearchQuery, pool: list[JobPosting], want: int) -> list[JobPosting] | None` — returns ranked jobs, or `None` when the sidecar is unavailable (caller falls back).

**Why hybrid:** `vector_search` silently drops ids absent from `job_vectors`, and during the ramp the sidecar covers only part of the index. Both `vector_search` and `SemanticReranker.rerank` return **cosine** scores, so covered ids are scored from pre-stored vectors and the uncovered remainder is embedded at query time. Correct at any coverage; monotonically cheaper as the ramp fills. `VectorIndex` is never used (it would load ~2.26 GB as float32).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_router_vectors.py
from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from ergon_tracker.index import router
from ergon_tracker.index.rich import RICH_SCHEMA_VERSION
from ergon_tracker.models import SearchQuery
from tests.test_rich_index import FAKE, _build_rich, _job


def _publish(remote, tmp_path, jobs):
    src = _build_rich(tmp_path, jobs)
    raw = src.read_bytes()
    (remote / "index-vectors.sqlite.gz").write_bytes(gzip.compress(raw))
    (remote / "manifest-vectors.json").write_text(
        json.dumps({"build_id": "b1", "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw), "schema_version": RICH_SCHEMA_VERSION})
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
        raise AssertionError("VectorIndex must never be built in the serving path (~2.26GB float32)")

    monkeypatch.setattr(rich, "VectorIndex", boom)
    monkeypatch.setattr(router, "_rich_path", lambda: None)
    q = SearchQuery(keywords="python", semantic=True, limit=2)
    router._vector_rerank(q, [_job("a", "A", "python")], want=2)  # must not touch VectorIndex
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_router_vectors.py -q`
Expected: FAIL — `_vector_rerank` / `_rich_path` not defined.

- [ ] **Step 3: Implement the router path**

Add to `router.py` (imports: add `RichCache` to the `.cache` import; add `from ..semantic import get_semantic_reranker` at module level so tests can monkeypatch it):

```python
def _rich_path():
    """Path to the cached vectors sidecar, or None (caller reranks at query time)."""
    return RichCache().ensure_fresh()


def _vector_rerank(query: SearchQuery, pool: list[JobPosting], want: int) -> list[JobPosting] | None:
    """Rank ``pool`` by cosine against PRE-STORED vectors; embed only the uncovered remainder.

    One query embedding instead of ~200 document embeddings. ``vector_search`` silently drops ids the
    sidecar doesn't cover, so during the ramp the uncovered remainder is scored with the query-time
    reranker — both return cosine, so the scores merge on one scale. Returns None when no sidecar is
    available. Never builds ``VectorIndex`` (it materializes every vector as float32)."""
    path = _rich_path()
    if path is None:
        return None
    from .rich import open_rich, vector_search

    r = get_semantic_reranker()
    qvec = r.embed_query(query.keywords or "")
    con = open_rich(path)
    try:
        scored = dict(
            vector_search(con, qvec, limit=len(pool), candidate_ids=[j.id for j in pool])
        )
    finally:
        con.close()
    uncovered = [j for j in pool if j.id not in scored]
    if uncovered:  # ramp not finished: score the remainder the old way (same cosine scale)
        for j, s in zip(uncovered, r.rerank(query.keywords or "", uncovered), strict=True):
            scored[j.id] = s
    for j in pool:
        j.score = scored.get(j.id, 0.0)
    return sorted(pool, key=lambda j: j.score, reverse=True)[:want]
```

Rewrite the rerank block of `try_index_ranked` (`router.py:93-104`) to try vectors first:

```python
    if query.semantic and query.keywords and len(indexed) > 1:
        # Rerank a WIDER candidate pool. Prefer PRE-STORED vectors (one query embedding); fall back
        # to query-time document embedding, then to the index's lexical order.
        try:
            from ..ranking import rank

            want = query.limit or 20
            pool = try_index(query.model_copy(update={"limit": max(want * 10, 200)})) or indexed
            ranked = _vector_rerank(query, pool, want)
            indexed = ranked if ranked is not None else rank(
                pool, query.keywords, reranker=get_semantic_reranker()
            )[:want]
        except Exception as exc:  # noqa: BLE001 - reranker optional; lexical order is fine
            log.warning("semantic rerank on index unavailable (%s); lexical order", exc)
    return indexed
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_router_vectors.py tests/test_rich_index.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check src/ergon_tracker/index/router.py tests/test_router_vectors.py
.venv/bin/ruff format src/ergon_tracker/index/router.py tests/test_router_vectors.py
.venv/bin/mypy
git add src/ergon_tracker/index/router.py tests/test_router_vectors.py
git commit -m "feat(rich): router ranks from pre-stored vectors (hybrid; one query embedding, clean fallback)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Publish plumbing — `index-vectors.sqlite.gz` + `manifest-vectors.json`

**Files:** Modify `scripts/build_index.py`, `.github/workflows/build-index.yml`

**Interfaces:**
- Produces: `build_and_publish_rich_incremental(...)` now writes `out/index-vectors.sqlite`, gzips to `index-vectors.sqlite.gz`, and writes `manifest-vectors.json` (`build_id`, `sha256` of the RAW bytes, `bytes`, `schema_version=RICH_SCHEMA_VERSION`).

- [ ] **Step 1: Update `build_index.py`**

In both `build_and_publish_rich` (`:305-319`) and `build_and_publish_rich_incremental` (`:322-334`), change `rich_db = out / "index-rich.sqlite"` → `out / "index-vectors.sqlite"`, gzip to `out / "index-vectors.sqlite.gz"`, and after gzipping write the manifest (mirroring the slim manifest):

```python
    from ergon_tracker.index.rich import RICH_SCHEMA_VERSION

    sha, nbytes = _gzip_file(rich_db, out / "index-vectors.sqlite.gz")
    (out / "manifest-vectors.json").write_text(
        json.dumps(
            {"build_id": build_id, "sha256": sha, "bytes": nbytes,
             "schema_version": RICH_SCHEMA_VERSION},
            indent=2,
        )
    )
```

(`_gzip_file` already returns `(sha256_of_raw_bytes, raw_byte_count)` — `build_index.py:31-66`.)

Update the two print lines (`:833`, `:884`) to say `index-vectors.sqlite.gz`.

- [ ] **Step 2: Update the workflow**

In `.github/workflows/build-index.yml`:
- Download step (`:67`) and decompress (`:71`): change the pattern/filename from `index-rich.sqlite.gz` to `index-vectors.sqlite.gz`, and add a `manifest-vectors.json` download.
- ASSETS list (`:135`): replace `index-rich.sqlite.gz` with `index-vectors.sqlite.gz manifest-vectors.json`.

- [ ] **Step 3: Verify locally (no CI spend, no embedding)**

Run: `.venv/bin/python -c "import yaml; y=yaml.safe_load(open('.github/workflows/build-index.yml')); print('valid yaml')"`
Run: `.venv/bin/pytest tests/test_rich_index.py tests/test_rich_cache.py tests/test_router_vectors.py -q`
Run: `.venv/bin/pytest --collect-only -q 2>&1 | tail -1`  (no import breakage)
Expected: valid yaml; all tests pass; full suite collects.

- [ ] **Step 4: Commit**

```bash
.venv/bin/ruff check scripts/build_index.py
.venv/bin/ruff format scripts/build_index.py
git add scripts/build_index.py .github/workflows/build-index.yml
git commit -m "feat(rich): publish index-vectors.sqlite.gz + manifest-vectors.json

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Stress gates — nothing is triggered until each passes

**Files:** none. This task spends CI, in a strictly gated order.

- [ ] **Step 1: Local, bounded — synthetic tests only (zero laptop load)**

Run: `.venv/bin/pytest tests/test_rich_index.py tests/test_rich_cache.py tests/test_router_vectors.py tests/test_index_cache.py -q`
Expected: all PASS. These use `FAKE = FakeReranker()` — **no real embedding, no worker processes, no model download.**

- [ ] **Step 2: CI migration dry-run against the real sidecar**

Trigger one manual rich build:
```bash
gh workflow run build-index.yml -f rich=true
```
It downloads the legacy 421.9 MB `index-rich.sqlite.gz`… which no longer matches the new download pattern. **Therefore, before triggering:** manually seed the new asset name once, on the runner's behalf, by renaming the release asset:
```bash
gh release download index-latest --pattern 'index-rich.sqlite.gz' --dir /tmp/richmig
gh release upload index-latest /tmp/richmig/index-rich.sqlite.gz#index-vectors.sqlite.gz --clobber
```
The build then downloads it as `index-vectors.sqlite.gz`, `_ensure_schema` detects the legacy `job_text` table, `migrate_legacy_rich` carries the 360k vectors + sigs forward, drops the text/FTS tables, and `VACUUM`s. Confirm from the run log:
- `rich tier (pruned=… embedded=… missing=…)` printed, **and** the resulting `index-vectors.sqlite.gz` is far smaller (expect ~150–200 MB gz, down from 421.9 MB — the text was 87%).
- The **core index still published** (`gh release view index-latest` shows a fresh `index.sqlite.gz`).

**STOP if:** rows lost (`embedded + carried != prior 360k + new`), the core index failed to publish, or the run exceeded 270 min.

- [ ] **Step 3: Read the measurements that set the cap**

```bash
gh run view <run-id> --json conclusion,startedAt,updatedAt
gh run view <run-id> --log | grep -iE "rich tier|peak|Maximum resident"
gh release view index-latest --json assets | python3 -c "import sys,json; a={x['name']:x['size'] for x in json.load(sys.stdin)['assets']}; print('vectors gz MB:', a.get('index-vectors.sqlite.gz',0)/1e6)"
```
Record: run duration, `missing`, asset size. **The duration is what sets the raised cap in Task 7 — do not guess it.**

---

## Task 7: Raise the cap, finish the ramp, record

**Files:** Modify `.github/workflows/build-index.yml` (env `ERGON_RICH_MAX_EMBED`); Modify `docs/extraction-baseline.md`

- [ ] **Step 1: Choose the cap from the measurement**

From Task 6 Step 3, let `T_measured` be the run duration at the current 120k cap and `T_overhead = T_measured - embed_time(120k)`, where `embed_time(n) ≈ n / 10 embeds-per-sec / 60` minutes (the all-in rate observed: 120k in ~140 min of embedding).
Set `N = floor((270 - T_overhead) * 60 * 10)` — i.e. fill the **270-min** budget (not 330), leaving ~60 min of headroom. Clamp `N` to `[120_000, 400_000]`.

Update `ERGON_RICH_MAX_EMBED: "120000"` (`build-index.yml:103`) to the chosen `N`, commit, push.

- [ ] **Step 2: Decide the shard question on evidence**

If Task 6's run shows peak RSS comfortably under the runner's limit and duration well under 270 min, the **optional 3–4 shard parallel embed** may be added later as its own change. **Default: do not shard.** Sequential runs at the raised cap already reduce the ramp to ~4–6 passes. Record the decision either way; do not implement sharding in this plan.

- [ ] **Step 3: Run the remaining passes, one at a time**

For each pass: `gh workflow run build-index.yml -f rich=true`, wait for completion, then verify `missing` decreased by ≈N and the core index published. **Halt immediately** if `missing` fails to drop, a run exceeds 270 min, or the core index does not publish. Repeat until `missing == 0`.

- [ ] **Step 4: Re-enable rich on the daily schedule**

Only once `missing == 0`: restore the schedule gate so maintenance is automatic (steady-state embeds only new/changed postings, which is cheap):
```yaml
${{ (github.event.inputs.rich == 'true' || github.event_name == 'schedule') && '--rich' || '' }}
```
Keep the cron at daily (`17 4 * * *`). Commit + push.

- [ ] **Step 5: Record**

Add a `### Rich tier — vectors-only, wired into serving (2026-07-09)` subsection to `docs/extraction-baseline.md`: the schema change and why (FTS rebuild was O(total rows); combined asset would have hit 85% of GitHub's 2 GiB cap), the migration that preserved the 360k embeddings, the final asset size, the number of ramp passes, and that `router.try_index_ranked` now ranks from pre-stored vectors with a hybrid fallback. Commit.

---

## Self-Review

**1. Spec coverage** (vs `2026-07-09-rich-vectors-only-design.md`):
- Unit 1 vectors-only schema, `sig` on the vectors row, all three FTS-rebuild sites removed → Task 1. ✔
- Unit 2 CI migration preserving the 360k embeddings, idempotent → Task 2. ✔
- Unit 3 `RichCache` + router hybrid vector ranking + `VectorIndex` guard + clean fallback → Tasks 3, 4. ✔
- Unit 4 measurement-gated re-ramp; shard decision on evidence, defaulted off → Tasks 6, 7. ✔
- Publish/download of `index-vectors.sqlite.gz` → Task 5. ✔
- Budgets (≤270 min, ≤609 MB, no cron bursts, manual-only through the ramp, daily restored at the end) → Global Constraints, Task 7 Steps 1/4. ✔
- Laptop never occupied — every task's local step is synthetic tests with `FAKE`; all embedding/migration/stress is CI → Global Constraints, Task 6 Step 1. ✔
- Invariants preserved (chunk-boundary equivalence, carry-forward, ramp-cap convergence, single-process) → Task 1 Step 4. ✔
- Dropped `fulltext_search`/full-JD FTS → Task 1 Step 3. ✔

**2. Placeholder scan:** No TBD/TODO. The one runtime-determined value (`ERGON_RICH_MAX_EMBED`) has an explicit formula and clamp in Task 7 Step 1, computed from a Task 6 measurement — not a guess. The asset-rename step in Task 6 Step 2 is spelled out as exact commands. Sharding is explicitly **out of this plan**, with the decision recorded.

**3. Type consistency:** `_embed_rows_into(con, rows: list[tuple[str,str,str]], …)` (id, sig, embed_text) is used identically by all three builders. `_delete_ids(con, ids)` vectors-only. `migrate_legacy_rich(con) -> int` and `_ensure_schema(con) -> None` consistent across Tasks 1–2. `RichCache.ensure_fresh() -> Path | None` matches `SlimCache`'s shape and the router's `_rich_path()`. `vector_search(...) -> list[tuple[str, float]]` consumed as `dict(...)` in `_vector_rerank`. `RICH_SCHEMA_VERSION` referenced by `rich.py`, `cache.py`, `build_index.py`, and both new test files. ✔
