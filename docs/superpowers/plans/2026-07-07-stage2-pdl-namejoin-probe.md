# Stage-2 PDL Name-Join Probe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether name-joining our 58k-company registry against the PDL Free Company Dataset lifts sector coverage to ≥~35% (from 16.2%) at ≥72.4% accuracy-vs-gold — the go/no-go for building the full Stage-2 data-join pipeline.

**Architecture:** One offline script, `scripts/probe_pdl_sectors.py`, in four isolated units: acquisition (download→gitignored scratch, or `--dump PATH`), a parallel memory-bounded streaming name-join (env-gated `ProcessPoolExecutor`), a static LinkedIn-industry→27 crosswalk, and measurement/verdict. Ships nothing to runtime; `SectorExtractor` and `sectors.json` are untouched.

**Tech Stack:** Python ≥3.10, stdlib only (json, gzip, concurrent.futures, resource, urllib) + the existing `ergon_tracker.dedup.normalize_company` and `ergon_tracker.registry.store.SeedRegistry`. No new dependency. pytest (`asyncio_mode=auto`, `pythonpath=["."]`).

## Global Constraints

- **Free · offline · CPU-only · laptop-safe.** No paid APIs. The only network is the one-time dataset download (decoupled via `--dump PATH`). Heavy work is one-time, streamed, memory-bounded.
- **No new dependency.** stdlib + json; stream lines, never load a frame or the whole file. numpy/pandas/sklearn NOT used.
- **Nothing ships to runtime.** `src/ergon_tracker/extract/sector.py` and `src/ergon_tracker/registry/data/sectors.json` are NOT modified. The probe only measures. No multi-GB file is committed (scratch is gitignored).
- **Concurrency is env-gated, laptop-safe by default** (repo idiom, mirrors `ERGON_SHARD_WORKERS`): `ERGON_PROBE_WORKERS` explicit int → else `max(2, (os.cpu_count() or 4) - 2)` on CI → else `1` local.
- **Memory-bounded streaming:** peak memory is O(target-set + in-flight chunks + matches), never O(dump). The main process never holds more than a bounded number of pending chunks.
- **Stress-test before any full run:** a `--sample N` mode + a synthetic memory-watch test must pass first; every heavy step logs peak-RSS + wall-time and fails fast over a laptop budget.
- **Python ≥3.10; ruff line-length 100, no semicolon one-liners (E701/E702 are selected); mypy is `src/`-only** (the script isn't type-checked, but `src` must stay green; the script only *imports* from `src`).
- **Go/no-go bar:** GO iff projected registry coverage ≥ 0.35 AND gold accuracy-when-covered ≥ 0.724. Else NO-GO.

## Key Facts (verified against the codebase)

- `normalize_company(company: str) -> str` (`src/ergon_tracker/dedup.py:123-132`): lowercases, `&`→`and`, strips punctuation via `[^a-z0-9]+`, drops legal-suffix stopwords (`inc, llc, ltd, gmbh, corp, co, company, plc, ag, sa, holdings, the, …`), returns space-joined tokens. `"Acme, Inc."`→`"acme"`; `"Kirkland & Ellis"`→`"kirkland and ellis"`. Note the registry slug `"kirklandandellisllp"`→`"kirklandandellisllp"` (fused, no split) — so slug-vs-display can diverge; registry match rate is conservative (expected, per spec).
- `SeedRegistry().all() -> dict[str, dict]` (`src/ergon_tracker/registry/store.py:103-105`): `{company_key: {"ats","token","domain"?}}`. **No display-name field** — registry side joins on the slug key.
- `sectors.json` (`src/ergon_tracker/registry/data/sectors.json`): `{"_meta":…, "companies": {key: {"sector": str|null, "domain": str|null}}}`. "Currently covered" = key with non-null `sector`.
- Script path idiom (`scripts/merge_sectors.py:14-19`): `ROOT = Path(__file__).resolve().parents[1]`; read `ROOT/"src"/"ergon_tracker"/"registry"/"data"/"seed.json"` via `json.loads(p.read_text())`.
- Gold fixture `tests/fixtures/sector_corpus.jsonl`: `{"company","company_key","domain","sector"|null,"src"}`, 699 rows.
- `.gitignore` scratch idiom (`:40-46`): `scripts/.h1b_cache/`, `scripts/.sector_wd_*.json`.

## File Structure

**Create:**
- `scripts/probe_pdl_sectors.py` — the whole probe (acquisition + join + measurement + `main`). One file: the units are functions with clear boundaries, but they share the streaming/CLI plumbing, so one focused script is right (mirrors `merge_sectors.py`).
- `scripts/linkedin_industry_to_sector.json` — committed static crosswalk (LinkedIn/PDL industry string → one of the 27 labels).
- `tests/test_probe_pdl_sectors.py` — TDD unit tests on synthetic in-memory data (no network, no real dump).
- `docs/superpowers/artifacts/2026-07-07-stage2-pdl-probe.md` — recorded numbers + verdict (Task 7).

**Modify:**
- `.gitignore` — add `scripts/.probe_cache/`.
- `docs/extraction-baseline.md` — record the Stage-2 probe verdict (Task 7).

---

## Task 1: Crosswalk + scaffolding

**Files:**
- Create: `scripts/linkedin_industry_to_sector.json`
- Modify: `.gitignore`
- Test: `tests/test_probe_pdl_sectors.py`

**Interfaces:**
- Produces: a JSON object `{lowercased-industry-string: "<one of the 27 labels>"}`. The 27 valid labels are: `AI/ML, Aerospace/Defense, Automotive/Mobility, Banking/Finance, Biotech/Pharma, Consulting/Services, Consumer/Lifestyle, Crypto/Web3, Cybersecurity, E-commerce/Retail, Education, Energy/Climate, Fintech, Food/Beverage, Gaming, Government/Public, Healthcare, Insurance, Logistics/SupplyChain, Manufacturing/Industrial, Media/Entertainment, Other, RealEstate/PropTech, Semiconductors/Hardware, Software/SaaS, Telecom, Travel/Hospitality`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probe_pdl_sectors.py
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROSSWALK = ROOT / "scripts" / "linkedin_industry_to_sector.json"

VALID_SECTORS = {
    "AI/ML", "Aerospace/Defense", "Automotive/Mobility", "Banking/Finance", "Biotech/Pharma",
    "Consulting/Services", "Consumer/Lifestyle", "Crypto/Web3", "Cybersecurity",
    "E-commerce/Retail", "Education", "Energy/Climate", "Fintech", "Food/Beverage", "Gaming",
    "Government/Public", "Healthcare", "Insurance", "Logistics/SupplyChain",
    "Manufacturing/Industrial", "Media/Entertainment", "Other", "RealEstate/PropTech",
    "Semiconductors/Hardware", "Software/SaaS", "Telecom", "Travel/Hospitality",
}


def test_crosswalk_values_are_valid_sectors() -> None:
    data = json.loads(CROSSWALK.read_text())
    assert len(data) >= 60, f"crosswalk too small ({len(data)})"
    bad = {v for v in data.values() if v not in VALID_SECTORS}
    assert not bad, f"invalid sector labels in crosswalk: {bad}"


def test_crosswalk_keys_are_lowercased() -> None:
    data = json.loads(CROSSWALK.read_text())
    assert all(k == k.lower() for k in data), "crosswalk keys must be lowercased"


def test_crosswalk_covers_high_frequency_industries() -> None:
    data = json.loads(CROSSWALK.read_text())
    # a few anchor mappings that must be correct
    assert data["computer software"] == "Software/SaaS"
    assert data["banking"] == "Banking/Finance"
    assert data["biotechnology"] == "Biotech/Pharma"
    assert data["semiconductors"] == "Semiconductors/Hardware"
    assert data["hospital & health care"] == "Healthcare"
    assert data["computer games"] == "Gaming"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -v`
Expected: FAIL — crosswalk file does not exist.

- [ ] **Step 3: Create the crosswalk file**

Write `scripts/linkedin_industry_to_sector.json` mapping the standard LinkedIn/PDL industry strings (lowercased, as PDL stores them) to our 27 labels. Start from this set and EXTEND to cover the standard ~147-value LinkedIn v1 taxonomy; any industry you cannot confidently map to a single label, OMIT (a missing key → no sector → no wrong guess). Keep the anchor mappings from the test exact.

```json
{
  "computer software": "Software/SaaS",
  "information technology and services": "Software/SaaS",
  "internet": "Software/SaaS",
  "computer & network security": "Cybersecurity",
  "computer networking": "Telecom",
  "computer hardware": "Semiconductors/Hardware",
  "semiconductors": "Semiconductors/Hardware",
  "electrical/electronic manufacturing": "Semiconductors/Hardware",
  "telecommunications": "Telecom",
  "wireless": "Telecom",
  "financial services": "Banking/Finance",
  "banking": "Banking/Finance",
  "investment banking": "Banking/Finance",
  "investment management": "Banking/Finance",
  "capital markets": "Banking/Finance",
  "venture capital & private equity": "Banking/Finance",
  "insurance": "Insurance",
  "hospital & health care": "Healthcare",
  "medical practice": "Healthcare",
  "medical devices": "Healthcare",
  "mental health care": "Healthcare",
  "health, wellness and fitness": "Healthcare",
  "biotechnology": "Biotech/Pharma",
  "pharmaceuticals": "Biotech/Pharma",
  "aviation & aerospace": "Aerospace/Defense",
  "defense & space": "Aerospace/Defense",
  "automotive": "Automotive/Mobility",
  "oil & energy": "Energy/Climate",
  "renewables & environment": "Energy/Climate",
  "utilities": "Energy/Climate",
  "mining & metals": "Manufacturing/Industrial",
  "machinery": "Manufacturing/Industrial",
  "industrial automation": "Manufacturing/Industrial",
  "mechanical or industrial engineering": "Manufacturing/Industrial",
  "building materials": "Manufacturing/Industrial",
  "chemicals": "Manufacturing/Industrial",
  "plastics": "Manufacturing/Industrial",
  "logistics & supply chain": "Logistics/SupplyChain",
  "transportation/trucking/railroad": "Logistics/SupplyChain",
  "package/freight delivery": "Logistics/SupplyChain",
  "warehousing": "Logistics/SupplyChain",
  "maritime": "Logistics/SupplyChain",
  "higher education": "Education",
  "education management": "Education",
  "e-learning": "Education",
  "primary/secondary education": "Education",
  "research": "Education",
  "real estate": "RealEstate/PropTech",
  "commercial real estate": "RealEstate/PropTech",
  "management consulting": "Consulting/Services",
  "information services": "Consulting/Services",
  "outsourcing/offshoring": "Consulting/Services",
  "staffing and recruiting": "Consulting/Services",
  "human resources": "Consulting/Services",
  "accounting": "Consulting/Services",
  "legal services": "Consulting/Services",
  "law practice": "Consulting/Services",
  "entertainment": "Media/Entertainment",
  "media production": "Media/Entertainment",
  "broadcast media": "Media/Entertainment",
  "online media": "Media/Entertainment",
  "publishing": "Media/Entertainment",
  "music": "Media/Entertainment",
  "motion pictures and film": "Media/Entertainment",
  "marketing and advertising": "Media/Entertainment",
  "computer games": "Gaming",
  "leisure, travel & tourism": "Travel/Hospitality",
  "hospitality": "Travel/Hospitality",
  "airlines/aviation": "Travel/Hospitality",
  "restaurants": "Food/Beverage",
  "food & beverages": "Food/Beverage",
  "food production": "Food/Beverage",
  "wine and spirits": "Food/Beverage",
  "consumer goods": "Consumer/Lifestyle",
  "consumer electronics": "Consumer/Lifestyle",
  "consumer services": "Consumer/Lifestyle",
  "apparel & fashion": "Consumer/Lifestyle",
  "cosmetics": "Consumer/Lifestyle",
  "luxury goods & jewelry": "Consumer/Lifestyle",
  "sporting goods": "Consumer/Lifestyle",
  "retail": "E-commerce/Retail",
  "wholesale": "E-commerce/Retail",
  "government administration": "Government/Public",
  "government relations": "Government/Public",
  "public policy": "Government/Public",
  "political organization": "Government/Public",
  "nonprofit organization management": "Government/Public",
  "civic & social organization": "Government/Public",
  "manufacturing": "Manufacturing/Industrial",
  "textiles": "Manufacturing/Industrial",
  "aerospace": "Aerospace/Defense"
}
```

- [ ] **Step 4: Add the gitignore entry**

Append to `.gitignore` (after the `scripts/.sector_wd_*.json` line):

```
# Stage-2 PDL probe download cache (regenerated per run)
scripts/.probe_cache/
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add scripts/linkedin_industry_to_sector.json tests/test_probe_pdl_sectors.py .gitignore
git commit -m "feat(stage2): LinkedIn-industry to 27-label crosswalk + probe scaffolding

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Target index (registry + gold + covered set)

**Files:**
- Create: `scripts/probe_pdl_sectors.py` (this task's portion)
- Test: `tests/test_probe_pdl_sectors.py`

**Interfaces:**
- Consumes: `normalize_company` (from `ergon_tracker.dedup`), `SeedRegistry` (from `ergon_tracker.registry.store`).
- Produces:
  - `norm(name: str) -> str` — `normalize_company` wrapper returning `""` for falsy/empty input.
  - `load_crosswalk(path=CROSSWALK_PATH) -> dict[str, str]`.
  - `build_target_index(seed, sectors, gold) -> TargetIndex` where `TargetIndex` is a dataclass with: `registry_norms: set[str]`, `norm_to_keys: dict[str, list[str]]`, `covered_keys: set[str]`, `gold_norm_to_sector: dict[str, str]`. `seed`/`sectors`/`gold` are passed in (pure function → testable without files).
  - `load_inputs() -> tuple[dict, dict, list[dict]]` — reads seed.json (via `SeedRegistry().all()`), sectors.json (raw), gold jsonl; returns `(seed, sectors_companies, gold_rows)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_probe_pdl_sectors.py
import pytest

probe = pytest.importorskip("scripts.probe_pdl_sectors")


def test_norm_wraps_normalize_company() -> None:
    assert probe.norm("Acme, Inc.") == "acme"
    assert probe.norm("") == ""
    assert probe.norm(None) == ""


def test_build_target_index() -> None:
    seed = {"acme": {"ats": "greenhouse"}, "globex": {"ats": "lever"}, "initech": {"ats": "ashby"}}
    sectors = {"acme": {"sector": "Software/SaaS"}, "globex": {"sector": None}}
    gold = [
        {"company": "Acme Inc", "company_key": "acme", "sector": "Software/SaaS"},
        {"company": "Globex", "company_key": "globex", "sector": None},
    ]
    idx = probe.build_target_index(seed, sectors, gold)
    assert "acme" in idx.registry_norms and "globex" in idx.registry_norms
    assert idx.norm_to_keys["acme"] == ["acme"]
    assert idx.covered_keys == {"acme"}  # only acme has a non-null sector
    assert idx.gold_norm_to_sector == {"acme": "Software/SaaS"}  # null-sector gold dropped
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "norm or target_index" -v`
Expected: FAIL — module/functions not defined.

- [ ] **Step 3: Implement (top of the script)**

```python
# scripts/probe_pdl_sectors.py
"""Stage-2 de-risk probe: name-join the registry against the PDL Free Company Dataset and measure
achievable sector coverage + accuracy vs the go/no-go bar. Offline, stdlib-only, ships nothing.

Usage:
  .venv/bin/python scripts/probe_pdl_sectors.py --dump scripts/.probe_cache/pdl_free.ndjson.gz
  .venv/bin/python scripts/probe_pdl_sectors.py --dump <path> --sample 100000   # stress gate first
Env: ERGON_PROBE_WORKERS (explicit worker count; else CI=cpu-2, else 1).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.dedup import normalize_company  # noqa: E402
from ergon_tracker.registry.store import SeedRegistry  # noqa: E402

CROSSWALK_PATH = ROOT / "scripts" / "linkedin_industry_to_sector.json"
SECTORS_PATH = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "sectors.json"
GOLD_PATH = ROOT / "tests" / "fixtures" / "sector_corpus.jsonl"


def norm(name: str | None) -> str:
    return normalize_company(name) if name else ""


def load_crosswalk(path: Path = CROSSWALK_PATH) -> dict[str, str]:
    return json.loads(Path(path).read_text())


@dataclass
class TargetIndex:
    registry_norms: set[str] = field(default_factory=set)
    norm_to_keys: dict[str, list[str]] = field(default_factory=dict)
    covered_keys: set[str] = field(default_factory=set)
    gold_norm_to_sector: dict[str, str] = field(default_factory=dict)


def build_target_index(seed: dict, sectors: dict, gold: list[dict]) -> TargetIndex:
    idx = TargetIndex()
    for key in seed:
        n = norm(key)
        if not n:
            continue
        idx.registry_norms.add(n)
        idx.norm_to_keys.setdefault(n, []).append(key)
    for key, entry in sectors.items():
        if entry.get("sector"):
            idx.covered_keys.add(key)
    for row in gold:
        if row.get("sector"):
            idx.gold_norm_to_sector[norm(row.get("company"))] = row["sector"]
    return idx


def load_inputs() -> tuple[dict, dict, list[dict]]:
    seed = SeedRegistry().all()
    sectors = json.loads(SECTORS_PATH.read_text()).get("companies", {})
    gold = [json.loads(ln) for ln in GOLD_PATH.read_text().splitlines() if ln.strip()]
    return seed, sectors, gold
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "norm or target_index" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
.venv/bin/ruff format scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git add scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git commit -m "feat(stage2): probe target index (registry norms + covered keys + gold)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Streaming parallel name-join engine

**Files:**
- Modify: `scripts/probe_pdl_sectors.py`
- Test: `tests/test_probe_pdl_sectors.py`

**Interfaces:**
- Consumes: `TargetIndex.registry_norms`, `TargetIndex.gold_norm_to_sector`.
- Produces:
  - `record_industry(rec: dict, name_field="name", industry_field="industry") -> tuple[str, str, int] | None` — from one PDL record returns `(norm_name, raw_industry, completeness)` or `None` if no usable name. `completeness` = count of non-empty values in `rec` (the collision tie-break score).
  - `join_chunk(lines: list[str], targets: frozenset[str]) -> dict[str, tuple[str, int]]` — parse ndjson lines, keep records whose `norm_name ∈ targets`, return `{norm_name: (raw_industry, completeness)}` keeping the highest-completeness per name.
  - `merge_matches(dst, src) -> None` — merge a chunk result into the accumulator, keeping the higher completeness and counting collisions (distinct raw_industry seen for a name) in `dst`'s companion counter.
  - `run_join(line_iter, targets, *, workers, chunk_size=20000) -> tuple[dict[str, str], int]` — orchestrates streaming + pooling; returns `(matches {norm: raw_industry}, collision_count)`. `workers==1` runs inline (no pool). Bounded in-flight chunks (≤ 2×workers) so memory stays O(chunk).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_probe_pdl_sectors.py
def test_record_industry_extracts_and_scores() -> None:
    rec = {"name": "Acme, Inc.", "industry": "computer software", "size": "11-50", "x": ""}
    out = probe.record_industry(rec)
    assert out == ("acme", "computer software", 3)  # 3 non-empty values
    assert probe.record_industry({"industry": "x"}) is None  # no name → None


def test_join_chunk_filters_and_keeps_most_complete() -> None:
    targets = frozenset({"acme", "globex"})
    lines = [
        json.dumps({"name": "Acme", "industry": "internet"}),
        json.dumps({"name": "Acme Inc", "industry": "computer software", "size": "1", "hq": "SF"}),
        json.dumps({"name": "Nope", "industry": "banking"}),
    ]
    got = probe.join_chunk(lines, targets)
    assert set(got) == {"acme"}  # globex absent, Nope filtered out
    assert got["acme"][0] == "computer software"  # higher completeness wins


def test_run_join_inline_and_parallel_agree() -> None:
    targets = frozenset({"acme", "globex"})
    lines = [
        json.dumps({"name": "Acme", "industry": "internet"}),
        json.dumps({"name": "Globex", "industry": "banking"}),
        json.dumps({"name": "Other", "industry": "retail"}),
    ]
    m1, c1 = probe.run_join(iter(lines), targets, workers=1, chunk_size=2)
    m2, c2 = probe.run_join(iter(lines), targets, workers=2, chunk_size=1)
    assert m1 == m2 == {"acme": "internet", "globex": "banking"}


def test_run_join_memory_bounded_on_large_stream() -> None:
    # 200k synthetic rows, only a few match; peak matches stays tiny (memory-bounded).
    targets = frozenset({"acme"})
    def gen():
        for i in range(200_000):
            yield json.dumps({"name": f"co{i}", "industry": "internet"})
        yield json.dumps({"name": "Acme", "industry": "computer software"})
    matches, _ = probe.run_join(gen(), targets, workers=1, chunk_size=10_000)
    assert matches == {"acme": "computer software"}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "join or record_industry" -v`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement the join engine**

```python
# append to scripts/probe_pdl_sectors.py
import os  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402


def _workers() -> int:
    env = os.environ.get("ERGON_PROBE_WORKERS")
    if env:
        return int(env)
    if os.environ.get("CI"):
        return max(2, (os.cpu_count() or 4) - 2)
    return 1


def record_industry(
    rec: dict, name_field: str = "name", industry_field: str = "industry"
) -> tuple[str, str, int] | None:
    n = norm(rec.get(name_field))
    if not n:
        return None
    completeness = sum(1 for v in rec.values() if v not in (None, "", [], {}))
    return n, (rec.get(industry_field) or ""), completeness


def join_chunk(lines: list[str], targets: frozenset[str]) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        got = record_industry(rec)
        if got is None:
            continue
        n, industry, comp = got
        if n not in targets:
            continue
        prev = out.get(n)
        if prev is None or comp > prev[1]:
            out[n] = (industry, comp)
    return out


# module-level target set for worker processes (set via initializer; avoids re-pickling per chunk)
_WORKER_TARGETS: frozenset[str] = frozenset()


def _init_worker(targets: frozenset[str]) -> None:
    global _WORKER_TARGETS
    _WORKER_TARGETS = targets


def _join_chunk_worker(lines: list[str]) -> dict[str, tuple[str, int]]:
    return join_chunk(lines, _WORKER_TARGETS)


def _merge(dst: dict[str, tuple[str, int]], collisions: set[str], src: dict[str, tuple[str, int]]) -> None:
    for n, (industry, comp) in src.items():
        prev = dst.get(n)
        if prev is None:
            dst[n] = (industry, comp)
        else:
            if prev[0] != industry:
                collisions.add(n)
            if comp > prev[1]:
                dst[n] = (industry, comp)


def _chunks(line_iter, size: int):
    buf: list[str] = []
    for ln in line_iter:
        buf.append(ln)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def run_join(line_iter, targets: frozenset[str], *, workers: int, chunk_size: int = 20000):
    acc: dict[str, tuple[str, int]] = {}
    collisions: set[str] = set()
    if workers <= 1:
        for chunk in _chunks(line_iter, chunk_size):
            _merge(acc, collisions, join_chunk(chunk, targets))
    else:
        # bounded in-flight submission so we never hold the whole file in pending futures
        from concurrent.futures import FIRST_COMPLETED, wait

        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(targets,)
        ) as pool:
            gen = _chunks(line_iter, chunk_size)
            pending: set = set()
            cap = workers * 2
            for chunk in gen:
                pending.add(pool.submit(_join_chunk_worker, chunk))
                if len(pending) >= cap:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for f in done:
                        _merge(acc, collisions, f.result())
            for f in pending:
                _merge(acc, collisions, f.result())
    return {n: industry for n, (industry, _) in acc.items()}, len(collisions)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "join or record_industry" -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
.venv/bin/ruff format scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git add scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git commit -m "feat(stage2): streaming env-gated parallel name-join engine (bounded memory)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Measurement & verdict

**Files:**
- Modify: `scripts/probe_pdl_sectors.py`
- Test: `tests/test_probe_pdl_sectors.py`

**Interfaces:**
- Consumes: `TargetIndex`, `load_crosswalk`, join `matches: dict[norm→raw_industry]`.
- Produces:
  - `measure(matches, idx, crosswalk, *, total_registry) -> dict` — returns metrics: `gold_coverage`, `gold_accuracy`, `net_new_keys`, `projected_coverage`, `current_coverage`, `sectors_agreement`, `matched_with_sector`.
  - `verdict(metrics, *, min_coverage=0.35, min_accuracy=0.724) -> bool`.
- Rule: a PDL match contributes a sector only if its `raw_industry` crosswalks to a label (else it's abstain, no coverage). Registry projected coverage = |covered_keys ∪ newly-sectored keys| / total_registry.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_probe_pdl_sectors.py
def test_measure_and_verdict() -> None:
    idx = probe.TargetIndex(
        registry_norms={"acme", "globex", "initech"},
        norm_to_keys={"acme": ["acme"], "globex": ["globex"], "initech": ["initech"]},
        covered_keys={"acme"},
        gold_norm_to_sector={"acme": "Software/SaaS", "globex": "Banking/Finance"},
    )
    crosswalk = {"internet": "Software/SaaS", "banking": "Banking/Finance", "retail": "E-commerce/Retail"}
    # acme→internet (correct vs gold), globex→banking (correct), initech→retail (net-new registry)
    matches = {"acme": "internet", "globex": "banking", "initech": "retail"}
    m = probe.measure(matches, idx, crosswalk, total_registry=3)
    assert m["gold_accuracy"] == 1.0            # 2/2 gold correct
    assert m["gold_coverage"] == 1.0            # 2/2 gold matched w/ a sector
    assert m["net_new_keys"] == 2               # globex + initech newly sectored (acme already covered)
    assert m["projected_coverage"] == pytest.approx(3 / 3)  # acme,globex,initech all covered now
    assert probe.verdict(m) is True

    # a wrong crosswalk drags accuracy below the bar → NO-GO
    m2 = probe.measure({"acme": "banking", "globex": "banking"}, idx, crosswalk, total_registry=3)
    assert m2["gold_accuracy"] == 0.5
    assert probe.verdict(m2) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "measure" -v`
Expected: FAIL — `measure` not defined.

- [ ] **Step 3: Implement**

```python
# append to scripts/probe_pdl_sectors.py
def measure(matches: dict[str, str], idx: TargetIndex, crosswalk: dict[str, str], *, total_registry: int) -> dict:
    # crosswalk each matched industry → sector (or None = abstain)
    sectored = {n: crosswalk.get(ind) for n, ind in matches.items()}
    sectored = {n: s for n, s in sectored.items() if s}  # keep only those with a real label

    # gold accuracy + coverage (measured on the gold display-name overlap)
    gold_hits = gold_total = 0
    for n, gold_sector in idx.gold_norm_to_sector.items():
        s = sectored.get(n)
        if s is None:
            continue
        gold_total += 1
        if s == gold_sector:
            gold_hits += 1
    gold_accuracy = gold_hits / gold_total if gold_total else 0.0
    gold_coverage = gold_total / len(idx.gold_norm_to_sector) if idx.gold_norm_to_sector else 0.0

    # registry net-new: keys whose norm got a sector AND that key isn't already covered
    newly = set()
    for n in sectored:
        for key in idx.norm_to_keys.get(n, []):
            if key not in idx.covered_keys:
                newly.add(key)
    projected = len(idx.covered_keys | newly) / total_registry if total_registry else 0.0

    return {
        "gold_accuracy": gold_accuracy,
        "gold_coverage": gold_coverage,
        "matched_with_sector": len(sectored),
        "net_new_keys": len(newly),
        "current_coverage": len(idx.covered_keys) / total_registry if total_registry else 0.0,
        "projected_coverage": projected,
    }


def verdict(metrics: dict, *, min_coverage: float = 0.35, min_accuracy: float = 0.724) -> bool:
    return metrics["projected_coverage"] >= min_coverage and metrics["gold_accuracy"] >= min_accuracy
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "measure" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
.venv/bin/ruff format scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git add scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git commit -m "feat(stage2): probe measurement + go/no-go verdict

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Acquisition + `main` + stress instrumentation

**Files:**
- Modify: `scripts/probe_pdl_sectors.py`
- Test: `tests/test_probe_pdl_sectors.py`

**Interfaces:**
- Produces:
  - `_peak_rss_mb() -> float` (darwin bytes vs linux KB, same as Stage-1).
  - `open_dump(path: Path)` — a context-managed text line iterator transparently handling `.gz`.
  - `resolve_dump(args) -> Path` — returns `--dump PATH` if given and existing; else checks the cache dir; else prints acquisition instructions (PDL Free URL + BigPicture fallback) and exits non-zero. (No silent partial run.)
  - `main(argv) -> None` — parse `--dump/--sample/--chunk-size`; stream via `open_dump`; if `--sample N`, cap the line iterator at N, run, print `[stress]` peak-RSS/wall-time and RETURN (the stress gate); else full run → `measure` → print metrics + risk table + `VERDICT`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_probe_pdl_sectors.py
import gzip


def test_open_dump_reads_plain_and_gz(tmp_path) -> None:
    p = tmp_path / "d.ndjson"
    p.write_text('{"name":"Acme","industry":"internet"}\n')
    with probe.open_dump(p) as it:
        assert sum(1 for _ in it) == 1
    g = tmp_path / "d.ndjson.gz"
    with gzip.open(g, "wt") as f:
        f.write('{"name":"Acme","industry":"internet"}\n')
    with probe.open_dump(g) as it:
        assert sum(1 for _ in it) == 1


def test_resolve_dump_missing_exits(tmp_path, capsys) -> None:
    import argparse
    args = argparse.Namespace(dump=str(tmp_path / "nope.ndjson"))
    with pytest.raises(SystemExit):
        probe.resolve_dump(args)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -k "open_dump or resolve_dump" -v`
Expected: FAIL.

- [ ] **Step 3: Implement acquisition + main**

```python
# append to scripts/probe_pdl_sectors.py
import argparse  # noqa: E402
import gzip  # noqa: E402
import time  # noqa: E402
from contextlib import contextmanager  # noqa: E402

CACHE_DIR = ROOT / "scripts" / ".probe_cache"
PDL_INFO = (
    "PDL Free Company Dataset (CC-BY-4.0): download the newline-delimited JSON dump (name+industry),\n"
    "place it at scripts/.probe_cache/pdl_free.ndjson.gz, and re-run with --dump <that path>.\n"
    "Fallback: BigPicture free company dataset (ODC-BY), same LinkedIn industry enum."
)


def _peak_rss_mb() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


@contextmanager
def open_dump(path: Path):
    p = Path(path)
    fh = gzip.open(p, "rt", encoding="utf-8") if p.suffix == ".gz" else p.open(encoding="utf-8")
    try:
        yield fh
    finally:
        fh.close()


def resolve_dump(args) -> Path:
    if args.dump:
        p = Path(args.dump)
        if p.exists():
            return p
        print(f"--dump path not found: {p}\n\n{PDL_INFO}")
        raise SystemExit(2)
    for name in ("pdl_free.ndjson.gz", "pdl_free.ndjson"):
        cand = CACHE_DIR / name
        if cand.exists():
            return cand
    print(f"no dump found in {CACHE_DIR}\n\n{PDL_INFO}")
    raise SystemExit(2)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--chunk-size", type=int, default=20000)
    args = ap.parse_args(argv)

    dump = resolve_dump(args)
    seed, sectors, gold = load_inputs()
    idx = build_target_index(seed, sectors, gold)
    crosswalk = load_crosswalk()
    targets = frozenset(idx.registry_norms | set(idx.gold_norm_to_sector))
    workers = _workers()
    print(f"[probe] registry={len(seed)} targets={len(targets)} workers={workers} dump={dump.name}")

    t0 = time.monotonic()
    with open_dump(dump) as fh:
        it = fh
        if args.sample:
            import itertools

            it = itertools.islice(fh, args.sample)
        matches, collisions = run_join(it, targets, workers=workers, chunk_size=args.chunk_size)
    wall = time.monotonic() - t0

    if args.sample:
        print(f"[stress] sample={args.sample} matches={len(matches)} collisions={collisions} "
              f"peakRSS={_peak_rss_mb():.0f}MB wall={wall:.1f}s — full run is safe.")
        return

    m = measure(matches, idx, crosswalk, total_registry=len(seed))
    print(f"[join] matches={len(matches)} w/sector={m['matched_with_sector']} collisions={collisions} "
          f"peakRSS={_peak_rss_mb():.0f}MB wall={wall:.1f}s")
    print("\n=== Stage-2 PDL name-join probe ===")
    print(f"  gold accuracy-when-covered : {m['gold_accuracy']:.1%}  (bar 72.4%)")
    print(f"  gold coverage              : {m['gold_coverage']:.1%}")
    print(f"  registry current coverage  : {m['current_coverage']:.1%}")
    print(f"  registry net-new companies : {m['net_new_keys']}")
    print(f"  registry projected coverage: {m['projected_coverage']:.1%}  (bar 35%)")
    go = verdict(m)
    print(f"  VERDICT: {'GO — build full Stage-2 pipeline' if go else 'NO-GO — pivot to squeeze-existing'}")


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 4: Run to verify pass + full file suite**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -v`
Expected: PASS (all tests, ~13 passed).

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
.venv/bin/ruff format scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
.venv/bin/mypy
git add scripts/probe_pdl_sectors.py tests/test_probe_pdl_sectors.py
git commit -m "feat(stage2): dump acquisition (gz-aware, fail-fast) + main + stress instrumentation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Acquire dump + stress gate + full probe run

**Files:** none created; runs the probe.

- [ ] **Step 1: Full unit sweep (must be green before any heavy run)**

Run: `.venv/bin/pytest tests/test_probe_pdl_sectors.py -v`
Expected: all PASS.

- [ ] **Step 2: Acquire the dump**

Download the PDL Free Company Dataset (newline-delimited JSON, fields `name` + `industry`) to `scripts/.probe_cache/pdl_free.ndjson.gz`. If PDL's free download is unreachable, use the BigPicture free company dataset (same industry enum) and pass its path via `--dump`. If NEITHER is obtainable, STOP and report the blocker to the user (the probe cannot run without a dump) — do not fabricate results.

Verify the file exists and is readable:
Run: `ls -lh scripts/.probe_cache/ && .venv/bin/python -c "import gzip,json; f=gzip.open('scripts/.probe_cache/pdl_free.ndjson.gz','rt'); print(json.loads(next(f)).keys())"`
Expected: prints the record keys (confirm `name` + `industry` exist; if the fields are named differently, note it — the join defaults may need `--name-field`/`--industry-field`, or adjust `record_industry` defaults in a follow-up).

- [ ] **Step 3: STRESS GATE — sample run, watch memory**

Run: `.venv/bin/python scripts/probe_pdl_sectors.py --dump scripts/.probe_cache/pdl_free.ndjson.gz --sample 200000`
Expected: `[stress] sample=200000 … peakRSS=…MB …` with peak RSS well under ~1.5 GB. If RSS is high or it errors, STOP and investigate before the full run.

- [ ] **Step 4: Full run**

Run: `.venv/bin/python scripts/probe_pdl_sectors.py --dump scripts/.probe_cache/pdl_free.ndjson.gz`
Expected: `[join]` + metrics + `VERDICT:` line. Capture the full output.

- [ ] **Step 5: (Optional) parallel sanity**

Run: `CI=1 .venv/bin/python scripts/probe_pdl_sectors.py --dump scripts/.probe_cache/pdl_free.ndjson.gz --sample 500000`
Expected: same match logic under multiple workers; peak RSS still bounded. Confirms the pool path is memory-safe.

---

## Task 7: Record the verdict + go/no-go

**Files:**
- Create: `docs/superpowers/artifacts/2026-07-07-stage2-pdl-probe.md`
- Modify: `docs/extraction-baseline.md`

- [ ] **Step 1: Write the probe artifact**

Record in `docs/superpowers/artifacts/2026-07-07-stage2-pdl-probe.md`: dataset + license, dump size/rows, workers + peak-RSS + wall-time, gold accuracy-when-covered, gold coverage, registry current vs projected coverage, net-new company count, collision count, and the GO/NO-GO verdict against the bar (≥35% projected coverage AND ≥72.4% accuracy). Note the slug-vs-display normalization caveat (registry coverage is conservative).

- [ ] **Step 2: Append a Stage-2 note to the baseline doc**

Add a `### Sector — Stage-2 PDL name-join probe (2026-07-07)` subsection to `docs/extraction-baseline.md` with the headline numbers and the decision:
- **GO** → next: full Stage-2 pipeline (download PDL+BigPicture, wire crosswalk into `merge_sectors`, add a `pdl` source, re-benchmark, ratchet the gate) gets its own spec+plan.
- **NO-GO** → pivot: automate/refresh the existing edgar/wikidata/slug pipeline, lift Wikidata's 58% accuracy, and do job-weighted brand curation (the memory's proven lever). Record why.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/artifacts/2026-07-07-stage2-pdl-probe.md docs/extraction-baseline.md
git commit -m "docs(stage2): record PDL name-join probe verdict + go/no-go

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** (checked against `2026-07-07-stage2-pdl-namejoin-probe-design.md`):
- Unit 1 acquisition (scratch cache, availability/fail-fast, BigPicture fallback, `--dump`) → Task 5 (`resolve_dump`, `open_dump`, `PDL_INFO`) + Task 6 Step 2. ✔
- Unit 2 parallel memory-bounded name-join, env-gated workers, collision tie-break → Task 3. ✔
- Unit 3 crosswalk → Task 1. ✔
- Unit 4 measurement (gold accuracy, net-new, projected coverage, sanity) + verdict → Task 4. ✔
- Concurrency env-gate `ERGON_PROBE_WORKERS` → Task 3 `_workers()`. ✔
- Stress gates (`--sample`, synthetic 1M-ish memory test, fail-fast/peak-RSS) → Task 3 `test_run_join_memory_bounded_on_large_stream`, Task 5 `--sample` + `_peak_rss_mb`, Task 6 Step 3. ✔
- No new dep / stdlib-only / no runtime wiring → Global Constraints; only imports from `src` are `normalize_company`, `SeedRegistry`. ✔
- Slug-vs-display caveat → Key Facts + Task 7 Step 1. ✔
- Go/no-go bar (0.35 / 0.724) → Task 4 `verdict` defaults + Task 6/7. ✔

**2. Placeholder scan:** No TBD/TODO. Every code step has complete code; every run step has an exact command + expected output. The crosswalk is a real, extensible starter (≥60 entries, test-gated). The one intentional runtime unknown — the dump URL/field names — is handled by `--dump PATH`, `resolve_dump`'s fail-fast, and Task 6 Step 2's field check, not left as a silent gap. ✔

**3. Type consistency:** `TargetIndex` fields (`registry_norms`, `norm_to_keys`, `covered_keys`, `gold_norm_to_sector`) are identical across Tasks 2/4. `run_join → (matches: dict[norm→industry], collisions:int)` consumed correctly by `main` and `measure`. `record_industry → (norm, industry, completeness)` consumed by `join_chunk`. `measure`'s metric keys match Task 4's test and `main`'s printout. `verdict(metrics, min_coverage=0.35, min_accuracy=0.724)` consistent. ✔
