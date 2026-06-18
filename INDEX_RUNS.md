# Search Index — build & dogfood log

Per-run notes for the broad-discovery search index (spec:
`docs/superpowers/specs/2026-06-18-search-index-design.md`; M1 plan:
`docs/superpowers/plans/2026-06-18-search-index-m1.md`).

## 2026-06-18 — M1 (pipeline proof) complete + live dogfood

**Status:** M1 done — all 12 tasks implemented TDD, full suite green (683+ tests), ruff clean
on all index files. Pipeline proven end-to-end on **live data**.

**Live build** (`scripts/build_index.py --limit-companies 60`):
- **18,469 jobs** crawled from 60 registry boards → `dist/index.sqlite` (28 MB raw),
  `integrity_check = ok`. (Extrapolated, the full ~46k-board index would be ~hundreds of MB raw
  / ~100 MB gzip — within the size budget; deltas/shards in v2 shrink per-query downloads.)
- 60 companies; 3,814 jobs with salary; 10,242 with a known (non-unknown) level; 5,685 FTS
  matches for "engineer".

**Dogfood through the router/SDK** (broad query, served from the local index, zero ATS contact):
- `try_index(SearchQuery(keywords="senior backend engineer", level=SENIOR, limit=8))`
  → **2 ms**, 8 results, all genuinely-senior backend roles (Adobe / NVIDIA / Salesforce),
  ranked by FTS bm25. Confirms the core goal: **queryable data with no throttling at query time.**

**Bug found & fixed during dogfood:** the first build returned 0 jobs — `_crawl` passed registry
*slugs* to `companies=`, but `resolve()` expects domains/URLs. Rewrote `_crawl` to iterate
registry entries directly by their stored `(ats, token)`, crash-isolated per board. (This is
exactly why the working-style memory mandates dogfooding through the real tools — the unit tests
were all green while the live path was broken.)

**Next:** M2 (smart-tiered incremental crawl + conditional requests + throttle back-pressure +
GitHub Action) and M3 (observability + data-quality gates) complete v1. Then v2 = Approach B
(sector-sharded index + deltas) for optimized search.

## 2026-06-18 — M2 (smart-tiered incremental crawl): scheduler + incremental + live proof

**Status:** M2 Tasks 1–7 done (TDD): adaptive scheduler (`index/scheduler.py` — BoardState,
tiers hot/warm/cold/quarantine, due-selection, throttle back-pressure), real `content_hash`,
`build_index_incremental` (carry-forward uncrawled boards, expire gone), `changed_companies`
diff, and `build_index.py --incremental` (crawl only due boards, persist `board_state.json`).
699 tests pass, ruff clean on index files.

**Live incremental build** (`--incremental --limit-companies 25`): crawled 25 due boards →
13,343 fresh → **9,723 deduped** → `index.sqlite` (15 MB) + `board_state.json` (25 boards).

**Throttle-proofing demonstrated** (on the real persisted state): Day 1 all 25 due (cold start);
after a month of no changes all → `cold`; **Day 2 due = 0/25** (carry-forward serves the index,
zero ATS contact); Day 8 → 25 due (weekly cold re-check). i.e. a stable board is crawled ~weekly,
not daily — the crawl load collapses while the index stays current.

**Remaining for v1:** M2 Task 8 (GitHub Action daily cron + publish to Releases) and M3
(observability + full data-quality gate suite). Then v2 = Approach B (sector shards + deltas).

## 2026-06-18 — M3 (gates + observability) + M2 Action: v1 feature-complete

**Done this iteration:**
- **Data-quality gates** (`index/gates.py`): integrity, schema, row-floor (vs prev), no-dup-ids,
  company-FK. **Good-or-nothing publish**: build to a temp file → gate → atomically promote +
  publish only if all pass; else previous snapshot stays live, run exits non-zero. gates.json
  always written. (5 tests)
- **GitHub Action** (`.github/workflows/build-index.yml`): daily cron → incremental build →
  gated publish to the stable `index-latest` release (assets clobbered). SDK `IndexCache` points
  at the stable per-tag URL.
- **Build-history time series** (`history.jsonl`, accumulated across CI runs via the release):
  per-build due/fresh/total/changed/throttled/errored/published — drift-detection backbone.

**Live proof (full gated pipeline):** re-run incremental → carry-forward 9723→9702 (stable) →
**all gates passed** → published → history recorded (throttled 0, errored 0). 705 tests pass.

**v1 status: feature-complete.** Queryable index (M1) + tiered throttle-proof incremental crawl
(M2) + gates/observability/daily Action (M3) all implemented, tested, and proven on live data.
Remaining v1 polish: full structured-JSON logging + secret redaction + `INDEX_STATUS.md`
generation, and the first real **full** (46k) CI build. Then v2 = Approach B (sector shards + deltas).
