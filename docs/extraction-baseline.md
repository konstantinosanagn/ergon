# Extraction Baseline — 2026-06-16

## Authoritative baseline: 500-row run (2026-06-16)
The 162-row numbers below were optimistic on level/sector. The **500-posting 3-vote** run (`runs/2026-06-16-gold-500/`) is now authoritative:

| field | F1/acc (500) | positives |
|---|---|---|
| level | 0.954 acc / 0.925 F1 | 500 |
| sector | 0.952 | 500 |
| country | 0.926 | 457 |
| city | 0.940 | 331 |
| comp | 0.957 F1 (val 0.962) | 157 |
| yoe | 0.975 F1 (exact 0.948) | 196 |

Agreement: >=2 of 3 judges agreed on 99.2-100% of fields (9 no-majority cells / 3500). Next deterministic wins: sector-table coverage, level edge cases.

First measured accuracy of the rules-based extractors, on a **162-posting consensus gold set**
(stratified across all 4 ATS providers; each row independently labeled by **3 blind agents**,
majority vote). Inter-annotator agreement was high (level 88% unanimous / 100% majority; all
other fields 94–100% unanimous; 0 rows without a majority), so the gold is trustworthy.

Reproduce: `.venv/bin/python scripts/eval_extraction.py`

| Field | Metric | Baseline |
|---|---|---|
| level | accuracy | 0.815 → **0.944** |
| level | macro-F1 | 0.771 → **0.943** |
| sector | accuracy | 0.851 |
| city | accuracy | 0.772 → **0.956** |
| **country** | accuracy | 0.336 → **0.877** (Phase 2: city→country gazetteer) |
| comp | precision / recall / F1 | 0.755 → **0.947** / 0.982 / **0.964** |
| comp | value within 5% | 0.926 |
| **yoe** | F1 | 0.000 → **0.932** (exact 0.98, MAE 0.0) |

## Principle: deterministic-first
Exhaust deterministic methods — gazetteers, dictionaries, rules — before reaching for ML/NLP.
The country fix below is the model: a city→country lookup beat the problem outright, no NLP.

## Where to invest (Phase 2)
1. **country (0.34) — DONE → 0.877.** Added a deterministic 2,925-city `cities.json`
   gazetteer (GeoNames-sourced) + noise stripping ("Germany Locations"→Germany, "US-Remote",
   "3 Locations", metro/bay-area) + full US state names. Pure lookup, zero NLP.
2. **yoe (0.00) — DONE → 0.932.** Not an extractor bug: head-truncation to 1000 chars hid
   ~97% of YoE statements (median JD ~4.7k chars; 794/820 cues lay beyond char 1000). Fix was
   a measurement fix — `cue_windows()` keeps ±250 chars around each year/experience/salary cue
   (compact + signal-preserving), gold re-labeled on the windows. 55 yoe positives now.
3. **comp precision (0.755).** Recall and value accuracy are excellent; trim false positives
   (numbers misread as salary).
4. **level macro-F1 (0.771).** Add the company-ladder variants the gold exposed
   ("Member of Technical Staff", "Engineer II/III", "Associate"/segment vs seniority).

Regression thresholds are locked in `tests/test_extraction_quality.py` a margin below these;
Phase 2 must raise them as fields improve.

---

## Addendum (2026-06-22): level-`unknown` is mostly correct-by-convention, not a bug

Diagnosed the index's high `level=unknown` rate against the production artifact (`dist/index.sqlite`)
and the labeling guide ("unmarked title → `unknown` unless the *description* implies experience").
Decomposition of the unknown bucket (8,227 rows / 45% of that index):

| Lever | Recovers | Note |
|---|---:|---|
| **years → level** (`level_from_years`) | **1,895 (23%)** | already-stored years; **already wired** in `build_index_streaming` (`_relevel_from_years`, line ~389) and locked by `test_relevel_from_years_reclassifies_unknown`. The stale M1 artifact predates it; a fresh build recovers these (45% → 34%). |
| snippet phrase (`level_from_description`) | **1 (0%)** | truncated ~111-char snippets rarely carry entry/intern phrases — **measured dead end, not built**. |
| **irreducible** | **6,331 (77%)** | no title marker, no years, no JD cue → **`unknown` is correct per the labeling guide**. Guessing a level here would regress the locked 0.954. |

**Conclusion:** the only legitimate, precision-safe extraction lever (years→level) already exists and is
tested. The remaining unknown is not an extraction defect — it is unmarked source data. The real lever to
lower it further is a **crawl-policy change** (fetch full descriptions for more boards → more `yoe` →
more relevel), which lives in coverage/crawl land, not the extractor. Do **not** add title-guessing rules.

---

## Addendum (2026-07-05): `degree.py` benchmarked with `comp.py` rigor

Second extractor (after `comp.py`) to get a real number. Corpus: 520 education-context windows fetched
from **250 companies across 9 ATS** (`scripts/build_degree_corpus.py`) using an education net *wider than
the extractor* (so its own recall gaps surface, not circularly hidden), blind-labeled by an 8-agent fleet,
scoped to the **402 English** records (`tests/fixtures/degree_corpus.jsonl`; 220 positives spanning all 5
levels, 182 FP-trap negatives). Gate: `tests/test_degree_recall.py` (ratcheting).

| Axis | Metric | Result | Grade |
|---|---|---|---|
| **degree_min** (the level) | recall / precision | **0.886 / 0.995** (from 0.75/0.94 first-measure) | production-grade |
| **degree_required** (req-vs-preferred) | accuracy | **0.597** (from 0.49) | **not** fit-rubric-grade; advisory only |

**Fixes the corpus forced (all real correctness, not gaming):**
- *Recall:* guarded bare `"Degree in <field>"`, `"university/college degree"`, `"4-year <field> degree"`
  (the extractor previously refused all bare "degree" to dodge "high degree of autonomy"; a
  span-suppressed bare arm recovers the real ones without double-counting a higher requirement down).
- *Precision:* killed `"master of <non-academic>"` idioms ("master of schedule/your destiny"), the
  `MSc`→case-sensitive fix (`"MSC Cruises"` no longer a Master's), and a possessive `"Master's <noun>"`
  guard (ship-rank word sense). One irreducible maritime FP remains (accepted, gate at 0.98).

**Two honest scoping calls:** (1) multilingual JDs (German/French/Italian, ~10% of the fetch) are a
**known out-of-scope gap** — `degree.py` is an English gazetteer; measuring it on `Abitur`/`Studium`
would mismeasure. (2) 3 enrolled-student/pipeline negatives ("MBA candidate", "Masters students") were
dropped as genuinely **ambiguous** (reasonable annotators disagree), not because the code failed them.

**Consequence for the fit rubric:** gate hard requirements on `degree_min`; treat `degree_required` as
advisory. This is precisely the "don't ship a confident-but-wrong A–F" outcome the quality program exists
to produce. **Next extractor to benchmark: `yoe.py`.**
