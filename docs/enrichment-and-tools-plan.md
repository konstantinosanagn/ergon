# Enrichment-Quality & Moat-Aligned Tools — Plan

> **Created:** 2026-07-05 · Supersedes the ad-hoc "what to build next" chatter after the
> career-ops / ai-job-search competitor pass (see `landscape-job-fetching-tools.md` §11).
> **Sibling docs:** `extraction-baseline.md` (point-in-time quality snapshots),
> `extraction-labeling-guide.md` (how to hand-label a corpus), `expansion-roadmap.md` (coverage).

## The governing principle
**Extraction quality is the foundation; the tools are the superstructure.** A fit rubric, a skills-gap
report, or a salary benchmark is only as honest as its *worst* field. Shipping a rubric on a `degree_min`
that's wrong 20% of the time is **worse than shipping nothing** — it renders a confident, authoritative,
sortable A–F grade that is *wrong*, and users trust it. So: **no user-facing tool ships on a field that
doesn't have a measured precision/recall/coverage number next to it.**

This is why the sequence below puts a quality program *before* the tools, not after.

## Where extraction quality actually stands (be honest)
Only **one** extractor has a real number. The rest are "reasonable code + unit tests," which is not the
same as "measured against hundreds of real JDs."

| Field | Precision/recall (when JD states it) | Coverage (how often populated) | Benchmarked at scale? |
|---|---|---|---|
| **salary** (`comp.py`) | ~100% recall / 100% precision | ~35–40% (gated by JD access) | ✅ 227-record real corpus + ratcheting gate (`test_comp_recall.py`) |
| **degree** (`degree.py`) | Unknown — required-vs-preferred scope is the hard half (~74% in the literature) | Unknown | ❌ 78 unit tests + 2 real JDs only |
| **yoe** (`yoe.py`) | Conservative, precision-first | Unknown | ❌ |
| **level** (`level.py`) | ~44% unknown after yoe→level default-on | Improved, still gaps | ❌ |
| **geo/country** (`geo.py`) | NULL-country (Workday placeholder) bug fixed | Improved | ❌ |
| **skills** (`skills.py`) | Gazetteer; never audited | Unknown | ❌ |
| **sponsorship** (`sponsorship.py`) | Reliable — extracted from full JD at crawl (caught BTIG's deep "no sponsorship" line) | tri-state, "unknown" common | partial |

## Two axes of quality — both required
1. **Precision/recall** — is the field *correct* when the JD states it?
2. **Coverage** — how often is it populated *at all*? Gated by **JD access**: ~half of JDs are on
   JS-rendered ATS (Workday/iCIMS/Phenom) we can't read until the **rich tier** lands. The rich tier is
   the coverage foundation; the benchmark program is the correctness foundation.
3. **Honest "unknown" (non-negotiable)** — never impute. A genuinely unstated field is `null`/`unknown`
   and is *excluded* from scoring, not guessed. (Research: keep Unspecified first-class.)

## The plan (foundation → superstructure)

### Phase A — Coverage foundation: the rich tier
Read **all** JDs, not just the readable half. Status: memory-safe reconcile shipped; `--rich` re-enabled
in CI (`9f72cef`), first 1.4M-scale run validating now. This unlocks coverage for every field.

### Phase B — Extraction-quality program (the gate for everything downstream)
For **each** extractor, replicate the `comp.py` method exactly — it is the template:
1. **Real hand-labeled corpus** — 150+ positives fetched from live Greenhouse/Lever JDs + 40+ FP traps,
   labeled per `extraction-labeling-guide.md`, spot-checked by hand.
2. **Precision/recall gate test** — a ratcheting `test_<field>_recall.py` that fails CI on regression.
3. **Publish the coverage %** per field in `INDEX_STATUS.md` — *no competitor publishes this*; it's both a
   marketing weapon and a forcing function for honesty.

Order (impact × how-unproven): **degree first** (newest, least validated, the fit rubric's biggest
dependency, scope-detection is genuinely hard) → yoe → skills → level → geo.

### Phase C — Moat-aligned tools (each ships only after its fields pass Phase B)
1. **Fit rubric** in `assess_fit` — A–F over weighted dims + a don't-bother threshold. Hard requirements
   (degree, yoe, salary, location) scored **deterministically** from our columns (cheap/consistent),
   soft fit via model. **Depends on:** degree, yoe, salary, level, geo all benchmark-passing. Each score
   carries provenance (`extracted` vs `unknown`); unknown fields are not scored, never imputed.
2. **Skills-gap tool** — aggregate required skills across matching roles vs. a résumé. **Depends on:**
   skills extractor benchmarked + a normalization pass ("React" == "React.js").
3. **Salary-benchmark tool** — percentiles by role/geo. **Depends on:** salary (done) + role/geo quality.

### Phase D — More filters (only after existing ones are hardened)
A mature rubric wants fields we don't extract yet: **citizenship/clearance-required**, **travel %**, and
**skills normalized to a taxonomy**. Add these *after* the current set is measured-good — adding filters
on an unmeasured base multiplies sloppiness rather than reducing it.

## What NOT to build (scope discipline)
No CV-generation / cover-letter / interview-prep suite. That's the career-ops/ai-job-search apply-layer —
crowded, 58.6k-star incumbent, and off our data moat. **Stay the substrate.** Expose fit/skills/salary
tools that *use* our data; aim to be the enriched-index/MCP/QUERY backend those apply-layer tools plug into.

## Immediate next step
Benchmark **`degree.py`** with `comp.py` rigor (real corpus, hand-labeled incl. required-vs-preferred
scope, precision/recall, ratcheting gate). If it clears ~95%, the fit rubric has earned its foundation;
if it's ~78%, we just avoided shipping confident-but-wrong fit scores. **No tool before its fields have a
number.**
