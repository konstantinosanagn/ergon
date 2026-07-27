# Publish / Durability Layer Redesign — R6 + R7 (north-star spec)

> ⚖️ **DESIGN / SPEC ONLY. No code in `src/`, `scripts/`, or `.github/workflows/` is changed by this
> document, and nothing here is deployed.** This is the "north-star" durability layer. Its primary
> deliverable is a rigorous, honest cost/benefit that decides whether each piece is worth building at
> all, given what already shipped (R1 disjoint-writers, the `manifest-set.json` torn-set verify, and
> the 210-min crawl-deadline). For two of the three items the answer is **"hold"** — the spec says so
> plainly and gives the trigger condition. The one item worth building **now** is the small companion
> the R6/R7 framing surfaced (a non-production publish channel), not R6 or R7 themselves.

**Date:** 2026-07-27
**Author:** architecture-review follow-up (items R6, R7 + the 2026-07-27 publish-regression incident)
**Scope:** the publish / release-asset / reader-download layer only. The crawl, extract, and gate
logic are unchanged. Everything here ships DARK behind a flag if built; flag-off is byte-identical to
today.

---

## 0. TL;DR verdict (read this first)

| Item | What it adds **beyond what shipped** | Marginal benefit / (effort + blast-radius) | Verdict |
|---|---|---|---|
| **Staging / `--no-publish` channel** (companion, §5) | Prevents an experimental/validation build from overwriting the production release — the *actual* cause of today's 76%→40% JD regression | **HIGH / LOW** | **BUILD NOW** |
| **R7** — durable fetch artifact + independently-retryable publish (§4) | Turns "publish step failed → re-run the whole crawl" into "re-run the 3-min assemble job". But the 210-min crawl-deadline already prevents the *only observed* data-loss, and the map/reduce workflow already implements this shape. | **LOW / MEDIUM** | **HOLD** — build when publish-side failures (not crawl timeouts) become the recurring loss mode |
| **R6** — content-addressed immutable assets + per-producer pointer flip (§3) | A structurally torn-read-proof swap. But `manifest-set.json` + `ERGON_VERIFY_SET_MANIFEST` (now default-ON) already reject torn reads, and R1 already makes cross-writer clobber impossible. R6 would **not** have prevented today's incident. | **LOW / HIGH** | **HOLD** — build only if we move off GitHub Releases, or torn-read incidents recur *despite* the set-manifest verify |

The rest of the document is the full design for each (so it is implementation-ready the day a trigger
fires), interleaved with the honest reason each trigger has **not** fired yet.

---

## 1. What already shipped, and therefore what R6/R7 must beat

R6 and R7 are not being designed in a vacuum. Three things landed in the last week that already buy
most of what the architecture review asked R6/R7 to buy. The marginal-value bar is set by these:

1. **R1 — single-writer-per-asset (merge `5071410`).** Every `index-latest` release asset has exactly
   one owning workflow: `build-index.yml` owns core/slim/shards/jd/liveness, `drain-detail.yml` owns
   `index-detail.*`, `embed-vectors.yml` owns `index-vectors.*`, `freshness-sweep.yml` owns
   `index-freshness.*`. A static test asserts the four workflows' upload sets are pairwise disjoint, so
   `gh release upload --clobber` can never destroy another producer's asset. **This is ~80% of R6's
   clobber-safety, already in production.** What R1 does *not* cover: a torn read *within* a single
   producer's multi-asset set (core + its manifest uploaded non-atomically).

2. **Torn-read mitigation — `manifest-set.json` + `ERGON_VERIFY_SET_MANIFEST` (Item 6, now default-ON).**
   The build uploads `manifest-set.json` **last**, after every other asset is fully up
   (`build-index.yml:374-379`). The SDK reader cross-checks the set manifest's recorded
   `index.sqlite.gz` sha against the `manifest.json` sha it already read (`cache.py:63-98`,
   `cache.py:287-289`); a disagreement means the set is mid-upload / internally inconsistent, and the
   reader falls back to its cached prior. An absent-or-incomplete set manifest is treated as consistent
   (older releases and forks are never penalised). **This is ~90% of R6's torn-read-safety, already in
   production**, and it was graduated to default-ON after a real build confirmed the set manifest lands
   cleanly (`cache.py:63-72`). What it does *not* give: it *detects* a torn set and falls back to
   stale; it does not let the reader *resolve the correct new set* during an in-flight upload. R6's
   immutability makes the torn read *structurally impossible* rather than *detected-and-avoided*. That
   is a real but small upgrade (see §3.7).

3. **Crawl-deadline — `ERGON_CRAWL_DEADLINE_S=12600` (210 min).** On 2026-07-25 the unbounded non-join
   crawl ran into the 350-min job timeout **mid-crawl** and published **nothing** — the single
   end-of-build publish never ran, losing the Item-2 JD sidecar (`build-index.yml:191-197`). The
   deadline caps the crawl so build + rich + publish always fit under `timeout-minutes: 350`
   (`build-index.yml:56-58`). A normal crawl finishes ~90 min, well under 210, so the daily full crawl
   is preserved. **This is the fix for the exact data-loss R7 targets — already in production.** What it
   does *not* cover: a failure *after* the crawl (in the build/gate/gzip/publish steps) still discards
   the crawl's fetched work, because `fresh.sqlite` lives only in the runner's ephemeral workspace.
   That residual gap is R7's real (small) territory.

> **The bar, stated precisely:** R6 must justify itself over "R1 + set-manifest verify"; R7 must
> justify itself over "the 210-min crawl-deadline". Neither gets credit for problems already solved.

### 1.1 The reader contract today (what R6 would change)

`src/ergon_tracker/index/cache.py` defines five downloader classes — `IndexCache` (core),
`SlimCache`, `RichCache` (vectors), `DetailCache`, `ShardCache`. Every one follows the **same
manifest-then-blob** pattern:

1. Fetch `manifest-{tier}.json` (`cache.py:274`, `:340`, `:390`, `:442`, `:504`).
2. Compare `schema_version` and `build_id` against the local cached manifest; if equal and the DB is on
   disk, it is already current (`cache.py:281-283`).
3. Fetch `{tier}.sqlite.gz`, gzip-decompress, **sha256-verify against the manifest's `sha256`**, reject
   on mismatch (`cache.py:302-304`, `:354-356`, …).
4. Write to a `.tmp` sibling and `tmp.replace(dst)` — **an atomic rename on the local filesystem**
   (`cache.py:305-307`). The *local cache* is already atomically swapped; the fragility is entirely on
   the *remote* release side, where "manifest" and "blob" are two separate GitHub Release assets with
   no atomic multi-asset publish.

The asset name is **fixed per tier** (`index.sqlite.gz`, `manifest.json`, …) and the `build_id` lives
*inside* the manifest JSON. R6's entire change is: make the **blob name carry the build_id** and make
the **manifest a tiny pointer** — inverting which of the two is the atomic commit point.

### 1.2 The writer contract today (the producers R6/R7 reorganise)

| Producer (workflow) | Owns assets | build_id source |
|---|---|---|
| `build-index.yml` (core build) | `index.sqlite.gz` + `manifest.json`, `index-slim.sqlite.gz` + `manifest-slim.json`, `shards/*.sqlite.gz` + `shards.json`, `index-jd.sqlite.gz` + `manifest-jd.json`, `index-liveness.sqlite.gz` + `manifest-liveness.json`, `manifest-set.json`, `manifest-delta.json` + `index-delta.sqlite.gz` + `deltas.json`, `board_state.json`, `crawl_cursor.json`, `history.jsonl`, `coverage.json`, `crawl-progress.json` | `_build_id()` = `build-<date>-<GITHUB_RUN_NUMBER\|HHMMSS>` (`scripts/build_index.py:313-324`) |
| `drain-detail.yml` (reduce) | `index-detail.sqlite.gz` + `manifest-detail.json` | build's build_id (carry-forward) |
| `embed-vectors.yml` (reduce) | `index-vectors.sqlite.gz` + `manifest-vectors.json` | build's build_id |
| `freshness-sweep.yml` (reduce) | `index-freshness.sqlite.gz` + `manifest-freshness.json` | build's build_id |

`build_id` is **already unique per run** (the CI run number is monotonic; `_build_id()` docstring,
`build_index.py:313-318`). This matters: R6's content-addressing needs a collision-free build key, and
we already have one. No new id scheme is required.

---

## 2. Naming the problem R6/R7 each actually solve

There are three distinct failure modes people conflate. Separating them is the whole analysis:

| Failure mode | Example | Already mitigated by | R6? | R7? |
|---|---|---|---|---|
| **Cross-writer clobber** — producer A's `--clobber` destroys producer B's asset | drain overwrites the core index | **R1 disjoint writers** (shipped) | redundant | no |
| **Torn read** — a reader downloads a *new* manifest against an *old* blob (or vice-versa) mid-upload | reader gets `manifest.json` for build-77 but `index.sqlite.gz` still build-76 | **`manifest-set.json` verify** (shipped, default-ON) | **yes — makes it structurally impossible** | no |
| **Fetched-work loss** — a crash *after* the crawl but *before* publish discards the crawl's output | build/gzip/publish step fails; the 90-min crawl is gone | **crawl-deadline** (prevents the *timeout* variant only) | no | **yes — makes the crawl a durable artifact** |
| **Wrong-but-valid publish** — a build that passes all gates publishes a *worse* index over a better one | today's incident: experimental full-crawl (higher rows, 40% JD) overwrites the daily build (76% JD) | **nothing** | **NO — immutability doesn't stop a valid-but-worse pointer flip** | no |

The fourth row is the one that actually bit us today (§5). **Neither R6 nor R7 addresses it.** That is
the single most important honest finding in this document.

---

## 3. R6 — content-addressed immutable assets + per-producer pointer flip

### 3.1 The design in one sentence

Each producer publishes its output blob under a **build-addressed, immutable** name
(`index-{build_id}.sqlite.gz`), and then, **as its last action**, flips a **tiny per-producer pointer
file** (`latest-core.json`) to name that build. Readers read the pointer, then fetch the immutable blob
it names. The pointer flip is a single-asset upload — the one atomic commit — and because the blob it
newly names is immutable and was fully uploaded *before* the flip, a torn read is structurally
impossible: any pointer a reader sees names a blob that already fully exists.

### 3.2 Naming scheme

For every tier `T` in {`core`, `slim`, `vectors`, `detail`, `jd`, `liveness`, `freshness`, `shards`}:

```
Immutable blob (content-addressed by build):   index-{build_id}.sqlite.gz            (T=core)
                                               index-slim-{build_id}.sqlite.gz       (T=slim)
                                               index-vectors-{build_id}.sqlite.gz    (T=vectors)
                                               shards-{build_id}/{sector}.sqlite.gz  (T=shards, per-sector)
Per-producer pointer (mutable, tiny, flipped LAST):
                                               latest-core.json
                                               latest-slim.json
                                               latest-vectors.json
                                               latest-detail.json
                                               latest-jd.json
                                               latest-liveness.json
                                               latest-freshness.json
                                               latest-shards.json
```

`build_id` is exactly `_build_id()`'s value (`build-<date>-<run>`), already unique per run. Because the
blob name embeds it, two builds **cannot** collide, and re-running a build with the same run number is a
no-op idempotent re-upload of a byte-identical asset (GitHub Releases dedups by name; `--clobber` of an
identical asset is safe). Immutability is a *convention enforced by the writer* (never re-upload an
existing `index-{build_id}...` name with different bytes), plus the prune job (§3.6); GitHub Releases
does not enforce immutability natively, so the writer must treat these names as write-once.

> **Why not sha-addressed (`index-{sha256}.sqlite.gz`)?** Content hashing is the textbook CAS choice,
> but here it costs a full gzip+hash before we know the name, and it makes the prune/GC job (§3.6)
> unable to order assets by recency without reading every manifest. `build_id` is already unique,
> already monotonic-ish (date + run number), and already the reader's freshness key. Build-addressing
> gives 100% of the atomicity benefit with a name we already compute. **Decision: build-addressed, not
> sha-addressed.** (The manifest still carries the sha256 for integrity — see §3.3.)

### 3.3 Pointer schema + who writes which

A pointer file is the *thin* replacement for today's `manifest-{tier}.json`. It carries only what the
reader needs to resolve and verify the immutable blob:

```jsonc
// latest-core.json  — written LAST by build-index.yml, and ONLY by it (R1's owner rule holds)
{
  "schema": "pointer/v1",
  "tier": "core",
  "build_id": "build-2026-07-27-123",
  "asset": "index-build-2026-07-27-123.sqlite.gz",   // the immutable blob to fetch
  "sha256": "…",                                     // integrity of the DECOMPRESSED sqlite (as today)
  "bytes": 480242183,                                // compressed size (prune + delta cost math)
  "schema_version": 7,                               // == db.SCHEMA_VERSION gate (as today)
  "published_at": "2026-07-27T10:41:22Z"
}
```

Owner map (unchanged from R1 — the pointer replaces that producer's manifest, so ownership is
identical):

| Pointer | Written by (sole writer) | Names blob |
|---|---|---|
| `latest-core.json` | `build-index.yml` | `index-{build_id}.sqlite.gz` |
| `latest-slim.json` | `build-index.yml` | `index-slim-{build_id}.sqlite.gz` |
| `latest-shards.json` | `build-index.yml` | `shards-{build_id}/{sector}.sqlite.gz` (list) |
| `latest-jd.json` | `build-index.yml` | `index-jd-{build_id}.sqlite.gz` |
| `latest-liveness.json` | `build-index.yml` | `index-liveness-{build_id}.sqlite.gz` |
| `latest-detail.json` | `drain-detail.yml` | `index-detail-{build_id}.sqlite.gz` |
| `latest-vectors.json` | `embed-vectors.yml` | `index-vectors-{build_id}.sqlite.gz` |
| `latest-freshness.json` | `freshness-sweep.yml` | `index-freshness-{build_id}.sqlite.gz` |

**Crucially, R6 makes `manifest-set.json` obsolete.** Today's set manifest exists solely to detect a
torn core+manifest read. Under R6 the pointer *is* the atomic commit and the blob it names is immutable,
so there is nothing to tear — the set manifest can be dropped once readers are cut over (§3.5).

### 3.4 Reader resolution — the download contract change in `cache.py`

Each of the five `*Cache.ensure_fresh()` methods changes its first two steps and nothing else. Concrete
diff for `IndexCache` (the others are mechanical mirrors):

```
today:                                          under R6:
  remote = json.loads(fetch("manifest.json"))     ptr   = json.loads(fetch("latest-core.json"))
  ...                                              build = ptr["build_id"]
  raw = gzip.decompress(fetch("index.sqlite.gz"))  raw   = gzip.decompress(fetch(ptr["asset"]))
  sha256(raw) == remote["sha256"]                  sha256(raw) == ptr["sha256"]   # unchanged
  tmp.replace(db_path)                             tmp.replace(db_path)           # unchanged
```

Everything else in `ensure_fresh` is untouched: the `schema_version` gate (`cache.py:278`), the
`build_id`-equality short-circuit (`cache.py:282`), the sha256 reject (`cache.py:302`), the atomic local
`tmp.replace` (`cache.py:307`), and the delta path (`cache.py:_try_delta`, `:_try_delta_chain`). The
delta manifests become `latest-delta.json` naming `index-delta-{from}-{to}.sqlite.gz`, immutable by
construction (a delta between two fixed builds never changes).

`cached_index_build_id()` (`cache.py:39-60`) changes its filename list from `manifest*.json` to
`latest-*.json` — a one-line edit.

**The download contract is: read one small pointer, then fetch exactly the immutable assets it names.**
No reader ever constructs an asset name from a fixed string again; it only follows names the pointer
gives it. That is what makes the swap atomic (§3.5).

### 3.5 Why this is a TRUE atomic swap (the core claim)

The publish sequence for a producer is:

1. Upload `index-{build_id}.sqlite.gz` (immutable; a name no prior build used).
2. Upload any sibling immutable blobs for the same build.
3. **Flip** `latest-core.json` to `{build_id, asset, sha256}` — a **single-asset** `--clobber` upload.

The atomicity argument:

- A reader's observable state is entirely determined by *which* `latest-core.json` bytes it fetches. It
  never independently guesses a blob name.
- Before the flip in step 3, the pointer names the **old** build, and the old build's immutable blob
  still exists (immutable, never overwritten). → reader resolves the OLD set, fully consistent.
- After the flip, the pointer names the **new** build, whose blob was **fully uploaded in step 1**
  (steps are ordered; the flip is last). → reader resolves the NEW set, fully consistent.
- There is no intermediate pointer state: the pointer is one asset; a `--clobber` of one asset is
  observed by a reader as either the old bytes or the new bytes, never a splice (GitHub serves an asset
  as a single object; a concurrent read during replace returns one complete version). → **no torn
  read is representable.**

Contrast with today: today the "commit" is spread across two assets (`manifest.json` + `index.sqlite.gz`)
that are uploaded separately, so a reader *can* observe (new manifest, old blob). The set-manifest
verify *detects* that and falls back to stale; R6 makes the state *unreachable*. **This is a genuine
upgrade from "detect-and-degrade" to "impossible" — but see §3.7 for why the marginal value is still
small.**

### 3.6 Pruning immutable assets (bounded free-tier storage)

Immutable assets accumulate forever unless pruned; a public GitHub Release has no hard asset-count cap
but the total counts against repo storage and every asset is a network object. Design:

- A **prune step** (append to each producer's publish job, or a tiny scheduled `prune-assets.yml`)
  enumerates release assets via the API, parses `build_id` out of each `index-*-{build_id}.*` name,
  and **deletes any build-addressed asset whose `build_id` is not referenced by ANY current
  `latest-*.json` pointer AND is older than `N` days** (default `N=3`).
- The **two-guard rule**: an asset is prune-eligible only if (a) no live pointer names it *and* (b) it
  is older than the retention window. Guard (a) alone is unsafe against a reader mid-download of a build
  that was just superseded; the `N`-day window covers in-flight readers (a download is minutes, not
  days). Guard (b) alone is unsafe against a rolled-back pointer that re-points at an "old" build.
- Retention `N` also bounds the **delta base window**: the row-level delta path (`cache.py:_try_delta`)
  needs the prior build's blob to exist for a reader that is one build behind. `N=3` days ≥ the delta
  chain window, so pruning never strands a delta.
- Prune is **best-effort and non-fatal** (like every add-on in this repo): a failed prune never fails a
  publish; it just leaves storage slightly higher until the next run.

Storage math: core ~480 MB compressed, ~8 tiers, `N=3` daily builds ⇒ steady-state ~ `3 × Σtiers` ≈
a few GB of retained immutable assets. Acceptable on the free tier; without pruning it is unbounded.
**Pruning is not optional under R6 — it is the cost that immutability imposes.** This is a real chunk of
R6's "effort + blast-radius" (a new deletion job that, if buggy, can delete a live asset → a reader
404 → live-fallback; not catastrophic, but it is net-new failure surface that today's clobber model
simply does not have).

### 3.7 Honest marginal value of R6 (the part that decides "hold")

**What R6 adds over the shipped set-manifest verify:** torn reads go from *detected-and-degraded-to-stale*
to *structurally-impossible*. The observable user difference is: today, during the ~seconds-long window
of a multi-asset upload, a reader that fetches mid-flight serves **yesterday's** index (stale, but
correct and fresh-enough — the data is a day old at most); under R6 it serves **today's** index during
that same window. **The delta is: a few-seconds-wide window per day where the reader is one build stale
instead of current.** For a job index refreshed daily and consumed by an SDK that live-falls-back on any
miss, this is nearly worthless.

**What R6 costs:** (1) a new prune/GC job with net-new delete-a-live-asset failure surface; (2) a
naming migration touching every producer workflow *and* all five `cache.py` reader classes; (3) a
dual-write window (§3.8) where both schemes are published, ~doubling publish upload volume temporarily;
(4) permanent ~2–3 GB of retained immutable assets; (5) the conceptual load of "which of 8 pointers is
authoritative" replacing the single set manifest.

**Verdict: HOLD.** Marginal benefit is a few-seconds-per-day staleness window that the set-manifest
verify already covers correctly; blast-radius is high (every producer + every reader + a new delete
job). Build R6 **only** when one of these triggers fires:

- **Trigger A:** we migrate off GitHub Releases to a store with no atomic multi-asset semantics *and*
  no cheap equivalent of the set-manifest check (e.g. a raw S3/R2 bucket where the current
  detect-and-degrade is harder to implement than immutability). Then immutability is the *simplest*
  correct design, not an upgrade.
- **Trigger B:** torn-read incidents recur in production *despite* `ERGON_VERIFY_SET_MANIFEST=1`
  (i.e. the detect-and-degrade proves insufficient — e.g. readers that can't tolerate one-build
  staleness). No such incident has occurred.

Until then, the set-manifest verify is the right amount of engineering.

### 3.8 Migration (if a trigger fires) — dual-write, cut readers, drop old

A safe cutover never has a flag-day:

1. **Dual-write (writers).** Each producer publishes **both** schemes for a window: the fixed-name blob
   + manifest (today) *and* the build-addressed blob + pointer (R6). `manifest-set.json` still published.
   Flag `ERGON_R6_DUAL_WRITE=1`. Byte-identical DB, just uploaded under two names — the parity gate is
   "the two schemes' `index-*.sqlite.gz` are byte-identical".
2. **Cut readers over (SDK).** `cache.py` prefers `latest-{tier}.json` when present, falls back to
   `manifest-{tier}.json` when absent (older releases, forks, dual-write-off). Flag
   `ERGON_R6_READ_POINTER=1`, default off → on after a real release is confirmed resolvable both ways.
   This is the same graduate-after-a-real-build discipline used for `ERGON_VERIFY_SET_MANIFEST`
   (`cache.py:63-72`).
3. **Drop old (writers).** Once telemetry shows no reader on the legacy path, stop publishing
   fixed-name blobs + `manifest-set.json`; keep only build-addressed + pointers. Prune the leftover
   fixed-name assets once.

### 3.9 R6 offline fixture test (implementation-ready)

A single hermetic test, no network — point a reader at a fixture "release" directory served by a local
`fetch` closure (the same `Callable[[str], bytes]` seam `cache.py` already uses, `cache.py:120-128`):

```
tests/test_pointer_resolution.py

fixture A "clean release":
  latest-core.json -> {build_id: B2, asset: index-B2.sqlite.gz, sha256: H2}
  index-B2.sqlite.gz            (valid, sha256 == H2)
  index-B1.sqlite.gz            (prior build still present — immutable)
  assert IndexCache.ensure_fresh() resolves B2, db sha matches H2.

fixture B "mid-flip torn state" (the critical case):
  latest-core.json -> {build_id: B1, ...}      # pointer NOT yet flipped
  index-B2.sqlite.gz            (new blob ALREADY uploaded — steps 1-2 done, step 3 not)
  index-B1.sqlite.gz            (old blob present)
  assert ensure_fresh() resolves B1 cleanly (the OLD set) — NO torn read, NO error,
         and never fetches index-B2 (the reader only follows the pointer).

fixture C "post-flip":
  latest-core.json -> {build_id: B2, ...}       # pointer flipped, both blobs present
  assert ensure_fresh() resolves B2.

fixture D "sha mismatch on named immutable blob":
  latest-core.json -> {build_id: B2, asset: index-B2.sqlite.gz, sha256: WRONG}
  assert ensure_fresh() rejects (returns None / stays on cache) — reuses cache.py:302-304 path.
```

Fixture B is the whole point: it simulates the exact instant between "new blob up" and "pointer flipped"
and asserts the reader resolves the OLD set with no tearing. This is the offline proof of §3.5.

---

## 4. R7 — decouple FETCH / ASSEMBLE / PUBLISH into independently-retryable stages

### 4.1 The design in one sentence

The crawl writes `fresh.sqlite` (+ the JD sidecar) as a **durable GitHub Actions workflow artifact**;
a **separate assemble+publish job** downloads that artifact, runs `build_index_from_fresh_db`
(`src/ergon_tracker/index/build.py:833`) + the gated publish. A publish/build/gate failure then loses
**no fetched work** — the fetch is durable in the artifact, so recovery is just re-running the assemble
job, not re-crawling.

### 4.2 The stage boundaries

| Stage | Input | Output | Cost | Idempotent? | Retryable independently? |
|---|---|---|---|---|---|
| **FETCH** (crawl) | registry + prior `board_state.json` + prior index (carry-forward base) | `fresh.sqlite` + `index-jd-fresh.sqlite` as **workflow artifacts** | ~90 min (the pole) | yes (re-crawl re-produces equivalent fresh DB) | yes |
| **ASSEMBLE** | `fresh.sqlite` artifact + prior index | built `index.sqlite` (pre-gzip), gates evaluated | ~2–3 min (SQL only) | yes | yes — **this is the retry that R7 unlocks** |
| **PUBLISH** | built index + pointers/manifests | release assets uploaded | ~1–2 min | yes (clobber/pointer-flip) | yes |

The seam already exists in the code: `scripts/build_index.py:2235` writes `fresh_path = out /
"fresh.sqlite"` during the streaming crawl, and `build_index_from_fresh_db` (`build.py:833`, called at
`build_index.py:2292`) reads it into the index. Today all three stages run in **one** `build` job in
`build-index.yml`, so a failure in ASSEMBLE or PUBLISH discards the FETCH output with the workspace. R7
splits the job at the existing `fresh.sqlite` boundary.

### 4.3 The artifact contract

```
FETCH stage uploads (actions/upload-artifact@v4, if-no-files-found: warn):
  fresh.sqlite                 # the streamed crawl output (build_index_from_fresh_db's input)
  index-jd-fresh.sqlite        # the Item-2 JD sidecar captured during the crawl
  crawl_cursor.json            # where the rotating window stopped (carry-forward scheduler state)
  board_state.json (delta)     # per-board BoardState updates the crawl produced
  fetch-manifest.json          # {build_id, boards_crawled, rows, jd_rows, partial: bool}

ASSEMBLE+PUBLISH stage: actions/download-artifact (merge-multiple), then the existing
  build_index_from_fresh_db -> _gated_publish -> publish_artifacts path, unchanged.
```

Two hard rules, both already established patterns in this repo:

- **Artifact, never release asset.** The fresh DB is an *intermediate*; it is a workflow artifact
  (ephemeral, 90-day GH retention, not part of `index-latest`). This is exactly how `crawl-mapreduce.yml`
  already treats per-shard fresh DBs (`crawl-mapreduce.yml:7-11`, "streaming a partial fresh DB … as a
  workflow ARTIFACT (never a release asset)").
- **`!cancelled()` upload.** The FETCH stage uploads its artifact even on a late failure so a partial
  fresh DB is still recoverable (`crawl-mapreduce.yml:131-142` does exactly this).

### 4.4 Retry semantics

- **PUBLISH fails** (gate flake, gzip OOM, upload 5xx): re-run the ASSEMBLE+PUBLISH job. It re-downloads
  the FETCH artifact and rebuilds — **the 90-min crawl is not repeated.** This is R7's entire payoff.
- **ASSEMBLE fails** (a build bug): fix, re-run ASSEMBLE+PUBLISH against the same artifact. Same payoff.
- **FETCH fails**: re-run FETCH (unavoidable — the crawl is the work). No worse than today.
- **Idempotency:** the ASSEMBLE stage is a pure function of (fresh artifact, prior index); re-running it
  produces the same index. PUBLISH is idempotent under both today's `--clobber` and R6's pointer flip.

### 4.5 Relationship to R3 (shelved) and the map/reduce workflow — the honest part

R7 is the **non-sharded durability slice of the same shape as R3**, and R3 is **shelved as of
2026-07-27** (`docs/superpowers/plans/2026-07-27-crawl-mapreduce-spec.md:1-11`): sharding cannot beat
the crawl's floor (the slowest single unsplittable host, SmartRecruiters ~152k jobs ≈ 119 min), so
map/reduce (~2h23m + reduce overhead) is ≈ or worse than single-process (~90 min typical), and the
210-min crawl-deadline already prevents the timeout it targeted → **no wall-clock win.** R3's code stays
dormant, flag-off byte-identical.

The consequence for R7 is decisive: **`crawl-mapreduce.yml` already implements R7's exact FETCH→ASSEMBLE
split**, just with `K` sharded FETCH stages instead of one. Its map jobs upload `fresh-shard-N.sqlite`
as artifacts (`crawl-mapreduce.yml:131-142`) and its reduce job downloads them and runs the gated
publish (`crawl-mapreduce.yml:145-…`). R7 is literally "the `K=1` version of the shelved workflow." So
R7 requires **no new mechanism** — it is a re-wiring of `build-index.yml`'s single job into two jobs
using machinery that already exists and is tested. That *lowers* R7's effort. But it also means the
durability R7 offers is already reachable by dispatching the (dormant) mapreduce workflow with the
single-runner partition, if we ever actually needed it.

### 4.6 Honest marginal value of R7 (the part that decides "hold")

**What R7 adds over the shipped crawl-deadline:** the deadline prevents the *timeout* loss mode (crawl
runs past 350 min → publish never runs). R7 additionally protects against a *post-crawl* failure
(build/gate/gzip/publish step crashes → the completed 90-min crawl is discarded with the workspace). In
the incident history, **the observed loss was the timeout variant (2026-07-25), which the deadline
already fixed.** A post-crawl publish crash that *also* required a full re-crawl **has not been
observed** — the build/gate/gzip/publish steps are SQL-and-IO, minutes long, and rarely fail; when they
do, the next daily build recovers automatically (carry-forward), at the cost of one day's freshness, not
data.

**What R7 costs:** splitting one job into two crosses a job boundary — the FETCH artifact (a multi-GB
`fresh.sqlite`) must be uploaded and re-downloaded between stages (minutes + artifact storage), which
the single-job design avoids entirely. It adds a job-orchestration surface (`needs:`, artifact
plumbing, the count-and-guard the mapreduce reduce already carries).

**Verdict: HOLD.** Marginal benefit is protection against a post-crawl-failure loss mode that has never
occurred, on top of the deadline that fixed the one that did; the cost is a multi-GB inter-job artifact
round-trip on every daily build. Build R7 **only** when this trigger fires:

- **Trigger:** publish-side failures (not crawl timeouts) become a *recurring* loss mode — e.g. after
  a future change makes ASSEMBLE expensive/flaky (a heavy re-enrich, a large delta computation), such
  that re-running it without re-crawling is a repeated real need. At that point R7 is cheap (the
  mapreduce machinery exists) and clearly justified. Track it via: count of daily builds that failed
  *after* the crawl completed. Today that count is ~0.

### 4.7 R7 offline test (implementation-ready)

```
tests/test_fetch_publish_decoupling.py  (no network; local dirs simulate the artifact handoff)

1. Run the FETCH stage against a fixture registry -> assert fresh.sqlite + index-jd-fresh.sqlite
   + fetch-manifest.json exist and fresh.sqlite has the expected rows.
2. Simulate a PUBLISH crash: invoke ASSEMBLE+PUBLISH but raise inside publish_artifacts
   (monkeypatch) AFTER build_index_from_fresh_db succeeds.
   -> assert fresh.sqlite (the artifact) is UNTOUCHED on disk (durable).
3. Re-run ASSEMBLE+PUBLISH against the SAME fresh.sqlite (no re-fetch) -> assert it builds the
   identical index and publishes -> proves the fetch survived and re-publish needs no re-crawl.
4. Parity: the two-stage path produces a byte-identical `jobs` table to today's single-job path
   on the same fixture (the repo's standard parity-gate template).
```

Step 2+3 are the whole point: kill PUBLISH, assert the FETCH artifact survives, re-run PUBLISH from it.

---

## 5. The real lesson from today — a non-production publish channel (BUILD NOW)

### 5.1 The incident

On 2026-07-27 a **validation dispatch of the (shelved) `crawl-mapreduce.yml` workflow published a
lower-coverage full-crawl index over a better one**, regressing production JD coverage **76% → 40%**.
Root cause: **experimental / validation builds write to the SAME production `index-latest` release** as
the daily build. The mapreduce reduce job is, by R1's design, a legitimate writer of the `index-latest`
core (it shares the `build-index` concurrency group, `crawl-mapreduce.yml:153-155`) — so when dispatched
for *validation*, it did exactly what it is built to do: gate-check its index and publish it. Its index
had **more rows** (a full crawl) but **lower JD coverage** (no Tier-3 detail merge in that validation
run), so:

- It **passed the publish gate.** The gate (`src/ergon_tracker/index/gates.py:46-102`) enforces
  `integrity_check`, `schema_version`, a **row-count floor** (`rows ≥ 0.75 × prev`, `gates.py:78-85`),
  no-duplicate-ids, and company-FK-intact. A full-crawl index has *more* rows, so the row floor passed
  comfortably. **There is no JD-coverage gate.**
- The **JD-coverage regression was only a WARN.** JD-capture is a first-class metric in the regression
  *tripwire*, but `_emit_metrics_regression` is explicitly **non-fatal** — it "reads + logs + writes a
  signal file only" (`scripts/build_index.py:1069-1085`). It warns; it does not block the publish.

So a valid-but-worse index sailed through every gate and clobbered the good one. **This is the
"wrong-but-valid publish" failure mode from §2's fourth row.**

### 5.2 Would R6 have prevented it? — reasoned through: NO

Walk it through the R6 mechanism (§3.5): the validation build would produce its own
`index-{its-build-id}.sqlite.gz` (immutable — fine), and then, as its last action, **flip
`latest-core.json` to point at its own worse index.** Immutability guarantees the pointed-to bytes are
internally consistent and fully uploaded; it says **nothing** about whether they *should* be the latest.
A wrong-but-valid publish is a *correct* atomic swap to the *wrong* target. R6 makes the swap clean; it
does not make the *decision to swap* correct. **R6 would not have prevented today's incident.** (Nor
would R7 — R7 protects fetched work from being lost, not production from being overwritten by a valid
worse build.)

This is the crux of the whole document: the review's north-star durability items (R6/R7) are aimed at
*mechanical* publish integrity (torn reads, lost fetches), but **today's actual production incident was a
*policy* failure — a non-production build was allowed to touch the production pointer.** The cheap,
urgent fix targets policy, not mechanics.

### 5.3 The actual fix — a staging/experimental release channel + `--no-publish` dry-run

Two complementary controls, either of which alone would have prevented today's incident; build both:

**(a) `--no-publish` / dry-run mode (the minimal, do-this-first control).**
A flag — `ERGON_NO_PUBLISH=1` (env) and/or `--no-publish` (CLI) — that runs the *entire* build including
`build_index_from_fresh_db`, the gates, and `publish_coverage` (so a validation run still emits
`coverage.json` / `gates.json` / `metrics_regression.json` for inspection), but **skips every
`gh release upload` to `index-latest`.** The `_gated_publish` + `publish_artifacts` calls
(`scripts/build_index.py:2318-2337`) become no-ops that instead write the assets to a local `dist/` and
print where they are. Validation dispatches set it; the daily cron does not. **Cost: a few lines and one
flag check at each upload site.** This is the single highest-value change in this entire document.

**(b) A staging release channel (the durable control).**
Give non-production builds their own release tag. Parameterise the release tag the workflows write to —
today hard-coded `index-latest` (`cache.py:31` `_TAG`, and every `gh release upload index-latest …` in
the workflows). A dispatched/validation run publishes to `index-staging` instead; the SDK reader stays
pinned to `index-latest` (its `_TAG` default, `cache.py:31`, already overridable via the `IndexCache(tag=…)`
constructor param — the seam exists). Then a validation build **cannot physically touch production** no
matter what it does, and its output is still fully inspectable at `index-staging`. Promotion from staging
to production, if ever wanted, is a deliberate manual copy — never an accident of dispatch.

**Guardrail to make (a)/(b) fail-safe:** default the mapreduce/validation workflows' `workflow_dispatch`
to the staging tag / `--no-publish`, so writing to production requires an *explicit* opt-in input, not
the default. The failure mode today was that production was the *default* target for a workflow whose
common use is validation. Invert the default.

**Optional hardening (not required to fix today, but cheap): a JD-coverage regression GATE.** Promote the
JD-capture metric from the WARN-only tripwire (`build_index.py:1069-1085`) to a real gate in
`evaluate_gates` — block a publish whose JD coverage drops more than `X%` below the durable last-published
coverage (mirroring the row-floor's `history.jsonl` durable-fallback logic, `gates.py:74-85`). This would
have caught today's 76%→40% drop *even without* the channel split, and it defends against any future
same-registry regression (not just dispatch accidents). Recommended as a fast-follow to (a)/(b), not a
substitute — a gate that rejects the bad publish is good, but a build that *cannot target production at
all* is strictly safer.

### 5.4 Why this beats R6/R7 on cost/benefit

- **Benefit:** directly prevents the incident that *actually happened in production today*, and the
  whole class of "a non-prod build stomped prod" — which is more likely to recur than a torn read
  (never observed with the set-manifest verify on) or a post-crawl fetch loss (never observed).
- **Cost:** `--no-publish` is a handful of lines and one env check at each upload site; the staging tag
  is parameterising a constant that is *already* a constructor parameter on the reader side
  (`cache.py:138` `tag=_TAG`). No new storage, no reader migration, no prune job, no inter-job artifact
  round-trip. Blast-radius is near-zero: flag-off / tag-default is byte-identical to today.

---

## 6. Ranked recommendation (the deliverable)

Ranked by (marginal benefit over what shipped) / (effort + blast-radius):

| Rank | Item | Marginal benefit | Effort + blast-radius | Verdict + trigger |
|---|---|---|---|---|
| **1** | **`--no-publish` dry-run + staging channel** (§5.3) | Prevents the *actual* production incident (non-prod build overwrites prod) — a recurring, observed class | LOW (few lines; reader tag is already a param) | **BUILD NOW** |
| **2** | **JD-coverage publish gate** (§5.3 optional) | Catches valid-but-worse publishes (76%→40%) even without the channel split; defends all future same-registry regressions | LOW–MED (extend `evaluate_gates` + a durable coverage floor) | **BUILD NOW as fast-follow** to #1 |
| **3** | **R7** — durable fetch artifact + retryable publish (§4) | Protects a *post-crawl* publish crash from re-crawling — a loss mode **never observed** (deadline fixed the one that was) | MED (job split + multi-GB inter-job artifact round-trip; machinery already exists in mapreduce) | **HOLD** — build when post-crawl-failure becomes a recurring loss mode (track: builds failing *after* crawl completes; today ~0) |
| **4** | **R6** — content-addressed immutable + pointer flip (§3) | Torn reads: *impossible* instead of *detected-and-degraded-to-stale* — a few-seconds/day staleness-window difference | HIGH (every producer + all 5 readers + a new prune/GC job with delete-a-live-asset surface + dual-write window + permanent retained storage) | **HOLD** — build only if we leave GitHub Releases (Trigger A) or torn reads recur despite the set-manifest verify (Trigger B); neither has happened |

**One-line stance:** the north-star mechanical-durability items (R6, R7) are well-designed and now
fully specced so they are implementation-ready the day a trigger fires — but **both are "hold"**,
because R1 + the set-manifest verify already make clobber/torn-reads a solved problem, and the 210-min
crawl-deadline already fixed the only observed data-loss. **The real, urgent, cheap work is the
policy fix R6/R7 do *not* provide: stop letting non-production builds write the production pointer.**

---

## 7. Self-review notes

- R6 fully specced: naming (§3.2), pointer schema + owners (§3.3), reader/`cache.py` download contract
  (§3.4), atomic-swap proof (§3.5), pruning (§3.6), migration/dual-write (§3.8), offline fixture test
  incl. the mid-flip torn-state case (§3.9). ✅
- R7 fully specced: stage boundaries at the existing `fresh.sqlite` seam (§4.2), artifact contract
  (§4.3), retry semantics (§4.4), the shelved-R3 / mapreduce relationship (§4.5), offline
  kill-publish-then-re-publish test (§4.7). ✅
- Honest cost/benefit for BOTH, quantified against what shipped, each with a build-now/hold verdict and
  an explicit trigger condition (§3.7, §4.6, §6). ✅
- Today's incident addressed: reasoned that R6 would NOT have prevented it (§5.2), identified the actual
  cheap fix (`--no-publish` + staging channel + optional JD-coverage gate), specced it, and ranked it
  #1/#2 above R6/R7 (§5, §6). ✅
- No code in `src/`/`scripts/`/workflows changed; nothing deployed. ✅
