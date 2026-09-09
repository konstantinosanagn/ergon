# Streaming / SQL-Merge Index Build Implementation Plan

> **STATUS: COMPLETE (2026-06-18).** Tasks 1–6 + integration done, parity-tested, and verified
> end-to-end on a real 5-board crawl (1657 jobs → streamed → built → gated → sharded). The
> incremental build is now memory-bounded (O(batch)+O(#companies)); swap is no longer required.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Checkbox steps.

**Goal:** Build the index without ever holding all jobs in memory, so a full ~46k-board crawl
(~1M+ rows) builds within RAM (no dependence on swap) and scales further.

**Architecture:** Stream jobs into SQLite in batches (`INSERT OR IGNORE` on the unique `id` =
exact-id dedup). Carry forward unchanged companies from the previous index via SQL `ATTACH`
(no Python objects). Aggregate companies + build FTS from the DB via streaming cursors
(memory O(#companies), not O(#jobs)). Keep the existing in-memory `build_index` for small
builds + tests; add a streaming path the crawl uses for large incremental builds.

**Tech Stack:** sqlite3 (ATTACH, INSERT OR IGNORE), existing schema/mapping/to_row.

**Known tradeoff:** the streaming path does exact-id dedup only (not `deduplicate()`'s fuzzy
within-company merge). Exact-id catches same-job dupes; fuzzy near-dupes (mostly aggregator
overlap, a minority) may yield extra rows. Per-batch `deduplicate()` still catches intra-batch
fuzzy dupes. Acceptable for a broad-discovery index at scale; documented.

---

### Task 1: `append_jobs(con, jobs, *, build_id)` — incremental insert  [build side]

**Files:** `src/ergon/index/build.py`, `tests/test_index_streaming.py`

- [ ] Insert a batch of JobPostings (+ job_sources) into an open connection via `INSERT OR
  IGNORE` (dedup on `id`). Reuse `to_row`. Return # rows actually inserted.
- [ ] Test: two batches with an overlapping id -> second insert ignored; row count correct.

### Task 2: `carry_forward(con, prev_db_path, crawled_keys)` — SQL merge  [build side]

- [ ] `ATTACH` prev DB; `INSERT OR IGNORE INTO jobs SELECT * FROM prev.jobs WHERE company_key
  NOT IN (<crawled temp table>)`; same for job_sources. No Python job objects.
- [ ] Test: prev has companies A,B; crawl re-did A (crawled_keys={A}); after carry_forward the
  new DB keeps fresh A rows + carried-forward B rows; matches `merge_incremental` semantics.

### Task 3: streaming company aggregation + `finalize_index`  [build side]

- [ ] `_aggregate_companies_streamed(con)`: cursor over `SELECT company, company_domain, source,
  sector FROM jobs` in `fetchmany` chunks; accumulate Company dict (O(#companies)); same rules
  as `aggregate_companies` (open_roles count, first non-null domain/sector, h1b lookup).
- [ ] `finalize_index(con, *, build_id)`: insert companies, FTS `rebuild`+`optimize`, meta
  (build_id,row_count), ANALYZE, VACUUM, integrity_check. Return row count.
- [ ] Test: company rows match `aggregate_companies` for the same jobs.

### Task 4: `build_index_streaming(job_batches, out, *, build_id, prev_db=None, crawled_keys=None)`

- [ ] Orchestrate: `fresh_db(out)` -> `append_jobs` per batch -> `carry_forward` (if prev) ->
  `finalize_index`. Returns row count.
- [ ] Test: parity with `build_index` on a small job set (same ids, same company rows); and an
  incremental case (prev + fresh + crawled_keys) matches `build_index_incremental`.

### Task 5: crawl streams batches  [crawl side]

**Files:** `scripts/build_index.py`

- [ ] `_crawl_due` writes each board's enriched jobs to a fresh SQLite via `append_jobs` (batch
  per board) instead of accumulating a Python `fresh` list; return the fresh-DB path + outcome.
- [ ] Incremental main: use `build_index_streaming(... prev_db=existing, crawled_keys=...)`.
- [ ] Shards: build from the final DB via SQL per-sector (already job-list based today — adapt
  to read from DB so sharding is also memory-bounded). Track as Task 6 if large.

### Task 6: shard build from DB (memory-bounded)

- [ ] `build_sharded_index` currently takes a job list; add a DB-sourced variant that partitions
  by sector via SQL so the full-scale shard build doesn't load all jobs.

## Self-review

- Memory after Tasks 1-4: O(batch) + O(#companies). After Task 5: crawl no longer holds all jobs.
- Correctness anchored by parity tests vs `build_index` / `build_index_incremental`.
- Existing in-memory build retained for small builds + as the parity oracle.
