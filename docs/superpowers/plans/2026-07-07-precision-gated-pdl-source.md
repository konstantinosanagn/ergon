# Precision-Gated PDL Sector Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a curated, high-precision PDL sector source that gap-fills currently-`unknown` companies in `sectors.json` (~+1,460 companies at ~95%+ accuracy) without regressing the 72.4% benchmark.

**Architecture:** A thin builder `scripts/sector_pdl.py` reuses the already-reviewed, stress-tested name-join from `probe_pdl_sectors.py`, keeps only a curated allow-list of LinkedIn industries, and writes committed `scripts/sector_pdl.json`. `merge_sectors.py` gains `pdl` as the last priority source (gap-fill), producing an updated `sectors.json`. The `test_sector_recall.py` gate is the acceptance check.

**Tech Stack:** Python ≥3.10, stdlib + json (reuses `probe_pdl_sectors`), pytest. No new dependency.

## Global Constraints

- **Free · offline · CPU-only · laptop-safe.** The heavy 7M-row join is the reused Stage-2 engine (env-gated `ProcessPoolExecutor` via `ERGON_PROBE_WORKERS`, bounded memory, deterministic). The builder adds only an O(matches) allow-list filter + key-map — **no new hot loop, no duplicated join/concurrency code**.
- **No new dependency.** stdlib + json; `sector_pdl.py` imports functions from `probe_pdl_sectors.py`.
- **Gap-fill only.** `pdl` is appended LAST in `merge_sectors` priority; it never overrides an existing label (curated/edgar/wikidata/slug win). Purely additive; cannot regress current accuracy.
- **Acceptance gate:** `tests/test_sector_recall.py` accuracy-when-covered must stay **≥ 0.68** (measured 72.4%). Coverage floor is **ratcheted up** to lock the gain.
- **Auditable:** committed `scripts/sector_pdl.json` carries `{sector, source:"pdl", industry}` per company.
- **Stress-test before the full run:** `sector_pdl.py --sample N` logs peak-RSS + wall-time first.
- **ruff line-length 100, no semicolon one-liners (E701/E702); mypy is `src/`-only** (scripts untyped, but `src` must stay green; only `sectors.json` data changes under `src`).
- **Only `sectors.json` grows in `src/`** — no `SectorExtractor` code change.

## Key Facts (verified against the codebase)

- **`merge_sectors.py`** (`scripts/merge_sectors.py`): `_load(name)` → `{key: sector}` from `scripts/<name>` where `v.get("sector")`. `curated = {k: v["sector"] for … if v.get("sector")}` — **every currently-sectored entry counts as curated**. Priority loop skips curated keys, else takes the first source in `priority = ["edgar", "wikidata", "slug"]` that has the key, writing `{"sector": val, "domain": seed[key].get("domain"), "source": src}`. `--apply` writes `json.dumps(sec, ensure_ascii=True, indent=1) + "\n"`. So appending `"pdl"` last = gap-fill vs the existing table.
- **Source file shape** (`sector_edgar.json`): `{company_key: {"sector", "source", "sic"}}`, `json.dump(..., indent=2, sort_keys=True)`. `sector_*.json` are **committed** (not gitignored). New `sector_pdl.json` follows suit.
- **`probe_pdl_sectors.py` reusable API:** `load_inputs() -> (seed, sectors, gold)`; `build_target_index(seed, sectors, gold) -> TargetIndex`; `TargetIndex` fields `registry_norms:set`, `norm_to_keys:dict[str,list[str]]`, `covered_keys:set`, `gold_norm_to_sector:dict[str,str]`; `run_join(line_iter, targets, *, workers, chunk_size=20000) -> (matches:dict[norm→industry], collisions:int)`; `open_dump(path)` ctx-mgr; `resolve_dump(args) -> Path`; `_workers() -> int`; `_peak_rss_mb() -> float`; `norm(name) -> str`. Script-import idiom (from `merge_sectors.py`): `sys.path.insert(0, str(ROOT / "scripts"))` then `import <module>`.
- **`test_sector_recall.py`**: `ACCURACY_GATE = 0.68` (line 26), `COVERAGE_GATE = 0.22` (line 27); tests `SectorExtractor.extract` on the 699-row gold corpus (reads the merged `sectors.json`).
- **Dump already cached** at `scripts/.probe_cache/pdl_free.ndjson.gz` (132 MB, from the Stage-2 probe).
- pytest config already has `pythonpath = ["."]` so `scripts.sector_pdl` imports as a namespace package.

## File Structure

**Create:**
- `scripts/sector_pdl.py` — allow-list constant + pure builder functions + `main` (reuses `probe_pdl_sectors`).
- `scripts/sector_pdl.json` — committed output (~1,000–1,500 entries).
- `tests/test_sector_pdl.py` — unit tests (synthetic; no network).

**Modify:**
- `scripts/merge_sectors.py` — extract a pure `apply_priority(...)`, add `pdl` source + priority entry.
- `tests/test_merge_sectors.py` — NEW, tests `apply_priority` gap-fill.
- `src/ergon_tracker/registry/data/sectors.json` — the merged result (shipped win).
- `tests/test_sector_recall.py` — ratchet `COVERAGE_GATE`.
- `docs/extraction-baseline.md` — record the new coverage/accuracy.

---

## Task 1: `sector_pdl.py` builder (allow-list + pure functions + main)

**Files:**
- Create: `scripts/sector_pdl.py`, `tests/test_sector_pdl.py`

**Interfaces:**
- Produces:
  - `PDL_ALLOWLIST: dict[str, str]` (trusted LinkedIn industry → 27-label).
  - `build_pdl_map(matches: dict[str, str], idx) -> dict[str, dict]` — `{company_key: {"sector","source":"pdl","industry"}}` for matched norms whose industry ∈ allow-list, expanded over `idx.norm_to_keys`.
  - `accuracy_on_gold(matches, idx) -> tuple[int, int]` — `(correct, total)` of allow-list predictions vs `idx.gold_norm_to_sector`.
  - `main(argv) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sector_pdl.py
from __future__ import annotations

import pytest

probe = pytest.importorskip("scripts.probe_pdl_sectors")
sp = pytest.importorskip("scripts.sector_pdl")


def test_allowlist_values_are_valid_and_excludes_coarse() -> None:
    valid = {
        "AI/ML", "Aerospace/Defense", "Automotive/Mobility", "Banking/Finance", "Biotech/Pharma",
        "Consulting/Services", "Consumer/Lifestyle", "Crypto/Web3", "Cybersecurity",
        "E-commerce/Retail", "Education", "Energy/Climate", "Fintech", "Food/Beverage", "Gaming",
        "Government/Public", "Healthcare", "Insurance", "Logistics/SupplyChain",
        "Manufacturing/Industrial", "Media/Entertainment", "Other", "RealEstate/PropTech",
        "Semiconductors/Hardware", "Software/SaaS", "Telecom", "Travel/Hospitality",
    }
    assert set(sp.PDL_ALLOWLIST.values()) <= valid
    # coarse/ambiguous buckets must NOT be trusted
    for bad in ("internet", "information technology and services", "financial services",
                "marketing and advertising", "consumer goods", "telecommunications"):
        assert bad not in sp.PDL_ALLOWLIST


def test_build_pdl_map_gates_allowlist_and_maps_keys() -> None:
    idx = probe.TargetIndex(norm_to_keys={"acme": ["acme"], "globex": ["globex", "globex2"]})
    matches = {"acme": "banking", "globex": "internet"}  # banking in-list, internet excluded
    out = sp.build_pdl_map(matches, idx)
    assert out == {"acme": {"sector": "Banking/Finance", "source": "pdl", "industry": "banking"}}


def test_build_pdl_map_expands_multiple_keys() -> None:
    idx = probe.TargetIndex(norm_to_keys={"acme": ["acme", "acmeinc"]})
    out = sp.build_pdl_map({"acme": "insurance"}, idx)
    assert set(out) == {"acme", "acmeinc"}
    assert out["acmeinc"]["sector"] == "Insurance"


def test_accuracy_on_gold() -> None:
    idx = probe.TargetIndex(gold_norm_to_sector={"acme": "Banking/Finance", "globex": "Insurance"})
    # acme→banking correct; globex→banking wrong (maps Banking/Finance != Insurance)
    assert sp.accuracy_on_gold({"acme": "banking", "globex": "banking"}, idx) == (1, 2)
    # out-of-list industries don't count toward total
    assert sp.accuracy_on_gold({"acme": "internet"}, idx) == (0, 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_sector_pdl.py -v`
Expected: FAIL — `scripts.sector_pdl` not importable.

- [ ] **Step 3: Implement `scripts/sector_pdl.py`**

```python
# scripts/sector_pdl.py
"""Build a precision-gated company→sector map from the PDL Free dataset (offline).

Reuses the Stage-2 name-join (scripts/probe_pdl_sectors.py) and keeps ONLY a curated allow-list of
high-precision LinkedIn industries, so the output is safe to gap-fill into the authoritative
sectors.json (via scripts/merge_sectors.py). Ships nothing itself; writes scripts/sector_pdl.json.

Usage:
  .venv/bin/python scripts/sector_pdl.py --dump scripts/.probe_cache/pdl_free.ndjson.gz
  .venv/bin/python scripts/sector_pdl.py --dump <path> --sample 200000   # stress gate first
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import probe_pdl_sectors as probe  # noqa: E402

OUT = ROOT / "scripts" / "sector_pdl.json"

# Curated allow-list: each LinkedIn industry maps 1:1 to a label AND scored high in the Stage-2 probe.
# Coarse/ambiguous buckets (internet, IT services, financial services, marketing, consumer goods,
# entertainment, real estate, telecommunications) are deliberately excluded — that is the gate.
PDL_ALLOWLIST: dict[str, str] = {
    "biotechnology": "Biotech/Pharma",
    "pharmaceuticals": "Biotech/Pharma",
    "banking": "Banking/Finance",
    "insurance": "Insurance",
    "medical devices": "Healthcare",
    "hospital & health care": "Healthcare",
    "oil & energy": "Energy/Climate",
    "utilities": "Energy/Climate",
    "chemicals": "Manufacturing/Industrial",
    "mining & metals": "Manufacturing/Industrial",
    "mechanical or industrial engineering": "Manufacturing/Industrial",
    "semiconductors": "Semiconductors/Hardware",
    "higher education": "Education",
}


def build_pdl_map(matches: dict[str, str], idx) -> dict[str, dict]:
    """Matched norm→industry → {registry_key: {sector, source, industry}} for allow-list industries."""
    out: dict[str, dict] = {}
    for n, industry in matches.items():
        sector = PDL_ALLOWLIST.get(industry)
        if not sector:
            continue
        for key in idx.norm_to_keys.get(n, []):
            out[key] = {"sector": sector, "source": "pdl", "industry": industry}
    return out


def accuracy_on_gold(matches: dict[str, str], idx) -> tuple[int, int]:
    """(correct, total) of allow-list predictions vs gold, on the gold∩matches∩allow-list overlap."""
    correct = total = 0
    for n, gold_sector in idx.gold_norm_to_sector.items():
        sector = PDL_ALLOWLIST.get(matches.get(n, ""))
        if not sector:
            continue
        total += 1
        correct += sector == gold_sector
    return correct, total


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--chunk-size", type=int, default=20000)
    args = ap.parse_args(argv)

    dump = probe.resolve_dump(args)
    seed, sectors, gold = probe.load_inputs()
    idx = probe.build_target_index(seed, sectors, gold)
    targets = frozenset(idx.registry_norms | set(idx.gold_norm_to_sector))
    workers = probe._workers()
    print(f"[pdl] registry={len(seed)} targets={len(targets)} workers={workers} dump={dump.name}")

    t0 = time.monotonic()
    with probe.open_dump(dump) as fh:
        it = itertools.islice(fh, args.sample) if args.sample else fh
        matches, collisions = probe.run_join(it, targets, workers=workers, chunk_size=args.chunk_size)
    wall = time.monotonic() - t0

    if args.sample:
        print(f"[stress] sample={args.sample} matches={len(matches)} "
              f"peakRSS={probe._peak_rss_mb():.0f}MB wall={wall:.1f}s — full run is safe.")
        return

    pdl_map = build_pdl_map(matches, idx)
    current = {k for k, v in sectors.items() if v.get("sector")}
    net_new = sum(1 for k in pdl_map if k not in current)
    correct, total = accuracy_on_gold(matches, idx)
    OUT.write_text(json.dumps(dict(sorted(pdl_map.items())), ensure_ascii=True, indent=1) + "\n")
    acc = f"{correct}/{total} = {correct / total:.1%}" if total else "n/a"
    print(f"[pdl] wrote {len(pdl_map)} entries → {OUT.name}; net-new vs current {net_new}; "
          f"gold-acc {acc}; collisions {collisions}; peakRSS={probe._peak_rss_mb():.0f}MB wall={wall:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_sector_pdl.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check scripts/sector_pdl.py tests/test_sector_pdl.py
.venv/bin/ruff format scripts/sector_pdl.py tests/test_sector_pdl.py
git add scripts/sector_pdl.py tests/test_sector_pdl.py
git commit -m "feat(sector): precision-gated PDL source builder (curated allow-list, reuses probe join)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `merge_sectors.py` gap-fill integration

**Files:**
- Modify: `scripts/merge_sectors.py`
- Create: `tests/test_merge_sectors.py`

**Interfaces:**
- Produces: `apply_priority(seed: dict, curated: dict, sources: dict, priority: list[str]) -> dict[str, dict]` — pure; for each non-curated seed key, the first source (in `priority` order) with a value wins, returning `{key: {"sector","domain","source"}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_merge_sectors.py
from __future__ import annotations

import pytest

ms = pytest.importorskip("scripts.merge_sectors")


def test_apply_priority_gapfill_and_no_override() -> None:
    seed = {"a": {"domain": None}, "b": {"domain": "b.com"}, "c": {"domain": None}}
    curated = {"a": "Software/SaaS"}  # 'a' already known → untouched
    sources = {
        "edgar": {"b": "Banking/Finance"},
        "wikidata": {},
        "slug": {},
        "pdl": {"b": "Insurance", "c": "Healthcare"},
    }
    out = ms.apply_priority(seed, curated, sources, ["edgar", "wikidata", "slug", "pdl"])
    assert "a" not in out  # curated skipped entirely
    assert out["b"] == {"sector": "Banking/Finance", "domain": "b.com", "source": "edgar"}  # edgar wins; pdl does NOT override
    assert out["c"] == {"sector": "Healthcare", "domain": None, "source": "pdl"}  # pdl gap-fills unknown c
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_merge_sectors.py -v`
Expected: FAIL — `apply_priority` not defined.

- [ ] **Step 3: Refactor `merge_sectors.py` — extract `apply_priority`, add pdl**

Add the pure function (place it above `main`):

```python
def apply_priority(seed: dict, curated: dict, sources: dict, priority: list[str]) -> dict[str, dict]:
    """Gap-fill non-curated keys: first source in ``priority`` order that has the key wins.
    ``pdl`` is last, so it only fills keys no higher source covered — it never overrides."""
    out: dict[str, dict] = {}
    for key in seed:
        if key in curated:
            continue
        for src in priority:
            val = sources[src].get(key)
            if val:
                out[key] = {"sector": val, "domain": seed[key].get("domain"), "source": src}
                break
    return out
```

In `main`, add pdl to `sources` (after the edgar/naics/wikidata entries):

```python
        "pdl": _load("sector_pdl.json"),
```

Replace the inline priority-merge loop with a call to `apply_priority` and add `pdl` last:

```python
    priority = ["edgar", "wikidata", "slug", "pdl"]
    filled = apply_priority(seed, curated, sources, priority)
    sec["companies"].update(filled)
    added = {s: 0 for s in priority}
    for rec in filled.values():
        added[rec["source"]] += 1
```

(Leave the accuracy-vs-curated reporting loop unchanged — it will now also print a `pdl` row since `pdl` is in `sources`.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_merge_sectors.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check scripts/merge_sectors.py tests/test_merge_sectors.py
.venv/bin/ruff format scripts/merge_sectors.py tests/test_merge_sectors.py
git add scripts/merge_sectors.py tests/test_merge_sectors.py
git commit -m "feat(sector): merge_sectors gap-fill via pure apply_priority + pdl source (last priority)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Build + stress gate + merge into sectors.json

**Files:** writes `scripts/sector_pdl.json`, updates `src/ergon_tracker/registry/data/sectors.json`.

- [ ] **Step 1: Full unit sweep (green before any heavy run)**

Run: `.venv/bin/pytest tests/test_sector_pdl.py tests/test_merge_sectors.py tests/test_probe_pdl_sectors.py -q`
Expected: all PASS.

- [ ] **Step 2: Confirm the dump is present**

Run: `ls -lh scripts/.probe_cache/pdl_free.ndjson.gz`
Expected: the 132 MB file exists (cached from the Stage-2 probe). If missing, re-acquire per `docs/superpowers/artifacts/2026-07-07-stage2-pdl-probe.md` (kagglehub download → CSV→ndjson.gz convert).

- [ ] **Step 3: STRESS GATE — sample run, watch memory**

Run: `.venv/bin/python scripts/sector_pdl.py --dump scripts/.probe_cache/pdl_free.ndjson.gz --sample 200000`
Expected: `[stress] … peakRSS=…MB …` well under ~1.5 GB. If high/errors, STOP.

- [ ] **Step 4: Build `sector_pdl.json`**

Run: `.venv/bin/python scripts/sector_pdl.py --dump scripts/.probe_cache/pdl_free.ndjson.gz`
Expected: `[pdl] wrote N entries … net-new … gold-acc …` — record N, net-new, and gold-acc (gold-acc should be ~95%+). `scripts/sector_pdl.json` created.

- [ ] **Step 5: Merge — dry-run then apply**

```bash
.venv/bin/python scripts/merge_sectors.py            # dry-run: shows the pdl row + added-by-source
.venv/bin/python scripts/merge_sectors.py --apply    # writes sectors.json
```
Expected: `added by source: {…, 'pdl': ~1000-1500}`; `sectors coverage: <new>/58078 = <higher>%`. Record the pdl added count.

- [ ] **Step 6: Measure the new gold accuracy + coverage (no ratchet yet)**

Run: `.venv/bin/pytest tests/test_sector_recall.py -v -s`
Expected: prints `sector accuracy-when-covered: …` (must be ≥ 68%, expect ≥ 72.4%) and `sector coverage: …` (higher than 26.7%). **Record the printed coverage %** — Task 4 ratchets the gate to just below it. If accuracy dropped below 68%, STOP and investigate (a gap-fill should not lower accuracy — likely an allow-list entry to drop).

---

## Task 4: Ratchet the gate + record + commit artifacts

**Files:**
- Modify: `tests/test_sector_recall.py`, `docs/extraction-baseline.md`
- Commit: `scripts/sector_pdl.json`, `src/ergon_tracker/registry/data/sectors.json`

- [ ] **Step 1: Ratchet `COVERAGE_GATE`**

Using the coverage printed in Task 3 Step 6, raise the floor in `tests/test_sector_recall.py:27` to ~0.02 below the measured value (round down to 2 decimals). Example if measured 0.30:

```python
COVERAGE_GATE = 0.28  # ratcheted after pdl gap-fill (measured <value>)
```

Update the inline comment with the actual measured value. Do NOT raise it above the measured coverage.

- [ ] **Step 2: Verify the gate passes**

Run: `.venv/bin/pytest tests/test_sector_recall.py -v`
Expected: PASS (accuracy ≥ 0.68; coverage ≥ new floor).

- [ ] **Step 3: Record the result in the baseline doc**

Add a `### Sector — precision-gated PDL source (2026-07-07)` subsection to `docs/extraction-baseline.md`: pdl added count, net-new companies, gold accuracy on the allow-list, the new overall sector coverage-when-covered/coverage numbers, the ratcheted `COVERAGE_GATE`, and that pdl is a gated gap-fill source (never overrides).

- [ ] **Step 4: Commit the shipped data + docs**

```bash
git add scripts/sector_pdl.json src/ergon_tracker/registry/data/sectors.json tests/test_sector_recall.py docs/extraction-baseline.md
git commit -m "feat(sector): ship precision-gated PDL gap-fill into sectors.json + ratchet coverage gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** (vs `2026-07-07-precision-gated-pdl-source-design.md`):
- Builder reusing the probe join + curated allow-list + `sector_pdl.json` → Task 1. ✔
- `merge_sectors` gap-fill (pdl last) → Task 2. ✔
- Acceptance gate (accuracy ≥ 0.68) + coverage ratchet → Task 3 Step 6, Task 4. ✔
- Concurrency reuse / no new hot loop / no new dep → Task 1 (imports `probe`), Global Constraints. ✔
- Stress gate before full run → Task 1 `--sample`, Task 3 Step 3. ✔
- Auditable committed `sector_pdl.json` → Task 1/3. ✔
- Only `sectors.json` changes in `src/`, no `SectorExtractor` edit → file structure. ✔
- Record in baseline doc → Task 4 Step 3. ✔

**2. Placeholder scan:** No TBD/TODO. The one runtime-determined value (`COVERAGE_GATE`) has an exact computation rule (measure in Task 3 Step 6 → set ~0.02 below in Task 4 Step 1), not a placeholder. All code steps carry complete code.

**3. Type consistency:** `build_pdl_map(matches, idx)`/`accuracy_on_gold(matches, idx)` take the probe's `TargetIndex` (fields `norm_to_keys`, `gold_norm_to_sector`) — consistent with Task 1 tests. `apply_priority(seed, curated, sources, priority)` signature matches its test and `main` call site. `sector_pdl.json` record `{sector, source:"pdl", industry}` is what `merge_sectors._load` reads (only needs `sector`). Priority `["edgar","wikidata","slug","pdl"]` consistent across Task 2 code + test. ✔
