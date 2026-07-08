# Wikidata Cleanup + Re-Derivable Merge — Design Spec

> **Created:** 2026-07-08 · **Status:** approved, pre-implementation.
> **Predecessor:** the precision-gated PDL source
> (`docs/superpowers/specs/2026-07-07-precision-gated-pdl-source-design.md`) shipped sector coverage
> 26.7%→36.6%. A diagnostic then showed the Wikidata source carries junk label-pass matches.
> **Goal:** (1) make `merge_sectors` re-derivable so any source can be corrected (today source labels
> freeze forever); (2) purge the obvious Wikidata junk (porn-industry / short-slug entity collisions)
> from the shipped `sectors.json`. The durable win is (1); (2) is its first beneficiary.

## Diagnosis that motivates this
Wikidata-vs-human-gold accuracy is **66% (31/47 overlap)** — not the crosswalk's fault. The domain-pass
(P856) matches are ~100% accurate; the pollution is the **label pass matching short/generic company
slugs to unrelated Wikidata entities** (`hud`→"shipbuilding", `harper`→"pornography industry",
`align`→"film production", `zoo`/`pronto`/`enter`→junk). The **113 "pornography industry"** hits are the
smoking gun. **1,912 of 13,624 shipped `sectors.json` entries are wikidata-sourced**, so correcting them
is an OVERRIDE — which today's merge cannot do, because it locks every existing label as "curated."

## Architecture — two units

### Unit 1 — re-derivable merge (`merge_sectors.py`), the durable win
Today: `curated = {k: sector for … if v.get("sector")}` — every sectored entry (incl. edgar/wikidata/
slug/pdl-derived) is treated as locked, so no source can ever be corrected. Verified split of the
current table: **1,453 hand-curated (no `source` field)** + 8,322 slug + 1,912 wikidata + 1,343 pdl +
594 edgar.

Change: lock **only hand-curated (sourceless)** entries, and **rebuild** the source-derived layer fresh
from the committed `sector_*.json` files each run:
```python
hand = {k: v for k, v in sec["companies"].items() if v.get("sector") and not v.get("source")}
curated = {k: v["sector"] for k, v in hand.items()}
filled = apply_priority(seed, curated, sources, priority)   # priority = ["edgar","wikidata","slug","pdl"]
sec["companies"] = {**hand, **filled}                        # REBUILD, not update-in-place
```
Consequences: (a) a source fix — e.g. a dropped wikidata entry — now actually disappears from the table
(it isn't re-produced and isn't hand-curated); (b) the whole table is reproducible from
(hand-curation + source files); (c) it is **idempotent** — a second `--apply` produces zero changes.
Hand-curated entries are never touched.

### Unit 2 — Wikidata post-filter (`scripts/clean_sector_wikidata.py`)
A pure, offline filter over the committed `scripts/sector_wikidata.json` (no Wikidata re-query — the
committed json is the input), dropping the low-confidence label-pass junk:
- **`WD_JUNK_INDUSTRIES` blacklist** — `wd_industry` values that are near-always spurious entity
  collisions for employers (headline: `pornography industry`; plus any equally-clear ones the data
  shows, e.g. adult/sex-industry variants). Conservative — only industries no real employer in our set
  legitimately carries.
- **Very-short-slug guard** — drop entries whose `company_key` length ≤ 3 (`hud`, `zoo`, `bcc`, `ecu`):
  acronym collisions the label pass injects.
It rewrites the cleaned `sector_wikidata.json` (an **auditable diff** showing exactly what was removed)
and prints the drop count by reason.

**Honest magnitude:** the committed json records only `company_key`, `wd_industry`, `wd_qid`, `sector`
(not which pass matched), so the offline filter removes the *obvious* junk (~150–250 entries) but not
subtler wrong-entity matches like `cisco`→"networking hardware" (that needs a re-query — out of scope).
The 700-gold number may barely move (n=47 overlap); the win is table **trust** (no porn-labeled
companies) + the reproducibility from Unit 1.

## Data flow
```
sector_wikidata.json ──clean_sector_wikidata.py──► sector_wikidata.json (cleaned, committed)
                                                          │
hand-curated (1,453, locked) + edgar/wikidata(cleaned)/slug/pdl ──merge_sectors(rebuild)──► sectors.json
```

## Validation, testing & acceptance
- **Acceptance gate:** after clean + re-merge, `tests/test_sector_recall.py` holds — accuracy-when-covered
  **≥ 0.68** (currently 73.4%), coverage **≥ 0.34** (ratcheted floor). Removing wrong labels should hold
  or raise accuracy; a small coverage dip from the purge is acceptable.
- **Before/after table composition** is measured and recorded (expect: wikidata −(150–250) junk; other
  sources stable; hand-curated unchanged) so the churn is understood, not surprising.
- **Idempotence test:** `apply_priority` on (hand-curated + sources) rebuilds a stable table; a second
  merge changes nothing.
- **TDD:**
  - `tests/test_clean_wikidata.py` — blacklisted-industry entry dropped; ≤3-char-slug entry dropped;
    a normal entry kept; drop counts correct.
  - `tests/test_merge_sectors.py` (extend) — `curated` locks ONLY sourceless entries; a source-derived
    entry is re-derived/overridable (prove a stale wikidata key drops when the cleaned source omits it,
    while a hand-curated key is preserved).
  - `tests/test_sector_recall.py` — end-to-end acceptance after the real re-merge.

## Deliverables
- **Create:** `scripts/clean_sector_wikidata.py`, `tests/test_clean_wikidata.py`.
- **Modify:** `scripts/merge_sectors.py` (lock only hand-curated; rebuild), `scripts/sector_wikidata.json`
  (cleaned), `src/ergon_tracker/registry/data/sectors.json` (re-merged), `tests/test_merge_sectors.py`.
- **Record:** `docs/extraction-baseline.md` — the cleanup + re-derivable-merge note + before/after numbers.

## Constraints honored
Free · offline (no Wikidata re-query; committed json is the input) · deterministic · **re-derivable**
(only hand-curation locked; sources reproducible) · additive-safe for hand-curation (never touched) ·
auditable (cleaned `sector_wikidata.json` diff) · no new dependency · no `SectorExtractor` code change
(only its data file changes). No concurrency needed — this is instant data-cleaning + a dict rebuild;
forcing multiprocessing here would be fluff.

## Out of scope (YAGNI)
Re-querying Wikidata / hardening the live SPARQL harvest (deferred — needs network + the slow resumable
harvest); fixing subtle long-slug wrong-entity matches (`cisco`-class) the offline filter can't see;
changing `SectorExtractor`; touching edgar/slug/pdl mappings.

## Open items to settle in the plan
- Final `WD_JUNK_INDUSTRIES` membership (start from `pornography industry`; add only industries the
  committed json shows are clearly spurious for our companies) and the exact short-slug length cutoff (≤3).
- Whether re-deriving all sources shifts any existing `source` labels (priority reassignment) — measure
  the before/after and confirm sector values are stable, not just the `source` tag.
