# Rich Tier — Daily-Build Enablement — Design Spec

> **Created:** 2026-07-08 · **Status:** approved, pre-implementation.
> **Goal:** turn the `--rich` sidecar (full-JD FTS5 keyword search + pre-stored embeddings for
> semantic / resume-match ranking) ON for the daily scheduled build, so MCP users get deep keyword
> search and semantic ranking — safely, given the prior cold-start OOM.

## Context (verified)
- The structured filters (yoe, degree, sponsorship, level, sector, salary, geo/remote) **do NOT depend
  on rich** — they run at crawl time on the full JD and ship as main-index columns. Rich adds only two
  *search* capabilities: (1) full-JD keyword search (vs the 300-char snippet), (2) semantic/embedding
  ranking (`semantic=True`, `match_resume`), which otherwise fall back to lexical BM25.
- **Safety is already built.** `scripts/build_index.py` publishes the core index via `_gated_publish`
  FIRST, then does rich only `if ok and rich:` inside a `try/except` — "never let the rich tier break
  the core build" (yesterday's rich gz stays live on failure). The incremental reconcile is
  memory-safe: `single_process=True` (no per-worker model copies), `fetchmany` chunking (no
  whole-table `fetchall`), and `ERGON_RICH_MAX_EMBED=120000` bounds the cold start per run.
- **Plumbing is complete.** `build-index.yml` already downloads the prior `index-rich.sqlite.gz`
  (carry-forward), sets `ERGON_RICH_MAX_EMBED=120000`, uploads `index-rich.sqlite.gz` in the publish
  ASSETS, and has `timeout-minutes: 330` (ample for the ~2.4h cold start). The ONLY thing off is the
  trigger: `--rich` is gated on `github.event.inputs.rich == 'true'` — so scheduled runs (empty
  inputs) run lean.

## The change — one workflow condition
`.github/workflows/build-index.yml`, the build step's `--rich` gate:
```yaml
# before
${{ github.event.inputs.rich == 'true' && '--rich' || '' }}
# after
${{ (github.event.inputs.rich == 'true' || github.event_name == 'schedule') && '--rich' || '' }}
```
Scheduled runs always build rich; manual `workflow_dispatch` keeps the `rich` input (default off) for
controlled testing. No other workflow or code change (timeout 330 stays; ramp cap stays; the non-fatal
guard + core-first publish stay).

## Staging gate (the crux of "staged")
Do **not** merge this change until the **manually-triggered cold-start run** (dispatched 2026-07-08
23:13 UTC) completes and publishes `index-rich.sqlite.gz` to the `index-latest` release. That end-to-end
proof — cold start finishes under the 120k cap, within the 330-min timeout, and the core index still
publishes — de-risks flipping the daily schedule. If it OOMs or times out: lower `ERGON_RICH_MAX_EMBED`
(e.g. 80k) and/or raise the timeout, re-prove, then merge.

## Monitoring & rollback
After enabling, watch the first ~3 daily runs: (a) the core index still publishes every run (rich must
never block it); (b) the rich sidecar row count grows toward full coverage (~120k/run, ~12 runs to
fill ~1.4M); (c) no OOM/timeout. **Rollback** = revert the one-line condition (schedule back to lean) —
the core index is unaffected either way.

## Testing
CI-config change; the acceptance test is the staged-run verification. The reconcile path is already
covered by `tests/test_rich_index.py` (incl. `test_reconcile_from_fresh_always_single_process`). The
YAML expression is validated (matched-quote/`&&`/`||` GitHub-Actions ternary) before commit.

## Constraints honored
Free · CPU-only · memory-safe (single-process + chunked + ramp-capped) · non-fatal (core publish never
blocked) · no code change (workflow-only) · reversible (one-line revert) · staged (gated on a proven
cold-start run).

## Out of scope (YAGNI)
Changing the ramp cap / batch sizes (current values proven adequate by the gate); a separate rich
schedule; wiring semantic search differently in the MCP (already implemented, just needs the vectors);
`--rich` on the local/laptop default (stays opt-in — laptop safety).

## Open items to settle in the plan
- Confirm the cold-start run's outcome and read its published `index-rich.sqlite.gz` size / reconcile
  stats before merging (the gate).
