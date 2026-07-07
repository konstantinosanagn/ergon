# Stage-2 PDL Name-Join Probe — Design Spec

> **Created:** 2026-07-07 · **Status:** approved, pre-implementation.
> **Parent:** `docs/superpowers/specs/2026-07-06-hybrid-sector-classifier-design.md` (Tier-1 data-join
> gazetteer). **Predecessor:** Stage-1 ML PoC concluded NEGATIVE (see `docs/extraction-baseline.md`
> "Sector — Stage-1 ML PoC"); the coverage lever is data, not ML.
> **Goal:** cheaply MEASURE whether name-joining our registry against the PDL Free Company Dataset
> lifts sector coverage past a go/no-go bar, BEFORE building or automating any full data pipeline.

## Why a probe (not the full pipeline)
Two facts, both verified against the current repo, make the full Tier-1 build risky to commit to blind:
- **Domain-join is nearly dead here.** Only **765 / 58,078** seed companies (1.32%) carry a domain,
  and only 280 / 9,430 `sectors.json` entries do. The parent spec's domain→sector spine (PDL/BigPicture
  by eTLD+1) can therefore reach almost nothing; the join must be by **normalized company name**.
- **The small sources are near-exhausted.** `sectors.json` already merges edgar (594) + wikidata
  (1,912) + slug (5,471) + curated (~1,453) = **9,430 companies = 16.2%** of the registry, at
  per-source accuracy-vs-gold of edgar 71% / wikidata 58% / slug 74%. Prior measurement (memory) found
  more of these adds little. The one genuinely **untested** lever is joining our 58k names against the
  large free company datasets (**PDL Free ~7M**, BigPicture ~17M), which carry LinkedIn-industry
  labels — orders of magnitude more companies than edgar/wikidata reached.

So we de-risk exactly as Stage-1 did: a self-contained probe answers "is the big name-join worth
building?" for a fraction of the cost of building it.

## Go / no-go bar (decides what we measure)
**GO** (build the full Stage-2 pipeline) iff BOTH hold:
- **Coverage:** projected registry coverage ≥ **~35%** (from today's 16.2%) — i.e. the name-join adds
  **~+20pp / thousands** of currently-`unknown` registry companies, AND
- **Accuracy:** accuracy-vs-gold on the 700-company benchmark ∩ PDL ≥ **72.4%** (today's bar; don't
  pollute the authoritative table).

**NO-GO** → pivot to squeeze-existing (automate + refresh edgar/wikidata/slug, lift Wikidata's 58%) +
job-weighted brand curation (the memory's proven lever). Either outcome is a cheap, decisive answer.

## Architecture — four isolated units
```
PDL Free dump (name, industry)  ──stream──►  name-join  ──►  crosswalk (LinkedIn→27)  ──►  measure/verdict
   (acquisition, one-time)      (parallel, bounded mem)     (static map)                 (coverage + accuracy + GO/NO-GO)
        ▲                                    ▲
   scratch cache (gitignored)     target-name set (58k registry + 700 gold), built once, broadcast
```

### Unit 1 — dataset acquisition (`scripts/probe_pdl_sectors.py`, acquisition path)
- Download the **PDL Free Company Dataset** (People Data Labs, **CC-BY-4.0**, ~7M rows; fields we need:
  company `name` + LinkedIn `industry`). Cache to a **gitignored scratch dir**
  (`scripts/.probe_cache/`, added to `.gitignore`); never commit the multi-GB dump.
- The acquisition step **verifies availability first** (HEAD/size check). If the free PDL download is
  unreachable, fall back to **BigPicture** (ODC-BY, same LinkedIn enum). If neither is obtainable, the
  probe **reports the blocker and exits non-zero** — it never half-runs on partial data.
- Accepts `--dump PATH` to point at an already-downloaded file (so the network step is decoupled from
  the compute step and re-runs are free).
- **Attribution:** PDL (CC-BY) / BigPicture (ODC-BY) require attribution → a `NOTICE` entry is added
  when (and only when) a shipped artifact is produced later; the probe itself ships no data, so it only
  records the source + license in its report.

### Unit 2 — name-join (parallel, memory-bounded)
- Build the **target set** once by applying `ergon_tracker.dedup.normalize_company` to the best
  available name on each side: the 700-gold uses its display `company` field; the 58k registry uses
  the `seed.json` **key (an ATS slug)**, since seed carries no display name. **Known limitation:**
  normalizing a slug (`acmecorp`) and a display name (`Acme Corp`) can diverge (suffix fusion,
  concatenation), so the registry-side match rate is a *conservative* estimate — the plan must report
  slug-vs-name normalization behavior and may enrich registry names from the live index if the slug
  join proves lossy. Accuracy is measured on the gold side (real display names), so the accuracy number
  is not affected by this. The set is small (~58k short strings) and is broadcast to every worker.
- **Stream** the dump in chunks (line/record iteration — never `fetchall`/full-load; the Stage-1
  rich-tier OOM lesson). Dispatch chunks to a bounded **`ProcessPoolExecutor`**; each worker normalizes
  its rows' names and keeps only those in the target set, returning `{norm_name → (raw_industry, …)}`.
  Peak memory is **O(target + matches)**, not O(7M).
- **Worker count is env-gated, laptop-safe by default** (the repo idiom, mirroring
  `ERGON_SHARD_WORKERS`): `ERGON_PROBE_WORKERS` explicit int wins → else `max(2, (os.cpu_count() or 4)
  - 2)` on CI → else `1` local.
- **Collisions:** when multiple PDL rows share a normalized name, a documented deterministic tie-break
  selects one (prefer the most-complete record; stable on ties). The **collision rate is reported**, so
  ambiguity is visible, not hidden.

### Unit 3 — crosswalk (`scripts/linkedin_industry_to_sector.json`)
- A first-pass static map of the ~147 LinkedIn industry enum values → our 27 labels, committed as a
  small reusable JSON. Genuinely ambiguous enums (e.g. "Information Technology and Services" spanning
  several of our tech labels) map to the best single label and are **flagged in the file** for later
  refinement. Unmapped/unknown industries fall through to no-label (not a wrong guess).

### Unit 4 — measurement & verdict
- **Coverage:** (a) match rate on the 700-gold; (b) on the 58k registry, the **net-new** count =
  matched AND currently `unknown` in `sectors.json`; projected registry coverage = 16.2% + net-new%.
- **Accuracy-when-covered:** on the 700-gold ∩ PDL subset with a gold sector, does the crosswalked-27
  label equal gold? (This gold overlap is the only place we can measure accuracy, so it is the proxy
  for the whole join.)
- **Sanity:** agreement with existing `sectors.json` where the join and the table overlap.
- **Verdict:** print the numbers and a single **GO / NO-GO** line evaluated against the bar above.

## Concurrency & stress-testing (mandatory, every heavy step)
- **Concurrency:** chunked streaming + bounded `ProcessPoolExecutor` (env-gated as above); the target
  set is built once and broadcast; if acquisition pulls multiple files, downloads run concurrently
  (bounded). No global list of 7M rows ever materializes.
- **Stress gates BEFORE any full 7M run:**
  1. **`--sample N`** processes the first N records, logs peak-RSS + wall-time, and asserts a laptop
     budget — run this first; it is the gate.
  2. A **synthetic ~1M-record stream** memory-watch test asserts peak RSS stays bounded (mirrors the
     Stage-1 rich-tier validation), so the memory profile is proven without the real download.
  3. The full run logs peak-RSS + wall-time and **fails fast** if over budget.
- **Laptop safety:** the dump is one-time to scratch; conservative local worker default; aggressive
  parallelism only under CI (env-gated).

## Testing
- TDD throughout. `tests/test_probe_pdl_sectors.py` runs on a **synthetic in-memory sample** — no
  network, no real dump — and covers: `normalize_company`-based join match/miss; crosswalk mapping
  (incl. unmapped→no-label); collision tie-break; the coverage/accuracy/net-new math; and a
  memory-bounded streaming assertion on a synthetic largish sample. The real download is never in the
  test path.
- pytest config already supports `scripts.` imports (`pythonpath=["."]`). Gate: the suite must RUN
  (not skip). Lint: ruff line-length 100, no semicolon one-liners (E701/E702). mypy is `src/`-only, so
  the script isn't type-checked, but `src` must stay green (the probe imports `normalize_company`).

## Deliverables, dependencies, artifacts
- **Deps:** none new — stdlib + json (stream lines; no pandas/sklearn). Reuses
  `ergon_tracker.dedup.normalize_company` and reads `seed.json` / `sectors.json` /
  `tests/fixtures/sector_corpus.jsonl`.
- **Ships nothing to runtime.** `SectorExtractor` and `sectors.json` are untouched; the probe only
  measures. A gazetteer artifact is produced only later, in the full pipeline, and only on GO.
- **Committed:** `scripts/probe_pdl_sectors.py`, `scripts/linkedin_industry_to_sector.json`,
  `tests/test_probe_pdl_sectors.py`, the report under `docs/superpowers/artifacts/`, and a `.gitignore`
  entry for `scripts/.probe_cache/`.

## Build method
Decomposed into bite-sized TDD tasks and executed via **subagent-driven-development** — parallel
specialized junior agents (crosswalk-builder, join-engine, measurement/verdict, tests), each with an
implementer + task-review loop and a final whole-branch review, exactly as Stage-1 was built.

## Constraints honored
Free · offline · CPU-only · laptop-safe (streaming, bounded memory, env-gated parallelism, stress-gated
before full runs) · deterministic (data join + static crosswalk, no ML) · license-clean (PDL CC-BY /
BigPicture ODC-BY, attribution recorded; nothing multi-GB committed) · no new dependency · no runtime
wiring.

## Out of scope (YAGNI)
Building/automating the full merge pipeline; downloading BigPicture unless PDL is unavailable or
insufficient; refining the crosswalk beyond a first pass; job-weighted live-index measurement (the bar
is company-count coverage + gold accuracy); any change to `SectorExtractor` or `sectors.json`.

## Open items to settle in the plan
- Exact PDL Free download URL/mirror + on-disk format (JSON-lines vs CSV) — confirmed in the
  acquisition task; `--dump PATH` decouples it so a manual download also works.
- The `--sample` size and the peak-RSS laptop budget number.
- Collision tie-break's exact "most-complete record" key ordering.
- Whether the registry-side join needs display names enriched from the live index (if the slug-based
  `normalize_company` join proves too lossy vs a spot-check) — decided from the sample-run match rate.
