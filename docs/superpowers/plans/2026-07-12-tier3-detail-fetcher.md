# Tier 3 — JD Detail-Fetcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch the JD bodies that list-only ATS sources never return in bulk (workday 545k, oracle, icims, …), extract structured fields from them, and merge those fields into the index — incrementally, politely, and surviving re-crawls.

**Architecture:** An index-driven reconcile pass mirroring the rich ramp. A rotating cursor selects postings lacking a description, a bounded `ERGON_DETAIL_MAX` slice is fetched per run through the EXISTING `AsyncFetcher` (per-host caps, `Retry-After`, circuit breaker), each JD is run through the extractors and DISCARDED (keeping a 300-char snippet + recovered fields), and the results land in a sig-gated `index-detail.sqlite` sidecar that the build merges into the index columns so a re-crawl can't wipe them.

**Tech Stack:** Python 3.10+, async (anyio), sqlite3, the existing `ergon_tracker.http.AsyncFetcher` + `ergon_tracker.enrich.enrich_in_place` + provider registry.

## Global Constraints
- **Politeness is absolute:** all fetching goes through `AsyncFetcher` (bounded global concurrency + per-host token bucket + `Retry-After` + per-host circuit breaker). Interleave the missing slice across hosts (`build_index._interleave_by_ats`). Never a raw N+1 loop. Per-run cost bounded by `ERGON_DETAIL_MAX` (measured, not guessed).
- **Non-fatal:** the core index `_gated_publish`es FIRST; the detail pass is a `try/except` add-on (like `--rich`). A dead posting increments `attempts` and is skipped after a retry cap; it never aborts the pass.
- **Discard the JD text** after extraction — store only recovered fields + a 300-char snippet. Never persist the full description (keeps the sidecar to tens of MB).
- **Carry-forward is sig-gated:** the build merge applies a sidecar row only when its `sig` equals the current index row's sig (else the posting is re-fetched). This is what survives re-crawls.
- **`--detail` is MANUAL-ONLY** until the stress gates pass, then joins the daily schedule.
- **Laptop-safe:** synthetic tests use a FAKE fetcher (offline, deterministic). Real detail fetches run in CI or a bounded controller stress run — never a large local fetch fleet.
- Branch `tier3-detail-fetcher` (off main, checked out). Commit per task, trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Mirror the vectors sidecar's proven shapes: `src/ergon_tracker/index/rich.py` (`_sig`, reconcile), `cache.py` (`RichCache`), the `build-index.yml` paired-publish guard.

## File Structure
- `src/ergon_tracker/index/detail.py` — NEW. Sidecar schema, `DetailRef`, `_sig`, `reconcile_detail_tier`, the build-merge helper. (Units 2+3+4 core.)
- `src/ergon_tracker/providers/base.py` — add optional `async def fetch_detail(self, ref, fetcher) -> str | None` (base returns `None`).
- `src/ergon_tracker/providers/{oracle,icims,workday,smartrecruiters}.py` — implement `fetch_detail`. (Unit 1, independent per provider.)
- `src/ergon_tracker/index/cache.py` — add `DetailCache` (mirrors `RichCache`).
- `scripts/build_index.py` — wire the detail reconcile pass + build merge + publish, gated on `--detail`.
- `.github/workflows/build-index.yml` — download/publish `index-detail.sqlite.gz` + `manifest-detail.json`; `--detail` gate.
- `tests/test_detail_tier.py`, `tests/test_detail_cache.py`, `tests/test_provider_fetch_detail.py`, `tests/live/test_detail_live.py` (controller-run gate).

---

### Task 1: Detail sidecar schema + `DetailRef` + `_sig`

**Files:**
- Create: `src/ergon_tracker/index/detail.py`
- Test: `tests/test_detail_tier.py`

**Interfaces:**
- Produces: `DETAIL_SCHEMA` (SQL), `DETAIL_SCHEMA_VERSION = 1`, `DetailRef` (dataclass: `id`, `source`, `token`, `apply_url`, `listing_url`, `content_sig`), `detail_sig(row: dict) -> str`, `open_detail(path) -> sqlite3.Connection`, `ensure_detail_schema(con)`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_detail_tier.py
import sqlite3
from ergon_tracker.index.detail import DETAIL_SCHEMA, ensure_detail_schema, detail_sig, DetailRef

def test_schema_and_sig():
    con = sqlite3.connect(":memory:")
    ensure_detail_schema(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(job_detail)")}
    assert {"id", "sig", "fetched_at", "attempts", "snippet",
            "salary_min", "salary_max", "salary_currency", "salary_interval",
            "years_min", "years_max", "degree_min", "degree_required",
            "sponsorship_offered"} <= cols
    # sig is stable + independent of the (to-be-fetched) description
    s1 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    s2 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    assert s1 == s2 and isinstance(s1, str)
    assert detail_sig({"content_hash": "xyz"}) != s1

def test_detailref_from_row():
    ref = DetailRef.from_row({"id": "1", "source": "oracle", "board_token": "t",
                              "apply_url": "http://x", "listing_url": None, "content_hash": "h"})
    assert ref.id == "1" and ref.source == "oracle" and ref.apply_url == "http://x"
```

- [ ] **Step 2: Run it → FAIL** (`ModuleNotFoundError`). `uv run pytest tests/test_detail_tier.py -q`

- [ ] **Step 3: Implement `detail.py` (schema + sig + DetailRef)**
```python
"""Tier-3 detail sidecar: recovered structured fields + snippet from per-posting JD detail fetches,
keyed by posting id with a sig for re-crawl-safe carry-forward. The JD text itself is never stored."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

DETAIL_SCHEMA_VERSION = 1
DETAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_detail (
  id TEXT PRIMARY KEY,
  sig TEXT,
  fetched_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  snippet TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
  years_min INTEGER, years_max INTEGER,
  degree_min TEXT, degree_required INTEGER,
  sponsorship_offered INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ensure_detail_schema(con: sqlite3.Connection) -> None:
    con.executescript(DETAIL_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(DETAIL_SCHEMA_VERSION),))
    con.commit()


def open_detail(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    ensure_detail_schema(con)
    return con


def detail_sig(row: dict[str, Any]) -> str:
    """Change signal for a posting, INDEPENDENT of the (to-be-fetched) description — so we only
    re-fetch when the posting materially changed. Uses content_hash if present, else title+level."""
    basis = row.get("content_hash") or f"{row.get('title', '')}|{row.get('level', '')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class DetailRef:
    id: str
    source: str
    token: str | None
    apply_url: str | None
    listing_url: str | None
    content_sig: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "DetailRef":
        return cls(
            id=str(row["id"]),
            source=str(row.get("source") or ""),
            token=row.get("board_token"),
            apply_url=row.get("apply_url"),
            listing_url=row.get("listing_url"),
            content_sig=detail_sig(row),
        )
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_detail_tier.py -q`
- [ ] **Step 5: Commit** `git add src/ergon_tracker/index/detail.py tests/test_detail_tier.py && git commit -m "feat(detail): tier-3 sidecar schema + DetailRef + sig ..."`

---

### Task 2: `reconcile_detail_tier` pass with an injectable fetcher (synthetic-testable)

**Files:**
- Modify: `src/ergon_tracker/index/detail.py`
- Test: `tests/test_detail_tier.py`

**Interfaces:**
- Consumes: `DetailRef`, `detail_sig`, `open_detail` (Task 1); `enrich_in_place`.
- Produces: `async def reconcile_detail_tier(detail_path, index_path, *, fetch_detail, max_details=None, sources=None, now) -> dict` returning `{"fetched": int, "failed": int, "missing": int}`. `fetch_detail` is an injected async callable `(DetailRef) -> str | None` — the FAKE in tests, the real provider dispatch in production. `now` injected (no `datetime.now()` in the pass) for determinism.

- [ ] **Step 1: Write the failing test (fake fetcher, all the invariants)**
```python
# add to tests/test_detail_tier.py
import anyio
from ergon_tracker.index.detail import reconcile_detail_tier, open_detail
# (helper builds a tiny index.sqlite with jobs rows: id, source, description empty, apply_url, content_hash)

def _mk_index(tmp_path, rows):
    import sqlite3
    p = tmp_path / "index.sqlite"; c = sqlite3.connect(p)
    c.execute("CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
              "listing_url TEXT, content_hash TEXT, description TEXT, snippet TEXT, "
              "salary_min REAL, salary_max REAL, years_min INTEGER)")
    c.executemany("INSERT INTO jobs (id,source,apply_url,content_hash,description) VALUES (?,?,?,?,?)",
                  rows); c.commit(); c.close(); return str(p)

def test_reconcile_fetches_missing_extracts_and_caps(tmp_path):
    idx = _mk_index(tmp_path, [(str(i), "oracle", f"http://x/{i}", f"h{i}", None) for i in range(5)])
    det = str(tmp_path / "detail.sqlite")
    async def fake(ref):  # returns a JD with a parseable salary
        return f"<p>Great role. Salary: $120,000 - $150,000 / year. Req {ref.id}.</p>"
    stats = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, max_details=3,
                                                    now=lambda: "2026-07-12T00:00:00Z"))
    assert stats["fetched"] == 3 and stats["missing"] == 5   # capped at 3 of 5
    con = open_detail(det)
    got = con.execute("SELECT salary_min, salary_max, snippet, fetched_at FROM job_detail").fetchall()
    assert len(got) == 3
    assert got[0][0] == 120000.0 and got[0][1] == 150000.0   # extracted, text discarded
    assert got[0][2] and len(got[0][2]) <= 300               # snippet kept
    assert got[0][3] == "2026-07-12T00:00:00Z"

def test_reconcile_nonfatal_and_retry_budget(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")
    async def boom(ref): raise TimeoutError("dead page")
    s1 = anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=boom, now=lambda: "t"))
    assert s1["failed"] == 1 and s1["fetched"] == 0
    con = open_detail(det)
    assert con.execute("SELECT attempts FROM job_detail WHERE id='1'").fetchone()[0] == 1  # counted, not fatal

def test_reconcile_sig_skips_unchanged(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "http://x/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")
    calls = []
    async def fake(ref): calls.append(ref.id); return "<p>Salary: $100,000 / year</p>"
    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))
    anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t"))  # 2nd run
    assert calls == ["1"]  # unchanged sig -> not re-fetched
```

- [ ] **Step 2: Run → FAIL.** `uv run pytest tests/test_detail_tier.py -q`

- [ ] **Step 3: Implement `reconcile_detail_tier`** — SELECT index rows where `description` is NULL/empty AND source in `sources` (Tier-3), LEFT JOIN the sidecar, keep rows where `job_detail.id IS NULL OR job_detail.sig != <current sig> OR (fetched_at IS NULL AND attempts < RETRY_CAP)`; order by a rotating cursor (stored in `meta`); cap at `max_details`. Interleave by source. For each: `desc = await fetch_detail(ref)`; on exception or None → increment `attempts` (retry budget `RETRY_CAP=3`); on success → build a `JobPosting` carrying `description_html=desc`, run `enrich_in_place`, write recovered fields + `snippet = _snippet(desc)` (300 chars, tag-stripped) + `sig` + `fetched_at=now()`. Concurrency via `anyio` task group bounded by a semaphore (the AsyncFetcher already bounds host rate; the semaphore bounds in-flight). `missing` = total still-empty count. Return the stats dict. Full code in the implementer's hands; the tests above pin every invariant.

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_detail_tier.py -q`
- [ ] **Step 5: Commit** `feat(detail): reconcile_detail_tier — bounded, non-fatal, sig-gated, injectable fetcher`

---

### Task 3: `fetch_detail` provider contract + oracle implementation (mid-size proving source)

**Files:**
- Modify: `src/ergon_tracker/providers/base.py` (add base `fetch_detail` returning `None`)
- Modify: `src/ergon_tracker/providers/oracle.py` (implement it)
- Test: `tests/test_provider_fetch_detail.py`

**Interfaces:**
- Consumes: `DetailRef` (Task 1), `AsyncFetcher`.
- Produces: `BaseProvider.fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None` (base → None); `OracleProvider.fetch_detail` fetches the requisition detail resource and returns the description HTML.

- [ ] **Step 1: Failing test with a FAKE fetcher returning the real oracle detail shape**
```python
# tests/test_provider_fetch_detail.py
import anyio
from ergon_tracker.providers.oracle import OracleProvider
from ergon_tracker.index.detail import DetailRef

class _FakeFetcher:
    def __init__(self, payload): self._p = payload
    async def get_json(self, url, **kw): return self._p

def test_oracle_fetch_detail_returns_description():
    # oracle detail resource shape: items[0].ExternalDescriptionStr (full HTML)
    payload = {"items": [{"ExternalDescriptionStr": "<p>Full JD. 5+ years. Master's required.</p>"}]}
    ref = DetailRef(id="1", source="oracle", token="host|site",
                    apply_url="https://h/hcmUI/.../job/1934", listing_url=None, content_sig="s")
    desc = anyio.run(lambda: OracleProvider().fetch_detail(ref, _FakeFetcher(payload)))
    assert desc and "Full JD" in desc

def test_base_fetch_detail_is_none():
    from ergon_tracker.providers.base import BaseProvider
    ref = DetailRef(id="1", source="x", token=None, apply_url=None, listing_url=None, content_sig="s")
    assert anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher({}))) is None
```

- [ ] **Step 2: Confirm the real oracle detail shape.** Read `oracle.py` (the `_VIEW`/`_API` + `ShortDescriptionStr` notes) and do ONE live GET of a requisition detail resource to confirm the field carrying the full description (likely `ExternalDescriptionStr`/`ExternalResponsibilitiesStr` under `items[0]`). Encode the confirmed field. Run → FAIL first.

- [ ] **Step 3: Implement.** Base: `async def fetch_detail(self, ref, fetcher): return None`. Oracle: build the detail resource URL from `ref` (host+site+id), `await fetcher.get_json(url)`, return the description HTML field (concatenate ExternalDescriptionStr + ExternalResponsibilitiesStr if both present), `None` on missing/shape-mismatch. Non-raising.

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(oracle): fetch_detail for tier-3 JD recovery + base contract`

---

### Task 4: Build merge — apply the sidecar into the index columns (sig-gated)

**Files:**
- Modify: `src/ergon_tracker/index/detail.py` (add `merge_detail_into_index(index_con, detail_path)`)
- Test: `tests/test_detail_tier.py`

- [ ] **Step 1: Failing test** — build a tiny index (a posting with empty salary) + a detail sidecar with recovered salary and a MATCHING sig → `merge_detail_into_index` sets the index row's salary; a NON-matching sig → index row untouched (stale sidecar not applied).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `ATTACH` the detail sidecar; `UPDATE jobs SET salary_min = d.salary_min, ... , snippet = COALESCE(d.snippet, jobs.snippet) FROM job_detail d WHERE jobs.id = d.id AND d.sig = <recompute current row sig>` — only overwrite columns that are currently NULL on the index row (don't clobber a value the list crawl DID provide) and only when the sig matches. Guard `degree_required`/`sponsorship_offered` int casts. Show the exact SQL against the real index schema.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(detail): sig-gated build merge of recovered fields into the index`

---

### Task 5: `DetailCache` + workflow plumbing (publish/download, `--detail` gate)

**Files:**
- Modify: `src/ergon_tracker/index/cache.py` (add `DetailCache`, mirroring `RichCache`)
- Modify: `scripts/build_index.py` (wire the reconcile pass + merge, gated on `--detail`; publish `index-detail.sqlite.gz` + `manifest-detail.json`)
- Modify: `.github/workflows/build-index.yml` (download prev detail sidecar; publish detail assets ONLY paired with their manifest; `--detail` manual-only)
- Test: `tests/test_detail_cache.py`

- [ ] **Step 1: `DetailCache`** — copy the `RichCache` structure verbatim, swapping `index-detail.sqlite.gz`/`manifest-detail.json`/`DETAIL_SCHEMA_VERSION`. Test with a `file://` remote (mirror `tests/test_rich_cache.py`): cold download, warm hit, stale build_id, absent→None, sha mismatch rejected, schema-version guard (with the accept-case pinned, per the Stage-1 lesson).
- [ ] **Step 2: `build_index.py`** — after `_gated_publish`, `if ok and detail:` run `reconcile_detail_tier` (dispatching `fetch_detail` per provider via the registry) then `merge_detail_into_index`, inside a `try/except` (non-fatal). Write `_write_detail_manifest` mirroring `_write_vectors_manifest`. `ERGON_DETAIL_MAX` from env.
- [ ] **Step 3: Workflow** — download `index-detail.sqlite.gz` (+ legacy-absent tolerant), publish it ONLY when `manifest-detail.json` also exists (the Stage-1 unpaired-asset guard), `--detail` gated `${{ inputs.detail == 'true' }}` (manual-only; NOT schedule yet). Validate YAML.
- [ ] **Step 4: Run** `uv run pytest tests/test_detail_cache.py -q` + full `uv run pytest -q` + ruff + mypy. Commit `feat(detail): DetailCache + build reconcile/merge + publish plumbing (--detail manual-only)`.

---

### Task 6: End-to-end synthetic stress test + coverage tool

**Files:**
- Create: `tests/test_detail_e2e.py`, `scripts/measure_detail_coverage.py`

- [ ] **Step 1:** e2e test with a FAKE fetcher (canned JDs + injected failures/timeouts across many synthetic postings): drive `reconcile_detail_tier` → `merge_detail_into_index` → assert the index rows gained the recovered salary/years/degree, the failed ones are marked `attempts`, the cap held, and a second run skips unchanged sigs (idempotent). This is the **synthetic stress test** (bounded, offline, deterministic).
- [ ] **Step 2:** `measure_detail_coverage.py` — reads the local index, prints description/salary/years/degree coverage per Tier-3 source (before/after), for reading the real lift after a CI run.
- [ ] **Step 3:** Full suite green + ruff + mypy. Commit `test(detail): end-to-end synthetic stress + coverage tool`.

---

### Task 7 (controller/CI gate — NOT an implementer task): Real mid-size stress run
Run `--detail` in CI against **oracle** at a low `ERGON_DETAIL_MAX` (e.g. 5k). Measure: per-detail latency, peak concurrent requests, per-host QPS vs the oracle cap, 429/circuit-breaker rate, throughput (details/min), wall-time, and the coverage lift (oracle salary/years/degree ~0% → ?). Confirm the core index still publishes and the detail sidecar publishes paired with its manifest. **Set `ERGON_DETAIL_MAX` from this data.**

### Task 8 (controller/CI gate): Drain oracle to `missing == 0`
Sequential `--detail` runs at the chosen cap until oracle's missing hits the churn floor; confirm carry-forward across re-crawls (a re-crawled oracle board keeps its recovered fields via the sig-gated merge).

### Task 9 (controller/CI gate): Extend to Workday (+ icims/smartrecruiters) and go daily
Implement `fetch_detail` for workday (externalPath→detail JSON), icims (JSON-LD), smartrecruiters (posting detail) — each an independent small task, parallelizable. Stress-run workday at the measured budget; drain; then flip `--detail` onto the daily schedule (like `--rich`).

## Self-Review
**Spec coverage:** Unit 1 (fetch_detail) → Tasks 3, 9; Unit 2 (sidecar) → Task 1; Unit 3 (reconcile) → Task 2; Unit 4 (merge + plumbing) → Tasks 4, 5; synthetic stress → Tasks 2, 6; real stress → Tasks 7–9; concurrency/politeness → reused AsyncFetcher + interleave in Task 2/5; carry-forward → sig in Tasks 1/2/4; staging (mid-size→workday) → Tasks 3/7/8 then 9. All spec sections mapped.
**Placeholder check:** Tasks 1–3 are code-complete; Tasks 4–6 give exact SQL/behavior + pinning tests (implementer writes the body against real schemas confirmed in-step); Tasks 7–9 are explicitly controller/CI gates, not code. No "TODO"/vague steps.
**Type consistency:** `DetailRef`, `detail_sig(dict)->str`, `reconcile_detail_tier(...)->dict`, `fetch_detail(DetailRef, AsyncFetcher)->str|None`, `merge_detail_into_index(con, path)` — consistent across tasks. `now`/`fetch_detail` injected for deterministic offline tests (no `datetime.now()`, no network in unit tests).

## Parallelization
Tasks 1→2→4 are sequential (same file `detail.py`, build on each other). Task 3 (base+oracle fetch_detail) and Task 5's `DetailCache` are **independent** → parallel agents. Task 9's per-provider `fetch_detail`s are **independent** → parallel wave. Controller runs all live/CI gates (Tasks 7–9) and integrates.

## Execution Handoff
Two options below.
