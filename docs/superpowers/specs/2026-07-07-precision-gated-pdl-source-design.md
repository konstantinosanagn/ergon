# Precision-Gated PDL Sector Source — Design Spec

> **Created:** 2026-07-07 · **Status:** approved, pre-implementation.
> **Parent/predecessor:** the Stage-2 PDL name-join probe
> (`docs/superpowers/specs/2026-07-07-stage2-pdl-namejoin-probe-design.md`,
> artifact `docs/superpowers/artifacts/2026-07-07-stage2-pdl-probe.md`) concluded NO-GO on the full
> standalone pipeline but surfaced a **precision-gated subset** as a safe, additive lever.
> **Goal:** ship that lever — add a curated, high-precision PDL source that gap-fills
> currently-`unknown` companies in `sectors.json`, lifting registry sector coverage at ~95%+ accuracy
> without regressing the current benchmark.

## Why this, and why gap-fill
The probe measured: full PDL name-join → 43.2% projected registry coverage but only 46.2% gold accuracy
(LinkedIn's coarse enum can't resolve our 27-label taxonomy). Restricting to high-accuracy industries →
~1,460 net-new companies at ~96% accuracy — above the current per-source quality (edgar 71% /
wikidata 58% / slug 74%). This spec ships exactly that restricted source, **gap-fill only** (never
overrides an existing label), so it is purely additive and cannot regress the measured 72.4% accuracy.

## Architecture — one thin builder + one merge hook
```
PDL dump (name, industry)  ──►  probe name-join (REUSED)  ──►  ALLOW-LIST filter  ──►  key-map  ──►  sector_pdl.json
                                (streaming, parallel,          (curated industries)   (norm→keys)     (committed)
                                 stress-tested — Stage 2)                                                  │
                                                                                                          ▼
                                                       merge_sectors.py  (curated > edgar > wikidata > slug > pdl)  ──►  sectors.json
                                                                              gap-fill: pdl fills only `unknown` keys
```

### Unit 1 — `scripts/sector_pdl.py` (the builder)
- **Reuses** `probe_pdl_sectors.py` (`load_inputs`, `build_target_index`, `run_join`, `open_dump`,
  `_workers`) — the memory-bounded, env-gated (`ERGON_PROBE_WORKERS`), deterministic, stress-tested
  name-join. **No new join/concurrency code is written**; the heavy path is the reviewed Stage-2 engine.
- Applies a **curated allow-list** `PDL_ALLOWLIST: dict[str, str]` mapping each trusted LinkedIn industry
  → one of our 27 labels. Only these industries produce output. Initial contents (each 1:1 unambiguous
  AND high-scoring in the probe):
  `biotechnology, pharmaceuticals → Biotech/Pharma; banking → Banking/Finance; insurance → Insurance;
  medical devices → Healthcare; hospital & health care → Healthcare; oil & energy → Energy/Climate;
  utilities → Energy/Climate; chemicals → Manufacturing/Industrial; mining & metals →
  Manufacturing/Industrial; mechanical or industrial engineering → Manufacturing/Industrial;
  semiconductors → Semiconductors/Hardware; higher education → Education.`
  Deliberately EXCLUDED (coarse/ambiguous, tanked probe accuracy): internet, information technology and
  services, financial services, marketing and advertising, consumer goods, entertainment, real estate,
  telecommunications.
- For each matched normalized name whose PDL industry ∈ allow-list, resolve to registry key(s) via
  `TargetIndex.norm_to_keys` and emit `{company_key: {"sector": <label>, "source": "pdl",
  "industry": <raw>}}` → committed `scripts/sector_pdl.json` (sorted keys, like `sector_edgar.json`).
- **Self-reports** (like the other builders): total matched, net-new vs current `sectors.json`, and
  accuracy-vs-gold on the allow-list overlap. `--dump PATH` decouples the dataset (gitignored cache);
  fail-fast if absent (reuse the probe's `resolve_dump`).

### Unit 2 — `merge_sectors.py` hook (gap-fill)
- Load `sector_pdl.json` as a source alongside edgar/wikidata/slug.
- Append `"pdl"` to the priority list so the order is **`edgar > wikidata > slug > pdl`** for non-curated
  keys (curated `sectors.json` entries always win, unchanged). Because pdl is last, it labels only keys
  that no higher source covered — i.e. gap-fill. It never overrides an existing label.
- Report pdl's added count + accuracy-vs-curated in the merge summary, matching the existing per-source
  reporting.

## Validation, ratchet & acceptance gate
- **Acceptance:** after building + merging, `tests/test_sector_recall.py` must stay green with
  **accuracy-when-covered ≥ 0.68 gate** (measured 72.4%). Gap-fill + ~95% allow-list means accuracy
  holds or rises.
- **Ratchet coverage:** measure the new coverage on the 700-gold after merge and raise `COVERAGE_GATE`
  from 0.22 to just below the newly-measured value, locking the gain against regression.
- The builder's accuracy-vs-gold self-report (~95%+ expected) is the per-source audit.

## Concurrency, optimization & stress-testing (mandatory)
- **Reuse, don't rebuild:** the 7M-row streaming join is the Stage-2 engine — env-gated
  `ProcessPoolExecutor` (`ERGON_PROBE_WORKERS` → CI cpu-2 → local 1), bounded in-flight submission
  (memory O(chunk)), deterministic inline==parallel. The builder adds only an O(matches) allow-list
  filter + key-map — no new hot loop.
- **Stress gates before the full run:** reuse the probe's `--sample N` (peak-RSS + wall-time, laptop
  budget) on a slice first; the full build logs peak-RSS + wall-time. (The probe already demonstrated
  111 MB / 14.5 s on the full 7.17M rows.)
- **No fluff / no redundant code:** the builder imports the probe's functions rather than duplicating
  them; the allow-list is a single constant; the merge change is additive (one source + one priority
  entry). No new dependency (stdlib + json).

## Testing (TDD)
- `tests/test_sector_pdl.py` (synthetic dump, no network): (a) an in-allow-list industry emits the
  correct label; (b) an out-of-list industry is dropped; (c) matched norm → correct registry key(s) in
  the output record shape `{sector, source:"pdl", industry}`; (d) accuracy/net-new counters correct.
- Merge test: `pdl` gap-fills a currently-`unknown` key but does NOT override an existing curated/edgar
  label (assert the higher source wins on a conflict, pdl wins only on `unknown`).
- `tests/test_sector_recall.py` is the end-to-end acceptance gate after the real merge (accuracy ≥ gate;
  coverage ≥ new ratcheted floor).

## Deliverables
- **Create:** `scripts/sector_pdl.py`, `scripts/sector_pdl.json` (committed, ~1,000–1,500 entries),
  `tests/test_sector_pdl.py`.
- **Modify:** `scripts/merge_sectors.py` (add pdl source + gap-fill priority),
  `src/ergon_tracker/registry/data/sectors.json` (the merged result — the shipped runtime win),
  `tests/test_sector_recall.py` (ratchet `COVERAGE_GATE`).
- **Record:** `docs/extraction-baseline.md` — new sector coverage/accuracy + that pdl is a gated,
  gap-fill source.

## Constraints honored
Free · offline · CPU-only · laptop-safe (reuses the stress-tested memory-bounded parallel join) ·
deterministic (curated allow-list + data join, no ML) · additive/gap-fill (no regression risk) ·
license-clean (PDL used offline to derive a compact company→sector map; the raw dump is never
committed) · no new dependency · auditable (committed `sector_pdl.json` with per-entry source+industry).

## Out of scope (YAGNI)
The coarse/ambiguous industries (no standalone pipeline); overriding existing labels (gap-fill only);
BigPicture; automating the dataset download in CI (the dump is fetched offline, `--dump PATH`); any
`SectorExtractor` code change (only its data file `sectors.json` grows).

## Open items to settle in the plan
- Exact `merge_sectors.py` integration points (how sources/priority are coded) — read verbatim in the plan.
- The precise `COVERAGE_GATE` new value (measured after the real merge).
- Final allow-list membership review (drop any entry that a fresh accuracy check flags), keeping the set
  curated + conservative.
