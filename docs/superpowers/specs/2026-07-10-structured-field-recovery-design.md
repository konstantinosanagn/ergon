# Structured-Field Recovery Across All ATSes — Design Spec

> **Created:** 2026-07-10 · **Status:** approved, pre-implementation.
> **Goal:** stop discarding job metadata we already download. Recover structured fields (level,
> salary, degree, years, geo, employment type, department) that ATS bulk responses already carry,
> fix the correctness bugs where we fetch content and drop it, and design the staged path to fetch
> the JD bodies we currently miss — so the filters and search operate on the data that exists.

## The problem this solves
The extractors are **not** the bottleneck — description **capture** is. 81% of the index
(1,198,447 / 1,482,739 live postings) has **no JD text**, so no text-based extraction (salary, years,
degree, skills) can run on it. Salary coverage tracks capture almost perfectly: where the JD is
captured (greenhouse/ashby/lever) salary is 52–53%; where it isn't (workday/smartrecruiters/…) it's
0–8%. Three user-supplied misses (Accrete/breezy `Salary Range: 170k-235k`; Alchemy Worx/workable
`$110-140k`; Ant-Tech/join `$180,000 to $320,000/year`) each parsed **perfectly** when fed to the
extractor — every miss was uncaptured data, not a parser failure.

Root cause is deliberate and documented in the providers: high-volume ATSes are crawled **list-only**
(one bulk request per board). Their list endpoints return metadata; the JD body needs an **N+1
per-posting detail fetch** the providers "deliberately skip to keep fetch to one batch"
(smartrecruiters.py verbatim). But **two separate things were conflated** under that decision:
1. Structured fields (level, salary, education) that ARE in the bulk payload — dropped for no reason.
2. The JD body that is genuinely detail-only — the real cost.

This spec separates them into three tiers and stages the work accordingly.

## Evidence base
A live-API field inventory was run across ~50 ATS providers (5 parallel agents, real registry tokens,
detail in `scratchpad/inventory-{A..E}.md`). **All headline numbers below are controller-verified
against the live APIs**, because the inventory revealed a systematic trap: agents reported *field
presence* as *fill rate*. Recruitee's reported "100% salary" was a salary object present 100% of the
time but with `min`/`max` **populated only 11%** — an empty shell. Join, conversely, was under-called
by me earlier: `salaryAmountFrom/To` is populated ~35–62% and **present even when `showSalary=false`**
(4/5 hidden postings still carried the amount). **The lesson is a hard rule for this project:
fill rate means POPULATED, never PRESENT.**

## Architecture — three tiers, one per stage

### Tier 1 — map structured fields already in the bulk payload (FREE, no new requests)
Each provider's `normalize()` gains field mappings for data it already fetches. Verified,
volume-weighted wins (`index postings × populated fill`):

| provider | index vol | raw field(s) | → attr | populated fill | est recoverable |
| --- | --- | --- | --- | --- | --- |
| smartrecruiters | 149,691 | `experienceLevel.{id,label}` | level | ~100%† | ~150,000 |
| jazzhr | 58,770 | `<experience>` | level | 100% (verified 47/47) | ~59,000 |
| workable | 67,656 | `experience` (bucket) | level | ~62–75% | ~45,000 |
| workable | 67,656 | `education` | degree | ~37% | ~25,000 |
| join | 43,311 | `salaryAmountFrom/To`÷100, `salaryFrequency` | salary | ~35–62% (verified, incl. `showSalary=false`) | ~15,000–27,000 |
| breezy | 27,991 | `salary` free-text (`"$78,000 / year"`) | salary | 38% (verified) | ~11,000 |
| personio | 8,165 | `seniority`, `yearsOfExperience` (already in `raw`, unpromoted) | level, years | ~100% / ~80% | ~8,000 |
| eightfold | 20,240 | `standardizedLocations` | geo | 72% | geo enrichment |
| successfactors | 43,341 | `colDate`, `colDepartment`, `colShifttype` | posted_at/dept/level | partial (per-tenant) | dept/date |
| phenom | 5,855 | `experienceLevel`, `compensationRange`, `industry` | level/salary/sector | 12.5–48% | ~2,000 |
| teamtailor | 12,703 | `_jobposting.baseSalary` (schema.org) | salary | 12% | ~1,500 |
| recruitee | 12,988 | `salary.{min,max}` | salary | **11%** (corrected from 100%) | ~1,400 |
| ceipal | 825 | `pay_rates` (structured) | salary | ~100% | ~800 |
| themuse | 1,449 | `levels` | level | 100% | ~1,400 |
| jobicy / himalayas | aggregators | `jobLevel`/`seniority`, `jobIndustry`/`categories` | level/sector | 100% | broad-reach |
| usajobs | 2 boards | `JobGrade`/`Low/HighGrade` | level | 100% | federal |
| applicantpro | 365 | `workplaceType`, `min/maxSalary` | remote/salary | 71% / 40% | small |
| zwayam / ripplehire | 74 / 307 | years-of-experience fields | years | 100% / 99% | small |
| remotive | feed | free-text salary | salary | 67% | small |
| arbeitnow | feed | `tags[]` | dept/industry | 100% | categorical |

† smartrecruiters `experienceLevel` is a clean enum; **must be populated-verified before the mapping
lands** (it drives the single largest number). Same gate applies to every row.

**Net Tier-1 (verified, conservative):** ~**260,000 seniority labels**, ~**25,000 degrees**,
~**30,000–40,000 salaries**, plus geo/employment/department enrichment. Index-wide level coverage is
43% today; these sources contribute ~0 to it, so Tier 1 alone likely pushes level past ~55%.

### Tier 1b — correctness bugs found by the sweep (same files, ship together)
We fetch the data and drop it — these are defects, not enrichment:
- **coveo** direct-mode (`UST`-style): `normalize()` reads `category`/`description`; the direct schema
  uses `obu`/`data`, so **department and description_html are always `None`** despite 99.5–100% present.
- **taleobe**: location mis-tagged on 2/3 sampled tenants (fix before adding its fields).
- **paycom**: `description_html` is a **153-char truncated teaser**, not the full JD — currently
  mischaracterized as the description; reclassify so downstream doesn't treat it as full text.
- **peopleclick**: discards `FLD_JPM_HIRING_RANGE_MIN/MAX` (salary) and `JPM_DESCRIPTION` (full JD),
  both ~100% fill (40 postings — tiny, but a clean bug).
- **personio**: `seniority`/`yearsOfExperience` captured into `raw` but never promoted.
- **lever**: top-level `country` ISO never mapped into `Location.country`.
- **paylocity**: public-page GUID ≠ API-feed GUID — an auto-discovery correctness risk (flagged;
  may split into its own fix if it's not a pure normalize change).

### Tier 2 — audit bulk-JD sources for under-extraction
Greenhouse/ashby/lever and aggregators already return the full JD; confirm none is under-extracted
(e.g. ashby equity/commission comp components have no schema slot; lever `categories.team` overwritten
by `department`). Small, mostly confirmatory.

### Tier 3 — fetch the missing JD bodies (EXPENSIVE, staged last)
For genuinely detail-only sources — **workday (544,725 = 37% of the index)**, oracle (67k), icims
(49k), taleo, radancy, dejobs, bamboohr, rippling, and others confirmed Tier-3 — the JD needs one
extra HTTP request **per posting**. A naive N+1 over workday alone is ~545k requests/crawl.

Design: a **bounded-concurrency, politeness-aware, incremental detail-fetcher**, modelled on the rich
embedding ramp:
- **Rotating slice per crawl** — a capped number of detail fetches per run (e.g. `ERGON_DETAIL_MAX`),
  advancing a per-source cursor; full coverage fills over N runs, then daily maintenance holds it.
- **Bounded concurrency** with per-host rate limiting and backoff — never a request storm; reuses the
  crawler's existing polite fetcher and concurrency primitives (the same interleave-by-ATS machinery
  that fixed the 429 storm).
- **Prioritized**: fetch detail only where it unlocks a field the posting lacks AND (optionally) the
  posting matches a high-value geo/role, so budget goes to what users query.
- **Non-fatal**: a detail-fetch failure falls back to the metadata-only posting; never blocks the crawl.

## Concurrency & optimization
- Tier 1/1b/2 add **zero** network cost — pure `normalize()` mapping of bytes already downloaded. The
  optimization there is *not re-fetching*: parse the structured field instead of the description.
- Tier 3 is the concurrency problem: reuse the existing async fetcher + `interleave_by_ats` +
  per-host caps; add a bounded worker pool and a per-source cursor. Measure peak concurrent requests
  and per-host QPS in a stress run before raising `ERGON_DETAIL_MAX`.
- Salary/level parsers already exist (`comp.py` proven on all three user formats; `level.py`); Tier 1
  reuses them for free-text fields (breezy, remotive) and maps enums directly (smartrecruiters, themuse).

## Testing — populated-fill gates + real-MCP stress tests
1. **Populated-fill acceptance gate (per field, per provider):** every mapping ships with a test that
   probes live sample boards and asserts the field is **populated** (not merely present) at ≥ its
   measured rate. This encodes the Recruitee lesson as a guardrail — no mapping claims a win it can't
   prove. Bounded, polite (few boards), skippable offline.
2. **Provider unit tests:** synthetic raw payloads → `normalize()` → assert the new attrs, incl. the
   bug regressions (coveo direct-mode dept/desc, taleobe location, paycom teaser flag).
3. **Real-MCP stress test (required, not synthetic-only):** after each provider batch, rebuild/refresh
   the index and query the live serving path via `mcp_server` / `try_index_ranked` — confirm recovered
   fields change filter results (e.g. `level=entry` now bites on smartrecruiters postings; salary
   filters return join/breezy postings that were invisible before). This is the same method that
   surfaced the original gaps.
4. **Tier-3 stress run:** one measured CI crawl at a low `ERGON_DETAIL_MAX` — record peak concurrency,
   per-host QPS, added wall-time, and JD-capture delta — before scaling.

## Staging (build order)
1. **Stage 1 — Tier 1 + 1b**, highest-volume first: smartrecruiters, jazzhr, workable, join, breezy,
   personio (the ~250k-level + salary bulk), then the correctness bugs, then the small/aggregator tail.
   One PR per provider (or tight group), each with its populated-fill gate + MCP check.
2. **Stage 2 — Tier 2 audit** of bulk-JD sources.
3. **Stage 3 — Tier 3 detail-fetcher**: build the bounded incremental fetcher; prove it on one
   mid-size Tier-3 source; stress-run; then extend to workday under a measured budget.

## Constraints honored
Free · offline-serving unaffected · Tier 1/2 add no fetch cost · Tier 3 is politeness-bounded and
incremental (never an N+1 storm) · non-fatal (detail failure never blocks the crawl) · reuses existing
parsers and concurrency primitives · every claim gated on *populated* fill measured live.

## Out of scope (YAGNI)
- Re-crawl orchestration to backfill history (normal crawl cycle repopulates as boards are revisited).
- New extractor models — the parsers are at ceiling; this spec is about feeding them data.
- Paylocity auto-discovery GUID fix if it proves to be more than a normalize change (separate ticket).
- Raising `ERGON_DETAIL_MAX` beyond what the Stage-3 stress run proves safe.

## Open items to settle in the plan
- Populated-verify smartrecruiters `experienceLevel` and workable `experience` fill before their
  mappings land (they drive the biggest numbers).
- Exact `ERGON_DETAIL_MAX` and per-host QPS caps — chosen from the Stage-3 stress run, not guessed.
- Whether Tier-3 prioritization keys on geo/role or fetches the whole rotating slice indiscriminately.
- SmartRecruiters/`customField` and workable `country/city/state` → confirm current geo mapping isn't
  already covering them before adding.
