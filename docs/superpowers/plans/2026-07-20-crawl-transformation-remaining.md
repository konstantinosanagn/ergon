# Crawl transformation — remaining work roadmap

**Date:** 2026-07-20
**Context:** the delta-driven crawl redesign is fully built + merged to main, DARK behind
`ERGON_DELTA_CRAWL` (default off). See `docs/superpowers/specs/2026-07-19-delta-driven-crawl-redesign.md`
and memory `jobspine-delta-crawl-redesign`. This roadmap sequences what's left, each with the same
evidence-first, gate-hard discipline: measure/verify before claiming, ship behind a gate, review.

## Execution order
1 (payoff, blocked on a real sweep-populated sidecar) → 3 & 4 in parallel (independent, low-risk) →
2 (informed by Plan 1's measurement) → 5 → 6 parked.

---

## Plan 1 — Activate the delta-crawl (flip the flag)  ⭐ the payoff
**Goal:** turn on the ~87% waste reduction with proof on REAL data first.
**Blocked on:** a daily freshness sweep publishing a `board_deltas`-bearing `index-freshness.sqlite`
(Phase 2 shipped 07-19 → next sweep populates it).
**Steps (each a gate):**
1. Precondition: confirm the published sidecar has non-empty `idset_hash` rows for deterministic sources.
2. Real parity dry-run: pull prod prev-index + real sidecar; run `_crawl_due` flag-off (full) vs
   `ERGON_DELTA_CRAWL=1` (delta) on a real board sample; assert byte-identical `jobs` rows (parity on
   LIVE data, not fixtures).
3. Measure real skip/reuse rate on prod data (validates the 87%/51% thesis; surfaces the enrich-reuse
   hit-rate → feeds Plan 2).
4. Flip dark→live: enable via the `delta_crawl` dispatch input for ONE manual run, inspect published
   index + row-floor/parity gates, then enable on the schedule.
5. Rollback: the flag is one line — flip off → next build reverts to full crawl.
**Verify:** delta index ≡ full on real boards; row-floor green; run well under 330 min.

## Plan 2 — Lift the enrich-reuse hit-rate
**Goal:** close the review-flagged gap (`enrich_hash` computed pre-enrich vs stored post-enrich →
enrichment-derived fields always miss reuse; safe but low-rate).
**Approach:** store a PRE-enrich fingerprint column (hash of the normalized inputs enrichment consumes,
computed before `enrich_in_place`) and key reuse on it.
**Gate:** delta-vs-full parity still byte-identical; measure hit-rate before/after on real boards.
**Dependency:** AFTER Plan 1 step-3 measurement — don't optimize blind.

## Plan 3 — board_token coverage push  (task #40)  [STARTING NOW]
**Goal:** raise freshness board coverage past registry-exact 71.5%.
**Approach:** extend `backfill_board_tokens` (build.py) with SAFE URL-derivation for SEARCH-INDEX
sources ONLY (workday, oracle, smartrecruiters, icims) — safe because their per-posting `confirm_departed`
re-checks every candidate via the posting's own detail URL, so a wrong board_token is a no-op there.
Do NOT URL-derive deterministic sources (a wrong token there could false-expire via the id-diff path;
the 1.2% jazzhr/ashby error stands). Registry stays exact + first.
**Gate:** cross-check derived vs registry agreement where both exist; measure coverage gain on the real
index; full suite. Residual fills via the now-resumable crawl.
**Independent** of the delta work.

## Plan 4 — Soft-404 expiry-rate monitoring  (task #38)  [STARTING NOW]
**Goal:** a tripwire on the body-marker soft-404 sources (adp/taleo/taleobe) so a parser drift that
starts mass-expiring LIVE rows is caught fast.
**Approach:** the freshness sweep already reports per-source counts; add a per-source expiry-RATE
(expired / candidates or expired / checked) computed in the sweep, aggregated across shards at merge,
and a threshold WARNING when a source's rate spikes. Observability-only, non-fatal, no correctness change.
**Gate:** unit tests for the rate calc + threshold; full suite. **Independent, low-effort.**

## Plan 5 — Slow background JD-refresh
**Goal:** catch silent JD-body edits invisible to content_hash/enrich_hash.
**Approach:** a decoupled slow rotation re-reading full JDs every N days, separate from the daily
membership sweep — a background trickle.
**Dependency:** meaningful only AFTER Plan 1 (once the daily path stops re-reading JDs).

## Plan 6 — The ~4% freshness tail (PARKED)
apicapture/schemaorg/ceipal/zwayam — genuinely unbuildable safely. Revisit only on a specific need.
