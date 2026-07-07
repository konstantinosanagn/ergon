# Stage-2 PDL Name-Join Probe — Result (2026-07-07)

**Verdict: NO-GO** on the standalone bar (≥35% projected registry coverage AND ≥72.4% accuracy),
with a **precision-gated consolation lever** worth folding into the existing merge.

## Setup
- **Dataset:** People Data Labs Free 7M Company Dataset (Kaggle `peopledatalabssf/free-7-million-company-dataset`),
  `companies_sorted.csv` 1.0 GB → converted (streaming) to `pdl_free.ndjson.gz` 132 MB.
  Fields used: `name` + `industry` (industry already lowercased LinkedIn enum). Used offline for
  measurement only; **nothing from it is redistributed or committed** (dump is gitignored).
- **Rows:** 7,173,423. **Join:** normalized-name (`normalize_company`) against 58,078 registry + 699 gold.
- **Runtime:** workers=1 (laptop), **peak RSS 111 MB**, wall **14.5 s** full pass; stress gate (200k sample) 110 MB / 0.4 s.

## Result (full crosswalk)
| Metric | Value | Bar |
| --- | --- | --- |
| Gold accuracy-when-covered | **46.2%** | ≥72.4% ❌ |
| Gold coverage | 47.3% | — |
| Registry current coverage | 16.2% | — |
| Registry net-new companies | **15,637** | — |
| Registry projected coverage | **43.2%** | ≥35% ✅ |

Coverage clears the bar easily; **accuracy fails badly.** Cause: LinkedIn's coarse industry enum
(e.g. the huge "internet"→Software/SaaS 55%, "financial services"→Banking 29%,
"marketing and advertising" 0%) does not resolve our fine 27-label taxonomy. The accurate industries
(biotechnology 100%, banking 100%, insurance 100%, medical devices 100%, oil & energy 100%,
chemicals 100%, mining & metals 100%, utilities 88%, hospital & health care 88%) are small/niche.

## Accuracy–coverage frontier (keep industries with gold-acc ≥ t)
| t | proj coverage | gold accuracy | net-new |
| --- | --- | --- | --- |
| 0.00 (full) | 43.2% | 46.2% | 15,637 |
| 0.50 | 22.7% | 77.3% | 3,780 |
| 0.72 | 19.1% | 93.4% | 1,691 |
| 0.80 | 18.8% | 96.2% | 1,460 |
| 1.00 | 18.0% | 100.0% | 1,043 |

**No operating point clears both bars.** Max coverage at ≥72.4% accuracy ≈ **22.7%** (< 35%).
Reaching 35%+ coverage forces accuracy down to ~47%.

## Decision → PIVOT (spec's pre-authorized fallback)
Do **not** build the full standalone PDL pipeline. Instead:
1. **Precision-gated PDL as an additive merge source (recommended, safe):** add PDL restricted to the
   high-accuracy industries (gold-acc ≥ ~0.8) as a new gated source in `scripts/merge_sectors.py`
   (priority below curated/edgar). Est. **~1,460 net-new companies at ~96% accuracy** — above the
   current table's per-source quality (edgar 71% / wikidata 58% / slug 74%). Small but clean.
2. Continue squeezing the existing pipeline: automate/refresh edgar+wikidata+slug, lift Wikidata's
   58%, and job-weighted brand curation (the memory's proven lever for the heavy opaque brands).

## Caveats
- Registry coverage is conservative: registry keys are ATS slugs (no display names), so the slug↔name
  `normalize_company` join undercounts real matches (see spec). Accuracy is measured on gold display
  names and is unaffected.
- Collisions on a normalized name are resolved deterministically (higher record-completeness, then
  lexicographically smaller industry), so results are reproducible inline == parallel.
