# Rich Tier — Vectors-Only + Wire It Into Serving — Design Spec

> **Created:** 2026-07-09 · **Status:** approved, pre-implementation.
> **Supersedes** the ramp approach in `2026-07-08-rich-daily-enablement-design.md` (halted at 360k).
> **Goal:** make the rich tier actually deliver a user-facing win (semantic ranking from pre-stored
> vectors) and make it scale, by shipping a **vectors-only** sidecar and **wiring it into the serving
> path** — which nothing does today.

## The discovery that reframes this
`open_rich` / `vector_search` / `fulltext_search` / `VectorIndex` are called **only by
`tests/test_rich_index.py`**. `src/` has zero serving references to `index-rich`, `job_vectors`, or
`job_text_fts` outside `rich.py`. `cache.py` has `IndexCache`/`SlimCache`/`ShardCache` but **no
`RichCache`** — the SDK never downloads the asset. `router.try_index_ranked` does semantic ranking by
**re-embedding a ~200-doc lexical pool at query time**.

⇒ **The sidecar is a write-only artifact.** ~13 CI-hours and a 421.9 MB release asset produced
something no user code reads. Semantic search and `match_resume` already work *without* it. Building it
was necessary but never sufficient; the consumer was never written.

## Measured numbers (not estimates)
| | per row | at 1,474,832 jobs |
| --- | --- | --- |
| `job_vectors` | **481 B** | 677 MB raw → **~609 MB gz** (int8 compresses poorly, ratio ~0.90) |
| `job_text` + FTS | **3,321 B** (6.9×) | 4,670 MB raw → ~1,518 MB gz |
| Combined (today) | 3,802 B | 5.2 GB raw → **1.70 GiB gz = 85% of GitHub's 2 GiB asset cap** |

Per-run duration fit: `T = 146 + 51.5 min per +120k cumulative sidecar rows`. Predicts **run 4 = 352 min,
crossing the 330-min timeout at ~480k** — exactly the observed wall. The growth term is the full
FTS rebuild (`INSERT INTO job_text_fts(job_text_fts) VALUES('rebuild')`, `rich.py:170,418`), which
rescans all of `job_text` every run. There are **no FTS sync triggers**; the rebuild is the only sync.

Embed throughput: ~10 embeds/s all-in on a CI runner (~12–18/s for the model alone).

## Architecture — four units

### Unit 1 — vectors-only sidecar (`rich.py`)
New asset `index-vectors.sqlite.gz`. Drop `job_text` and `job_text_fts` entirely; move `sig` onto the
vectors row so change-detection survives:
```sql
CREATE TABLE job_vectors (id TEXT PRIMARY KEY, sig TEXT, scale REAL NOT NULL, vec BLOB NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```
No FTS ⇒ **no `'rebuild'` ⇒ no growth term.** Per-run cost becomes O(changed rows) plus a ~constant
gz round-trip. `have` becomes `SELECT id, sig FROM job_vectors` (an O(n) read of ~1.4M short strings —
seconds, not the ~50 min/run the FTS rebuild cost). All three rebuild call sites are removed.

### Unit 2 — migration that preserves the 360k embeddings (runs in CI)
The reconcile detects a legacy sidecar (has `job_text`) and migrates **in place on the runner**:
`SELECT v.id, t.sig, v.scale, v.vec FROM job_vectors v JOIN job_text t USING(id)` → new schema, then
drops the legacy tables. Idempotent, one-time, and **never runs on the developer's laptop**. The
~13 CI-hours of embeddings already paid for are carried forward, not recomputed.

### Unit 3 — wire it into serving (the actual user win)
- **`RichCache`** in `cache.py`, mirroring `IndexCache`/`SlimCache`/`ShardCache`: downloads and caches
  `index-vectors.sqlite.gz`, tolerating absence.
- **`router.try_index_ranked`**: when `query.semantic` and the vectors sidecar is available — embed the
  **query once**, pull a **wider** lexical candidate pool, and rank with
  `vector_search(con, qvec, candidate_ids=pool_ids)` using pre-stored vectors.
  Today this path embeds ~200 *documents per query*; this makes it **one embedding per query** (~200×
  less query-time compute), which affords a ~10× wider pool and materially better recall.
- **Absence is a non-event:** if the sidecar (or the `semantic` extra) is missing, the code falls back
  to exactly today's behaviour — query-time rerank, else BM25 lexical order.
- **Explicitly NOT used:** `VectorIndex`, which loads every vector and converts to float32 (~2.26 GB
  RAM at 1.47M). The serving path uses the candidate-restricted `vector_search` only. A test guards this.

### Unit 4 — re-ramp (CI only)
With the growth term gone, raise `ERGON_RICH_MAX_EMBED` to a value **chosen from a measured stress run**
(target run duration ≤ 270 min). Remaining 1,119,280 jobs ⇒ ~4–6 runs instead of never.
**Optional 3–4 shard parallel embed** (see Concurrency) — enabled only if the stress run proves it safe.

## Resource budgets — deliberate headroom, never "it fits"
| Limit | Ceiling | Target | Headroom |
| --- | --- | --- | --- |
| Release asset / file | 2 GiB | ~609 MB | 70% |
| Job timeout | 330 min | **≤ 270 min** | ~60 min |
| Concurrent jobs | ~20 (free public) | **≤ 4** | large |
| Actions cache | 10 GB/repo | untouched (73 MB used) | — |

`--rich` stays **manual-only** through the ramp; it returns to the daily schedule only after the new
design is proven, at which point daily maintenance is cheap (only new/changed postings embed). The
existing `concurrency: build-index` group continues to serialize runs. No cron bursts, no API polling
storms — runs are triggered deliberately.

## Concurrency & optimization — real levers only
- **Embedding stays `single_process=True` on CI.** Per-worker ONNX model copies caused the original CI
  OOM (`a44dca3`) and, in a local benchmark, crashed the developer's laptop. Not revisited.
- **`pigz` already parallelizes gzip** — nothing to add.
- **Vectors are mergeable; an FTS index is not.** Dropping FTS unlocks an *embarrassingly parallel*
  embed: N jobs each embed a disjoint slice of `missing` ids into a partial vectors DB; a cheap final
  step `ATTACH`es and `INSERT`s them together. Capped at **3–4 shards** (not 12) to stay well within
  GitHub's allowance. **Off by default**; enabled only if the stress run shows per-shard peak RSS and
  duration are safe.
- **No redundant work:** skip the gz round-trip when nothing changed; retain chunked `fetchmany`
  streaming and bounded batches unchanged.

## HARD RULE — the laptop is never occupied
All embedding, migration, and stress runs execute **on GitHub runners**. Local execution is limited to
**synthetic unit tests with a fake reranker** — no real embedding fleet, no `parallel` workers, no model
downloads. Any test that could trigger real or parallel embedding is bounded to a handful of rows.
Rationale: a local `fastembed parallel=N` benchmark (each worker loading its own ~67 MB ONNX model, with
a `<stdin>` respawn storm) crashed the developer's machine.

## Stress gates — nothing is triggered until each passes
1. **Local, bounded:** unit + invariant tests on synthetic data (fake reranker).
2. **Migration dry-run in CI** against the real 421.9 MB sidecar: assert all 360k vectors + sigs carry
   over, row counts match, zero loss.
3. **One measured CI stress run** at the *current* cap with the new schema: record peak RSS, wall-time,
   asset size; confirm `missing` drops as expected **and the core index still publishes**.
4. **Only then** pick the raised cap from the measured duration (≤ 270 min) and trigger the remaining runs.

## Testing
Preserve the existing invariants, adapted to the vectors-only schema:
`test_reconcile_from_fresh_chunk_boundaries_match_single_fetch` (chunked == single-fetch, byte-identical
state), the cold-then-carry-forward test, and `test_reconcile_from_fresh_ramp_cap_converges`.
New tests: migration fidelity (legacy → vectors-only, no row loss, idempotent); `RichCache`
download/cache-hit/absent; router ranks from pre-stored vectors when present and **degrades to today's
behaviour when absent**; and a guard that the serving path never instantiates `VectorIndex`.

## Deliverables
- `src/ergon_tracker/index/rich.py` — vectors-only schema; remove `job_text`/`job_text_fts` and all
  three FTS-rebuild call sites; `sig` on the vectors row; legacy-schema migration.
- `src/ergon_tracker/index/cache.py` — `RichCache`.
- `src/ergon_tracker/index/router.py` — candidate-restricted vector ranking + graceful fallback.
- `scripts/build_index.py` + `.github/workflows/build-index.yml` — publish/download
  `index-vectors.sqlite.gz` (replacing `index-rich.sqlite.gz`).
- `tests/test_rich_index.py` (adapted) + new tests; `docs/extraction-baseline.md` record.

## Constraints honored
Free · CI-only heavy work (laptop never occupied) · memory-safe (`single_process`, chunked streaming) ·
deliberate headroom under every GitHub limit · non-fatal (core index publishes first; rich failure never
blocks it) · reversible · no new runtime dependency (numpy already arrives with the `semantic` extra).

## Out of scope (YAGNI)
Full-JD FTS / `fulltext_search` (unwired, 87% of storage, sole cause of the scaling wall — ship later as
a separate *sharded* text sidecar, and only then does incremental-FTS work matter); whole-index
`VectorIndex` brute force (2.26 GB RAM); any local embedding; raising concurrency beyond 4 shards;
re-enabling `--rich` on the daily schedule before the stress gates pass.

## Open items to settle in the plan
- The raised `ERGON_RICH_MAX_EMBED` value — chosen from the measured stress run, not guessed.
- Whether the 3–4 shard parallel embed is enabled (decided by the stress run's per-shard RSS/duration).
- Candidate-pool width for the new vector ranking path (start ~10× today's 200; tune against latency).
