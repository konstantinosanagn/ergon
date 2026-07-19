# Daily Freshness Sweep — design

**Status:** design (approved architecture; awaiting spec review)
**Date:** 2026-07-18

## Goal
Re-verify that **every stored posting still exists on its board at least once a day**, so the
index never serves postings that have been dead for days. Today's effective re-verification
interval is **~5 days** (window gate: 12k boards/run vs a 58,078-board registry) to **~23 days**
(the liveness pass, capped at 2k boards/run reusing the expensive full fetch — its backlog grows).

## Core idea
Check **boards, not postings** (58k boards vs 1.48M postings = ~25× fewer requests), and fetch
**IDs only** (skip JD/enrich/dedup/insert — the costly parts the current liveness pass wastefully
reuses). A departed posting = an id in our stored `active` set that's absent from the board's
current id set. Action: `UPDATE jobs SET status='expired'` (already filtered by every query path,
so it's invisible immediately with zero query changes; `COUNT(*)` unchanged → row_floor gate safe).

## Per-provider strategy (measured)
| Class | Providers | Strategy |
|---|---|---|
| **Deterministic board, ETag-cached** | greenhouse, ashby, breezy, lever | conditional GET → unchanged board = ~0-byte 304; else id-diff. **Quick win: greenhouse `conditional_url` must drop `?content=true` (12.5× smaller miss).** |
| **Deterministic board, no ETag** | workable, jazzhr, rippling, join, dejobs | id-diff on the light list. (Test ETag for workable/rippling/jazzhr as a follow-up.) |
| **Search-index list (reshuffles → list-miss is a false positive)** | oracle, smartrecruiters, successfactors | bulk-list → *candidates* (missing-delta) → confirm each candidate via the **already-wired per-posting detail endpoint** (200/404); only expire on confirmed failure. |
| **Bloated list → per-posting primary** | icims (33 KB/job), eightfold (97% facet redundancy) | skip bulk relist for membership; per-posting 200/404 existence check on stored active ids; bulk-walk only occasionally to discover new. |

Reliability gate: deterministic-board sources expire on 1 confirmed miss; search-index sources
require the per-posting-404 confirmation (no streak needed — 404 is definitive). Keep a small
`dead_streak` insurance only where neither signal is definitive.

## Where it runs (approved)
A **new, separate, daily GitHub workflow** `freshness-sweep.yml`, **20-way matrix sharded BY HOST**
(rate-capped hosts stay whole — `AsyncFetcher` token buckets are per-process; splitting one host
multiplies its real rate → ban). Runs in parallel to the daily build on our **free public CI**
(distinct concurrency group from `build-index`). **join.com on its own dedicated shard** (19,937
boards, ~5–7 req/s ⇒ ~50 min floor that can't be sped up by concurrency) so it overlaps the rest.

## Integration (carry-forward of expiries)
The sweep publishes the set of newly-expired ids (a small `freshness-expired.json` release asset,
or writes `status='expired'` into a published `index-freshness.sqlite` sidecar). The daily
`build_index` reads it and applies `status='expired'` during `carry_forward` so expiries survive the
next full rebuild (mirroring how the detail/liveness sidecars are carried forward). The sweep must
NOT hard-delete and must be non-fatal.

## Cost model
- ~tens of thousands of requests/day, low-single-digit GB/day.
- Wall-clock floor = **join ~50 min** on its dedicated shard; all other providers finish under it in
  parallel. Conditional GET cuts bandwidth (not join's request-count wall-clock — the rate limit is
  per-request). Fits the 330-min ceiling with wide margin; free on the public repo.

## Components / files
- `src/ergon_tracker/index/freshness.py` (new) — the sweep engine: per-board id-set fetch routing,
  diff, per-posting-confirm for search-index sources, `status='expired'` writer, sidecar.
- Provider `list_ids(token, fetcher) -> set[str]` light path (or reuse existing list endpoints with a
  no-enrich flag) on each of the 15 providers; greenhouse `conditional_url` fix.
- `scripts/freshness_sweep.py` — CLI entry (shard arg, host-sharded board selection).
- `.github/workflows/freshness-sweep.yml` — daily, 20-way host-sharded matrix, join isolated.
- `scripts/build_index.py` — consume the expired-id set in `carry_forward`.

## Phases
0. Sweep engine + per-provider `list_ids` + greenhouse quick win (offline, TDD).
1. Per-posting-confirm path for search-index sources (offline, TDD).
2. `status='expired'` writer + sidecar + build carry-forward integration (offline, TDD).
3. CLI + host-sharding logic (offline unit test of the shard partition; small live smoke).
4. Workflow (20-way host-sharded); dry-run one shard live; measure real wall-clock/bandwidth.
5. Stress test: end-to-end on a synthetic index (expire departed, keep live, COUNT unchanged,
   search excludes expired); concurrency/rate-limit honored; non-fatal on provider failure.

## Non-goals / risks
- Not a re-crawl: no JD/enrich (that stays the tiered build's job).
- join's ~50-min floor is inherent (single host, per-process buckets) — mitigated by its own shard.
- Search-index per-posting confirmation is the costliest slice — bounded to the missing-delta.
- Must never expire a live posting: deterministic-board 1-miss is safe (full dump); search-index
  requires the 404 confirm before expiring.
