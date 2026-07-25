# Pipeline Restructuring Implementation Plan

> **For agentic workers:** Each item below is executed by a specialized agent in an isolated git worktree, TDD-style, and gated + parity-tested before merge. The senior orchestrator reviews every diff. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove the accidental complexity the 5-lane audit measured — carry-forward staleness (rooted in join.com = 34% of the registry @ 0.58 boards/s), discard-after-extract (full JD stored nowhere → extractors can't re-run), and the duplicate detail path — while keeping the *sound* core (per-ATS providers, the forced crawl/drain split, the concurrency model).

**Architecture:** A "list-first, recover-later, carry-forward" pipeline. We are NOT rewriting it — we are (a) isolating the one pathological host so ~66% of the registry crawls fully daily, (b) persisting the full JD so extraction is replayable, (c) versioning enrichment so a fix propagates, (d) deleting the duplicate/3×-republish plumbing. Every change ships behind a parity gate.

**Tech Stack:** Python 3.10+, anyio, sqlite (FTS5), httpx/curl_cffi, GitHub Actions.

## Global Constraints (apply to EVERY task, verbatim)

- **PARITY GATE (non-negotiable):** every change ships with a test proving the new pipeline produces a **byte-identical `jobs` table to the old EXCEPT the intended delta**, on REAL boards (the delta-crawl parity tests are the template: `tests/test_delta_crawl_skip.py`, `tests/test_index_matches_parity.py`). No architectural change merges without one.
- **Gate every task:** `python -m pytest -q` + `ruff check src tests scripts` + `mypy src/ergon_tracker` all green. Real tests, not only synthetic.
- **Stress-test the concurrency:** any change to the crawl/detail/publish paths must be validated under the real concurrent worker pool (not just single-threaded), and any new I/O must `await` the shared fetcher (no blocking calls, no per-call clients, stateless providers).
- **Clean, no-fluff, organized code** matching the surrounding module's idiom; DRY; YAGNI; frequent commits.
- **No assumptions — measure.** Verify claims against the real index (`scratchpad/index.sqlite`) or live probing before asserting. (The audit lesson: probe, don't assume.)
- **Isolation:** each item runs in its own git worktree; merges are sequential; the orchestrator resolves conflicts + reviews.
- **Ships dark where risky:** structural changes (join isolation, persist-JD, unify) ship behind a flag with flag-off = byte-identical to today, validated by the parity gate, then flipped.

---

## Dependency & conflict graph (decides parallelism)

| Item | Core files | Depends on | Conflicts-with (same file region) |
|---|---|---|---|
| 1 Isolate join | `build_index.py:_registry_window`, `build-index.yml` | — | 4,6 (build-index.yml); 5 (crawl) |
| 2 Persist full JD | `schema.sql`, `mapping.py:to_row`, new JD sidecar, `build.py`, `build_index.py:publish` | — | 8 (to_row/schema); 3 (mapping) |
| 3 Re-enrich trigger | `mapping.py:enrich_hash`, `build_index.py` enrich-reuse | **2** (retroactive needs stored JD) | 2,8 (mapping) |
| 4 Collapse detail path | `build-index.yml` (ERGON_DETAIL_MAX→0), `build_index.py` detail | 1 (yml) | 1,6 (yml) |
| 5 Unify sweep+crawl | `freshness.py`, `build_index.py` crawl, workflows | 1 | 1,4 (crawl) |
| 6 Publish once/atomic | `build_index.py:_gated_publish`+publish, `build-index.yml`, `cache.py` | 1,4 (yml) | 1,4 (yml) |
| 7 Workable ?details | `providers/workable.py` (+ its detail-source lists) | — | **NONE — isolated** |
| 8 Metadata completeness | `enrich.py`, `mapping.py:to_row`, `schema.sql`, `models.py` | 2 (mapping/schema) | 2,3 (mapping/schema) |

**Parallelizable NOW (Phase 1):** 1, 2, 7 — different file regions, no logical dependency. ✅ **DONE — merged to main** (Item 7 = 54614da, Item 1 = 807c0a5, Item 2 = 48b5a13).
**Sequenced (Phase 2, after 1+2 merge):** 3 (after 2), 4 (after 1), 8 (after 2). ✅ **DONE — merged to main** (Item 4 = 6e80ffa, Item 8 = 0c41f78, Item 3 = 4a5e1af; cross-item fix: employment_type added to _REENRICH_COLS).
**Final (Phase 3):** 6 (after 1+4), 5 (biggest; after 1 — supersedes the window for non-join). ✅ **Item 6 DONE — merged to main** (ff4cc02; ships dark behind ERGON_VERIFY_SET_MANIFEST). ✅ **Item 5 DONE — reframed by 3-agent recon + closed at A.**

### Item 5 outcome (reframed by measurement, 2026-07-24/25)
Recon proved the plan's premise stale: `ERGON_DELTA_CRAWL` is already ON in prod + primary (~87.8% deterministic skip), so "unify walks / make delta primary" was already shipped. The evidence redirected Item 5 to a **correctness** fix — the already-on skip is membership-only and missed in-place EDITs:
- **A-1 (merged 33ca4bb):** reverse ETag/id-set precedence so the 4 full-body-validator providers (lever/ashby/teamtailor/personio) use their edit-safe 304 instead of the membership-only skip. Never-worse-than-today.
- **A-2 (merged d85ae77):** fold per-posting `updated_at` into the fingerprint for greenhouse+recruitee (proven-stable; 11 others left id-only with evidence). Ships DARK behind `ERGON_DELTA_CONTENT_VERSION` (default OFF). Adversarially verified (2 agents): cross-side hash agreement + flag-off byte-identity + pure-ids membership all confirmed. Fixed a one-sided-flip footgun.
- **B (NOT shipped — deliberate, evidence-based):** `board_count`-driven skip for search-index sources (Workday ~37%) is UNSAFE — a same page-1 count can't rule out add+remove churn that hides a departure (reshuffling lists have no id-set hash to catch it) ⇒ risk of serving an EXPIRED-live posting (cardinal sin). `board_count` is a change-candidate, never an unchanged-proof (R2's rule). Closed at A per user decision. Only safe uses would be positive-prioritization (low value) or bounded-staleness sweep deferral (accepts an expired-live latency regression) — both declined.

**Ships-dark flags now in the code, pending a deliberate production flip after a validation build:** `ERGON_VERIFY_SET_MANIFEST` (Item 6, SDK torn-set rejection) + `ERGON_DELTA_CONTENT_VERSION` (A-2, must be flipped LOCKSTEP in build-index.yml AND freshness-sweep.yml).

---

# PHASE 1 (parallel — 3 isolated tracks)

## Item 1 — Isolate join into its own shard/cadence  ⭐ highest leverage

**Goal:** Stop one host (join.com, 34% of the registry @ 0.58 boards/s) from forcing the 12k window on everyone → the other ~66% (~38k boards) crawls FULLY every day → dejobs/jazzhr recover in 1 build not 5.

**Files:**
- Modify: `scripts/build_index.py:_registry_window` (~1046) — carve join out of the rotating slice; add a `--exclude-source`/`--only-source` or a registry partition.
- Modify: `.github/workflows/build-index.yml` — the daily build crawls the non-join registry in FULL (`--limit-companies` ≥ 38k for the non-join set), with join on its own rotating cadence/shard (a dedicated `--only-source join` step or a small join-shard matrix, deadline-boxed).
- Fix the live contradiction: `build_index.py:1339-1343` comment (host_budget default 0) vs `build-index.yml:166` (override 1800) — reconcile so join's boxing is intentional + documented, not silently dropping coverage.
- Test: `tests/test_build_index_script.py` (window partition), a parity test that the non-join crawl covers 100% of non-join boards.

**Approach:** Partition the registry by source: join (and any other >X-boards/slow host) → a separate crawl unit with its own budget/cadence; everything else → one full daily crawl. Measure the non-join full-crawl wall-clock (audit says ~38k @ ~5 boards/s ≈ well under the ceiling) and confirm it fits the 330-min timeout with headroom.

**Parity + stress gate:** parity — a build with the partition ON produces the same `jobs` rows as a full crawl for the non-join boards; join boards still covered over their cadence. Stress — run the real concurrent pool over the non-join set, confirm wall-clock < timeout and no per-host breaker trips.

**Interfaces produced:** `_registry_window(..., exclude_sources=..., only_sources=...)` or equivalent partition API (Items 4/5 build on the crawl orchestration).

## Item 2 — Persist the full JD (compressed sidecar)  ⭐ biggest product unlock

**Goal:** Stop discard-after-extract. Store the full JD (compressed) so extraction is **replayable without re-crawl** → retroactive re-enrichment (Item 3) + un-caps every JD-derived field.

**Files:**
- Create: a JD store — either a new `index-jd.sqlite` sidecar (`id → compressed full JD`) mirroring the detail/rich sidecar pattern (`src/ergon_tracker/index/jd_store.py`), OR a compressed `description_full` blob column. **Decision: a separate compressed sidecar** (keeps the core index small; the SDK/rich already handle sidecars; avoids bloating the ~hundreds-of-MB core). Confirm size impact by measuring real JD lengths.
- Modify: `scripts/build_index.py` publish path — write + publish the JD sidecar (like `write_fresh_rich`), carry-forward tolerant.
- Modify: `.github/workflows/build-index.yml` — download/publish the JD sidecar.
- Test: `tests/test_jd_store.py` — a fetched JD round-trips (compress→store→read) identical; carry-forward preserves it; absent sidecar is non-fatal.

**Approach:** On crawl/detail, when a full JD is available (`description_text`/`description_html`), gzip-store it in the JD sidecar keyed by `id`. Core index keeps the 300-char snippet (unchanged — no core-schema churn). The JD sidecar is the replay source for Item 3.

**Parity + stress gate:** parity — the core `jobs` table is byte-identical with the JD-store change on (it only ADDS a sidecar). Stress — measure the sidecar size on the real 1M-JD corpus (compressed) + the write throughput under the concurrent pool.

**Interfaces produced:** `jd_store.put(id, jd) / jd_store.get(id) -> str|None` (Item 3 consumes `get`).

## Item 7 — Workable `?details=true` (folds 67k JDs into the call we already make)

**Goal:** Workable is list-only BY ACCIDENT — the bulk call omits `?details=true`. Adding it returns every JD in the ONE call we already make, eliminating workable's drain. (workable.py's own docstring already documents this.)

**Files:**
- Modify: `src/ergon_tracker/providers/workable.py` — `fetch()` adds `?details=true`; `normalize()` captures the `description`. Honor the per-board dedup the docstring notes (first posting pays, siblings reuse by shortcode).
- Modify: `scripts/build_index.py:_TIER3_DETAIL_SOURCES` + `liveness.CONFIRM_VIA_DETAIL_SOURCES` — remove workable if its JD now comes from bulk (or keep as fallback — verify).
- Test: `tests/test_workable.py` — fetch with `?details=true` returns JD inline; normalize populates description.

**Parity + stress gate:** parity — workable rows now have JD from bulk (measure no-JD drop for workable). Stress — confirm the single `?details=true` call isn't materially slower / rate-heavier than the current call (it's the same endpoint).

**Interfaces produced:** none (isolated).

---

# PHASE 2 (after Phase 1 merges — sequenced on real dependencies)

## Item 3 — Re-enrich trigger (depends on Item 2)
**Goal:** Improving an extractor propagates to ALL rows. **Files:** `mapping.py:enrich_hash` (add a `NORMALIZER_VERSION`/`ENRICH_VERSION` component so a version bump invalidates the reuse-skip), `build_index.py` enrich-reuse (on version mismatch, re-enrich from the Item-2 JD sidecar without re-crawl). **Parity gate:** flag-off byte-identical; a version bump re-enriches every row from the stored JD, matching a full re-enrich.

## Item 4 — Collapse duplicate detail path (depends on Item 1's yml)
**Goal:** Daily inline `--detail` → merge-only (drain owns fetching) → shorter builds, kills "one build later." **Files:** `build-index.yml` (ERGON_DETAIL_MAX→0/small), `build_index.py` (keep the unconditional merge). **Parity gate:** the merge-and-republish still runs; no coverage loss (the sharded drain already fetches). **Stress:** measure daily build wall-clock drop.

## Item 8 — Metadata completeness (depends on Item 2's mapping/schema)
**Goal:** Capture what we drop. **Files:** `enrich.py` (add an employment_type extractor; wire the existing `extract/skills.py`), `mapping.py:to_row` (store `locations[1:]` + `region`; stop dup'ing listing_url), `schema.sql`/`models.py` (drop dead `salary_annual`/`closes_at` OR populate them; add job_tags population). **Parity gate:** existing columns unchanged; new fields additive.

---

# PHASE 3 (final)

## Item 6 — Publish core once + atomic release swap (after 1,4)
**Goal:** No 3× re-gzip, no torn reads, no cancellation footgun. **Files:** `build_index.py` (fold `--detail`/`--liveness` reconcile before a SINGLE final `publish_artifacts`), `build-index.yml` (single atomic upload; a top-level manifest naming all asset shas), `cache.py` (SDK rejects a torn set). **Parity gate:** published artifacts identical to today's final state, just written once.

## Item 5 — Unify freshness-sweep + crawl (biggest; after 1)
**Goal:** One registry walk. The sweep already checks membership daily for all boards → make it DRIVE the crawl (fetch+normalize only boards whose membership moved; carry forward the rest with confidence). Delta becomes the primary path, not a dark 10%-effective flag. **Files:** `freshness.py`, `build_index.py` crawl, `build-index.yml` + `freshness-sweep.yml`. **Parity gate:** the membership-driven crawl produces the same `jobs` table as the full crawl on real boards. **This is the largest change — own phase, own review.**

---

## Self-review notes
- Every roadmap item (1-8) has a task. ✅
- Every task has a PARITY gate + a concurrency/stress check. ✅
- Dependency graph enforced: 3←2, 4←1, 8←2, 6←1,4, 5←1. Phase 1 = {1,2,7} truly independent. ✅
- No placeholders: each item names exact files + the approach + the gate. The executing agents produce the bite-sized TDD steps within each item (write parity test → implement → gate → stress-test).
