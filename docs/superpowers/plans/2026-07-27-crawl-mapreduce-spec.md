# R3 — Map/Reduce the Non-Join Crawl Across Parallel Runners

> ⛔ **VALIDATED, SHELVED — DO NOT ACTIVATE (2026-07-27).** Implemented dark and measured on two real
> dispatch runs. Sharding cannot beat the crawl's floor = the slowest single *unsplittable* host
> (SmartRecruiters ~152k jobs on one API host → ~119 min); single-process already overlaps that host
> with all others concurrently, so map/reduce (~2h23m + reduce overhead) ≈ or **worse than**
> single-process (~90 min typical, 210 min capped), and the 210-min crawl-deadline already prevents the
> timeout this targeted → **no wall-clock win.** The code stays dormant (flag-off = byte-identical). The
> real payoff was a side effect: the load-rebalance fix (un-collapsing Workday in `freshness_shard`)
> also balances the live freshness-sweep's 20-shard matrix (shipped, merge `7915170`). See
> [[jobspine-delta-crawl-redesign]] for the full verdict + numbers.

**Status:** DESIGN / SPEC ONLY. No code in `src/` or `scripts/` is changed by this document. Ships DARK behind a flag; flag-off is byte-identical to today's single-process crawl.
**Date:** 2026-07-27
**Author:** architecture-review follow-up (item R3)
**Scope:** the daily **non-join** crawl only. The join shard stays exactly as it is today (its own `10:17` cron in `build-index.yml`).

---

## 1. Problem

The daily non-join crawl is the pipeline's dominant phase and runs on **one runner**.

- `build-index.yml` runs the whole non-join registry (~38k boards) in a single `build` job (`.github/workflows/build-index.yml:54-321`), `timeout-minutes: 350` (`build-index.yml:56`).
- A normal crawl finishes in **~90 min**; a slow/late CI day is capped by a **210-min** global crawl deadline (`ERGON_CRAWL_DEADLINE_S=12600`, set in the "Resolve crawl partition" step, `build-index.yml:197`). When the cap fires, the crawl stops dispatching new boards and publishes a **PARTIAL** index (the un-crawled boards stay `due` and carry forward).
- The cap exists because on 2026-07-25 the crawl ran unbounded into the 350-min job timeout **mid-crawl** and published **nothing** — the single end-of-build publish never ran, losing the Item-2 JD sidecar (`build-index.yml:192-197`, `scripts/build_index.py:1626-1636`).

Everything downstream of the crawl (build/carry-forward/finalize/publish) is already fast (SQL-only, a few minutes). **The crawl is the pole.** One runner = one slowness point, and the mitigation so far has been a deadline that trades coverage for safety.

## 2. The proven pattern (reuse, do not re-derive)

This repo has shipped the **sharded-matrix + merge-job** shape three times:

| Workflow | Map (matrix) | Reduce (merge job) | Release writer | Concurrency group |
|---|---|---|---|---|
| `drain-detail.yml` | 20-shard Tier-3 JD drain → `index-detail-shard-N.sqlite` artifact | `merge` job → `merge_detail_shards.py` → publish `index-detail.sqlite.gz` | reduce only | `build-index` (shared) |
| `embed-vectors.yml` | 20-shard vector embed → `index-vectors-shard-N.sqlite` artifact | `merge` job → `merge_vectors_shards.py` → publish `index-vectors.sqlite.gz` | reduce only | `build-index` (shared) |
| `freshness-sweep.yml` | 20-shard board sweep → `index-freshness-shard-N.sqlite` artifact | `merge` job → `merge_freshness_shards.py` → publish `index-freshness.sqlite.gz` | reduce only | `freshness-sweep` (own) |

All three: `fail-fast: false` (`drain-detail.yml:78`), per-shard artifact upload on `!cancelled()` (`drain-detail.yml:191`), merge job on `if: !cancelled()` that merges *whatever shards finished* (`drain-detail.yml:200`, `merge_freshness_shards.py:187-191`), a count-and-guard step (`drain-detail.yml:229-236`), and `NUM_SHARDS: 20` (`drain-detail.yml:71`).

**R3 applies this exact shape to the crawl itself.** The one genuinely new piece is the **deterministic merge of crawl STATE** (`board_state` / cursor / `idset_hash` / dedup), because — unlike the drain (row-keyed UPSERT), embed (id-keyed), and freshness (natural-key union) — the crawl's outputs include per-board scheduler state that today is written by a single process that sees all boards. §6 is that design.

## 3. The host-sharding key (reuse `shard_boards`)

The correctness invariant that makes ANY crawl fan-out safe is **politeness**: `AsyncFetcher`'s per-host token bucket is a per-process structure (`freshness_shard.py:6-12`). If two boards that hit the same real backend land on two different shards, the backend sees up to `NUM_SHARDS`× the intended request rate — a self-inflicted ban.

`src/ergon_tracker/index/freshness_shard.py` already solves this for boards expressed as `(source, board_token)` pairs:

- `shard_boards(boards, shard, num_shards)` (`freshness_shard.py:144`) returns only this shard's slice, partitioning so **every board whose fetch contends on the same politeness bucket lands on exactly ONE shard**.
- The bucket is `board_rate_bucket(source, token)` = `rate_key_for_host(board_host(source, token))` (`freshness_shard.py:109-117`) — the exact string `AsyncFetcher` keys its token bucket on.
- Partition is by **SHA-1** of the bucket, NOT Python's salted `hash()`, so 20 independent processes agree with zero coordination (`freshness_shard.py:126-141`).
- `join.com` is carved out to a reserved shard 0 (`ISOLATED_HOSTS`, `freshness_shard.py:123`).

**R3 reuses `shard_boards` verbatim.** The crawl's board universe is `(e["ats"], e["token"])` for each registry entry — the same `(source, token)` tuple `shard_boards` expects, and the same tuple `BoardState.key` is built from (`scheduler.py:57-59`). `e["ats"]` is the provider/source name; `e["token"]` the board token. No new sharding logic is written.

### 3.1 The join / shard-0 decision

The R3 matrix crawls the **non-join** partition (`ERGON_CRAWL_EXCLUDE_SOURCES=join`, `build-index.yml:188`). Since `join.com` is excluded, no board maps to the reserved shard 0 (`_shard_for_bucket` sends every non-isolated bucket to `1..num_shards-1`, `freshness_shard.py:139-141`). **Shard 0 will therefore be empty for the non-join partition.**

Decision for v1: **accept the idle shard 0** — run the matrix `[0..19]`, shard 0 crawls nothing, contributes an empty fresh DB, advances no state. This reuses `shard_boards` with zero new code and zero correctness risk; the cost is one idle runner (19 effective shards, still a ~19× fan-out). Do **not** invent a "no-reserve" variant for v1.

Optional later refinement (out of scope for v1): add a `reserve_isolated: bool = True` parameter to `shard_boards`/`_shard_for_bucket` so a partition with no isolated host uses all K shards as hash targets. Only worth it if measurement shows one idle shard matters, which it will not.

## 4. What needs a global view — and what does not

The single hardest correctness question: **does any per-board crawl step need to see all boards?** Answer, verified against the code: **the MAP (crawl) phase needs NO global view; only the REDUCE does.**

| Crawl step | Data it reads | Global? |
|---|---|---|
| Conditional GET / 304 carry-forward (`build_index.py:1470-1483`) | this board's own `state.etag/last_modified` | No |
| Delta-skip on unchanged id-set (`build_index.py:1457-1465`) | this board's `state.idset_hash` + the freshness sidecar hash for this board | No |
| `idset_hash` stamp (`build_index.py:1505-1509`) | this board's raws only | No |
| Enrich-reuse (`_load_board_reuse_rows`, `build_index.py:1712`) | prev index rows for `(source, token)` — this board only | No |
| Zero-result company resolution (`prior_company_keys_by_board`, `build_index.py:1341-1351`) | prev index rows for this board's `(source, token)` | No |
| Per-board dedup (`deduplicate(board_jobs)`, `build_index.py:1555`) | one board's jobs | No |
| Cross-board dedup | **exact-id only**, via `append_jobs` `INSERT OR IGNORE` on unique `id` (`build.py:326-354`) | reduce-side, trivial |

Two facts make the reduce a simple union:

1. **`job.id` is fully determined by `(source, source_job_id)`**: `make_job_id = sha1(f"{source}:{source_job_id}")[:16]` (`models.py:91-93`). A given `(source, board)` lives on exactly one shard, and a `source_job_id` is unique within its board, so **no two shards can produce the same `job.id` for a genuine posting.** Cross-shard id collision is impossible by construction.
2. **The streaming crawl never does cross-board fuzzy dedup** — `build_index_from_fresh_db` copies rows with `INSERT OR IGNORE` on `id` (`build.py:846-847`). So the union of K per-shard fresh DBs via `INSERT OR IGNORE` is **byte-identical** to what one process streaming all boards would have inserted.

The steps that genuinely need the union are all already reduce-shaped and run once: `changed_companies_sql` (`build.py:206`, diffs merged-fresh vs prev), `carry_forward` (`build.py:371`, drops crawled companies' stale rows), company aggregation (`finalize_index`, `build.py:557`), and `apply_outcome` tiering (needs the global `changed` set — see §6.4).

## 5. Component changes (files, functions, new args)

### 5.1 `src/ergon_tracker/index/scheduler.py` — one merge helper (new)

Add a pure function (offline-testable, mirrors `load_state`/`save_state`):

```
def merge_states(prev: dict[str, BoardState],
                 shard_states: list[dict[str, BoardState]]) -> dict[str, BoardState]
```

- Start from a copy of `prev` (the full prior state).
- For each shard state in ascending shard order, overlay every entry whose `last_crawled == <this build's date>` OR that is a never-seen board this shard seeded (`_new_boards`). Disjoint partition ⇒ at most one shard touches any board key ⇒ order-insensitive; ascending order is fixed only for reproducibility.
- Return the merged dict. (Details and correctness argument in §6.2.)

Nothing else in `scheduler.py` changes; `BoardState` already carries `idset_hash`, `etag`, `last_modified` and the tiering fields (`scheduler.py:37-59`).

### 5.2 `scripts/build_index.py` — `_crawl_due` gains shard awareness

`_crawl_due(...)` (`build_index.py:1281`) gains two optional args, both default `None` ⇒ today's behaviour byte-for-byte:

```
async def _crawl_due(..., shard: int | None = None, num_shards: int | None = None)
```

When both are set, immediately after the board universe is assembled (the `window` at `build_index.py:1353` **plus** the never-seen `_new_boards` pull-in at `build_index.py:1372-1376`), apply a single choke-point filter:

```
if shard is not None and num_shards is not None:
    keep = set(shard_boards([(e["ats"], e["token"]) for _, e in <all window+new entries>],
                            shard, num_shards))
    boards = {k: v for k, v in boards.items() if (v[1]["ats"], v[1]["token"]) in keep}
```

so `due` (`build_index.py:1379`) is computed over this shard's slice only. Everything downstream in `grab` is unchanged: politeness, conditional GET, delta-skip, `idset_hash` stamping, streaming to `fresh.sqlite`, `fresh_rich`, and the JD sidecar all operate on the sharded subset. **The `_registry_window` cursor math is untouched and identical across shards** (they all read the same registry + same cursor, so all compute the same `next_cursor`; §6.3).

Critical: the filter must cover BOTH the window and the `_new_boards` pull-in, so every never-seen board is crawled by exactly one shard.

### 5.3 `scripts/build_index.py` — a new `--crawl-shard-only` mode (the MAP)

Mirrors `--detail-shard-only` (`build_index.py:1825-1868`) and `--embed-shard-only` (`build_index.py:1870-1890`). It runs ONLY the crawl for its shard and writes **workflow artifacts** — never touches the release, never builds, never carries forward, never publishes.

`build_index.py main()`:
- new flags `--crawl-shard-only`, reuse existing `--shard N` / `--num-shards N` parsing (`build_index.py:1831-1836`).
- guard: requires `--shard`/`--num-shards`; requires the prior `index.sqlite` on disk for reuse lookups (download+gunzip beforehand, exactly like the drain, `build_index.py:1857-1861`).

The mode does, in order:
1. `only_sources, exclude_sources = _crawl_partition()` (`build_index.py:1198`) — reads `ERGON_CRAWL_EXCLUDE_SOURCES=join` from env, same as today.
2. `states = load_state(prev board_state.json)` (full prior state, downloaded).
3. `cursor = _load_cursor(...)` (`build_index.py:1764`).
4. `outcome, next_cursor = anyio.run(_crawl_due, limit, states, fresh_shard_path, build_id, cursor, rich=True, prev_db, jd=True, only_sources, exclude_sources, shard, num_shards)`.
5. Write these artifacts to `dist/`, all suffixed `-shard-N`:
   - `fresh-shard-N.sqlite` — this shard's crawled `jobs` + `job_sources` + `fresh_rich` (the crawl already streams all three here, `build_index.py:1400,1560,1566`).
   - `index-jd-shard-N.sqlite` — this shard's JD sidecar (the crawl writes it to `<fresh_dir>/index-jd.sqlite`, `build_index.py:1409-1413`; rename to the shard name).
   - `crawl-outcome-shard-N.json` — a serialized form of `outcome` (per board key: `companies` list, `error`, `http_429`, `not_modified`) plus `next_cursor`.
   - `board-state-shard-N.json` — `save_state(states, ...)` of this shard's post-crawl `states` dict. This carries the **crawl-time** field advances (`etag`, `last_modified`, `idset_hash` set inside `grab`) for the boards it crawled. Tiering (`apply_outcome`) is **NOT** applied here (the shard doesn't know the global `changed` set) — the reduce applies it (§6.4).

No `_fold_network_into_fresh`, no `changed_companies_sql`, no `apply_outcome`, no build, no publish in the map.

### 5.4 `scripts/merge_crawl_shards.py` — a new merge script (the REDUCE core)

New file, mirroring `merge_freshness_shards.py` structure (ATTACH each shard, `INSERT OR IGNORE`, per-shard resilience, deterministic sorted order, stdlib+`ergon_tracker` importable via the `sys.path` trick at `merge_freshness_shards.py:56-59`).

```
merge_crawl_shards(shard_dir, out_fresh_path, out_jd_path) -> dict
```

- Union the K `fresh-shard-*.sqlite` into `out_fresh_path` (`jobs`, `job_sources`, `fresh_rich`), each via `INSERT OR IGNORE` in its own transaction (per-table guard, `merge_freshness_shards.py:116-130`). Sorted path order for determinism. Correctness: §4 (no cross-shard id collision).
- Union the K `index-jd-shard-*.sqlite` into `out_jd_path` (`jd_store` schema; `INSERT OR IGNORE`/prefer-existing — JD ids are also `job.id`, disjoint by shard).
- Return counts for logging + the count-and-guard.

### 5.5 `scripts/build_index.py` — a new `--crawl-reduce` mode (the REDUCE orchestrator)

This is **today's `main()` incremental block (`build_index.py:1908-2206`) with the crawl call replaced by "load the merged shard artifacts".** Concretely, `--crawl-reduce`:

1. Download prev `index.sqlite`, `board_state.json`, `crawl_cursor.json`, `history.jsonl`, `index-freshness.sqlite`, `index-jd.sqlite`, and the detail/liveness/vectors sidecars (identical to `build-index.yml:87-139`).
2. `merge_crawl_shards(...)` → `dist/fresh.sqlite` + `dist/index-jd.sqlite` (the same on-disk paths today's `main()` produces at `build_index.py:1935,1980`).
3. Load + union the K `crawl-outcome-shard-N.json` → one `outcome` dict and the common `next_cursor` (assert all shards agree; §6.3).
4. Load prev `states = load_state(...)`; load the K `board-state-shard-N.json`; `states = merge_states(prev, shard_states)` (§5.1, §6.2).
5. `net_keys = anyio.run(_fold_network_into_fresh, fresh_path, network_pages, build_id)` (`build_index.py:1953`) — the Workable bulk feed runs ONCE here (it is not sharded; `network_pages` defaults 0 ⇒ no-op on the daily).
6. `changed = changed_companies_sql(fresh_path, prev_db)` (`build_index.py:1954`) — global diff over the merged fresh DB.
7. `crawled_keys = union of outcome companies | net_keys` (`build_index.py:1955-1958`).
8. `apply_outcome` per crawled board using `changed` (§6.4) — the exact loop at `build_index.py:1960-1969`.
9. `save_state(states, ...)` + `_save_cursor(..., next_cursor)` (`build_index.py:1972-1973`).
10. **From here to the end, run today's block verbatim**: `build_index_from_fresh_db` (`build_index.py:1981`), `_backfill_board_tokens`, `_apply_freshness`, `_gated_publish` (`build_index.py:2012`), metrics/history, rich/detail/liveness sidecars, the single core `publish_artifacts` (`build_index.py:2161`), shards/slim/delta, set manifest. **Only the reduce writes the release.**

The reduce is the single point again — but it is the *fast* part (SQL union + carry-forward + finalize, no network), the same few-minutes work that follows the crawl today. The 90-min pole (the crawl) is what got fanned out.

## 6. The deterministic-merge design (the correctness crux)

### 6.1 Fresh-DB / dedup merge

- Each shard streams jobs into `fresh-shard-N.sqlite` via `append_jobs` (`INSERT OR IGNORE` on unique `id`, `build.py:340-343`) with per-board fuzzy `deduplicate` already applied (`build_index.py:1555`).
- Reduce unions all K via `INSERT OR IGNORE` (`merge_crawl_shards`).
- **Claim:** `union(fresh-shard-0..K-1) == fresh_single` on all identity/data columns.
  **Proof:** the partition is disjoint by `(source, board)` (§3), `job.id` is a pure function of `(source, source_job_id)` (§4, `models.py:93`), and every board's rows are produced identically whether crawled alone or with siblings (per-board processing reads no cross-board state, §4). So each `id` is produced by exactly one shard with identical column values; `INSERT OR IGNORE` over the union inserts exactly that set. Cross-board dedup in the single-process path is also only `INSERT OR IGNORE` on `id` (`build.py:846`), so the two are identical. ∎
- `fresh_rich` and the JD sidecar are keyed on `id` too ⇒ same argument.

### 6.2 `board_state` merge

`board_state.json` is a full dict of all boards (`save_state`, `scheduler.py:145-147`). In the single-process path, `_crawl_due` mutates only window∩due boards' `etag/last_modified/idset_hash` (inside `grab`), then `main()` calls `apply_outcome` on those boards; all other boards pass through untouched and are re-saved.

Sharded, each board is in exactly one shard's window (disjoint), so:

- **Overlay rule:** merged state = prev full state, with each board that some shard advanced replaced by that shard's post-crawl entry. "Advanced" = the shard crawled it (its key is in that shard's `outcome`) or seeded it as never-seen. At most one shard qualifies per board ⇒ no conflict, order-insensitive.
- `idset_hash`, `etag`, `last_modified` ride along **for free** — they are `BoardState` fields the crawling shard already stamped in `grab` (`build_index.py:1479,1505`). No separate merge.
- Boards no shard touched keep their prev entry — exactly the single-process outcome.

**Equivalence to single-process** holds because `apply_outcome` (§6.4) is per-board independent, so applying it board-by-board in the reduce over the union of outcomes yields the identical state a single process would have written.

### 6.3 Cursor / window merge

For the **non-join daily** crawl, `ERGON_CRAWL_MAX_WINDOW=0` and `--limit-companies 60000 ≥ non-join board count`, so `_registry_window` returns the WHOLE partition with `next_cursor = 0` (`build_index.py:1273-1275`). Every shard reads the same registry and same cursor ⇒ every shard computes the **identical** `next_cursor`.

- **Merge rule:** assert all shards' `next_cursor` are equal; write that value (`_save_cursor`, `build_index.py:1772`). For the non-join daily this is always `0`. The general case (a bounded rotating window) is still correct because the window is a deterministic function of `(registry, cursor, limit, max_window, partition)`, all identical across shards — sharding only sub-selects *within* the shared window, it never changes the window boundary or the cursor advance.

### 6.4 `apply_outcome` / tiering (the one step that moves to reduce)

`apply_outcome` (`scheduler.py:105-130`) needs `changed` (`board_changed = o["companies"] & changed`, `build_index.py:1961`), and `changed` is global (needs the merged fresh DB). So the shard **must not** call it. Instead:

- The shard emits raw per-board outcome (`companies`, `error`, `http_429`) in `crawl-outcome-shard-N.json`, and the crawl-time field advances (`etag/last_modified/idset_hash`) in `board-state-shard-N.json`.
- The reduce, after `changed_companies_sql`, runs the exact loop at `build_index.py:1960-1969` over the **union** outcome against the merged `states`. Because `apply_outcome` is a pure per-board fold with no cross-board coupling, the result is independent of ordering and identical to single-process.

### 6.5 `idset_hash` correctness under delta-crawl

The delta-skip (`build_index.py:1457-1465`) compares this board's `state.idset_hash` to the freshness sidecar's published hash for the **same** board. Both sides are per-board; each shard has the full prev `board_state` (so its boards' prior `idset_hash` is present) and the full freshness sidecar (read-only). Skipping is identical to single-process. The re-stamp on a crawled board (`build_index.py:1505`) is written into that shard's `board-state-shard-N.json` and overlaid in §6.2. No global view needed.

### 6.6 What the reduce inherits unchanged (already correct)

`carry_forward` (`build.py:371`, drops crawled companies' stale prev rows, keyed on the union `crawled_keys`), the `first_seen` restore keyed on `content_hash` (`build.py:417-428`), `_relevel_from_years`, `_purge_ancient`, `apply_freshness_expiries` (`build.py:450`), `_gated_publish` row-floor gate — all run once in the reduce over the merged+carried index, exactly as today.

## 7. The workflow: `.github/workflows/crawl-mapreduce.yml` (new)

Mirrors `drain-detail.yml` (matrix + `merge`) and `build-index.yml` (download/build/publish env).

```
name: crawl-mapreduce
on:
  schedule:
    - cron: "17 4 * * *"   # non-join slot (replaces build-index.yml's non-join 04:17 cron on cutover)
  workflow_dispatch:
    inputs: { limit_companies, rich, detail, jd, liveness, delta_crawl, ... }   # mirror build-index.yml
env:
  NUM_SHARDS: 20
permissions:
  contents: write   # ONLY the reduce job actually writes; map jobs write no release assets

jobs:
  map:
    # NO release-writing → its own light concurrency group (or none); fully parallel with everything.
    concurrency: { group: crawl-map, cancel-in-progress: false }
    runs-on: ubuntu-latest
    timeout-minutes: 120          # generous over the slowest single host-bucket (~30-40 min target)
    strategy:
      fail-fast: false            # one shard's failure must not cancel the other 19
      matrix: { shard: [0,1,...,19] }
    steps:
      - checkout; free 40GB toolchains; add 16G swap; setup uv; install -e ".[semantic]"
      - download+gunzip prev index.sqlite.gz + board_state.json + crawl_cursor.json
        + index-freshness.sqlite.gz + index-jd.sqlite.gz         # for reuse/conditional-GET/delta
      - run: |
          [ -f dist/index.sqlite ] || { echo "no prior index — nothing to crawl yet"; exit 0; }
          ERGON_CRAWL_EXCLUDE_SOURCES=join ERGON_CRAWL_MAX_WINDOW=0 \
          ERGON_CRAWL_DEADLINE_S=<per-shard box, e.g. 3600> ERGON_DELTA_CRAWL=1 ERGON_DELTA_CONTENT_VERSION=1 \
          uv run python scripts/build_index.py --crawl-shard-only \
            --shard ${{ matrix.shard }} --num-shards ${{ env.NUM_SHARDS }} \
            --limit-companies 60000 --rich --jd --out dist
      - upload-artifact (if: !cancelled()): fresh-shard-N, index-jd-shard-N,
        crawl-outcome-shard-N.json, board-state-shard-N.json     # if-no-files-found: warn

  reduce:
    needs: map
    if: ${{ !cancelled() }}       # a timed-out shard must not skip the reduce
    concurrency: { group: build-index, cancel-in-progress: false }   # SHARED — see §7.1
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - checkout; free disk; add swap; setup uv; ensure pigz; install -e ".[semantic]"
      - download+gunzip prev index + all sidecars (as build-index.yml:87-139)
      - download-artifact: pattern crawl-* , merge-multiple, if-no-artifact-found: warn
      - count-and-guard (fail only if ZERO shards produced fresh DBs — mirror drain-detail.yml:229-236)
      - run: uv run python scripts/build_index.py --crawl-reduce --sharded --rich --detail --jd --liveness --out dist
        # (this merges shards, builds the ONE core index, gates, and publishes the release)
      - the exact publish + cursor-sync + diagnostics steps from build-index.yml:331-479
  notify:
    needs: [map, reduce]; if: always()      # identical to build-index.yml:487-536
```

### 7.1 Concurrency: reconciling "own group" with the single-writer principle

The map jobs write **no release assets** (only ephemeral per-run artifacts), so they need no serialization — give them their **own group** `crawl-map` (or none). This is the "own concurrency group" the disjoint-writer principle calls for.

The **reduce** job DOES write `index.sqlite` to the shared `index-latest` release — the same asset the join shard (still in `build-index.yml`, `10:17`), `drain-detail.yml`, and `embed-vectors.yml` write to. Those three already share `concurrency: group: build-index` (`drain-detail.yml:66-68`, `embed-vectors.yml:38`). To preserve the **global single-writer** guarantee across all release-writers, the reduce job joins `group: build-index`. Net: the fan-out (map) is unserialized and maximally parallel; the single release write (reduce) is serialized against every other release-writer — exactly the intent.

## 8. Free-tier feasibility + cron slot

- **Concurrency budget:** GitHub Free allows 20 concurrent jobs; `drain-detail`/`embed-vectors`/`freshness-sweep` already run 20-shard matrices within it. R3's `map` reuses the same 20-wide matrix. Public-repo Actions minutes are free, so 20× the per-shard minutes is free.
- **Cron slot:** non-join `map` at `04:17 UTC` (the slot `build-index.yml`'s non-join cron uses today). Existing slots stay clear: freshness `08:17`, drain `09:30`, join shard `10:17`, embed `06:00`. The `reduce` runs on `needs: map` (no cron of its own) and serializes on `build-index` — the next release-writer after it (embed `06:00`) is ~1.7h later, ample headroom for a fanned-out crawl (~30-40 min) + reduce (~few min).
- **Disk/memory per runner:** reuse `drain-detail.yml`'s "free ~40GB toolchains" (`drain-detail.yml:84-97`) + 16G swap (`drain-detail.yml:99-108`) on both map and reduce. Each shard's `fresh-shard-N.sqlite` holds only *this run's freshly-crawled rows* (a fraction of the 1.4M-row backlog), so per-shard disk is small; the reduce stages the union plus the prev index — the same footprint today's single build already handles (`build-index.yml:62-75`).
- **Bandwidth note (measure, tune if needed):** all 20 map shards download the ~500MB `index.sqlite.gz` for reuse lookups. That is 20× a release-asset download from the same release — free but not instant. If it dominates, a later optimization is to have shards fetch only a `(source, board_token) → prior rows` projection, but v1 downloads the whole prev index (simplest, matches the drain, `drain-detail.yml:125-127`).

## 9. The isolated offline parity test (proves correctness with NO full run)

New test `tests/test_crawl_mapreduce_parity.py`. **Offline, deterministic, no network.** This is the gate that must pass before the flag is flipped.

### 9.1 Fixture

- A temporary `SeedRegistry` of a **fixed small slice** — e.g. 12 boards using **real source names** so `board_rate_bucket` sees real hosts:
  - 3 `greenhouse` boards (all → `boards-api.greenhouse.io`, one bucket → one shard) — proves same-host boards co-locate.
  - 3 `lever` boards (all → `api.lever.co`) — a second single-host bucket.
  - 4 `breezy` boards (each `{token}.breezy.hr`, collapsed to the shared `breezy.hr` bucket, `freshness_shard.py:67-76`) — proves subdomain collapse.
  - 2 `ashby` boards.
  Choose `num_shards` small (**4**) so buckets actually distribute across shards ≥2 and at least two distinct shards receive boards. (Verify with a direct `shard_boards` assertion that the fixture splits across ≥2 shards, else the test proves nothing.)
- Monkeypatch, per fixture source, so the crawl is offline and deterministic:
  - `provider.fetch(token, ...)` → canned raws keyed by token (a small dict fixture). Include a board that returns **zero** raws (exercises the zero-result company-resolution branch, `build_index.py:1578-1598`) and a board whose raws change vs a seeded prev index (exercises `changed`).
  - `provider.conditional_url(token)` → `None` (skip the conditional-GET path so the test is hermetic; the 304 path is covered by existing tests).
- A seeded prev `index.sqlite` + prev `board_state.json` built from a first single-process crawl of the fixture, so carry-forward, enrich-reuse, and `changed` are all exercised (not a cold start).

### 9.2 Procedure

1. **Run A (single process):** `_crawl_due(..., shard=None, num_shards=None)` over the whole fixture → `fresh_A.sqlite` + `state_A`; run the `main()` reduce tail (`_fold_network_into_fresh` no-op, `changed_companies_sql`, `apply_outcome` loop, `build_index_from_fresh_db`) → `index_A.sqlite` + `board_state_A.json`.
2. **Run B (K-shard):** for `shard in 0..3`: `_crawl_due(..., shard=shard, num_shards=4)` → `fresh_B_shard.sqlite` + `board-state-B-shard.json` + `crawl-outcome-B-shard.json`. Then `merge_crawl_shards` → `fresh_B.sqlite`; `merge_states` → `state_B`; union outcomes; run the identical reduce tail → `index_B.sqlite` + `board_state_B.json`.
3. Use the **same `build_id` and same `_today()`** for both runs (freeze the clock) so date/provenance columns match.

### 9.3 Assertions

- **`jobs` identity/data byte-identity:**
  `SELECT id, content_hash, enrich_hash, company_key, source, company, company_domain, title, department, level, sector, city, country, location, remote, salary_min, salary_max, salary_currency, years_min, years_max, degree_min, degree_required, visa_sponsor, sponsorship_offered, apply_url, listing_url, board_token, posted_at, updated_at, status FROM jobs ORDER BY id`
  must be **equal row-for-row** between `index_A` and `index_B`. (Exclude only volatile provenance that is date-stamped identically anyway; assert `first_seen/last_seen/build_id` equal too since the clock is frozen.)
- **`job_sources` equality:** `SELECT * FROM job_sources ORDER BY job_id, source, source_job_id` equal.
- **`companies` equality:** `SELECT * FROM companies ORDER BY company_key` equal (proves aggregation over the merged index matches).
- **`board_state` equivalence:** same set of keys; for each key, equal `idset_hash, etag, last_modified, tier, next_due, last_crawled, consecutive_unchanged, consecutive_errors, throttle_score`.
- **Cursor:** `next_cursor_A == next_cursor_B` and all shards agreed.
- **Row count:** `COUNT(*) FROM jobs` equal (belt-and-suspenders on the gate).

### 9.4 Unit-level companions (fast, no crawl)

- `merge_crawl_shards`: two hand-built shard fresh DBs with disjoint ids → union equals their concatenation; a duplicate id across shards (synthetic) → `INSERT OR IGNORE` keeps the first, no error (documents the negligible edge, §11).
- `merge_states`: prev + two disjoint shard deltas → correct overlay; a board in no shard → prev retained; assert order-insensitivity (swap shard order → identical result).
- `shard_boards` on the fixture → partitions cover the whole set with no overlap (∪ = all, ∩ = ∅), and shard 0 is empty when join is excluded (§3.1).

## 10. Risks — proven offline vs. measurable only on a real run

**Proven offline (by §9):**
- Merge determinism / byte-identity of `jobs`, `job_sources`, `companies`, `board_state`, cursor.
- Host-politeness preserved (each bucket on one shard — `shard_boards` invariant, plus the fixture split assertion).
- Flag-off byte-identity (defaults `None` ⇒ untouched paths).

**Measurable ONLY on a real run:**
- **The actual wall-clock win.** The floor is the *slowest single host-bucket*, not `total/20`. `shard_boards` is host-hashed, NOT load-balanced, and politeness forbids splitting a bucket — so if `apply.workable.com` (~3/s crawl) and `bamboohr` both land on one shard, that shard is the pole. Target ~30-40 min per `build-index.yml`'s host-rate notes (`build-index.yml:168-169`), but **measure per-shard wall-clock on run 1**; if one shard dominates, pin known-heavy buckets to dedicated shards (the `MEGAHOST_SHARDS` technique the drain already uses, referenced at `freshness_shard.py:14-16,28-33`).
- Real 429 behaviour under fan-out — expected identical to today (per-bucket rate is unchanged: one bucket, one shard, one token bucket), but confirm on the first scheduled run, exactly as `drain-detail.yml:53-59` advises watching the first run.
- Reduce disk/memory when merging 20 real fresh DBs + staging the prev index — should match today's single build's footprint, but verify no ENOSPC (the drain hit this once, `drain-detail.yml:84-90`).
- Per-shard prev-index download bandwidth (§8).

**Correctness risks and mitigations:**
- **A shard fails mid-crawl:** its boards emit no outcome ⇒ not in `crawled_keys` ⇒ `carry_forward` keeps their prev rows (no loss), and their `board_state` isn't advanced ⇒ they stay `due` and re-crawl next run. Self-healing, mirrors today's per-board carry-forward. The `_gated_publish` row-floor gate (`build_index.py:1075`) is the backstop against a mass-failure collapse. Reduce runs on `if: !cancelled()` and merges whatever finished (mirrors `drain-detail.yml:200`).
- **Never-seen board double-crawl:** prevented by applying `shard_boards` to the `_new_boards` pull-in too (§5.2). If missed, two shards crawl it and both stamp identical rows (same `id`) — `INSERT OR IGNORE` dedups, so at worst a wasted fetch, never corruption. Covered by a §9.4 assertion.
- **Same `source_job_id` on two boards of subdomain-token sources landing on different shards** (theoretical): `id = sha1(source:source_job_id)` could then collide across shards. `source_job_id`s are per-company/per-board and this cannot happen for the fixed-host sources (same bucket → same shard); for subdomain sources it would require two companies to share a raw source id — effectively impossible, and even single-process resolves it by `INSERT OR IGNORE` first-writer-wins. Negligible, pre-existing, documented in the §9.4 unit test.

## 11. Phased rollout

1. **Ship dark.** Land `merge_states`, `_crawl_due` shard args, `--crawl-shard-only`, `merge_crawl_shards.py`, `--crawl-reduce`, and `crawl-mapreduce.yml` **as `workflow_dispatch`-only** (comment out the `schedule:` block). All new code paths are gated on `--shard/--num-shards` being set, so `build-index.yml`'s non-join crawl is **byte-identical** to today (defaults `None`).
2. **Offline gate.** The §9 parity test must pass in CI (`ci.yml`). This *proves correctness* before any real fan-out.
3. **Shadow run.** `workflow_dispatch` the new workflow once against live boards, WITHOUT retiring the single-process non-join cron. Compare the reduce's published `jobs` identity columns against the same day's single-process build (diff the two `index.sqlite` on the §9.3 column set). Measure per-shard wall-clock (§10).
4. **Flip.** Enable `crawl-mapreduce.yml`'s `04:17` `schedule` and **remove the non-join `04:17` cron from `build-index.yml`** (leave the join `10:17` cron intact) — in one commit, so the non-join crawl has exactly one owner and the `build-index` concurrency group prevents any overlap between the reduce and the join shard.
5. **Rollback.** Re-add the non-join cron to `build-index.yml` and disable `crawl-mapreduce.yml`'s schedule — one commit, no data migration (both write the same `index-latest` assets in the same schema).

## 12. Non-goals / out of scope

- Join partition (stays single-process on its own `10:17` cron).
- Folding join into shard 0 of the matrix (a tempting future simplification the freshness sweep already does, but explicitly out of scope: "keep join as its own workflow").
- The `reserve_isolated=False` `shard_boards` refinement (§3.1) — only if measurement shows the idle shard 0 matters.
- Load-balancing heavy host-buckets across shards — forbidden by politeness; the mitigation is dedicated shards for known-heavy buckets (§10), only if a real run shows imbalance.
- Any change to the downstream sidecars (detail/embed/freshness/liveness) — they already merge; R3 only changes who produces the core `fresh.sqlite`.

## 13. File-change summary

| File | Change |
|---|---|
| `src/ergon_tracker/index/scheduler.py` | + `merge_states(prev, shard_states)` (pure) |
| `scripts/build_index.py` | `_crawl_due` gains `shard`/`num_shards` (default None); + `--crawl-shard-only` mode (MAP); + `--crawl-reduce` mode (REDUCE = today's `main()` tail minus the crawl call) |
| `scripts/merge_crawl_shards.py` | NEW — union K fresh DBs + K JD sidecars (`INSERT OR IGNORE`, per-shard resilience) |
| `.github/workflows/crawl-mapreduce.yml` | NEW — 20-shard `map` matrix (artifacts) + `reduce` job (release) + `notify` |
| `.github/workflows/build-index.yml` | on cutover: remove the non-join `04:17` cron (keep join `10:17`) |
| `tests/test_crawl_mapreduce_parity.py` | NEW — the §9 offline parity test + §9.4 unit companions |

No changes to `freshness_shard.py`, `build.py` (`build_index_from_fresh_db`/`carry_forward`/`changed_companies_sql` reused as-is), `dedup.py`, `models.py`, `mapping.py`.
