# Delta-Driven Crawl Redesign — design spec

**Status:** approved architecture, phased implementation
**Date:** 2026-07-19
**Author:** senior-SWE synthesis of a 4-agent investigation (all headline numbers independently re-verified against the live index / live HTTP, cited inline)

## 1. Problem (measured, not assumed)

The daily build blind-re-crawls a rotating ~12k-board window (full 58,078-board registry every
~5 days): for every due board it re-fetches the full listing, re-normalizes, re-enriches, re-hashes,
and re-inserts **every** posting — new or not. Measured on the real index (build-69 → build-70):

- **87–89% of postings on re-crawled boards are unchanged** — verified two independent ways:
  by `first_seen` (42,822 new / 387,642 crawled = 11.0% new) and by day-over-day `content_hash`
  diff (337,295 unchanged / 387,642 = 87.0%; 10.3% added, 2.7% updated).
- **~45–51% of re-crawled boards are byte-identical** day-to-day (3,841 / 8,630 = 44.5% identical
  id-set AND content_hashes; 4,436 / 8,618 = 51.5% had zero new postings).
- Enrich CPU measured at **1.53 ms/posting** ⇒ ~593 s/build, **~87% (~515 s) redundant**.

So **~87% of the crawl's per-posting work and ~half the board fetches are pure re-processing of
unchanged data.** The 330-min CI ceiling forces the 12k chunking precisely because a blind full
crawl is too expensive — the chunking is a symptom of the waste, not a fix.

## 2. The insight

We already pay for a daily, whole-registry, cheap change signal and throw it away: the **freshness
sweep** (`src/ergon_tracker/index/freshness.py`, daily, 20 shards) already fetches **every** board's
live id-set (id-only) and computes `removed = stored − live`. It never computes `added = live −
stored` and never persists the id-set. That id-set is exactly the validator the build needs to skip
unchanged boards. **Feed the sweep's diff back into the build, and expensive work collapses to the
day's genuine delta.**

## 3. Existing substrate (verified in code — this is wiring, not a rewrite)

- Tiered scheduler `hot/warm/cold/quarantine` with per-board `BoardState` (`scheduler.py:38`):
  `etag, last_modified, last_crawled, last_changed, consecutive_unchanged, tier, next_due`, persisted
  in `board_state.json` (54,208 boards).
- Cross-build **ETag/304 conditional GET already live** (`build_index.py:1079`): a stored validator →
  304 → carry forward at 0 bytes; a 200 reuses the body via `raws_from_body`. 7 providers today.
- `carry_forward` reuses prior enriched rows for non-crawled companies; `first_seen` preserved by
  `(company_key, content_hash)`.
- `build_delta` / `changed_companies_sql` already compute a `content_hash` upsert delta between two
  index DBs — the exact internal shape a delta build emits.

## 4. Target architecture

One daily **id-pass** (the freshness sweep, extended) is the change-detector; the **build** consumes
its per-board delta and does expensive work only on the delta.

```
DAILY id-pass (extended freshness sweep, all 58k boards, id-only / 304):
  per board -> { removed, added, unchanged, updated, idset_hash }  -> sidecar + board_state
      removed  -> expire (already done)
      board unchanged (304 OR idset_hash == stored) -> mark skip; nothing else

DELTA BUILD (consumes the sidecar):
  carry the entire prior index forward, then apply only:
      added / updated ids -> JD fetch (bulk-JD: already in list body, 0 extra req;
                             list-only: one fetch_detail per id) -> enrich -> upsert
      unchanged ids       -> reuse prior enriched row (enrich_hash match), no re-enrich
      skipped boards       -> untouched
  finalize / slim / delta / shards as today
```

Per-provider JD source for an added id (verified by snippet-presence):
- **Bulk-JD** (greenhouse 100%, lever 93%, ashby 100%, breezy, recruitee, teamtailor, …): JD is
  already in the one list body — **zero extra requests**, just enrich the added subset.
- **List-only** (workday, oracle, smartrecruiters, icims, successfactors, jazzhr, dejobs, eightfold):
  one `fetch_detail(DetailRef, fetcher)` per **added** id — the existing Tier-3 primitive.

## 5. Change-detection capability matrix (live-verified 2026-07-19)

| Tier | Providers (share of 1.47M active) | Cheapest "did it change?" signal |
|---|---|---|
| **304 conditional GET, 0 bytes** | greenhouse, lever, ashby, breezy, teamtailor, personio (**coded today**) + **smartrecruiters, icims (verified 304, NOT coded — free lever)** | ~47% of postings once SR+icims wired; 0-byte skip |
| **id-set hash (from the sweep)** | join, workday, bamboohr, jazzhr, rippling, oracle, successfactors, workable, recruitee, radancy, … (no ETag) | The sweep already fetches the id-set; hash it → skip unchanged |
| **page-1 total-count gate** | workday (`total` uncapped), join (`pagination.total`), ukg, dejobs, bamboohr | change-candidate trigger (same total ≠ same id-set — pair with id-set hash) |

Sitemap `<lastmod>` exists on some tenants but is CMS-dependent, not per-provider — opportunistic only.

## 6. Phases (each independently shippable + gated)

### Phase 1 — Quick wins (low risk, immediate)
- Add `conditional_url` overrides for **smartrecruiters** and **icims** (both verified to return 304).
  Zero-byte skip for ~200K more postings (+13.6%).
- Merge the held `feat/crawl-resumable` branch: bounded window (`ERGON_CRAWL_MAX_WINDOW`), full crawls
  routed through the streaming path, cursor/`fresh.sqlite` uploaded on `always()` — a kill loses ≤1
  window, never hours.
**Gate:** live conditional-GET test (real 304), full suite, ruff, mypy.

### Phase 2 — id-set-hash validator (the core lever)
- Extend the sweep: for each board, alongside `removed`, compute `added = live − stored` and
  `idset_hash = sha1(sorted(live_ids))`; persist `{added, removed, idset_hash}` to the sweep sidecar
  and stamp `board_state.idset_hash`.
- Build: before the expensive path, skip any board whose `idset_hash` is unchanged since last build
  (extends the ETag win to the ~74% validator-less boards). Consume `added` to drive inserts.
**Correctness gates:** the added-side gets a safety guard symmetric to the removed-side valves
(empty/None/`>50%` fraction) so a truncated live fetch can't flag phantom adds; ordering — the sweep
(or a fused id-pass) must run **before** the build so the delta is current.
**Gate:** parity test (delta build vs full crawl on a real board sample = identical rows), full suite.

### Phase 3 — enrich-reuse (work reduction)
- Add `enrich_hash = sha1(content_hash-fields + description_text)` — **content_hash excludes the JD
  body**, so enrich-reuse MUST key on a body-inclusive hash or it serves stale salary/yoe/degree.
- On a crawled board, `normalize()` (cheap, no enrich) each live posting, compute `enrich_hash`; for
  ids whose `enrich_hash` is unchanged, copy the prior enriched row; run `enrich_in_place` only on
  added/updated (~13%). Saves ~515 s enrich CPU/build + the list-only JD re-fetches.
**Gate:** enrich-reuse parity (reused row == freshly enriched row when enrich_hash matches; a JD-body
edit that keeps content_hash MUST re-enrich), full suite.

### Phase 4 — concurrency & scheduling (efficiency)
- Replace `start_soon`-per-board with a **bounded worker pool** over the interleaved queue
  (O(workers) memory, not O(window)).
- **Per-host deadline-boxing**: cap cumulative wall-clock per `rate_key` (join measured at 0.58
  boards/s vs greenhouse 6.3 — its 5-jobs/page pagination is the tail); once a host exceeds its
  budget, stop dispatching to it — untouched boards stay `due` and roll to the next run.
- Raise the **global** `CapacityLimiter` (64 → ~150–200) with per-host caps unchanged (token buckets
  already make this safe) so more distinct hosts run in parallel.
- **join page-1 pre-check**: join has no `conditional_url`; a 1-request page-1 count/id-set check
  before committing to full pagination cuts its per-board cost 3–9×. Two-tier pools: high-concurrency
  cheap-check tier vs conservative heavy-fetch tier.
**Gate:** real crawl micro-benchmark (wall-clock before/after on a live board sample), full suite.

## 7. Correctness invariants (non-negotiable)

1. **Never expire a live posting** — the sweep's empty-set / `>50%` fraction / per-posting-confirm
   valves are preserved; the added side gets the symmetric guard.
2. **enrich_hash includes the JD body** — no stale enrichment.
3. **Every posting is re-verified daily** — the id-pass already does this; the redesign only removes
   redundant JD re-reads of unchanged postings, so freshness is preserved and new postings land within
   a day (better than today's ≤5-day window latency).
4. **Delta build ≡ full crawl** on the same inputs (parity-tested on real boards).
5. **Ordering**: the id-pass feeds the build with a current diff (fuse or sequence them).
6. Reshuffling sources' false-adds are bounded waste (deduped by content_hash), never a correctness
   bug.

## 8. Testing strategy — REAL, not only synthetic

- **Synthetic/unit**: delta computation, `idset_hash`/`enrich_hash`, skip logic, added-side guard,
  worker-pool bounds, deadline-box — fast, deterministic, in CI.
- **Real / integration (the emphasis)**:
  - Live conditional-GET: assert a real 304 for smartrecruiters + icims (gated by a live-net flag).
  - **Delta-vs-full parity on real boards**: crawl a real board sample two ways (full crawl; delta
    from a prior id-set) and assert identical rows — the central correctness stress test.
  - **Work-reduction measurement**: run the delta build against the real build-70 index and assert
    the measured skip (~87% postings, ~half boards) matches the thesis — a regression tripwire.
  - **Concurrency micro-benchmark**: real wall-clock with/without deadline-box + worker pool on a
    live host sample (greenhouse vs join), asserting the join tail is bounded.
- **Stress**: a real one-shard end-to-end run measuring wall-clock, bandwidth, and that no live
  posting is expired.

## 9. Non-goals / risks

- Silent JD-body edits that don't change title/salary/location are invisible to `content_hash`;
  caught only by a **slow background full-JD refresh** (decoupled from membership, e.g. every N days) —
  a trickle, not the daily hot path.
- Cold-start / never-crawled boards still need one full crawl (the win is steady-state).
- This does not change WHAT is crawled (registry/coverage) — only how little work each day repeats.

## 10. Clean-code mandate

No fluff: reuse existing primitives (`BoardState`, `conditional_get`, `raws_from_body`,
`fetch_detail`, `build_delta`, the sweep's `board_live_ids`) rather than new parallel machinery;
delete the legacy in-memory `_crawl` path (Phase 1 branch already does); every new function typed,
tested, and single-responsibility.
