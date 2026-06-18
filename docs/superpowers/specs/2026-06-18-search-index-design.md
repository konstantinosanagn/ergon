# Design: Broad-Discovery Search Index (ergon-tracker)

**Date:** 2026-06-18
**Status:** Approved design → implementation plan next
**Topic:** A self-updating, free, locally-queried search index that makes *broad discovery*
across all ATS sources fast — without users getting throttled.

---

## 1. Problem & decision

`ergon-tracker` fetches live from 31 providers. Only **4** (adzuna, smartrecruiters, usajobs,
workday) support server-side keyword search; the other **27** (Greenhouse/Lever/Ashby/…) have
no keyword API, so a *broad* query ("all senior backend H-1B jobs anywhere") must fetch-then-
filter thousands of boards. From a user's IP that is slow and gets throttled (boards on a shared
host — e.g. `boards.greenhouse.io` — funnel through one rate limiter). Targeted/company queries
are already fast and are **not** the problem.

**Decision:** build a search index. One **polite daily CI crawler** touches the ATSes; users
query a **local index file** and never touch the ATSes for broad discovery. This *is* the
"accessible without throttling" fix.

### Decisions locked during brainstorming
- **Primary use:** broad discovery (the slow path). Targeted stays live.
- **Freshness:** daily (~24h). Industry-normal; jobs rarely change intraday; we re-verify on click.
- **Hosting:** **free-CI snapshot** — a scheduled GitHub Action (free on a public repo) builds a
  compressed index and publishes it as a GitHub Release asset; the SDK downloads + queries it
  locally. ≈ $0, no server, one crawl serves everyone.
- **Coverage:** full registry, **smart-tiered** (hot/cold by change frequency; sector as a
  first-class column / future shard key).
- **Approach:** ship **A** (single SQLite/FTS5 file) on an isolated `IndexBackend` interface,
  architected so **B** (sector shards + daily deltas) is a clean swap with no caller changes.

### Goals
- Fast broad discovery (one local indexed read, not thousands of live fetches).
- Zero ATS contact from user machines for broad queries.
- Free to operate; reproducible; auditable; never serves a bad index; never hard-fails.

### Non-goals (v1)
- Hosted query API / claude.ai web connector (deferred; ruled out by free-CI choice).
- Sub-daily freshness; sector sharding + deltas (B, fast-follow); skills extraction (hook only).

---

## 2. Architecture (two planes)

```
BUILD PLANE   (GitHub Actions — scheduled daily; free on a public repo)
  registry (46k) ─► Crawler (tiered/incremental/conditional/polite) ─► provider.normalize
       ─► dedup ─► enrich (level/geo/comp/sector/visa/sponsorship) ─► Index Builder
       ─► index.sqlite (FTS5 + filter columns + lifecycle + companies) ─► validate (gates)
       ─► gzip ─► GitHub Release asset  index-YYYY-MM-DD.sqlite.gz  + manifest.json

QUERY PLANE   (SDK / CLI / MCP — per user; ZERO ATS contact for broad queries)
  broad query ─► IndexBackend ─► IndexCache.ensure_fresh (manifest TTL, sha256, atomic swap)
                              ─► SQLite FTS5 MATCH + WHERE  ─► JobPosting[]  (deduped/enriched/ranked)
                                  ∪ live(adzuna,usajobs) merged + deduped   (keyed search APIs)
  targeted query (companies=/sources=) ─► live engine (unchanged)  ◄─ also the fallback
```

- The crawler **reuses the live engine** (fan-out → `normalize` → `dedup` → `enrich`); it writes
  rows instead of returning. ~80% existing code. No new scraping surface.
- The build plane is the *only* thing that touches ATSes — once/day, politely, rotating CI IPs.

---

## 3. Canonical models & canonicalizers

The index persists the **same canonical objects** the live engine already produces — no second
normalization path (no drift).

**Reused as-is:** `JobPosting` (+ `Location`, `Salary`, `Provenance`, enums); `provider.normalize`;
`dedup.normalize_company` / `normalize_title` / `blocking_key` / `deduplicate`; `make_job_id`;
`extract/*` (geo/level/comp/yoe/sector/visa/sponsorship); `Salary.as_text`.

**Added:**
- **`Company`** model (`models.py`): `company_key, display_name, domain, primary_ats, board_token,
  sector, h1b_sponsor, h1b_last_filed, open_roles, first_seen, last_seen`.
- **Company canonicalizer/aggregator** (`canonicalize.py`): `aggregate_companies(jobs) → list[Company]`,
  keyed by the existing `normalize_company` (so `company_key` matches live dedup byte-for-byte).
- **`index/mapping.py`**: the single `to_row(JobPosting)->dict` / `from_row(row)->JobPosting`
  mapping (build + read share it; round-trip tested).
- **`role_family`** = `normalize_title()` surfaced as a stored column (no new logic).

---

## 4. Index schema (SQLite + FTS5)

DDL lives in a versioned `schema.sql` (single source of truth) + a data dictionary
(`docs/index-schema.md`). Dedup/enrich happen at build time → the index holds canonical,
already-deduped rows.

### `companies`
```sql
CREATE TABLE companies (
  company_key TEXT PRIMARY KEY, display_name TEXT, domain TEXT,
  primary_ats TEXT, board_token TEXT, sector TEXT,
  h1b_sponsor INTEGER, h1b_last_filed TEXT, open_roles INTEGER,
  first_seen TEXT, last_seen TEXT
);
```

### `jobs` (filterable + lifecycle + traceability + navigation)
```sql
CREATE TABLE jobs (
  rowid INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  company_key TEXT REFERENCES companies(company_key),
  source TEXT NOT NULL, company TEXT NOT NULL, company_domain TEXT,
  title TEXT NOT NULL, department TEXT, role_family TEXT,
  location TEXT, city TEXT, country TEXT,
  remote TEXT NOT NULL CHECK (remote IN ('onsite','hybrid','remote','unknown')),
  level TEXT NOT NULL CHECK (level IN ('intern','entry','junior','mid','senior','staff',
                                       'principal','lead','manager','director','executive','unknown')),
  employment_type TEXT NOT NULL, sector TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT, salary_annual REAL,
  years_min INTEGER, years_max INTEGER,
  visa_sponsor INTEGER CHECK (visa_sponsor IN (1)),
  visa_last_filed TEXT,
  sponsorship_offered INTEGER CHECK (sponsorship_offered IN (0,1)),
  apply_url TEXT, listing_url TEXT, board_token TEXT,
  posted_at TEXT, updated_at TEXT, closes_at TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired')),
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  expired_at TEXT, expiry_reason TEXT,
  fetched_at TEXT NOT NULL, build_id TEXT NOT NULL, snippet TEXT,
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max)
);
```

### `job_sources` (normalized 1:N provenance)
```sql
CREATE TABLE job_sources (
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  source TEXT NOT NULL, source_job_id TEXT NOT NULL, apply_url TEXT, fetched_at TEXT NOT NULL,
  PRIMARY KEY (job_id, source, source_job_id)
);
```

### `job_events` (lifecycle audit; reposting detection)
```sql
CREATE TABLE job_events (
  job_id TEXT, build_id TEXT, at TEXT,
  event TEXT CHECK (event IN ('appeared','changed','expired','reappeared'))
);
```

### `job_tags` (extensible; v1 empty — skills/tech hook)
```sql
CREATE TABLE job_tags (job_id TEXT, tag TEXT, kind TEXT);
```

### `crawl_health` + `meta`
- `crawl_health(build_id, provider, boards_ok, boards_failed, jobs_emitted, http_429,
  http_timeout, retries, circuit_breaker_trips, elapsed_ms, sample_errors)`.
- `meta` key/value manifest: `schema_version, build_id, git_commit, tool_version,
  crawl_started_at, crawl_ended_at, registry_size, registry_hash, row_count, h1b_index_date,
  tier_policy`.

### FTS + indexes
```sql
CREATE VIRTUAL TABLE jobs_fts USING fts5(
  title, company, department, snippet,
  content='jobs', content_rowid='rowid',
  tokenize="porter unicode61 remove_diacritics 2");
```
- Ranked by `bm25(jobs_fts, 10,3,3,1)` (title ≫ company/dept ≫ snippet).
- Single-column indexes on every filter; composites `(level,sector)`, `(country,remote)`,
  `(sector,posted_at)`, `(company_key,status)`, `(role_family,status)`; partial indexes
  `WHERE visa_sponsor=1`, `WHERE sponsorship_offered=1`, `WHERE status='active'`.
- Ship `ANALYZE` stats (`sqlite_stat1`).

### Constraints / integrity
NOT NULL + CHECK enums + UNIQUE(id) + FK(company_key, job_id) + salary ordering CHECK; build runs
`PRAGMA integrity_check` and fails loudly if not "ok".

---

## 5. Lifecycle & expiry (clean soft-delete → purge)

- Each build: a job on a **crawled** board now gone → `status='expired'`, `expired_at`,
  `expiry_reason='gone_from_board'`; `closes_at` past → `'past_closes_at'`; unseen > N days →
  `'unseen_Nd'`.
- **Tombstones retained `retention_days` (≈30)** so users can still see "closed on …", then
  **purged**. `companies.open_roles` counts only `active`. Queries default `status='active'`
  (`include_expired` opt-in).
- `job_events` records `appeared/changed/expired/reappeared` — an `expired`→`reappeared` pair is a
  **reposting** signal.

### Navigation unlocked (indexed)
`company_jobs(company_key)` (same company), `related_jobs(job)` (same `role_family` across
companies, or same company + role_family), same sector/city. Exposed as SDK helpers (and later
MCP tools).

---

## 6. Crawler & smart-tiered freshness (build plane)

State (previous index + `board_state`) persists between builds via Release assets / a state
branch — never committed to `main`.

### `board_state` scheduler
Per board: `provider, token, sector, last_crawled, last_changed, content_etag, last_modified,
consecutive_unchanged, consecutive_errors, throttle_score, tier, next_due`.

**Tiers (adaptive):** `hot` (changed in last few days → daily) · `warm` (~2 weeks → ~3 days) ·
`cold` (long-unchanged/tiny/repeated-304 → weekly) · `quarantine` (errors or rising
`throttle_score` → backed off hard).

### Incremental daily flow
1. Download previous index + `board_state`; compute **due** boards (`next_due ≤ today` + all `hot`).
2. Fetch due boards with **conditional requests** (`ETag`/`Last-Modified`):
   - **304** → carry forward prior jobs, bump `last_seen`, maybe demote tier.
   - **200** → normalize; `content_hash` diff → set `last_changed`, recompute tier; enrich only
     changed/new (enrichment cached by `content_hash`).
   - **error/429** → carry forward prior jobs (never lost), bump counters, maybe quarantine.
3. Not-due boards → carry forward unchanged (no `last_seen` bump).
4. Expiry (Section 5) → dedup union → build → gates → publish.

### Throttle back-pressure
Per-host `throttle_score` (429s ÷ requests) auto-demotes a pushing-back host's boards to a slower
tier next build — the crawl gets quieter exactly where it's rate-limited.

### Cold start
First build = full registry crawl, **checkpointed/resumable** (the hardened `_checkpoint_append`
pattern) so a CI timeout/kill resumes next run. Later builds are incremental.

### Orchestration
`scripts/build_index.py` + `.github/workflows/build-index.yml` (daily cron, public-repo free
runner). Honors existing `AsyncFetcher` politeness (per-host 5/s, Retry-After, circuit breaker).

---

## 7. Query layer & integration

### `IndexBackend` (isolation boundary; A→B swap)
```python
class IndexBackend(Protocol):
    def available(self) -> bool: ...
    def metadata(self) -> dict: ...
    def search(self, query: SearchQuery) -> list[JobPosting]: ...
```
- v1 `SqliteIndexBackend`; v2 `ShardedIndexBackend` (same interface).

### `IndexCache` (download/verify/freshness)
TTL-gated manifest check (≤ once/24h); download gz → **verify sha256** → decompress → atomic
rename into `~/.cache/ergon-tracker/index.sqlite`; **schema_version gate** → ignore + live
fallback on mismatch; open read-only (`query_only`, `mmap_size`, `cache_size`).

### Query execution (mirrors `matches()` semantics)
- Keyword → `jobs_fts MATCH` + WHERE + `ORDER BY bm25`. Filter-only → indexed `jobs` scan +
  `ORDER BY posted_at DESC`. Row→`JobPosting` with provenance join over the result set only.

### Routing in `run_search` (public API unchanged)
- `companies=`/`sources=` → **live**. Broad (`auto`) → **index** ∪ live(adzuna,usajobs) merged +
  deduped. Index missing/corrupt/schema-bumped/error → **live fallback** (never hard-fail).
- `prefer="auto"|"index"|"live"` knob; `ERGON_INDEX=off` forces live.

### Transparency
Results carry origin (`source: index|live|hybrid`, `index_date`); a `SourceHealth`-style entry
`index (built 2026-06-18, N rows)`; CLI shows snapshot date; `ergon-tracker index pull|status`.

---

## 8. Observability, logging, metrics, data-quality gates

Everything correlates on **`build_id`** (logs ↔ `crawl_health` ↔ `manifest` ↔ `git_commit`).

- **Structured logging:** JSON-lines `runs/index/<build_id>/build.jsonl`; levels DEBUG/INFO/
  WARNING/ERROR; **secret-redaction filter** scrubs `ADZUNA_*`/`USAJOBS_*`/`TAVILY_API_KEY`/
  `Authorization*` (artifacts ship publicly).
- **Per-stage + per-provider + per-host metrics:** stage timings/counts/drop-reasons; provider
  boards_ok/failed/304/jobs/429/timeouts/retries/breaker-trips/elapsed; per-host `throttle_score`.
- **Run artifacts:** `runs/index/<build_id>/` = build.jsonl, crawl_health.json, manifest.json,
  gates.json, samples/ (≤50 dropped records). Retention: last 14 build dirs + `history.jsonl`
  (one summary row/build, forever).
- **Drift detection:** today vs trailing median on rows / per-provider counts / dedup ratio /
  enrichment coverage.
- **Data-quality gates (good-or-nothing publish):** integrity_check/sha/row/dup/FK/CHECK;
  volume band vs median; high-volume provider at ~0; too many providers failed; enrichment-floor
  drop; null/enum sanity; schema match; staleness guard. A tripped gate → CI red, `latest`
  untouched (previous good snapshot stays live), `gates.json` records actual-vs-threshold.
- **Cost tracking:** CI minutes, HTTP requests, Tavily credits, artifact/storage sizes.
- **SDK logging:** `logging.getLogger("ergon_tracker.index")`, no handlers in lib; MCP surfaces
  `index_date`/`source`; telemetry local-only, off by default.
- **Status surface:** generated `INDEX_STATUS.md` from `history.jsonl`; optional auto-filed GitHub
  issue on gate failure.
- Slots into existing `runs/` + `finalize_run.py` + `RUNS.md` convention (new `runs/index/` +
  `INDEX_RUNS.md`).

---

## 9. Build hygiene & publishing

1. Build temp file from `schema.sql`; single-transaction bulk insert; `PRAGMA foreign_keys=ON`.
2. FTS `('optimize')` → `ANALYZE` → `PRAGMA optimize` → `VACUUM` → `PRAGMA integrity_check`.
3. Deterministic: stable order by `id`; only `build_id`/timestamps vary → reproducible, diff-able
   (ready for deltas).
4. Write `manifest.json` (sha256, bytes, row_count, schema_version, build_id); **atomic publish**
   (upload asset, then flip `latest` pointer). SDK verifies sha256 post-download.
- Size budget: snippet-not-JD + external-content FTS ≈ **350 MB raw / ~100 MB gzip** for ~1.4M
  rows; B adds sector shards + daily deltas to shrink per-query downloads.

---

## 10. Testing strategy

Offline + deterministic (fixtures, temp dirs, monkeypatched remote).

- **Anti-drift (key):** `matches()` ↔ SQL parity across all filters incl `include_unknown_*`;
  `from_row(to_row(j))==j`; `company_key==normalize_company`, `role_family==normalize_title`.
- **Build plane:** schema applied, FTS MATCH + bm25 ordering, constraints enforced, integrity ok,
  build-time dedup, `companies.open_roles`; lifecycle transitions + retention purge + reappear
  events; scheduler tier transitions / due-selection / 304 + error carry-forward / back-pressure /
  cold-start resume; each quality gate trips on bad data & passes on good; determinism (double
  build identical modulo build_id).
- **Query plane:** keyword + filter-only paths, every filter, limit/order, row→JobPosting +
  provenance, navigation helpers; IndexCache TTL/sha-reject/atomic/schema-mismatch/decompress;
  routing + live fallback + hybrid merge; `prefer`/`ERGON_INDEX=off`.
- **Observability:** build_id/stage in logs; secret redaction; history.jsonl + gates.json written.
- **E2E (offline):** build fixture index → publish to temp release → cache downloads+verifies →
  broad `run_search` returns ranked results → remove index → live fallback. Optional perf-sanity.

### Dogfood through the dev tools (mandatory — experience what users experience)
Automated offline tests prove correctness; they do **not** prove the *experience*. Every feature
must also be exercised **through the actual user-facing surfaces and against live data**, the way
a user/agent will:
- **SDK:** `from ergon_tracker import search` — broad query served from a real built index;
  inspect `source`/`index_date`, ranking, dedup, lifecycle fields, `related_jobs`/`company_jobs`.
- **CLI:** `ergon-tracker index pull|status`, then `search … ` (broad → index, targeted → live),
  confirm the table (incl. salary/sponsor columns), snapshot-date display, `--verbose` logs,
  `ERGON_INDEX=off` fallback.
- **MCP:** drive `search_jobs` / `list_h1b_sponsors` / `resolve_company` through an actual MCP
  client (stdio handshake), confirm `index_date`/`source` in responses and that an agent can do
  the international-student + résumé flows end-to-end.
- **Live build:** run a real (small-tier) `build_index.py` against live ATSes once, inspect the
  resulting SQLite (row counts, FTS queries, `crawl_health`, `throttle_score`, gates.json), and
  confirm a real broad query returns sane, ranked, deduped, freshly-dated results.

Each milestone (M1–M3) isn't "done" until it passes **both** the automated suite **and** a
manual dogfood pass through SDK + CLI + MCP on real data, with findings logged.

---

## 11. Phasing / milestones

1. **M1 — pipeline proof:** schema.sql + builder + mapping + Company canonicalizer; build #1 =
   full crawl on a tier → SQLite → gates → publish; `SqliteIndexBackend` + `IndexCache` + routing
   + live fallback. End-to-end offline test green.
2. **M2 — smart tiering:** `board_state` scheduler, conditional requests, carry-forward, throttle
   back-pressure, resumable cold start; full registry coverage.
3. **M3 — observability hardening:** full gate suite, history/status, drift detection.
4. **B (fast-follow) — sector shards + daily deltas** behind the same `IndexBackend`.

---

## 12. Risks & mitigations

- **ATS bans on the CI crawler IP** → conditional requests + tiering (low steady-state load) +
  per-host back-pressure + rotating GitHub runner IPs + circuit breaker. Measured via
  `throttle_score`.
- **Index size / download cost** → snippet-not-JD, external-content FTS, gzip; B's deltas +
  sector shards for steady-state.
- **Index/live drift** → single canonicalizer set + `matches()`↔SQL parity test.
- **Bad build shipped** → good-or-nothing gates + atomic `latest` flip + previous snapshot stays.
- **Free-tier overrun** → cost metrics trended; public-repo Actions are free; tiering caps work.
- **ToS** → only public ATS JSON (as today); index re-serves the same data we already fetch; no
  new scraping surface. (Keyed APIs Adzuna/USAJOBS stay live per their terms.)

---

## 13. New components (files)

- `src/ergon_tracker/models.py` — add `Company`.
- `src/ergon_tracker/canonicalize.py` — `aggregate_companies()`.
- `src/ergon_tracker/index/schema.sql`, `index/mapping.py`, `index/backend.py`
  (`IndexBackend`, `SqliteIndexBackend`), `index/cache.py` (`IndexCache`), `index/query.py`
  (SearchQuery→SQL).
- `scripts/build_index.py` (crawler/builder), `scripts/index_state.py` (board_state scheduler).
- `.github/workflows/build-index.yml` (daily cron).
- `docs/index-schema.md` (data dictionary), `INDEX_RUNS.md`/`INDEX_STATUS.md`.
- Engine routing in `engine.py`; CLI `index pull|status`; MCP `index_date`/`source` surfacing.

---

## 14. Open questions / future

- B specifics: shard granularity (sector vs sector+region), delta format (row-level vs SQLite
  page diff), shard routing for multi-sector queries.
- Skills/tech extraction → populate `job_tags` → "related by skill".
- Optional hosted API + claude.ai web connector (separate spec; only if demand).
