# Search Index — M2 (Smart-Tiered Incremental Crawl) Implementation Plan

> **For agentic workers:** implement task-by-task (TDD). Builds on M1. Spec:
> `docs/superpowers/specs/2026-06-18-search-index-design.md` §6.

**Goal:** Make the *crawl side* throttle-proof: crawl only the boards due today (tiered by change
frequency), carry forward everything else from the previous snapshot, skip unchanged boards via
conditional requests, and back off hosts that push back — so a daily build touches a fraction of
the 46k boards instead of all of them, while the published index stays current.

**Architecture:** A persisted `board_state` (one row per board) drives an adaptive scheduler
(hot/warm/cold/quarantine). The incremental builder loads the previous index + state, fetches
only due boards (conditionally), carries forward the rest, recomputes tiers from outcomes, and
republishes. All scheduler logic is pure + offline-tested; the crawl wiring reuses providers.

**Tech Stack:** Python stdlib, existing `AsyncFetcher` (now used for conditional GET + 304),
pydantic, pytest. GitHub Actions for the daily cron.

---

### Task 1: `BoardState` model + persistence
- Create `src/ergon/index/scheduler.py` (`BoardState` dataclass/pydantic; `load_state`/`save_state` JSON).
- Test `tests/test_index_scheduler.py`: round-trip load/save; tolerant of missing file (empty).

### Task 2: Tier policy (pure)
- `assign_tier(state, today) -> str` in scheduler.py: hot (changed ≤ HOT_DAYS), warm (≤ WARM_DAYS),
  cold (else / repeated 304), quarantine (consecutive_errors ≥ ERR_MAX or throttle_score ≥ THROTTLE_MAX).
- Tests: each tier boundary; quarantine on errors/throttle.

### Task 3: Due-board selection (pure)
- `due_boards(states, today) -> list[key]`: all `hot`; `warm`/`cold` whose `next_due ≤ today`;
  never `quarantine` (until cooldown elapsed). `compute_next_due(tier, today)` per cadence.
- Tests: hot always due; cold not due until interval; quarantine skipped; cooldown re-admits.

### Task 4: Throttle back-pressure (pure)
- `apply_outcome(state, *, changed, error, http_429, today)`: updates last_crawled/last_changed,
  consecutive_unchanged/errors, throttle_score (EWMA of 429-rate), then re-tiers + next_due.
- Tests: rising 429s → quarantine/demote; a change → hot + reset counters; clean unchanged → demote toward cold.

### Task 5: Real `content_hash`
- Add `content_hash(job) -> str` (sha1 of normalized title|company|location|salary) in mapping.py;
  use it in `to_row` (replaces M1's `id` placeholder). Enables change detection + future deltas.
- Tests: stable for same content; differs when title/salary changes.

### Task 6: Incremental build (carry-forward + conditional)
- `build_index_incremental(prev_db, board_state, due_fetch_fn, path, build_id)` in build.py:
  load prior jobs; for due boards use freshly-fetched jobs (304 → carry prior); non-due → carry prior;
  expire jobs gone from a crawled board; dedup union → write; update + return new board_state.
- Conditional GET helper in scheduler/crawl: send `If-None-Match`/`If-Modified-Since`; treat 304 as carry.
- Tests (offline, fake fetchers): 304 carries prior; 200-changed updates; gone-from-board expires;
  non-due carried; resumable checkpoint reused from M1 census pattern.

### Task 7: Wire into `build_index.py`
- Add `--incremental` (load prev index + state from `dist/`/Release) vs cold full crawl; persist state.
- Test: pure plumbing helper (state path resolution) offline.

### Task 8: GitHub Action (daily cron)
- `.github/workflows/build-index.yml`: daily `schedule:` cron, public-repo runner, `uv` install,
  download prev artifacts, run `build_index.py --incremental`, run gates, upload Release asset +
  manifest. (Smoke: `act`-style dry validation or a lint of the YAML; real run on push.)

### Task 9: Live dogfood (mandatory)
- Run an incremental build over a tier twice; confirm 2nd run fetches far fewer boards (304s/carry),
  index stays correct, throttle_score sane. Log to `INDEX_RUNS.md`.

---

**Self-review:** scheduler logic (T1-4) pure + offline-tested; content_hash (T5) feeds change
detection; incremental build (T6) is the throttle-proofing; GH Action (T8) automates it; dogfood
(T9) proves fewer fetches on re-run. Defer to M3: full data-quality gate suite + observability
history + status surface. Defer to v2: sector shards + deltas.
