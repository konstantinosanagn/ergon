# Tier 3 — JD Detail-Fetcher for List-Only ATS Sources — Design Spec

> **Created:** 2026-07-12 · **Status:** approved, pre-implementation.
> **Part of** the 3-tier structured-field-recovery program (spec `2026-07-10-structured-field-recovery-design.md`).
> Tier 1 (map structured fields already in bulk payloads) shipped to `main`. Tier 2 (audit bulk-JD
> sources) was found marginal and skipped. **Tier 3 is the big remaining lever:** fetch the JD bodies
> the list-only ATS sources never return in bulk, so text-based extraction (salary/years/degree/skills)
> can finally run on them.

## The problem
**81% of the index (~1.2M of ~1.48M live postings) has no JD text.** High-volume ATS sources are
crawled **list-only** (one bulk request per board); their list endpoints return metadata but not the
description body, so no text extraction runs and salary/years/degree coverage is ~0% there. The gap is
concentrated: **Workday alone is 544,725 postings = 37% of the index at 0% text**, plus oracle (67k),
icims (49k), and the smartrecruiters/workable JD tails. The description lives on a **per-posting detail
page** — one extra HTTP request per posting (N+1), deliberately skipped historically to keep crawls
free/fast/polite. Tier 3 pays that cost **incrementally and politely**, draining the backlog over N
runs exactly as the rich embedding ramp did.

## What already exists (Tier 3 reuses, doesn't reinvent)
- **Politeness/concurrency is built.** `ergon_tracker.http.AsyncFetcher` already provides bounded
  global concurrency, per-host token-bucket rate limiting, `Retry-After`-honoring retries, and a
  per-host circuit breaker — with Workday tenants rate-limited *per full host* (independent data
  centers) after a past 429 storm. `build_index._interleave_by_ats` spreads load across backends.
- **Detail URLs are available.** Each Tier-3 posting carries `apply_url`/`listing_url` in the index
  (workday derives from `externalPath`; oracle has `_VIEW`; icims has per-detail JSON-LD).
- **The carry-forward pattern is proven.** The vectors sidecar (`index-vectors.sqlite` + `sig`)
  reconciles against the index each run, carrying forward already-computed data keyed by posting id.
  Tier 3 mirrors this so a re-crawl can't wipe a detail-recovered field.
- **Discard-after-extract is the established stance.** Rich went vectors-only to avoid full-text
  bloat; Tier 3 likewise extracts fields from the JD and discards the text (keeping a real snippet).

## Coverage strategy (decided)
**Drain everything, rotating cursor.** A per-source cursor rotates through ALL Tier-3 postings lacking
a description, fetching `ERGON_DETAIL_MAX` per run until `missing == 0`, then daily maintenance only
fetches new/changed postings. No priority scoring (simplest, predictable, eventually 100%).

## Architecture — four units (largely disjoint → parallelizable)

### Unit 1 — per-provider `fetch_detail` (the only provider-specific piece)
Add an **optional** async method to the provider contract:
```python
async def fetch_detail(self, ref: DetailRef, fetcher: AsyncFetcher) -> str | None:
    """Fetch ONE posting's detail page and return its description HTML, or None. Non-fatal."""
```
`DetailRef` carries what the provider needs to build the detail URL (id, token, `apply_url`/
`listing_url`, `externalPath`) — read from the index row, no re-crawl of the list. Each Tier-3
provider implements it against its own detail endpoint (workday detail JSON, oracle detail resource,
icims JSON-LD). Providers that don't implement it are skipped (base returns `None`). **This is the
unit to parallelize across providers** — each is independent.

### Unit 2 — the detail sidecar (build-time carry-forward store)
A SQLite sidecar `index-detail.sqlite`, keyed by posting id, storing only what was recovered — never
the full JD:
```sql
CREATE TABLE job_detail (
  id TEXT PRIMARY KEY,
  sig TEXT,                 -- change signal (content_hash of title/level/location); re-fetch on change
  fetched_at TEXT,          -- detail-fetched marker; NULL/absent => still "missing"
  attempts INTEGER,         -- bounded-retry budget for dead/404 pages
  snippet TEXT,             -- a real 300-char snippet (upgrades these sources from 0-char snippets)
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
  years_min INTEGER, years_max INTEGER,
  degree_min TEXT, degree_required INTEGER,
  sponsorship_offered INTEGER
);
```
The recovered fields are the extractor outputs — **the full description text is discarded after
extraction** to keep the sidecar small (fields+snippet ≈ hundreds of bytes/row, vs KBs for full JD).

### Unit 3 — the detail-fetch reconcile pass (mirrors the rich reconcile)
Runs AFTER the core index builds (non-fatal add-on, like `--rich`):
1. Select the **missing slice**: index postings from Tier-3 sources where the description is absent
   AND (`id` not in the sidecar OR `sig` changed), capped at `ERGON_DETAIL_MAX`, ordered by a rotating
   per-source cursor (so every board's postings are reached over N runs).
2. **Interleave the slice across hosts** (`_interleave_by_ats`) so no single Workday tenant is hit
   in a burst, and fetch each posting's detail concurrently through the shared `AsyncFetcher`
   (bounded global concurrency + per-host token bucket + backoff + circuit breaker — all existing).
3. For each fetched JD: run the existing extractors (`enrich_in_place` over a JobPosting carrying the
   fetched description) → write the recovered fields + a 300-char snippet + `fetched_at` into the
   sidecar → **discard the text**.
4. **Non-fatal per posting**: a fetch/parse failure increments `attempts`; after a retry cap the
   posting is marked done (never retried forever). One dead posting never sinks the pass; the pass
   failing never blocks the core index publish.

### Unit 4 — build merge + workflow plumbing
- **Build merge:** after the fresh index is built, apply the sidecar to the main index columns —
  `UPDATE jobs SET salary_*/years_*/degree_*/sponsorship_offered/snippet = detail.* WHERE jobs.id =
  detail.id AND detail.sig == <current row sig>`. Sig-gated so a materially-changed posting is
  re-fetched, not stale-merged. This is what makes the recovered fields **survive a re-crawl** — the
  crux the whole design turns on.
- **Publish/carry-forward** `index-detail.sqlite.gz` (+ manifest) on the release; the workflow
  downloads it each run so the backlog and recovered data persist (paired-with-manifest publish, same
  guard the vectors plumbing uses). `--detail` gated manual-only until the stress gates pass, then
  joins the daily schedule.

## Concurrency & optimization (first-class)
- **Reuse, don't rebuild:** all fetching goes through `AsyncFetcher` — bounded global concurrency,
  per-host token buckets, `Retry-After` retries, per-host circuit breaker. No new HTTP machinery.
- **Interleave the missing slice by host** so the concurrent detail fetches spread across many Workday
  tenants / SmartRecruiters / Oracle hosts rather than serializing on one — the same fix that killed
  the 2,181× 429 storm for list crawls.
- **Per-tenant caps already tuned** (workday per-full-host, workable 3/s, adp 1/6s). Tier 3 inherits
  them; the stress run confirms peak per-host QPS stays under them.
- **`ERGON_DETAIL_MAX` bounds per-run cost**, chosen from a measured stress run (target: detail pass
  fits the remaining time budget after crawl+build+embed, ≤ the 330-min timeout with headroom).
- **No redundant work:** sig-gated skip of already-fetched/unchanged postings; bounded retries on
  dead pages; the rotating cursor guarantees forward progress without rescanning covered rows.

## Resource budgets — deliberate headroom
| Limit | Ceiling | Target |
| --- | --- | --- |
| Job timeout | 330 min | detail pass sized so crawl+build+embed+detail ≤ ~300 min |
| Detail sidecar asset | 2 GiB/file | fields+snippet only → tens of MB gz at full coverage |
| Per-host QPS | provider caps (e.g. workable 3/s) | stress run confirms peak < cap |
| Detail fetches/run | — | `ERGON_DETAIL_MAX`, measured; drains 545k over N runs |

## Stress testing — synthetic AND real (both required)
1. **Synthetic (offline, deterministic, laptop-safe):** unit tests with a **fake `AsyncFetcher`** that
   returns canned detail bodies (and injected failures/timeouts/429s). Cover: the reconcile pass
   selects only missing rows; `ERGON_DETAIL_MAX` cap honored; **sig carry-forward** (unchanged posting
   skipped, changed posting re-fetched); **non-fatal** (a failing fetch increments `attempts`, doesn't
   abort the pass, respects the retry cap); the build merge applies sidecar fields and is sig-gated;
   interleave spreads the slice across hosts. No network.
2. **Real, mid-size (controller-run or CI, bounded + polite):** a live detail-fetch run against a
   **mid-size Tier-3 source (oracle ~67k or icims ~49k)** at a small cap — measure per-detail latency,
   peak concurrent requests, **per-host QPS vs the cap**, 429/circuit-breaker rate, throughput
   (details/min), and the recovered-field lift (salary/years/degree coverage on that source: ~0% →
   ?). This sets `ERGON_DETAIL_MAX` from data, not a guess.
3. **CI stress gate before scaling:** one measured CI run at the chosen cap on the mid-size source —
   confirm the detail pass fits the timeout, the sidecar publishes, the core index still publishes,
   and `missing` drops. **Only then** extend to Workday.

## Staging (build order)
1. **Unit 2 + 3 + synthetic tests** — the sidecar + reconcile pass with a fake fetcher (no live calls).
2. **Unit 1 for ONE mid-size provider** (oracle or icims) — its `fetch_detail`.
3. **Real mid-size stress run** → set `ERGON_DETAIL_MAX`.
4. **Unit 4** — build merge + publish plumbing + `--detail` (manual-only).
5. **CI stress gate** on the mid-size source; drain it to `missing == 0`.
6. **Extend `fetch_detail` to Workday** (+ oracle/icims/smartrecruiters); drain under the measured
   budget; restore `--detail` on the daily schedule.

## Implementation approach — parallel junior-SWE agents
The four units are largely **disjoint files**, so the plan will fan out parallel agents (the proven
Tier-1 pattern): one agent per provider `fetch_detail` (Unit 1, independent files), one for the
sidecar schema+reconcile (Unit 2/3), one for the build merge + workflow (Unit 4). The controller runs
every live stress gate itself (the slow-network work that stalled agents in Tier 1) and integrates +
runs the full suite. Each landed piece ships with synthetic tests; the real stress runs are
controller/CI-driven.

## Constraints honored
Free · CPU-only · **politeness-bounded (never an N+1 storm — bounded concurrency, per-host caps,
interleaved, measured `ERGON_DETAIL_MAX`)** · incremental (rotating slice, drains over N runs) ·
non-fatal (core index publishes first; a detail failure never blocks it) · carry-forward (sig-gated
sidecar survives re-crawls) · lean index (discard text, keep snippet+fields) · reuses existing fetcher
+ extractors + carry-forward pattern · no new runtime dependency.

## Out of scope (YAGNI)
- **Full-JD storage / keyword search** — Tier 3 recovers *structured fields* + a snippet, then discards
  the text. Full-text search is a separate future *sharded text sidecar*.
- **Priority/query-value scoring** — drain-everything with a rotating cursor was chosen.
- **Non-list-only sources** — greenhouse/ashby/lever already have the JD in bulk (Tier 2, skipped).
- **Raising `ERGON_DETAIL_MAX` beyond the stress-proven value.**

## Open items to settle in the plan
- The mid-size proving source (oracle vs icims) — pick by cleanest detail endpoint + fill.
- Exact `sig` for detail change-detection (content_hash of title/level/location — reuse the rich sig
  minus the description, since the description is what we're fetching).
- `ERGON_DETAIL_MAX` and per-host QPS headroom — from the mid-size stress run.
- Retry/attempts cap for dead detail pages.
- Whether the build merge is a Python pass or SQL `UPDATE ... FROM` (attach the sidecar).
