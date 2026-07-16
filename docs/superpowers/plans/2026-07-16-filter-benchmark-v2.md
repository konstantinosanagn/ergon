# Filter Benchmark v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Benchmark every filter's extraction accuracy on a 10k+, all-provider sample, with an adjudication-first human loop, and turn the findings into per-ATS fixes + ratcheted CI gates + published stats.

**Architecture:** A `scripts/bench/` package holds pure, unit-tested logic (schema, stratified sampling, extractor runner, agreement/triage, scoring with confidence intervals, report rendering); thin CLIs drive the offline phases (crawl → label → adjudicate → score). Ground truth = a 3-way blind LLM fleet (majority vote) auto-accepted where it agrees with the live extractor, with humans adjudicating only triage-ordered conflicts via a self-contained "Label Auditor" HTML Artifact. Large corpora/labels stay out of git; only the report, enlarged fixtures, and the auditor are tracked.

**Tech Stack:** Python 3.10–3.13, pytest + respx, ruff, mypy --strict; existing `ergon_tracker` SDK (providers, `enrich_in_place`, extractors); the Agent/Workflow fleet for labeling; a single static HTML file for the auditor.

## Global Constraints

- **Offline, not in CI.** The crawl and fleet-labeling are heavy and run offline. Only the ratcheted `tests/test_*_recall.py` gates and the `scripts/bench/` unit tests run in CI. Never add a network/crawl step to CI.
- **JD fields need the real input.** Benchmark salary-text / yoe / degree / sponsorship against a **live full-JD refetch**, never the 300-char snippet.
- **Coverage ≠ precision.** Always report data-coverage (did the posting state it?) separately from extractor precision. "The ATS didn't say" is never counted as an extractor miss.
- **Ground truth:** 3-way blind fleet, majority vote; ties and extractor-vs-fleet conflicts go to the human queue. Auto-accept a (row, field) as gold only when extractor == fleet-majority; audit a random agreement slice to estimate the false-agreement rate.
- **Human budget:** ≤ ~500 contested rows + ~100 random-agreement calibration.
- **Data placement:** `bench/*.jsonl` is git-ignored. Track only `bench/report.md`, `bench/report.json`, the enlarged `tests/fixtures/*_corpus.jsonl`, and `scripts/bench/label_auditor.html`.
- **Green bar:** every code task ends ruff-clean, mypy --strict-clean, and pytest-green. New unit tests live under `tests/bench/`.
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Branch-first off `main`.
- **Stratify, don't volume-weight.** Every provider that exists gets a sample floor so small/new ATSes are measured. Report what was dropped/capped; never silently truncate.

---

### Task 0: `scripts/bench/` scaffolding + corpus schema

**Files:**
- Create: `scripts/bench/__init__.py`
- Create: `scripts/bench/schema.py`
- Create: `tests/bench/__init__.py`
- Create: `tests/bench/test_schema.py`
- Modify: `.gitignore` (add `bench/*.jsonl`)

**Interfaces:**
- Produces: `CorpusRow` (TypedDict) with keys `id, source, company, title, description_text, location_raw, structured_salary, apply_url, language, sector_hint, country_hint`; `Prediction`/`GoldLabel`/`Correction` dict shapes; `FIELDS` (the benchmarked field list); `read_jsonl(path)->list[dict]` and `write_jsonl(path, rows)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_schema.py
from pathlib import Path
from scripts.bench.schema import FIELDS, corpus_row, read_jsonl, write_jsonl

def test_fields_cover_every_benchmarked_filter():
    assert set(FIELDS) >= {
        "level", "sector", "country", "city", "remote", "employment_type",
        "salary", "yoe", "degree", "sponsorship", "posted_at", "visa_sponsor",
    }

def test_corpus_row_defaults_and_roundtrip(tmp_path: Path):
    row = corpus_row(id="greenhouse:1", source="greenhouse", title="Engineer")
    assert row["description_text"] == "" and row["structured_salary"] is None
    p = tmp_path / "c.jsonl"
    write_jsonl(p, [row])
    assert read_jsonl(p) == [row]
```

- [ ] **Step 2: Run it — expect ImportError/FAIL**

Run: `.venv/bin/python -m pytest tests/bench/test_schema.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `scripts/bench/schema.py`**

```python
"""Data shapes + JSONL IO for the filter benchmark (bench v2)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Every field the benchmark scores. Grouped by regime in the report, listed flat here.
FIELDS: list[str] = [
    "level", "sector", "country", "city", "remote", "employment_type",
    "salary", "yoe", "degree", "sponsorship", "posted_at", "visa_sponsor",
]

_CORPUS_DEFAULTS: dict[str, Any] = {
    "id": "", "source": "", "company": "", "title": "", "description_text": "",
    "location_raw": "", "structured_salary": None, "apply_url": "", "language": "en",
    "sector_hint": None, "country_hint": None,
}

def corpus_row(**kw: Any) -> dict[str, Any]:
    """A corpus row with defaults filled; unknown keys are kept (forward-compatible)."""
    return {**_CORPUS_DEFAULTS, **kw}

def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
```

- [ ] **Step 4: Add `.gitignore` entry + empty `__init__.py` files, run test to pass**

Add to `.gitignore`: `bench/*.jsonl`. Create empty `scripts/bench/__init__.py` and `tests/bench/__init__.py`.
Run: `.venv/bin/python -m pytest tests/bench/test_schema.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/bench-v2
git add scripts/bench tests/bench .gitignore
git commit -m "feat(bench): corpus schema + jsonl io scaffolding"
```

---

### Task 1: stratified sampling allocator

**Files:**
- Create: `scripts/bench/strata.py`
- Create: `tests/bench/test_strata.py`

**Interfaces:**
- Produces: `allocate(available: dict[str,int], total: int, floor: int) -> dict[str,int]` — given rows available per stratum (e.g. per provider), a total budget, and a per-stratum floor, return how many to draw per stratum: give each present stratum `min(available, floor)` first, then distribute the remainder proportionally to leftover availability; never exceed `available`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bench/test_strata.py
from scripts.bench.strata import allocate

def test_floor_first_then_proportional_remainder():
    avail = {"greenhouse": 5000, "coveo": 40, "peopleadmin": 120}
    out = allocate(avail, total=1000, floor=100)
    assert out["coveo"] == 40                    # floor capped at availability
    assert out["peopleadmin"] >= 100             # small provider gets its floor
    assert sum(out.values()) == 1000
    assert all(out[k] <= avail[k] for k in avail)

def test_total_capped_at_available():
    out = allocate({"a": 10, "b": 5}, total=1000, floor=100)
    assert out == {"a": 10, "b": 5}              # cannot draw more than exists
```

- [ ] **Step 2: Run — expect FAIL.** `.venv/bin/python -m pytest tests/bench/test_strata.py -q`

- [ ] **Step 3: Implement `scripts/bench/strata.py`**

```python
"""Stratified allocation: guarantee a floor per stratum, then fill proportionally."""
from __future__ import annotations

def allocate(available: dict[str, int], total: int, floor: int) -> dict[str, int]:
    avail = {k: v for k, v in available.items() if v > 0}
    if not avail:
        return {}
    if sum(avail.values()) <= total:
        return dict(avail)  # take everything
    out = {k: min(v, floor) for k, v in avail.items()}
    remaining = total - sum(out.values())
    # Distribute the remainder proportionally to unused availability, largest-remainder rounding.
    while remaining > 0:
        headroom = {k: avail[k] - out[k] for k in avail if avail[k] > out[k]}
        if not headroom:
            break
        pool = sum(headroom.values())
        added = 0
        for k, room in sorted(headroom.items(), key=lambda kv: -kv[1]):
            give = min(room, max(1, remaining * room // pool))
            give = min(give, remaining - added)
            out[k] += give
            added += give
            if added >= remaining:
                break
        remaining -= added
        if added == 0:
            break
    return out
```

- [ ] **Step 4: Run — PASS.** Adjust rounding until `sum == total` holds. Commit `feat(bench): stratified sampling allocator`.

---

### Task 2: extractor runner (predictions over a corpus row)

**Files:**
- Create: `scripts/bench/predict.py`
- Create: `tests/bench/test_predict.py`

**Interfaces:**
- Consumes: `CorpusRow` (Task 0), `ergon_tracker.enrich.enrich_in_place`, `JobPosting`.
- Produces: `predict(row: dict) -> dict[str, Any]` — reconstruct a `JobPosting` from the corpus row (title + description + location + structured salary), run the real enrichment, and read back the extractor's value for every field in `FIELDS` (normalized to the same vocabulary the fleet labels use: level→str, salary→`{min,max,currency}|None`, yoe→`{min,max}|None`, degree→str|None, sponsorship→bool|None, remote→bool, posted_at passthrough, etc.).

- [ ] **Step 1: Write the failing test** (uses a JD that states level+salary+yoe+degree so the extractors fire)

```python
# tests/bench/test_predict.py
from scripts.bench.predict import predict

def test_predict_reads_back_extractor_values():
    row = {
        "id": "greenhouse:1", "source": "greenhouse", "company": "Acme",
        "title": "Senior Software Engineer",
        "description_text": "5+ years of experience. Bachelor's degree required. $150,000-$180,000 USD.",
        "location_raw": "New York, NY", "structured_salary": None,
    }
    p = predict(row)
    assert p["level"] == "senior"
    assert p["country"] == "United States" and p["city"] == "New York"
    assert p["salary"]["min"] == 150000 and p["salary"]["currency"] == "USD"
    assert p["yoe"]["min"] == 5
    assert p["degree"] in {"bachelor", "bachelors"}
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement `scripts/bench/predict.py`** — build a `JobPosting` via `JobPosting.create(...)` with the row's title/company/locations, set `description_html`/`description_text`, run `enrich_in_place`, then map `job.level.value`, `job.locations[0].country/city`, `job.salary`, `job.years_experience_min/max`, `job.degree_min`, `job.sponsorship_offered`, `job.remote`, `job.sector`, `job.employment_type`, `job.visa_sponsor`, `job.posted_at` into the flat dict. Mirror the exact vocabulary in `docs/extraction-labeling-guide.md`.

- [ ] **Step 4: Run — PASS.** Fix vocabulary mismatches against the real extractor output. Commit `feat(bench): extractor runner`.

---

### Task 3: JD-bearing stratified crawl CLI

**Files:**
- Create: `scripts/bench/crawl_corpus.py`
- Create: `tests/bench/test_crawl_corpus.py` (unit-tests the pure helpers, respx-mocks one provider)

**Interfaces:**
- Consumes: `SeedRegistry`, provider `fetch`/`normalize`, `strata.allocate`, `schema.corpus_row/write_jsonl`, the JD-in-bulk provider list.
- Produces: CLI `python -m scripts.bench.crawl_corpus --out bench/corpus_jd.jsonl --total 15000 --floor 150` writing stratified JD-bearing rows across ALL providers; pure helper `select_targets(registry, total, floor) -> dict[str,list[str]]` (provider → chosen company tokens) that is unit-tested.

- [ ] **Step 1: Write the failing test** for `select_targets` (stub a tiny registry, assert per-provider floor + no provider missing) and for `row_from_job(job, source, url)` producing a valid `corpus_row` with `description_text` populated.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement.** `select_targets` uses `strata.allocate` over `registry` company counts per ATS; the crawl loops targets, fetches via the provider, normalizes, keeps only rows whose `description_text` is non-empty for JD-in-bulk sources (or issues a detail fetch for the enterprise sources via the existing Tier-3 detail path), dedups by `id`, and writes `corpus_row`s. Rate-limit via the shared `AsyncFetcher`. Log the realized per-provider counts + anything capped.

- [ ] **Step 4: Run unit tests — PASS.** Then a **real smoke run**: `python -m scripts.bench.crawl_corpus --out bench/corpus_jd.jsonl --total 300 --floor 20`; assert the file has ≥250 rows spanning ≥10 providers with non-empty `description_text`. Commit `feat(bench): stratified JD crawl`.

---

### Task 4: index-sampled structured supplement CLI

**Files:**
- Create: `scripts/bench/sample_structured.py`
- Create: `tests/bench/test_sample_structured.py`

**Interfaces:**
- Consumes: the cached index db (via `IndexCache`/direct sqlite read), `strata.allocate`, `schema`.
- Produces: CLI `python -m scripts.bench.sample_structured --out bench/corpus_structured.jsonl --total 10000 --floor 200` — sample rows from the local index for the no-JD fields (level, geo, sector, employment_type, remote, recency), stratified per provider; pure helper `stratified_sql_ids(counts, total, floor)` unit-tested against a synthetic counts dict.

- [ ] **Steps:** TDD the pure `stratified_sql_ids` helper (uses `allocate`), then implement the sqlite sampling (title/location/source/employment_type/posted_at columns only — no JD). Smoke-run at `--total 500`. Commit `feat(bench): structured index sample`.

---

### Task 5: fleet labeling orchestration

**Files:**
- Create: `scripts/bench/label_fleet.workflow.js` (Workflow script) OR documented Agent-fleet procedure
- Create: `scripts/bench/merge_votes.py`
- Create: `tests/bench/test_merge_votes.py`

**Interfaces:**
- Consumes: `bench/corpus_jd.jsonl` + `bench/corpus_structured.jsonl`, the rubric in `docs/extraction-labeling-guide.md` (extended in Task 11 for employment_type/recency/visa).
- Produces: `bench/labels.jsonl` — per row: `{id, votes: {field: [v1,v2,v3]}, gold: {field: majority}, split: {field: bool}}`. `merge_votes(votes: dict[str,list]) -> tuple[gold, split]` is the unit-tested core (majority; tie → `split=True`, gold=`None`).

- [ ] **Step 1: Write the failing test** for `merge_votes` (3 agree → gold+no split; 2/1 → majority; 1/1/1 → split, gold None; null handling).
- [ ] **Step 2–4:** implement `merge_votes`, PASS. Then the fleet: a Workflow that fans the corpus into shards, runs **3 independent labelers per shard** (diverse model/prompt) each emitting per-field labels per the rubric, and `merge_votes` reduces the three vote files. Acceptance: `labels.jsonl` has one line per corpus row with a `gold` per field and `split` flags. Commit `feat(bench): fleet labeling + vote merge`.

---

### Task 6: agreement classification + triage queue

**Files:**
- Create: `scripts/bench/triage.py`
- Create: `tests/bench/test_triage.py`

**Interfaces:**
- Consumes: predictions (Task 2), labels (Task 5).
- Produces: `agreement(pred, gold, field) -> "agree"|"conflict"|"coverage"|"na"` (value-aware equality per field type — numeric within tolerance, categorical exact, null-aware); `build_queue(rows, preds, labels, *, calib=100) -> list[dict]` — the triage-ordered audit queue: (1) conflicts, (2) coverage gaps, (3) fleet-split, then (4) a fixed `calib` random sample of agreements; each item carries `id, field, extractor_value, fleet_value, reason, url`.

- [ ] **Step 1: Write the failing test** — construct 4 synthetic rows hitting each triage class; assert ordering (conflict first, calibration last) and that agree-rows aren't in the queue except the calibration sample; assert numeric tolerance (`salary 150000 vs 151000` within 5% → agree).
- [ ] **Steps 2–4:** implement, PASS. Commit `feat(bench): agreement + triage queue`.

---

### Task 7: Label Auditor Artifact

**Files:**
- Create: `scripts/bench/label_auditor.html`

**Interfaces:**
- Consumes (at runtime, via file-picker): `bench/audit_queue.jsonl`.
- Produces (via download): `bench/corrections.jsonl` — `{id, field, verdict: "extractor"|"fleet"|"correct", value?, note?}` per adjudicated item.

- [ ] **Step 1:** Build the self-contained page (inline CSS/JS, no external calls): file-picker load; per item show title/source/company/location, the **full JD in a scrollable pane**, the **clickable apply URL**, and a field row with extractor-value vs fleet-value + the triage reason; one-key verdicts (`e`=extractor right, `f`=fleet right, `c`=type a correction, `s`=skip/ambiguous); progress bar; `localStorage` autosave; "download corrections" button emitting JSONL.
- [ ] **Step 2:** Publish via the Artifact tool; manual acceptance — load a 20-row sample `audit_queue.jsonl`, adjudicate, download, confirm the JSONL shape round-trips into Task 8. Commit `feat(bench): label auditor artifact` (HTML tracked; jsonl git-ignored).

---

### Task 8: corrections ingest → resolved gold

**Files:**
- Create: `scripts/bench/resolve_gold.py`
- Create: `tests/bench/test_resolve_gold.py`

**Interfaces:**
- Consumes: `labels.jsonl` (fleet gold), `corrections.jsonl` (human verdicts), predictions.
- Produces: `resolve(labels, preds, corrections) -> resolved.jsonl` — final gold per (row, field): human correction wins; else auto-accept extractor==fleet; else fleet-majority; each tagged `review_state: human|auto|fleet|unreviewed`. Also `calibration_stats(resolved, corrections)` → the human-verified fraction + the measured false-agreement rate on the calibration slice.

- [ ] **Steps:** TDD `resolve` precedence + `calibration_stats` on synthetic inputs; implement; PASS. Commit `feat(bench): resolve gold + calibration`.

---

### Task 9: scoring library (metrics + Wilson CI + provider matrix)

**Files:**
- Create: `scripts/bench/scoring.py`
- Create: `tests/bench/test_scoring.py`

**Interfaces:**
- Produces:
  - `wilson_ci(k, n, z=1.96) -> (lo, hi)`.
  - `score_field(field, rows) -> {n, accuracy, precision, recall, coverage, ci}` — categorical (exact, null-aware), numeric/range (stated-detection precision/recall + value-within-tolerance), tri-state (sponsorship). Coverage = fraction gold-stated; value-accuracy computed on the covered slice.
  - `provider_matrix(field, rows) -> dict[source, field_metrics]`.

- [ ] **Step 1: Write the failing test** — `wilson_ci(8,10)` bounds sanity; a categorical field with 1 known error → accuracy 0.9 + CI; a numeric field where value is within 5% → counted correct; coverage separated from precision; `provider_matrix` splits by source.
- [ ] **Steps 2–4:** implement, PASS. Commit `feat(bench): scoring + confidence intervals + provider matrix`.

---

### Task 10: report generator

**Files:**
- Create: `scripts/bench/score.py` (CLI)
- Create: `scripts/bench/report.py`
- Create: `tests/bench/test_report.py`

**Interfaces:**
- Consumes: `resolved.jsonl`, `predictions`, `scoring`.
- Produces: CLI `python -m scripts.bench.score --resolved bench/resolved.jsonl --out bench/report` → `bench/report.md` (per-field table with CIs + the provider×field matrix + coverage-vs-precision + calibration stats + a "worst per-ATS cells" section) and `bench/report.json` (machine, drives Task 12). `render_markdown(report_obj) -> str` is unit-tested for shape.

- [ ] **Steps:** TDD `render_markdown` on a synthetic report obj (asserts every FIELD + a matrix section appear); implement CLI; run end-to-end on whatever corpus exists; commit `feat(bench): report generator` (report.md/json tracked).

---

### Task 11: new-filter benchmarks + rubric extension

**Files:**
- Modify: `docs/extraction-labeling-guide.md` (add employment_type, posted_at/recency, visa rubric)
- Create: `scripts/bench/parity.py` + `tests/bench/test_parity.py`
- Create: `scripts/bench/dedup_eval.py` + `tests/bench/test_dedup_eval.py`

**Interfaces:**
- `parity.check_row(query, job) -> bool` — assert `SearchQuery.matches()` (client) and the index `_where` SQL agree for a given row+filter across the structured filters; report the divergence rate for keywords (FTS vs substring) and flag `max_last_seen_age_days` (SQL-only).
- `dedup_eval.sample_pairs(jobs) -> pairs` + precision/recall of `dedup` merges vs fleet-judged "same role?" labels.
- employment_type / posted_at / visa_sponsor are scored by Task 9 once present in `FIELDS` + labeled (visa precision = sampled matched employers verified against the DoL set).

- [ ] **Steps:** TDD `parity.check_row` (a row that matches a filter client-side must match in SQL) and `dedup_eval` pairing on synthetic clusters; extend the rubric doc; PASS. Commit `feat(bench): parity + dedup + new-filter benchmarks`.

---

### Task 12: diagnose, fix, ratchet, publish

**Files:**
- Modify: the specific `src/ergon_tracker/extract/*.py` / `providers/*.py` the report indicts (per-ATS defects)
- Modify: enlarged `tests/fixtures/<field>_corpus.jsonl` (promote human-confirmed rows into the ratcheting fixtures)
- Modify: `tests/test_<field>_recall.py` gate constants; `README.md` / `docs/extraction-baseline.md` accuracy tables

**Interfaces:**
- Consumes: `bench/report.json` (the worst per-ATS/field cells).

- [ ] **Step 1:** For each indicted cell, reproduce with a failing unit test against a real offending payload (fetch it, don't assume), fix the extractor/provider, re-run that field's slice, confirm improvement. One commit per fix.
- [ ] **Step 2:** Promote a de-duped, human-confirmed slice of the new corpus into `tests/fixtures/<field>_corpus.jsonl` (enlarging the ratcheting corpora with all-provider coverage). Raise each `*_GATE` to just under the newly-measured accuracy. Run the full suite — green.
- [ ] **Step 3:** Publish the accuracy table (per-field + headline per-provider notes, with CIs) into `README.md` + `docs/extraction-baseline.md`. Commit `docs: publish bench v2 accuracy + ratchet gates`.
- [ ] **Step 4:** Open a PR from `feat/bench-v2` summarizing the program, the provider matrix, the fixes, and the new gates.

---

## Self-review notes

- **Spec coverage:** every spec section maps to a task — corpus (T3/T4), fleet+adjudication (T5–T8), metrics/provider-matrix/CIs (T9/T10), new benchmarks (T11), diagnose/fix/ratchet/publish (T12). The Artifact is T7.
- **Type consistency:** `FIELDS` (T0) is the single field vocabulary consumed by predict (T2), triage (T6), scoring (T9); `corpus_row` shape is produced by T3/T4 and consumed everywhere; `allocate` (T1) is reused by T3/T4.
- **Right-sizing:** each task ends at an independently testable deliverable; the heavy offline phases (T3–T5, T7) carry smoke/manual acceptance because they cross the network / the browser, while all pure logic (T0–T2, T6, T8–T11) is unit-tested with real code shown.
- **Placeholders:** none — engine tasks show complete code; procedural tasks give exact CLIs + acceptance checks.
