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
| **degree** (`degree.py`) | **level 88.6% recall / 99.5% precision**; **scope (req-vs-pref) 59.7%** — the hard half | measured on the corpus below | ✅ 402-record real corpus + ratcheting gate (`test_degree_recall.py`) |
| **yoe** (`yoe.py`) | **97.8% recall**; **87.7% precision** on an adversarial-negative set (field precision higher) | measured on a 539-record real corpus | ✅ 539-record real corpus + ratcheting gate (`test_yoe_recall.py`) |
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

Order (impact × how-unproven): ~~**degree first**~~ **✅ DONE (2026-07-05)** → ~~**yoe**~~ **✅ DONE (2026-07-06)** → **skills next** → level → geo.

**yoe.py result (2026-07-06):** same rig — 540 "<number> years/months" windows fetched across 237
companies / 8 ATS (`scripts/build_yoe_corpus.py`, a net wider than the extractor), blind-labeled by
an 8-agent fleet, 539 English records. **Recall 94.4%→97.8%, precision 64.6%→87.7%** (the negatives
are adversarially enriched with company-age numbers, so field precision is higher). Real bugs the
corpus forced: **"6–10+ years" now opens the top** (was capping at 10 — the biggest recall win),
company/product-age vetoes ("for over 35 years, we've…", "45 year track record", "30+ years of
expertise, we combine"), and **month-denominated noise** (contracts/training/timelines) now needs a
real experience cue. Also fixed 5 systematic gold-label slips against the rubric. **This axis is
production-grade** — the fit rubric can use `years_min`/`years_max` as a hard filter.

**degree.py result (2026-07-05):** benchmarked with `comp.py` rigor — 520 real education windows
fetched across 250 companies / 9 ATS (`scripts/build_degree_corpus.py`, a net *wider* than the
extractor so recall gaps show), blind-labeled by an 8-agent fleet, scoped to the 402 English
records (multilingual is a separate known gap). Numbers + the fixes they forced:
- **degree_min (level): 88.6% recall / 99.5% precision** (from 75%/94% at first measure). Fixes:
  guarded bare `"Degree in X"` / `"university degree"` / `"4-year <field> degree"` (recall), and
  killed the `"master of <non-academic>"` idiom, `MSC`-company, and possessive `"Master's <noun>"`
  ship-rank false positives (precision). **This axis is production-grade.**
- **degree_required (scope): 59.7%** — the genuinely hard half (literature ~74%). The corpus proves
  it is **NOT fit-rubric-grade**: the fit rubric must use `degree_min` as the hard filter and treat
  `degree_required` as *advisory only*, never as an authoritative A–F input. **This is exactly the
  "avoid confident-but-wrong" outcome the plan was built to catch.**

### Phase C — Moat-aligned tools (each ships only after its fields pass Phase B)
1. **Fit rubric** in `assess_fit` — A–F over weighted dims + a don't-bother threshold. Hard requirements
   (degree, yoe, salary, location) scored **deterministically** from our columns (cheap/consistent),
   soft fit via model. **Depends on:** degree, yoe, salary, level, geo all benchmark-passing. Each score
   carries provenance (`extracted` vs `unknown`); unknown fields are not scored, never imputed.
   **Degree caveat (measured 2026-07-05):** gate on `degree_min` (88.6%/99.5% — solid); `degree_required`
   is only 59.7%, so it may narrow/annotate but must **not** flip a pass/fail on its own.
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
Benchmarked so far (all with the same fetch→window→blind-fleet→ratcheting-gate rig):
- ~~`comp.py`~~ ✅ (salary) — 100%/100% on a 227-record corpus.
- ~~`degree.py`~~ ✅ (2026-07-05) — `degree_min` 88.6%/99.5% (grade); `degree_required` 59.7% (advisory).
- ~~`yoe.py`~~ ✅ (2026-07-06) — 97.8% recall / 87.7% precision (grade). Fit rubric can gate on years.

**Next: benchmark `skills.py`** (the gazetteer — never audited) with the same rig, retargeted to skill
mentions; it needs a normalization pass ("React" == "React.js") before the skills-gap tool. Then
`level.py` → `geo.py`. **No tool before its fields have a number.**
