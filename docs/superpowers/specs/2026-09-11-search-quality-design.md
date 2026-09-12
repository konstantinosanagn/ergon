# Search quality: judged evaluation, query understanding, title aliases, reranking

Design spec · 2026-09-11 · status: approved in review, awaiting implementation plan

## 1. Problem

Broad search runs BM25 over an FTS5 index of ~1.4M active postings. A query such as
*software engineer position in NYC for >130k new grad* is three different problems glued
together, and today only the first is attempted:

| part | what it is | what BM25 alone does with it |
|---|---|---|
| *software engineer* (which should include ML engineer, SDE, developer) | title synonymy | matches the literal tokens only |
| *NYC*, *>130k* | hard constraints | treats them as keywords; "130k" matches nothing useful |
| *new grad* | a seniority concept that postings express as "0–1 years", "recent graduate", "early career" | matches nothing |

The evidence reviewed before this spec (LinkedIn's query-understanding paper, BEIR and
e-commerce hybrid benchmarks, Indeed's seniority work, TalentCLEF title matching) says the
largest measured gains in job search came from parsing the query into structured fields and
filtering, then from a reranker over lexical candidates; dense retrieval helps least on corpora
where titles already overlap the query lexically, and fails on numbers, negation and exact
identifiers. Nothing here has been measured on this corpus. This spec builds the measurement
first and gates every change on it.

## 2. Decisions taken

- One shared `understand(text) -> SearchQuery` used by CLI, SDK and MCP. Explicit parameters
  always win over parsed values.
- Query understanding is rules + gazetteers (approach A). A learned parser (approach B) is
  specced only if the harness shows a slice where A's misses cost recall (§7).
- Relevance judgments are LLM-drafted and human-adjudicated; the LLM is never the sole judge.
- The reranker ships as an optional `[rerank]` extra; the default path stays dependency-free.
- Each step ships as a default only when it clears a statistical gate on the judged set with
  no slice regressing. No target number is guessed up front; the baseline run sets it.
- Dense/hybrid retrieval is measured last, under the same rules, with a tuned fusion weight.

## 3. Evaluation harness

Location: `scripts/search_eval/` (tracked pipeline tooling), data in `data/search_eval/`.

### 3.1 Query set

`queries.yaml`: ~60 entries, each `id`, `text`, `slice`, optional `expect` (hard constraints a
correct result must satisfy, e.g. `city: New York`, `salary_min: 130000`, `max_years: 1`).

| slice | example | tests |
|---|---|---|
| `constraint` | software engineer NYC >130k new grad | filters extracted and applied |
| `title-synonym` | ML engineer; SDE; data scientist NLP | alias expansion, reranker |
| `seniority` | entry level analyst; senior backend remote | years/level inference |
| `exact-identifier` | C++; Rust; Kubernetes; CPA | BM25 must not regress |
| `plain` | nurse practitioner Chicago | baseline behaviour preserved |

### 3.2 Pooling

Each named run configuration contributes its top-20 per query to the judgment pool. Adding a
run only asks for judgments on results not yet graded; existing grades are never re-asked.
The report shows each run's *unjudged rate* in its top-10 so a pooling artifact is visible.

### 3.3 Judging

Scale and prompt follow UMBRELA (0 irrelevant / 1 related / 2 highly relevant / 3 perfectly
relevant; "measure intent match, measure constraint satisfaction, decide"), with UMBRELA's
trustworthiness aspect replaced by our `expect` block. A deterministic check marks any
`expect` violation as grade 0 before the LLM sees the pair.

UMBRELA's confusion matrices show LLMs get "irrelevant" right ~75% of the time but the three
relevant grades only 30–50%. So the auditor (HTML, same pattern as
`scripts/bench/label_auditor.html`) shows the adjudicator every non-zero draft plus a 10%
sample of zeros; unseen zeros stand. Output: `qrels.tsv` in TREC format with an added
`source` column (`llm` | `human`), committed.

### 3.4 Metrics and library

`ranx` (TREC-validated, paired significance tests, `optimize_fusion`). nDCG@10 and Recall@100
(grade ≥ 2, UMBRELA's binarization) per query, aggregated overall and per slice. Recall@100 is
reported separately because it says whether the candidate pool contains the right jobs — the
number that bounds what any reranker can do. Latency p50/p95 on a laptop CPU reported next to
every run.

### 3.5 Reproducibility

Runs pin an index `build_id`; the harness refuses to compare runs across builds.
`make search-eval` prints the comparison table and writes the side-by-side HTML (§8). CI runs
the harness on committed qrels as a regression check, not a gate.

Out of scope: click data (none exists), latency as a gate (reported only), multilingual
queries (a slice can be added later).

## 4. Query understanding: `understand(text) -> (SearchQuery, Parse)`

Module: `src/ergon/query/understand.py`. Pure, deterministic, no I/O, sub-millisecond.
Called by the CLI on its positional keywords, by the SDK when `SearchQuery(keywords=...)` has
no explicit value for a slot the parser fills, and by the MCP `search_jobs` tool on
`keywords`. A parsed value only fills an empty field; it never overrides a caller's value.

### 4.1 Extractors, in order (each consumes its span)

1. **Salary** — `>130k`, `130k+`, `$130,000`, `130-160k`, `over 130k`, `€80k`, `£`,
   `at least 100k` → `salary_min` / `salary_max` / `salary_currency`. Currency from symbol or
   trailing code, else from the parsed country, else none (today's "any currency"). A pay
   period, when stated (`/hr`, `hourly`, `per month`), is normalized to annual with explicit
   factors; absent, the period is left unknown rather than guessed. Two documented traps are in
   the test table: a `k`/`m` suffix followed by a letter is not a multiplier (`$95,000 M`), and
   bare numbers without `k`/currency are never salaries (so `3 years`, `C++ 17`, `Java 8` and
   `2 openings` are untouched).
2. **Experience / level** — phrase table → fields. *new grad, recent graduate, graduate
   program, campus, university hire, early career* → `max_years=1`, `level=entry`; *entry
   level, junior* → `max_years=2`; `0-2 years`, `3+ years`, `5-8 yrs` → `min/max_years`;
   *intern/internship* → `level=intern`; *senior, staff, principal, lead, manager, director,
   executive* → `level` via the existing `infer_level` vocabulary in `extract/level.py`, so
   query and posting share one enum. The query-side vocabulary is LinkedIn's seven levels
   mapped onto `JobLevel`, with the conventional bands (entry 0–2, associate 3–5, senior 5+).
   **When a query states years, years win over the level word** (Indeed: "entry level" labels
   do not mean inexperienced). `include_unknown_years` keeps its current default (true); the
   `seniority` slice decides whether that stays.
3. **Location** — a committed gazetteer `data/query_places.yaml` derived from GeoNames alternate
   names (cities with population ≥ 100k plus every city present in the current index's
   postings, their aliases and admin codes; regenerated by a script in `scripts/`; GeoNames CC-BY 4.0 attribution in `NOTICE`) so *NYC → New York, SF →
   San Francisco, Bay Area → San Francisco region* resolve; state and country codes;
   *remote/hybrid/on-site*. The existing `extract/geo.py` normalizer is applied so the query's
   `city`/`country` match the posting's. Prepositions are cues, not requirements. *remote* →
   `remote=True`.
4. **Employment type** — *full-time, part-time, contract, intern* → `employment_type` (intern
   also sets level).
5. **Role phrase** — whatever remains, whitespace-normalized; this is `keywords` for alias
   expansion and BM25. If everything was consumed, `keywords` is `None` and the search is
   filter-only (already supported).

### 4.2 Explainability, logging, failure

`Parse` records each filled field with its source span; `ergon search --explain` and the MCP
result `meta` show it. `ERGON_QUERY_LOG=path` (opt-in, local only) appends `{text, parse}` —
the training data for approach B if it is ever warranted. No extractor may raise: an exception
inside one drops that slot and leaves its text in the role phrase, degrading to today's keyword
search, never to an empty result. Property-tested with random text.

### 4.3 Interaction with existing surfaces

CLI flags (`--level`, `--salary-min`, …) keep working and win over parsing; the `infer_level`
flag becomes an alias for one release. `SearchQuery.keywords` semantics are unchanged for
callers that already pass clean keywords. A release note calls out that an agent passing
"NYC" inside `keywords` will now get filtered results (§8).

### 4.4 Tests

A table of ~80 query → expected-fields cases including the adversarial ones above; the
property test; the eval `constraint` slice as the end-to-end check.

## 5. Title aliases

### 5.1 Data

`data/title_aliases.yaml`, committed and diffable, generated by
`scripts/build_title_aliases.py` from a pinned O*NET release (30.3, CC-BY 4.0, attribution in
`NOTICE`) plus hand-maintained `data/title_aliases.overrides.yaml`. One entry per O*NET
occupation: `label`, `aliases` (lay titles, parentheticals split into separate aliases so
*"UI Designer (User Interface Designer)"* yields both), `acronyms` (curated: SWE, SDE, MLE,
DS, DE, PM, QA, SRE, …), `related` (curated cross-occupation links with a one-line reason).

Facts that shaped this: O*NET's *Software Developers* (15-1252) carries 93 aliases including
Software Engineer, Software Development Engineer, Full Stack, DevOps, Site Reliability
Engineer; its *Short Title* column has no tech acronyms, so acronyms are hand-curated;
**Machine Learning Engineer is filed under Research Scientists, Data Scientists and Systems
Engineers, never under Software Developers** — so `15-1252 → 15-2051, 15-1221` is a
deliberate `related` row whose survival the `title-synonym` slice decides; *SRE* expands to
both Site Reliability Engineer and Software Requirements Engineer, so acronyms map to sets.
ESCO (CC-BY 4.0, 28 languages) is the documented source for non-English aliases later.

### 5.2 Lookup and expansion

The role phrase is matched case-insensitively, longest-match-first, against aliases and
acronyms; hyphens and plurals normalized; acronyms match as whole tokens only. No match → no
expansion → today's behaviour. At query time only, in `_query_match`: the original phrase keeps
full-field matching; each alias variant is an OR arm restricted to the title column with
`allow_any=False`, capped at 12 variants ranked by O*NET source frequency then curated
priority; `related` occupations contribute their top 3 each. `--explain` shows the occupation
matched and the variants used.

Not in this spec: index-time normalization (an occupation code per posting enabling
*filter by occupation*). Query-time first because it is reversible without a rebuild; the
harness tells us whether document-side normalization would add recall the expansion misses.

### 5.3 Tests

Lookup table tests (case, plural, hyphen, acronym-as-token, parenthetical splitting), the cap,
the collision set, no-match passthrough; the generator tested against a fixture slice of the
O*NET file so a new release cannot silently change the table; the `title-synonym` slice as the
gate with the MLE↔SWE link measured before/after.

## 6. Cross-encoder reranker (`[rerank]` extra)

### 6.1 Interface first

`src/ergon/rerank.py` defines a `Reranker` protocol (`score(query, docs) -> list[float]`)
with pluggable backends: `fastembed` cross-encoder (ONNX, the `[semantic]` runtime already in
use), `fastembed` late-interaction (ONNX, for ColBERT-class models), and
`sentence-transformers` (PyTorch) as an **eval-only** backend. Wired in `router.py` where
`_vector_rerank` sits: after candidates and filters, before the limit. `SearchQuery.rerank`
tri-state, CLI `--rerank/--no-rerank`, MCP parameter of the same name.

### 6.2 Candidates and shipping rule

Measured on the judged set under identical conditions (same top-50, same input):
`ms-marco-MiniLM-L6` (baseline), **Ettin-17M and Ettin-32M** (Apache-2.0, ModernBERT; the 17M
beats MiniLM-L12 by +0.051 nDCG@10 on MTEB and runs ~267 pairs/s on a desktop CPU), and
**answerai-colbert-small** (33M late interaction, ONNX under fastembed). **zerank-2** (4B,
Apache-2.0) is run once offline as the quality ceiling, not as a candidate.

The extra ships the best model that clears the gate *and* stays under 400 ms at k=50 on a
laptop CPU, via ONNX under fastembed's runtime. If an Ettin model wins, exporting it to ONNX
(via `optimum`) is part of the work; if the export loses quality, the next model ships and
Ettin remains an eval result. No PyTorch in the install. Excluded: `jina-reranker-v3`
(CC-BY-NC), `mxbai-rerank-v2`, `Qwen3-Reranker-0.6B`, `bge-reranker-v2-m3` (0.5–0.6B, CPU-slow
or beaten by smaller models).

### 6.3 Behaviour

Depth 50 (the measured knee), configurable to 100 for the eval. Budget `ERGON_RERANK_MAX_MS`
(default 400): a one-time calibration batch on first use; if the depth would exceed the budget
it is reduced, never the answer skipped; results below the depth keep BM25 order beneath the
reranked block. Query side: the role phrase from `understand()`, not the raw query. Document
side: `title · company · location · snippet`, title first so truncation never drops it
(well under 512 tokens). Reranker score orders the block; no BM25 blending until
`ranx.optimize_fusion` on the judged set produces a weight. `--explain` shows depth, model,
per-result score and calibrated latency. Any load or scoring failure → one warning, BM25 order
returned; an explicit `rerank=True` without the extra installed → a clear error naming it.

### 6.4 Tests

A fake backend (fixed scores) proves wiring, depth cap, tail preservation, fallback and input
format (length-asserted); one opt-in live test loads the shipped model and checks a known
ordering; the `title-synonym` and `seniority` slices are the gate, latency reported alongside.

Future, recorded: once the judged set exceeds ~1,000 graded pairs, fine-tuning the shipped
model on it; and a comparison against zerank-2's instruction mode if "soft" preferences that
are not filters are ever wanted.

## 7. Gating, defaults and rollout

Every step is a flag: `understand`, `aliases`, `rerank`, `dense`, each tri-state on
`SearchQuery` with CLI and MCP equivalents. Package defaults live in
`data/search_eval/defaults.yaml`, written by the harness together with the run ids and metrics
that justified them; a default changes only through a PR that includes the eval report.

**Gate**, against the current default configuration on the same judged set:
1. `ranx` paired Student's t on nDCG@10, p < 0.05, Bonferroni-corrected across steps compared
   in that PR.
2. No slice regresses: per-slice nDCG@10 and Recall@100 each ≥ default minus that slice's
   bootstrap 95% CI half-width. `exact-identifier` is the guardrail every step must leave
   untouched.
3. Unjudged rate of the candidate's top-10 under 10%, else extend the pool and re-judge first.
4. Latency p50/p95 reported; only the reranker has a hard budget.

**Side-by-side review** is the second layer: for each gated PR the harness emits an HTML of
default vs candidate top-10 per query with grades and diffs; a human looks before a default
flips. With no traffic to A/B on, this replaces online testing.

**Dense/hybrid** is measured last as `dense`: BM25 candidates fused with embedding retrieval,
fusion weight from `ranx.optimize_fusion` (never untuned RRF by default). It becomes a default
only by the same gate; otherwise it stays opt-in and the vectors sidecar's future is a separate
decision made with data.

**Order**: baseline BM25 → `understand` → `aliases` → `rerank` (per model) → `dense`; each
measured on top of the previous defaults, sequenced in the implementation plan so no step is
measured against a moving default.

**Release rules**: a default flip is a minor version with the report linked; a semantics
change (filters applied from free text) is called out explicitly.

**Approach B trigger**: after `understand` ships, the harness reports parser coverage per
slice. If any slice's Recall@100 gap to the oracle (filters applied by hand from `expect`)
exceeds that slice's CI half-width, approach B is specced, starting from an existing
job-posting NER model (e.g. a `jobbert`-based 8-class tagger) fine-tuned on the query log.
Otherwise B is not built.

## 8. Out of scope

Index-time occupation normalization; multilingual queries and aliases (ESCO is the identified
source); click or engagement signals; LLM-at-query-time parsing (approach C) on the default
path; changes to the index storage or distribution (separate memo).

## 9. Sources consulted

LinkedIn *Semantic Search* (arXiv 2602.07309) and *LLM-Enhanced Query Understanding*
(2509.09690); BEIR and WANDS hybrid results; *Theoretical Limitations of Embedding-Based
Retrieval* (2508.21038); UMBRELA (2406.06519) and *Judging the Judges* (2502.13908); Indeed
Hiring Lab on entry-level postings (2026-07-23); TalentCLEF 2025 title linking; O*NET 30.3
Job Titles and ESCO v1.2 licences; GeoNames/geonamescache; Ettin reranker release; zerank-2
model card and latency post; answerai-colbert-small; ranx.
