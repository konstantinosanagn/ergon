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
| **degree_required** (req-vs-preferred) | accuracy | **0.611** (from 0.49; +tight "or equivalent required") | **not** fit-rubric-grade; advisory only |

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

---

## Addendum (2026-07-06): `yoe.py` benchmarked with the same rig

Third extractor to earn a measured number. Corpus: 540 "<number> years/months" windows fetched from
**237 companies across 8 ATS** (`scripts/build_yoe_corpus.py`, a net wider than the extractor so recall
gaps show), blind-labeled by an 8-agent fleet, 539 English records (`tests/fixtures/yoe_corpus.jsonl`;
409 positives, 130 FP-trap negatives). Gate: `tests/test_yoe_recall.py`.

| Axis | Result | Grade |
|---|---|---|
| **recall** (exact min/max) | **0.978** (from 0.944) | production-grade |
| **precision** (adversarial negatives) | **0.877** (from 0.646) | worst-case; field precision higher |

**Bugs the corpus forced (all real correctness):**
- *Recall:* **"N–M+ years" now opens the upper bound** — `yoe.py` silently dropped the trailing `+`
  and reported `(N, M)`; "6–10+ years" means "6 or more" → `(6, None)`. Biggest single win.
- *Precision:* company/product **age & tenure** vetoes — before-framing ("for over 35 years"),
  after-tenure phrases ("N year track record", "N years of success/excellence"), and same-sentence
  company-achievement verbs ("30+ years of expertise, we combine"); plus **month-denominated noise**
  (contracts/training/timelines/probation) now requires a real experience cue.
- 5 systematic gold-label slips fixed against the rubric ("6–10+ years" mislabeled `(6,10)`;
  "less than 2 years" mislabeled `(2,None)`).

**Note on the 0.877:** the negative set is *adversarially enriched* — the broad net specifically
surfaces company-age numbers, which are far rarer in random JDs; field precision is higher. Recall is
the field-representative axis.

**Consequence for the fit rubric:** `years_min`/`years_max` are production-grade — usable as a hard
filter. **Next extractor to benchmark: `skills.py`.**

---

## Addendum (2026-07-06): `skills.py` benchmarked (set-valued)

Fourth extractor. Corpus: 800 requirement/skill-section windows fetched from **329 companies across 8
ATS** (`scripts/build_skills_corpus.py`, anchored on skill-CONTEXT cues so windows can contain skills
the gazetteer doesn't know yet), blind-labeled by an 8-agent fleet for the SET of concrete technical
skills present (`tests/fixtures/skills_corpus.jsonl`; 315 windows carry ≥1 skill). Gate:
`tests/test_skills_recall.py`.

| Axis | Result | How measured |
|---|---|---|
| **precision** | **0.995** (5/943 extractions are collisions) | word-sense collision rate — a deterministic matcher can't hallucinate, so raw "unlabeled extractions" are human UNDER-listing, not errors |
| **recall** | **0.927** vs human labels | of labeler-named in-vocab skills, fraction found |

**Why precision is a collision rate, not raw agreement:** `skills.py` matches literal surface forms,
so every extraction is genuinely in the text. Of 943 extractions only **5** were word-sense
collisions (`rest`, `excel`, `rails`, `sap`, `ruby`); the other 108 "unlabeled" extractions are real
skills the human labeler simply didn't list (javascript, aws, github…). Gating raw precision would
fail the suite every time we add a legitimate skill, so the gate scores the collision rate instead.

**Fixes the corpus forced:**
- *Precision:* an `excel`-verb guard ("excel at/in", "to excel") and dropping bare `rest` (kept
  `rest api`/`restful`) removed the top collisions. The pre-existing omission of bare
  `go`/`c`/`r`/`spring`/`swift` was validated — **zero** collisions from them.
- *Recall:* `llm` plurals ("LLMs", "large language models").
- *Coverage:* **+23 skills** the labelers surfaced (crm, powerpoint, microsoft office, github,
  gitlab, hubspot, devops, databricks, redshift, clickhouse, google analytics/ads/sheets, photoshop,
  illustrator, after effects, ios, android, erp, cad, revit, sas), 91→114. Collision-prone tokens
  labelers named (word, outlook, notion, slack, confluence) were deliberately NOT added.

**Consequence for the skills-gap tool:** the gazetteer's alias map IS the normalization pass the tool
needs, and it's now measured-good. **Deferred gap worklist** (vaguer/collision-prone): saas,
generative-ai, cloud, distributed-systems, data-modeling/pipelines/viz, business-intelligence,
claude/chatgpt/gemini. **Next extractor to benchmark: `level.py`.**

---

## Addendum (2026-07-06): `level.py` benchmarked on ENTERPRISE titles

Fifth extractor. Unlike the old 500-row gold (startup-heavy, greenhouse/lever), this corpus is 900
real postings from **enterprise ATS** (taleo, dejobs, apicapture, paycom, peoplesoft;
`scripts/build_level_corpus.py`), blind-labeled for the level the **title** conveys (the extractor,
`LevelExtractor.extract`, is title-only — the years→level relevel is a separate build step).
Gate: `tests/test_level_recall.py` (accuracy + macro-F1; multi-class).

| Metric | Result |
|---|---|
| **accuracy** | **0.820** (from 0.809 first-measure) |
| **macro-F1** (gold classes) | **0.736** (from 0.703) |
| accuracy on non-`unknown` gold | 0.703 |

**Honest finding — enterprise titles are materially harder than the old 0.954 gold.** The gap is
genuine title ambiguity, not simple bugs: bare "Associate" (entry in consulting, mid in banking),
IC-"X Manager" (Product/Treasury/Change Manager), "Supervisor" (first-line manager vs unknown),
dual-rank "Analyst/Sr Analyst" postings (one req, two levels), and numeric ladders ("Level 4",
"SPEC 3") — cases where blind human labelers and a deterministic classifier reasonably differ. Per
the repo labeling guide, unmarked titles → `unknown`, which the classifier follows.

**Fixes the corpus forced (clean, real gaps):** +intern forms (Student Assistant, V.I.E, Working
Student, Student Position), "Princ" abbreviation → principal, +IC-manager (Business Development /
Case Manager). intern F1 0.62→0.82, principal 0.55→0.67.

**Consequence for the fit rubric:** `level` is usable but weaker than comp/yoe — weight it lower and
never let an ambiguous level flip a pass/fail (same caution as `degree_required`). **Last extractor
to benchmark: `geo.py`.**

---

## Addendum (2026-07-06): `geo.py` benchmarked — extraction-quality program COMPLETE

Sixth and final extractor. Corpus: 800 DISTINCT location strings (deduped) from enterprise ATS
(taleo, dejobs, peoplesoft; `scripts/build_geo_corpus.py`), blind-labeled for country + city. Gate:
`tests/test_geo_recall.py`.

| Field | Result |
|---|---|
| **country accuracy** | **0.948** (from 0.770) |
| **city accuracy** | **0.889** (from 0.801) |

**Bugs the corpus forced — all enterprise-HRIS formats geo.py never parsed:**
- **2-letter ISO codes colliding with US states**: "Toronto, ON, CA" → CA=**Canada** (was California);
  "Cologne, NW, DE" → **Germany** (was Delaware); "Vadodara, …, IN" → **India** (was Indiana).
  Resolved by POSITION — a country-slot code (after a region, or postal-adjacent) is the country;
  a bare "Chicago, IL" (2 segments, no postal) stays Illinois.
- **ISO-3 codes** (POL/CAN/DEU/IND) folded into the alias table (unambiguous — no 3-letter US state).
- **PeopleSoft dash formats**: "United States-Texas-Garden City", "Kansas-Topeka, Kansas-Wichita",
  "CA-Irvine", "USA-WV-Heaters" — split via a state/country-NAME dash rule + a case-sensitive
  UPPERCASE-CODE dash rule (so "Co-op"/"de-facto" are never broken).

Remaining tail (hard, not pursued): bare US cities absent from the gazetteer ("Mckeesport"), and
foreign hyphenated region names ("Germany-North Rhine Westphalia-Düsseldorf").

**country/city are production-grade — the fit rubric can gate on location.**

---

## The extraction-quality program is COMPLETE (2026-07-06)

All six rules-based extractors now have a real number from a real hand-labeled corpus + a ratcheting
CI gate (the `comp.py` method, replicated field by field):

| Field | Metric | Result | Grade |
|---|---|---|---|
| **comp** (salary) | recall / precision | 1.00 / 1.00 | production |
| **skills** | precision / recall | 0.995 / 0.927 | production |
| **sponsorship** | tri-state accuracy | 0.989 | production |
| **remote** | accuracy / precision (location path) | 0.994 / 1.00 | production |
| **yoe** | recall / precision | 0.978 / 0.969 | production |
| **geo/country** | accuracy | 0.948 | production |
| **geo/city** | accuracy | 0.969 | production |
| **degree_min** | recall / precision | 0.905 / 0.995 | production |
| **level** | accuracy / macro-F1 | 0.822 / 0.738 | usable (advisory on ambiguous) |
| **sector** | accuracy-when-covered / coverage | 0.724 / 0.267 | usable; coverage name-limited |
| **degree_required** | accuracy | 0.611 | advisory only |

**Addendum (2026-07-06) — the last three fields (remote / sponsorship / sector):**
- **remote** (`geo.is_remote`, location path): 99.4% acc / 100% precision on the 800-string geo corpus
  (13 remote; the "misses" were label noise like `COL`/`"Any city, TN"`). Description-path is a
  separate minor mechanism, not separately corpus-benchmarked.
- **sponsorship** (`detect_sponsorship`): **91.3% → 98.9%** on 183 real "sponsor" windows. Added `_POS`
  ("help sponsor", "sponsorship for qualified candidates", "sponsorship support") + `_NEG` ("… are not
  available", "not able to support … sponsorship"); corrected 5 gold labels where "unable to offer …
  sponsorship" was mislabeled True.
- **sector** (`SectorExtractor`): **72.4% accuracy-when-covered, 26.7% coverage** on 700 distinct
  companies. Added high-precision name keywords (energy/solar, steel/chemicals, asset-management/
  securities, games, diagnostics/biotech, supply-chain). Coverage is **inherently name-limited** —
  opaque startup names carry no industry signal, so a large unknown share is correct-by-design. Real
  coverage lever = a bigger company→sector gazetteer or an LLM classifier, not regex.

**Phase C (moat-aligned tools) is unblocked.** The fit rubric gates on the production-grade fields and
treats `sector` coverage-permitting, `degree_required` + ambiguous `level` as advisory — no
confident-but-wrong A–F.

### Sector — Stage-1 ML PoC (2026-07-07) — NEGATIVE, pivot to data-join

Per `docs/superpowers/specs/2026-07-06-hybrid-sector-classifier-design.md` /
`docs/superpowers/plans/2026-07-06-sector-classifier-stage1-poc.md`, we built and benchmarked the
**Tier-2 embedding classifier** (bge-small + CL2N + TLD one-hot → L2 logreg, per-class Platt,
3-gate abstention) on the existing 687-labeled / 27-class corpus.

**Result — Tier-2 ML does NOT beat the deterministic baseline (72.4% acc @ 26.7% cov).** The honest
**held-out 5-fold CV** read (train-set eval was a leak — see below):

| operating point | ML (held-out) | baseline |
| --- | --- | --- |
| full coverage | 100% cov / **29.8%** acc (macro-F1 0.175) | — |
| ≈ baseline coverage | ~26.7% cov / **~55%** acc | 26.7% cov / **72.4%** acc |
| ≈ baseline accuracy | **~16.7%** cov / ~74.8% acc | 26.7% cov / 72.4% acc |

At every comparable point the curated gazetteer wins: it holds exact company→sector mappings, whereas
bge-small must generalize from weak inputs (name + domain-label + example title) and reaches only ~30%
raw accuracy across 27 classes. This is exactly the spec's predicted failure mode ("company NAME is a
weak feature; it's a weak-*input* problem, not a weak-model problem").

**Methodology note (defect caught):** the first eval scored the saved model on its own training
corpus and reported an inflated **93.2% acc / 51.2% cov / "ML BEATS"** — a data leak. Fixed by an
honest stratified-CV eval (`scripts/eval_sector_classifier.py`, default path; the leaked scoring now
lives behind `--score-model` under a warning). Runtime peak RSS 441–563 MB, single-process embedding
(laptop-safe); stress gate passed before the full run.

**Go/no-go → PIVOT (spec's pre-authorized fallback):** do **not** ship Tier-2 ML as designed. Invest
Stage-2 in the **Tier-1 data-join gazetteer** — the real coverage lever — which is already partly
built (`scripts/merge_sectors.py`, `sector_edgar.py`, `sector_wikidata.py`, `build_sector_naics.py`,
`classify_sectors.py`; `sectors.json` already holds ~22k companies). Tier-2 could be revisited later
only if given a materially stronger input signal (e.g. company descriptions), which is out of scope
now. The PoC code (`sector_features.py`, `sector_clf.py`, the train/eval scripts) is retained as the
harness for any future re-test but is **not wired into `SectorExtractor`** and ships nothing.
