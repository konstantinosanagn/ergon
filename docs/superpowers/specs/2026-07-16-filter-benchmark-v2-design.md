# Filter Benchmark v2 — exhaustive, large-sample accuracy at scale

**Status:** design (awaiting review)
**Date:** 2026-07-16
**Author:** kanagn (+ Claude)

## 1. Goal

Measure the real-world accuracy of **every filter** the SDK/MCP/CLI exposes, on a **large,
provider-diverse sample** (10k+ rows, not the current 182–899/field), now that the source set has
grown from the original 4 ATSes to 54 providers. Four objectives, in priority order:

1. **Diagnose per-ATS bugs** — a provider × field accuracy matrix that surfaces which newly
   integrated ATSes (iCIMS, SuccessFactors, Oracle, Eightfold, Radancy, Phenom, Coveo, PeopleAdmin,
   dejobs, UKG, ADP …) have extraction breaks.
2. **Publish defensible stats** — precision / recall / accuracy / coverage per field with
   confidence intervals, fit for the README/docs.
3. **Ratchet CI gates** — update the `*_GATE` regression thresholds from the bigger sample.
4. **Fix what we find** — a follow-up pass that repairs the worst defects and re-measures.

## 2. Background (measured, not assumed)

- Live index: **1.48M jobs / 40 active sources** (build-2026-07-16-67). ~**96% is live-JD-refetchable**.
- **No published artifact stores full JD text** anymore (the rich sidecar is vectors-only; the main
  index keeps only a 300-char snippet). So JD-dependent extractors **must be benchmarked against a
  live full-JD refetch** — the snippet is a truncated preview, not the extractor's real input.
- **Data is not the constraint; labeling is.** JD-in-bulk boards (greenhouse, ashby, lever,
  recruitee, teamtailor, pinpoint, jazzhr, dejobs, join, personio, workable) return full descriptions
  with one request per board (~410k+ JDs, zero N+1). Enterprise ATSes need per-posting detail fetches.
- Filters split into four regimes, each benchmarked differently:
  - **Deterministic text-rules (JD-required):** salary-text arm, yoe, degree, sponsorship_offered.
  - **Title/structured (no JD):** level, employment_type, remote, recency.
  - **Company-gazetteer:** sector, visa_sponsor, country, city.
  - **Direct ATS payload (accuracy = provider fidelity):** employment_type, posted_at, structured
    salary → **must be sliced per-provider**.
- Existing harness (to extend, not replace): per-field corpora in `tests/fixtures/<field>_corpus.jsonl`
  + ratcheting `tests/test_<field>_recall.py`; blind-agent-fleet labeling; the rubric in
  `docs/extraction-labeling-guide.md`; builders `scripts/build_<field>_corpus.py`.

## 3. The core idea — adjudication, not labeling

Hand-labeling 10k+ rows is infeasible. Instead:

1. **The LLM fleet labels everything.** A multi-model blind fleet independently labels every row for
   every field (majority vote), per the extended rubric.
2. **Two independent signals agree → auto-accept.** For each (row, field), we have the fleet's gold
   *and* the extractor's output. Where they **agree**, that value is accepted as ground truth with no
   human review (two independent methods concurring is strong evidence).
3. **The human adjudicates only disagreements**, ordered by value. This is what the Artifact is for.
   Human effort scales with the *disagreement rate*, not the row count.
4. **A random audit slice** (including agreements) calibrates the residual error — because agreement
   isn't proof (both could share a bias). This yields a published "labels are N% human-verified" +
   an estimate of the auto-accept false-rate.

**Triage priority for the human queue** (highest value first):
1. Extractor ≠ fleet, both non-null (hard conflict — where real bugs hide).
2. One side abstains (`unknown`/null), the other asserts a value (coverage / recall gap).
3. Fleet internally split, no majority (genuinely ambiguous rows).
4. Random sample of *agreements* (calibration; small, fixed budget).

Unreviewed rows keep the fleet/auto label, flagged `review_state: unreviewed`, so metrics can be
reported both "all rows" and "human-confirmed only."

## 4. The Artifact — "Label Auditor"

A self-contained HTML page (published as an Artifact; strict CSP, no external calls). Data flows by
files, not network:

- **Load:** a file-picker reads the local `audit_queue.jsonl` (the triage-ordered rows). No embedding,
  so it scales to any size.
- **Per row it shows:**
  - Posting context: `title`, `source` (ATS), `company`, `location_raw`, the **full JD** (scrollable
    pane), structured salary if present, and the **apply/posting URL as a clickable link** so you can
    open the live posting and verify yourself.
  - A per-field table: **what the extractor produced** vs **what the fleet labeled**, with the
    agreement/conflict state color-coded and the triage reason.
- **Your action per contested field:** `extractor is right` / `fleet is right` / `enter the correct
  value` / `ambiguous — skip`. One-key shortcuts; the row advances automatically.
- **Ergonomics:** progress bar, `localStorage` autosave (survives refresh), jump-to-next-conflict,
  filter by field or provider, and a running tally.
- **Export:** downloads `corrections.jsonl` — one record per adjudicated (row, field) with your
  verdict and any corrected value. That file is handed back and ingested by the harness.

This directly matches the requested flow: *see what the extractors manage, confirm the output, or
open the URL and check it yourself.*

## 5. Corpus architecture — one mega-corpus + supplements

- **`bench/corpus_jd.jsonl`** — the JD-bearing mega-corpus (~12–15k postings). Live crawl,
  **stratified to guarantee coverage** of every provider, sector, country, and language (EN/DE/FR/ES),
  not volume-proportional — small/new ATSes get a floor (e.g. ≥150 rows each where they exist).
  Carries full JD, structured fields, and the source URL. Labeled once for all JD + structured fields.
- **`bench/corpus_structured.jsonl`** — ~10k+ rows drawn straight from the 1.48M index for fields that
  need no JD (level, geo, sector, employment_type, remote, recency). Cheap to scale.
- **Targeted top-ups** (folded into the JD corpus): sponsorship *positives* (only ~2.6k exist in all
  1.48M — must be hunted), salary-disclosing geographies (US CO/NY/CA/WA, UK), degree-stating JDs, so
  the positive class is measurable, not swamped by base-rate nulls.

Reuses the crawl/sampling helpers in `scripts/` (`snapshot_corpus.py`, the `build_*_corpus.py`
strata logic) generalized to all 54 providers.

## 6. Metrics & report

Per field: **precision / recall / accuracy / coverage** with **Wilson score confidence intervals**.
Reported on both "all rows" and "human-confirmed only" bases. Two rules that keep it honest:

- **Coverage (data sparsity) is reported separately from extractor precision** — "the ATS never
  stated salary" must never be counted as "our extractor missed it."
- **The provider × field accuracy matrix is the headline diagnostic** — it localizes failures to
  specific ATSes.

New benchmarks added (currently unmeasured):
- **employment_type** — parsed value vs ATS-stated value (per-provider fidelity).
- **posted_at / recency** — presence + plausibility vs ATS-stated date; staleness distribution.
- **visa_sponsor** — precision of the DoL-LCA employer match (sampled matched employers verified).
- **dedup** — pair-sampled precision/recall: are merged clusters truly the same role; are distinct
  roles wrongly merged.
- **keyword & SQL-vs-client parity** — the FTS `_match_expr` vs `matches()` substring divergence, and
  the SQL-only `max_last_seen_age_days`; assert structured filters agree row-for-row where intended.

Output: `bench/report.md` (human) + `bench/report.json` (machine, drives the gate ratchet).

## 7. Program phases

0. **Harness + schema** — corpus/label/report data models; scoring library with CIs and the
   provider matrix; wire into a `bench/` area (kept out of the CI test path by default — these are
   heavy offline runs, distinct from the fast `tests/` gates).
1. **Crawl** — build `corpus_jd.jsonl` (stratified, all providers) + `corpus_structured.jsonl`.
2. **Fleet labeling** — multi-model blind labels for every row/field → `labels.jsonl`.
3. **Adjudication** — generate the triage-ordered `audit_queue.jsonl`; you drive the Label Auditor
   Artifact; ingest `corrections.jsonl`; compute label-quality calibration.
4. **Score** — per-field + per-provider metrics with CIs → `report.md` / `report.json`.
5. **Diagnose & fix** — repair the worst per-ATS/extractor defects the report surfaces; re-run
   scoring on the affected slices; confirm improvement.
6. **Ratchet & publish** — raise `*_GATE`s from the new numbers; refresh the enlarged
   `tests/fixtures/<field>_corpus.jsonl` gates; publish the accuracy table to docs/README.

## 8. File structure

```
bench/                              # new; heavy offline benchmark artifacts (git-tracked corpus/labels)
  corpus_jd.jsonl                   # JD-bearing mega-corpus (stratified, all providers)
  corpus_structured.jsonl           # index-sampled structured-only rows
  labels.jsonl                      # fleet labels (per row/field, with votes)
  audit_queue.jsonl                 # triage-ordered contested rows (input to the Artifact)
  corrections.jsonl                 # human verdicts (output of the Artifact)
  report.md / report.json           # metrics + provider matrix + CIs
scripts/bench/
  crawl_corpus.py                   # phase 1: stratified live crawl (all 54 providers)
  sample_structured.py              # phase 1: index-sampled structured rows
  build_audit_queue.py              # phase 3: triage ordering (extractor vs fleet)
  score.py                          # phase 4: metrics, CIs, provider matrix -> report
  label_auditor.html                # the Artifact source (published via the Artifact tool)
docs/
  extraction-labeling-guide.md      # extend with employment_type / recency / visa rubric
```

Labeling itself runs via the agent fleet (Workflow/Agent), not a committed script.

## 9. Non-goals / risks

- **Not** a live-CI job — the crawl + fleet labeling are heavy, run offline; only the ratcheted
  `tests/*_recall.py` gates stay in CI.
- **Sponsorship positives** are genuinely scarce (~0.2%); its sample stays smaller (few hundred–2k)
  and the report says so rather than faking a balanced set.
- **Auto-accept bias:** mitigated by the random-agreement audit slice; the report publishes the
  measured false-agreement rate, not an assumed zero.
- **Taleo (~11k, JS-rendered) and most apicapture** are not JD-refetchable — excluded from JD-field
  scoring and noted as a coverage gap, not counted against extractor accuracy.
- Corpus size on disk (JD text for ~15k rows) — store gzipped; keep in `bench/`, not the package.

## 10. Open questions for review

1. Is `bench/` (git-tracked corpus + labels) the right home, or keep the large JSONL out of git
   (e.g. a release asset) and track only the report + gates?
2. Fleet size / model mix for labeling (how many independent labelers per row; which models).
3. Human audit budget — how many contested rows are you willing to adjudicate (caps phase 3)?
