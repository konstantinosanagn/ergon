# Hybrid Sector Classifier — Design Spec

> **Created:** 2026-07-06 · **Status:** approved, pre-implementation.
> **Goal:** raise `sector` (company→industry) from **72.4% accuracy / 26.7% coverage** toward high
> accuracy AND materially higher coverage, while staying **free, offline, CPU-at-runtime,
> laptop-safe**, license-clean for the PyPI roadmap, and **deterministic-first** (ML is the last
> resort, never the first). Sibling docs: `docs/extraction-baseline.md`, `docs/enrichment-and-tools-plan.md`.

## Motivating research (2026-07-06 parallel survey)
Four web-search research passes converged on:
- **Company NAME is a weak feature; descriptions/DOMAIN carry the signal.** Every high-scoring result
  in the literature (BERT-on-website 0.88 macro-F1 / 13 labels; InProC >0.92 NAICS) uses text/domain,
  not names. Our 27% coverage is a *weak-input* problem, not a *weak-model* problem.
- **The real coverage lever is a domain→industry data join**, not ML: PDL Free Company Dataset (7M,
  CC-BY-4.0), BigPicture (17M, ODC-BY), Wikidata P452 (CC0), **SEC EDGAR SIC codes** (public domain).
- **For the ML residual**, the literature-backed, free, CPU recipe is a **frozen sentence-embedding +
  L2-regularized logistic-regression probe** (Bank of England 2025; BlackRock 2023; InProC 2022).
  Keep **bge-small** (already a dep via fastembed; tops MTEB-Classification for its tier). **Skip
  SetFit** (needs torch, won't reuse frozen vectors, marginal past ~16 shots/class). Abstain via a
  **calibrated-probability + margin + centroid-distance** gate to preserve precision.
- No off-the-shelf model matches our 27-label taxonomy → we don't reuse a supervised model; we do
  reuse zero-shot `deberta-v3-base-zeroshot-v2.0` (MIT) as a **dev-only accuracy ceiling reference**.

Sources: arXiv 2305.01028, MDPI Information 15/2/77 & 15/2/89, arXiv 2305.13532 (InProC), arXiv
2308.08031 (BlackRock), SetFit (2209.11055), SimpleShot (1911.04623), sklearn calibration docs,
PDL/BigPicture/Wikidata/SEC-EDGAR/Census-NAICS.

## Architecture — a 4-tier cascade (most-precise → most-general, abstain over guess)
```
company (name, domain, example JD title)
 Tier 0  name/brand rules (existing name_sector + company_sector)   ← keep, highest precision
 Tier 1  DATA-JOIN GAZETTEER (domain→sector, then name→sector)       ← NEW: coverage spine (deterministic)
 Tier 2  EMBEDDING CLASSIFIER (bge-small + calibrated logreg)        ← NEW: ML residual, abstains
 Tier 3  unknown (None)                                              ← abstain > wrong guess
```
`SectorExtractor.extract(inp)` runs the tiers in order and returns the first non-None; each tier only
sees the prior tier's misses.

### Tier 1 — data-join gazetteer (deterministic, the coverage spine)
- **Scope (now):** build the gazetteer by joining **only our crawled companies** (~58k registry
  boards / ~44k index companies) against the free datasets → a **bounded artifact (~few MB
  compressed)**, refreshed offline periodically. *Future extension (flagged by product): evaluate a
  global domain→sector dataset for open-ended coverage.*
- **Sources & priority (authoritative wins on conflict):**
  1. SEC EDGAR submissions (public domain) — name/ticker → SIC-4digit (human-assigned). Also the
     training-label source (see Tier 2).
  2. Wikidata P452 (CC0) — domain(P856)/name → industry Q-item.
  3. PDL Free Company Dataset (CC-BY-4.0) — eTLD+1 domain → LinkedIn-industry. Primary spine.
  4. BigPicture (ODC-BY) — same LinkedIn enum, fills domains PDL misses.
- **Two one-time static crosswalks** (public-domain inputs: Census NAICS 2022, SEC SIC list,
  NAICS↔SIC concordance): LinkedIn ~147-enum → our 27 labels; SIC-4digit (~1,000) → our 27 labels;
  Wikidata Q-items roll up via subclass.
- **Runtime:** normalize domain to eTLD+1 → dict lookup; fallback normalized-name lookup. Instant.
- **Licensing:** PDL (CC-BY) + BigPicture (ODC-BY) require **attribution** → a `NOTICE` in the repo.
  SEC/Wikidata carry none.

### Tier 2 — embedding classifier (ML residual)
- **Input string:** `"{name}. {domain-without-TLD}. {example JD title}"`.
- **Features:** bge-small (fastembed) embedding, **CL2N** (mean-center then L2-normalize) + appended
  **one-hot TLD dims** (`.ai`/`.io`→tech, `.bank`→finance, `.edu`→education, `.gov`→government, …).
- **Head:** multinomial **logistic regression**, L2-regularized, `class_weight="balanced"`, `C` chosen
  by stratified CV; **Platt-calibrated** (`method="sigmoid"`, not isotonic — <1000 samples). A
  nearest-class-centroid kept as a robust fallback for thin classes.
- **Training labels — quality upgrade:** train/eval on **SEC-EDGAR-SIC-derived + gazetteer-join
  labels** (authoritative), NOT the AI-blind labels. (Stage-1 PoC bootstraps on the existing 700 blind
  labels for a first read; Stage-3 retrains on the upgraded labels.)
- **Abstention (3-gate):** predict a sector only if calibrated top-1 prob ≥ τ_prob AND
  margin(top1−top2) ≥ τ_margin AND cosine-to-predicted-centroid ≥ τ_sim. Thresholds tuned on
  validation to a **target precision (≥~85%)**; report achieved coverage. Else → Tier 3 (unknown).
- **Runtime:** weight matrix + centroids + thresholds extracted to `.npz`; inference is **numpy-only**
  (no sklearn shipped). Embeds only Tier-0/1 misses (the residual) → cheap.

## Staged delivery (each stage independently shippable)
- **Stage 1 — PoC (first, de-risking gate):** Tier 2 alone on the *existing 700-company corpus*,
  stratified held-out split + 5-fold CV. Report accuracy-at-coverage, macro-F1, per-class vs the
  72.4%/26.7% baseline. **Self-contained, no downloads, ~1 hr.** If ML doesn't beat the baseline on
  our features, pivot to data-only (Tier 1) and skip Tier 2 investment.
- **Stage 2 — data-join gazetteer (Tier 1):** the offline data pipeline + crosswalks + shipped
  gazetteer artifact. The main coverage win.
- **Stage 3 — wire the cascade:** integrate Tiers 0-1-2-3 into `SectorExtractor`, retrain Tier 2 on
  upgraded labels, re-benchmark end-to-end, publish the number, ratchet the gate.

## Evaluation
- Metrics: **macro-F1 + per-class F1 + accuracy-at-coverage (risk–coverage curve) + coverage%**, vs
  72.4%/26.7%. Stratified held-out split; 5-fold CV on the small data.
- Labels upgraded to SEC-EDGAR SIC where available; provenance reported.
- `deberta-v3-base-zeroshot-v2.0` run once in dev as the ceiling reference (not shipped).
- Gate: extend `tests/test_sector_recall.py` with accuracy-at-coverage + coverage floor (ratcheting).

## Dependencies, artifacts, cadence
- **Deps:** `scikit-learn` (+ dataset tooling) are **train/offline-only** → a `[sector-train]` extra.
  Runtime stays `fastembed` (`semantic` extra) + numpy. **No new runtime hard dep.**
- **Artifacts** in `src/ergon_tracker/registry/data/`: `sector_gazetteer.json(.gz)` (domain/name →
  sector), `sector_clf.npz` (weights + centroids + thresholds + label list), and a `NOTICE`
  (attribution).
- **Offline build scripts:** `scripts/build_sector_gazetteer.py`, `scripts/train_sector_classifier.py`.
  Rebuilt periodically like `sectors.json` — **not per daily build**; multi-GB dumps are downloaded to
  a cache, processed, discarded; only compact artifacts are committed.

## Optimization & stress-testing (cross-cutting, mandatory in EVERY stage)
Every step is built concurrency-first and memory-bounded, and **stress-tested on a small sample
before any full run** (the recurring session lesson: the rich-tier OOM came from `fetchall()` on a
large table + per-worker model copies; the fix was chunked streaming + single-process embedding +
CI-gated parallelism). Applied here:
- **Tier 1 data pipeline (`build_sector_gazetteer.py`):** the source dumps are 7–24M rows / multi-GB.
  **Never load a dump fully into memory** — stream/iterate in chunks (`fetchmany`/line-iterate),
  join against *our* bounded company set via an in-memory index of just our ~60–100k
  domains/names (small), and parallelize the independent per-source parse+crosswalk with a bounded
  `ProcessPoolExecutor` (CI-gated worker count, laptop-safe local default). Downloads run
  concurrently (bounded). Output is the compact committed artifact; dumps are discarded.
- **Tier 2 embedding:** batch through fastembed with a bounded batch size, **single-process**
  embedding (no per-worker model copies), reusing the rich-tier's proven memory-safe pattern; embed
  only the residual/labeled set, not the world.
- **Training:** the logreg + CV is cheap; the cost is embedding the labeled set — batch it. Threshold
  tuning sweeps are vectorized (numpy), not Python loops over records.
- **Stress-test gates (before any "full" run):** (1) run the data pipeline on a **small sample**
  (a few thousand rows per source) and assert join/crosswalk correctness + a bounded peak-RSS before
  the full multi-GB pass; (2) stress the embedding at target scale with a memory watch (assert peak
  RSS stays bounded, mirroring the rich-tier validation); (3) validate the classifier on the
  held-out split + 5-fold CV *before* committing to a full retrain on the upgraded labels. Each
  heavy step logs peak memory + wall time and fails fast if it exceeds a laptop-safe budget.
- **Laptop safety:** all heavy work (multi-GB joins, at-scale embedding) is **offline/one-time or
  CI-gated**, never in the per-daily build; local defaults are conservative, aggressive parallelism
  is env-gated (same pattern as `ERGON_SHARD_WORKERS`/`ERGON_CRAWL_CONCURRENCY`).

## Constraints honored
Free · offline · CPU-at-runtime · laptop-safe (heavy data/embedding work is offline/one-time, not in
the daily build) · deterministic-first (ML is Tier 2, after rules + data-join) · license-clean for
PyPI (all sources MIT/BSD/Apache/CC0/CC-BY/ODC-BY/public-domain; attribution NOTICE added) · abstains
rather than guesses.

## Out of scope (YAGNI)
Fine-tuning a transformer; runtime LLM/zero-shot calls; a global (non-crawled) company dataset (future
extension); descriptions scraped from company websites (Tier-2 uses name+domain+JD-title only for now).

## Open items to settle in the plan
- Exact one-hot TLD dimension list.
- Gazetteer artifact format/size cap + gzip decision.
- Whether Stage-1 PoC keeps the 700 blind labels or pulls a quick SEC-EDGAR label subset first.
- Target-precision value for the abstention threshold (start ≥85%, tune).
