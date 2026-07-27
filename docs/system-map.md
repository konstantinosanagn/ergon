# jobspine / ergon-tracker — System Map

**Generated:** 2026-07-20 · a grand-perspective conceptual review consolidated from an 8-lane
parallel code survey (Core SDK, Providers, Index build, Extraction, Serving, Automations, Registry,
Tests/metrics). ~52k LOC src+scripts, 54 providers, 232 test files (~1,994 test fns), 7 workflows.

---

## 1. What jobspine IS (one paragraph)

A **unified, free, offline-first job-data platform**. It crawls **54 ATS/aggregator platforms**
directly (Greenhouse…Workday…Eightfold), enriches every posting with structured fields no
mainstream job API exposes (salary-from-text, years-of-experience, degree, seniority, geo/remote,
sector, H-1B sponsor history), and publishes a **daily prebuilt SQLite/FTS5 index of ~1.48M live
postings across ~58k company boards** as a GitHub Release. Consumers query it four ways — Python
SDK, CLI, MCP server, HTTP QUERY endpoint — entirely offline against a downloaded snapshot, or hit
live ATS boards for targeted company queries.

---

## 2. The stack (top to bottom)

```
 CONSUMPTION   Python SDK · CLI (ergon-tracker) · MCP (ergon-tracker-mcp, 9 tools) · HTTP QUERY /jobs
      │        serialization.job_to_dict = one shared wire shape
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 QUERY/SERVE   engine.run_search → index fast-path (router: full/slim/sharded + vector rerank)
      │                          ↘ live fan-out (targeted) → dedup → BM25F rank → health
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 INTELLIGENCE  enrich_in_place → extract/{comp,yoe,degree,level,geo,sector,sponsorship,visa}
      │        deterministic-first (regex/gazetteer/table); ML tier (semantic, sector_clf) = built, unwired
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 INDEX         daily build_index.py: crawl→normalize→enrich→dedup→SQLite/FTS5→gates→publish
      │        + sidecars: detail(Tier-3 JD) · liveness · rich(vectors) · slim · sharded · delta
      │        delta-crawl (idset-hash skip + enrich-reuse) · freshness sweep (membership) · row-floor gate
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 SOURCE        providers/ (54) — Provider Protocol: fetch/normalize/matches/conditional_url/
      │        fetch_detail/board_count. http.AsyncFetcher (per-host rate/breaker/budget). crawl_pool.
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 UNIVERSE      registry/seed.json (58,078 company→{ats,token}) — literally WHO gets crawled.
               data/: h1b_sponsors(76k) · sectors(13.5k) · apicapture(67)
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 AUTOMATION    7 workflows on GitHub Actions → single index-latest release (mutable shared datastore)
               build 04:17 · embed 06:00 · sweep 08:17 UTC ; CI gates every push/PR
```

---

## 3. What lives where (repo map)

| Path | Role |
|---|---|
| `src/ergon_tracker/models.py` | The `JobPosting` contract + `SearchQuery.matches()` client filter |
| `src/ergon_tracker/{client,engine,sync}.py` | Async/sync entry + `run_search` orchestrator (index fast-path + live fan-out) |
| `src/ergon_tracker/{ranking,dedup,canonicalize}.py` | BM25F rank · cross-source merge · Company rollup |
| `src/ergon_tracker/{http,crawl_pool}.py` | `AsyncFetcher` (rate/breaker/budget) · bounded worker pool |
| `src/ergon_tracker/providers/` (54) | The ATS moat. `base.py` = Protocol + registry |
| `src/ergon_tracker/extract/` (15) + `enrich.py` | Field extraction (deterministic-first) |
| `src/ergon_tracker/semantic.py`, `extract/sector_clf.py` | ML tier — **built but unwired from enrich** |
| `src/ergon_tracker/index/` (18) | Build, schema, gates, freshness, delta, sidecars, client cache |
| `src/ergon_tracker/serve/query_app.py` | HTTP QUERY surface (ETag/304, cache, single-flight) |
| `src/ergon_tracker/{cli,mcp_server}.py` | CLI (6 cmds) · MCP (9 tools) |
| `src/ergon_tracker/registry/` + `data/` | The crawl universe + gold data assets |
| `scripts/` (~90) | ~10 on the automated path; rest = one-off discovery/coverage tooling |
| `.github/workflows/` (7) | The freshness automation loop |
| `tests/` (232, ~1,994 fns) + `tests/fixtures/` | Ratcheting recall/precision gates + parity + corpora |

---

## 4. Services we offer (surfaces × audience)

| Surface | Entry | Audience | Notes |
|---|---|---|---|
| **Python SDK** | `from ergon_tracker import search` / `AsyncErgonTracker` | app/pipeline devs | sync + async; `to_pandas/to_polars` |
| **CLI** | `ergon-tracker {search,match-resume,resolve,sources,sponsors,version}` | terminal users | ~25 search flags; no `--max-degree` (MCP has it) |
| **MCP server** | `ergon-tracker-mcp` (stdio, 9 tools) | AI agents / Claude | search_jobs, whats_new, match_resume, assess_fit, h1b_jobs, list_companies… |
| **HTTP QUERY** | `serve.serve()` → `QUERY /jobs` | backend/agent fleets | ETag/304, cache, single-flight — **undocumented + no console script** |
| **Prebuilt index** | auto-download to `~/.cache/ergon-tracker` | all broad-search consumers | delta/chain updates, slim + per-sector shards, sha256-verified |

---

## 5. Product metrics (the numbers we show customers)

### Extraction quality — measured / CI gate (blind-labeled real-JD corpora)
| Field | Measured | Gate | Corpus |
|---|---|---|---|
| Salary (comp) | recall 1.00 / prec 1.00 | .90 / 1.00 | 227 |
| Years-experience | recall 97.8% / prec 96.9% | .95 / .92 | 538 |
| Degree (level) | recall 90.5% / prec 99.5% | .88 / .98 | 401 |
| Degree (scope) | 61.1% (advisory) | .60 | 401 |
| Seniority level | acc 82.2% / F1 .738 | .78 / .68 | 899 |
| Geo | country 94.8% / city 96.9%(EN) | .90 / .92 | 799 |
| Skills | recall 92.7% / coll-prec 99.5% | .88 / .97 | 799 |
| Sector | acc-when-covered 73.7% / cov 36.4% | .68 / .34 | 699 |
| Sponsorship | tri-state 98.9% | .95 | 182 |
| Remote | acc 99.4% | — | via geo |
| Multilingual | DE/FR/ES yoe·degree·salary (FR yoe 96.1%) | looser | thin (ES salary 3) |

### Index scale & coverage (INDEX_STATUS build-66)
- **1,489,365 active jobs · 44,432 companies · 54 providers · 58,078-board registry**
- Salary disclosed 392,532 (26%) · H-1B sponsor employer 500,048 · level unknown 41% · remote unknown 68%
- **JD text capture: only ~19% of rows carry JD text (81% list-only)** — the dominant data ceiling
- board_token coverage 71.5% exact → ~84.7% target · sector table 23.3% of registry (unknown = 67% of postings)
- SEC public-co coverage 19.3% (1,544/8,015; ~6,012 pending) · join = **34% of the registry**

### Efficiency
- ~87% of daily enrich CPU was redundant re-processing → delta-crawl skip target ~87% postings / ~half boards
- **Plan 2 (2026-07-20): pre-enrich fingerprint flips ~24–44% of an unchanged board's postings from guaranteed reuse-miss → hit**
- Freshness board-membership coverage 55% → 96% · rich vectors int8 (389B vs 1536B float32)

---

## 6. Automations — the freshness loop

| UTC | Workflow | Produces |
|---|---|---|
| 04:17 | **build-index** (330-min cap, delta-crawl ON) | core index + slim/delta/detail/liveness sidecars, board_state, cursor |
| 06:00 | embed-vectors (20-shard) | vectors sidecar |
| 08:17 | freshness-sweep (20-shard, own group→parallel) | freshness sidecar (checks BOARDS not postings, ~25× cheaper) |
| manual | drain-detail (20-shard) | Tier-3 JD detail sidecar |
| PR/push | ci.yml | ruff+mypy+pytest gate (Py 3.10–3.13) |

**Closed loop:** sweep(day N) → freshness sidecar → build(day N+1) `apply_freshness_expiries` flips
departed postings to `status='expired'` (never hard-deletes → row-floor safe). Shard invariant:
each host's rate-bucket → exactly one shard (as polite as unsharded); join.com pinned to shard 0.
Resumability: cursor+state uploaded on `always()` so a timeout loses ≤1 window.

---

## 7. The moat

1. **Source breadth, verified.** 54 ATS platforms incl. the hard JS/faceted giants (Workday ≈37% of
   the index, Eightfold, iCIMS) competitors can't crawl without a browser; proxied-giants capture
   (Goldman via apicapture) + dejobs federation for bot-walled sites. Every registry entry was
   live-verified through the real provider stack.
2. **Field intelligence.** Salary-from-text, YoE, degree, sponsor history — structured, benchmarked,
   multilingual — none of which JobSpy-class aggregators expose.
3. **Freshness + correctness layer.** Daily membership sweep + liveness confirm + row-floor
   good-or-nothing publish — no scraper-aggregator has this.

---

## 8. Cross-cutting priorities — optimize / expand / stress-test

Ranked; items flagged by ≥2 independent lanes are **high-confidence**.

### A. Highest-leverage EXPANSION
1. **JD-text capture (81% gap)** ⟵ *lanes 2,4,8.* The single biggest data lever. Salary/yoe/degree
   extractors are ~90%+ recall but can't fire on the 81% of rows with no JD body. Levers: ukg (~40%
   salary in body), SR (~40% empty primary section), and the Tier-3 detail drain (currently manual-only).
2. **Wire the two stranded ML components** ⟵ *lane 4.* `sector_clf.py` (calibrated, abstaining) and
   `semantic.py` (reranker) are built + tested but **not called by `enrich_in_place`** — activating
   the sector classifier is the obvious lever for the 36% sector-coverage gap.
3. **Registry `domain` backfill (1.3% → ~100%)** ⟵ *lane 7.* The resolver's domain lookup is inert
   for 98.7% of the registry; cheap high-value fill.
4. **SEC longtail ~6,012** ⟵ *lane 7.* Dominant unbuilt lever = vanity-domain ATS detection
   (content-probe resolver) + new providers (ADP WFN, Paylocity, Dayforce, Phenom).

### B. Highest-leverage OPTIMIZATION
5. **Pre-`matches()` enrichment waste** ⟵ *lane 1.* `enrich_in_place` runs on every fetched record
   before the keyword filter drops non-matches — pre-filter title/company first to cut enrich cost.
6. **No pagination** ⟵ *lane 3.* `query.py` is LIMIT-only (no OFFSET/cursor) — a functional gap for
   any UI wanting page 2.
7. **Consolidate the 4 copy-paste tier caches** ⟵ *lane 3.* Only `IndexCache` has delta support;
   slim/detail/rich full-download every build.
8. **Ship serve/ as a real surface** ⟵ *lane 5.* A production-shaped HTTP QUERY server with zero
   docs and no console script. Add `ergon-tracker-serve` + README section (lowest-effort win).

### C. RELIABILITY / STRESS-TEST
9. **No alerting anywhere** ⟵ *lane 6.* Every operational failure is silent (continue-on-error +
   artifact-only tripwires). A failing daily cron goes unnoticed until visible staleness. Add
   notify-on-failure.
10. **No CI gate on the big product metrics** ⟵ *lane 8.* JD-capture %, freshness %, delta skip-rate
    (the biggest levers) are measured in one-off scripts, not ratcheting tests.
11. **The `fetch_detail` None-vs-raise contract** ⟵ *lanes 2,3.* Highest-consequence correctness
    surface: `None` = "expire a live row". Soft-404 sources (adp/taleo/taleobe) decide dead from
    markup — a markup change could mass-expire. Stress-test + keep the expiry-rate tripwire on merged totals.
12. **Hand-synced source lists** ⟵ *lanes 2,3.* `CONFIRM_VIA_DETAIL_SOURCES`, `_TIER3_DETAIL_SOURCES`,
    `_LOCATION_CAPABLE_SOURCES`, `fetch_detail` overrides — 3–4 enumerations of "sources with detail",
    synced by hand, no binding assertion. Drift silently drops confirm coverage.
13. **Delta-crawl at scale** ⟵ *lane 3.* Just flipped ON (2026-07-20); ramp + parity must be watched.

### D. HYGIENE / DEBT
14. Stale headline metrics (INDEX_STATUS pinned to build-66; README counts hand-maintained) — no
    single source of truth for live counts ⟵ *lanes 5,8.*
15. Not on PyPI (v0.1.0) — every install is a git clone; the pip-extras story is unexercised ⟵ *lane 5.*
16. ~90 scripts, mostly dormant one-offs + large committed JSON state (candidates_dead.json 1.3MB) =
    repo bloat ⟵ *lane 6.*
17. `count_sanity_check` built but not wired into `run_search`; `corrections.jsonl` empty (human-
    correction loop unused) ⟵ *lanes 1,8.*

---

*Full per-lane detail in the review scratchpad (lane1–lane8).*
