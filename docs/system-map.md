# ergon — System Map

**Generated:** 2026-07-20 · **corrected:** 2026-09-09 (§8 items re-checked against the code; several
2026-07-20 claims had gone stale or were never accurate — each is annotated inline).
56,008 LOC src+scripts, 54 registered providers, 271 test files (2,336 test fns), 8 workflows.

---

## 1. What jobspine IS (one paragraph)

A **unified, free, offline-first job-data platform**. It crawls **54 ATS/aggregator platforms**
directly (Greenhouse…Workday…Eightfold), enriches every posting with structured fields no
mainstream job API exposes (salary-from-text, years-of-experience, degree, seniority, geo/remote,
sector, H-1B sponsor history), and publishes a **daily prebuilt SQLite/FTS5 index of ~1.32M live
postings across ~40k companies** (drawn from a 58,078-board crawl registry — the registry is who we
*attempt*, not who returns rows) as a GitHub Release. Consumers query it four ways — Python
SDK, CLI, MCP server, HTTP QUERY endpoint — entirely offline against a downloaded snapshot, or hit
live ATS boards for targeted company queries.

---

## 2. The stack (top to bottom)

```
 CONSUMPTION   Python SDK · CLI (ergon) · MCP (ergon-mcp, 9 tools) · HTTP QUERY /jobs
      │        serialization.job_to_dict = one shared wire shape
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 QUERY/SERVE   engine.run_search → index fast-path (router: full/slim/sharded + vector rerank)
      │                          ↘ live fan-out (targeted) → dedup → BM25F rank → health
 ─────┼──────────────────────────────────────────────────────────────────────────────────
 INTELLIGENCE  enrich_in_place → extract/{comp,yoe,degree,level,geo,sector,sponsorship,visa}
      │        deterministic-first (regex/gazetteer/table); semantic.py = wired at QUERY time
      │        (rerank), NOT in enrich; sector_clf.py = built, deliberately NOT wired (see §8.2)
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
               build 04:17 + 10:17 · sweep 01:17 · embed 06:00 · drain 09:30 UTC ; CI on push/PR/weekly
```

---

## 3. What lives where (repo map)

| Path | Role |
|---|---|
| `src/ergon/models.py` | The `JobPosting` contract + `SearchQuery.matches()` client filter |
| `src/ergon/{client,engine,sync}.py` | Async/sync entry + `run_search` orchestrator (index fast-path + live fan-out) |
| `src/ergon/{ranking,dedup,canonicalize}.py` | BM25F rank · cross-source merge · Company rollup |
| `src/ergon/{http,crawl_pool}.py` | `AsyncFetcher` (rate/breaker/budget) · bounded worker pool |
| `src/ergon/providers/` (54) | The ATS moat. `base.py` = Protocol + registry |
| `src/ergon/extract/` (15) + `enrich.py` | Field extraction (deterministic-first) |
| `src/ergon/semantic.py` | Query-time reranker — **wired**: engine.py:178, resume.py:68, index/router.py:10, index/rich.py:240 |
| `src/ergon/extract/sector_clf.py` | **Deliberately unwired** — held-out acc 29.8% vs the gazetteer's 72.4%; its own header forbids wiring it |
| `src/ergon/index/` (18) | Build, schema, gates, freshness, delta, sidecars, client cache |
| `src/ergon/serve/query_app.py` | HTTP QUERY surface (ETag/304, cache, single-flight) |
| `src/ergon/{cli,mcp_server}.py` | CLI (6 cmds) · MCP (9 tools) |
| `src/ergon/registry/` + `data/` | The crawl universe + gold data assets |
| `scripts/` (~90) | ~10 on the automated path; rest = one-off discovery/coverage tooling |
| `.github/workflows/` (7) | The freshness automation loop |
| `tests/` (232, ~1,994 fns) + `tests/fixtures/` | Ratcheting recall/precision gates + parity + corpora |

---

## 4. Services we offer (surfaces × audience)

| Surface | Entry | Audience | Notes |
|---|---|---|---|
| **Python SDK** | `from ergon import search` / `AsyncErgon` | app/pipeline devs | sync + async; `to_pandas/to_polars` |
| **CLI** | `ergon {search,match-resume,resolve,sources,sponsors,version}` | terminal users | ~25 search flags; no `--max-degree` (MCP has it) |
| **MCP server** | `ergon-mcp` (stdio, 9 tools) | AI agents / Claude | search_jobs, whats_new, match_resume, assess_fit, h1b_jobs, list_companies… |
| **HTTP QUERY** | `serve.serve()` → `QUERY /jobs` | backend/agent fleets | ETag/304, cache, single-flight — **undocumented + no console script** |
| **Prebuilt index** | auto-download to `~/.cache/ergon` | all broad-search consumers | delta/chain updates, slim + per-sector shards, sha256-verified |

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

### Index scale & coverage (live `coverage.json`, build-2026-09-08-184)
- **1,323,067 active jobs · 40,011 companies · 42 providers with rows · 58,078-board registry**
  (54 providers are *registered*; 12 contribute no rows — the 8 aggregator/keyed sources plus
  dayforce, paycom, tesla and paylocity, the last of which has no registry boards at all)
- Salary disclosed 463,456 · JD text 1,235,643
- **JD capture is no longer the ceiling: 93.4% of active rows now carry JD text**, up from ~19% on
  2026-07-20. The Tier-3 drain closed it. The ceiling is now crawl freshness, not JD coverage.
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
| 01:17 | freshness-sweep (20-shard, own group→parallel) | freshness sidecar (checks BOARDS not postings, ~25× cheaper) |
| 09:30 | drain-detail (20-shard) — **scheduled, no longer manual-only** | Tier-3 JD detail sidecar |
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
1. ~~**JD-text capture (81% gap)**~~ — **RESOLVED 2026-09.** JD coverage is 93.4% of active rows;
   the Tier-3 drain now runs on a 09:30 cron. This is no longer the ceiling.
2. ~~**Wire the two stranded ML components**~~ — **THIS RECOMMENDATION WAS WRONG. Do not act on it.**
   - `semantic.py` was never stranded: it is wired at *query* time (`engine.py:178`, `resume.py:68`,
     `index/router.py:10`, `index/rich.py:240`). It is a reranker; `enrich_in_place` is the wrong
     place for it, so "not called by enrich" was true but not a defect.
   - `sector_clf.py` is a **documented no-go**, not an oversight. Held-out 5-fold CV: 29.8% accuracy
     / macro-F1 0.175, against the gazetteer's 72.4%. Its own header (`sector_clf.py:6-20`) is titled
     "WHY THIS CLASSIFIER IS INTENTIONALLY NOT WIRED" and states that wiring it "would stamp ~360k
     WRONG sectors onto the index". The model artifact is gitignored and does not ship.
   The 36% sector-coverage gap is real; this is not its fix.
3. **Registry `domain` backfill (1.3% → ~100%)** ⟵ *lane 7.* The resolver's domain lookup is inert
   for 98.7% of the registry; cheap high-value fill.
4. **SEC longtail ~6,012** ⟵ *lane 7.* Dominant unbuilt lever = vanity-domain ATS detection
   (content-probe resolver). *Correction:* ADP, Paylocity, Dayforce and Phenom are all **already
   built and registered** — phenom alone contributes ~22k rows. The gap is registry coverage, not
   missing adapters (paylocity has 0 registry boards; dayforce 18; paycom 14).

### B. Highest-leverage OPTIMIZATION
5. **Pre-`matches()` enrichment waste** ⟵ *lane 1.* `enrich_in_place` runs on every fetched record
   before the keyword filter drops non-matches — pre-filter title/company first to cut enrich cost.
6. **No pagination** ⟵ *lane 3.* `query.py` is LIMIT-only (no OFFSET/cursor) — a functional gap for
   any UI wanting page 2.
7. **Consolidate the 5 copy-paste tier caches** ⟵ *lane 3.* `IndexCache`, `SlimCache`, `RichCache`,
   `DetailCache`, `ShardCache` (the map said 4). Only `IndexCache` has delta support; the other four
   full-download every build.
8. **Ship serve/ as a real surface** ⟵ *lane 5.* A production-shaped HTTP QUERY server with zero
   docs and no console script. Add `ergon-serve` + README section (lowest-effort win).

### C. RELIABILITY / STRESS-TEST
9. ~~**No alerting anywhere**~~ — **BUILT 2026-07.** `scripts/notify_ops.py` opens/updates a
   deduplicated GitHub issue, wired into `build-index.yml` and `freshness-sweep.yml`. The remaining
   gap is not emission but *reception*: six ops-alert issues (#17–#22) have been open and unread
   since 2026-07-21, and all alerting is in-band, so a cron that never runs emits nothing at all.
10. **No CI gate on the big product metrics** ⟵ *lane 8.* JD-capture %, freshness %, delta skip-rate
    (the biggest levers) are measured in one-off scripts, not ratcheting tests.
11. **The `fetch_detail` None-vs-raise contract** ⟵ *lanes 2,3.* Still the highest-consequence
    correctness surface, and **confirmed violated**: `join.py:308`, `jobvite.py:165` and
    `workable.py:298` do `except Exception: return None`, so a transient timeout reads as "dead".
    All three are in `CONFIRM_VIA_DETAIL_SOURCES`, where one `None` expires the row — 110,693 rows
    (7.4% of the index) are exposed. `liveness.py` also lacks the empty-board valve `freshness.py:521`
    has. And no gate can catch it: `gates.py:104` counts *all* rows including expired, so a mass
    expiry leaves `COUNT(*)` unchanged.
12. **Hand-synced source lists** ⟵ *lanes 2,3.* `CONFIRM_VIA_DETAIL_SOURCES`, `_TIER3_DETAIL_SOURCES`,
    `_LOCATION_CAPABLE_SOURCES`, `fetch_detail` overrides — 3–4 enumerations of "sources with detail",
    synced by hand, no binding assertion. Drift silently drops confirm coverage.
13. **Delta-crawl at scale** ⟵ *lane 3.* Just flipped ON (2026-07-20); ramp + parity must be watched.

### D. HYGIENE / DEBT
14. Stale headline metrics (INDEX_STATUS pinned to build-66; README counts hand-maintained) — no
    single source of truth for live counts ⟵ *lanes 5,8.*
15. Not on PyPI (v0.1.0) — every install is a git clone; the pip-extras story is unexercised ⟵ *lane 5.*
    This also blocks the MCP registry, which requires a public install path and a unique, immutable
    version string per publication.
16. 93 scripts, mostly dormant one-offs. *Correction:* the "committed JSON state" claim was never
    accurate — `scripts/candidates_dead.json` is gitignored (`.gitignore` `scripts/candidates_*.json`)
    and has never been committed. The whole tracked tree is 24 MB / 678 files, dominated by legitimate
    package data (`seed.json` 5.3 MB, `h1b_sponsors.json` 4.0 MB) and test corpora.
17. `count_sanity_check` built but not wired into `run_search`; `corrections.jsonl` empty (human-
    correction loop unused) ⟵ *lanes 1,8.*

---

*Full per-lane detail in the review scratchpad (lane1–lane8).*

---

## 9. Corrections log

| Date | Change |
|---|---|
| 2026-09-09 | §8.2 retracted — `semantic.py` is wired; `sector_clf.py` is a documented no-go (29.8% held-out). §8.1 and §8.9 marked resolved. §8.16 struck as never accurate. §8.4, §8.7, §8.11 corrected. §1/§5 scale and §6 cron times refreshed against live data. |

**Unresolved and not visible in this map:** the 04:17 non-join crawl has failed to publish every day
since 2026-07-28. Its `jd_coverage` gate is evaluated on `db_tmp` at `build_index.py:2413`, but JD
text is merged in at `:2536` — after the gate — so a full re-crawl collapses pre-merge coverage by
construction and can never satisfy the gate. The only builds that publish are join-only
carry-forwards refreshing ~0.13% of the registry per day.
