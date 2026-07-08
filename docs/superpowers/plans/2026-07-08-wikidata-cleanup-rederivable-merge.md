# Wikidata Cleanup + Re-Derivable Merge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `merge_sectors` re-derivable (only hand-curated entries locked; sources rebuilt each run) and purge the obvious Wikidata junk (porn-industry + short-slug entity collisions) from the shipped `sectors.json`, without regressing the sector benchmark.

**Architecture:** A pure offline filter (`scripts/clean_sector_wikidata.py`) rewrites the committed `sector_wikidata.json`, dropping low-confidence label-pass entries. `merge_sectors.py` extracts a pure `rebuild_table` that locks only the 1,453 sourceless hand-curated entries and re-derives all source layers from the committed `sector_*.json` files, so the wikidata purge (and any future source fix) actually takes effect. Idempotent; hand-curation never touched.

**Tech Stack:** Python ≥3.10, stdlib + json only. No new dependency. No concurrency (instant data-cleaning + dict rebuild).

## Global Constraints

- **Free · offline** — no Wikidata re-query; the committed `sector_wikidata.json` is the filter's input.
- **stdlib + json only; no new dependency.** No numpy/pandas.
- **Re-derivable merge:** lock ONLY hand-curated (entries with a truthy `sector` and NO `source` field — verified 1,453 of them). All source-tagged entries (edgar/wikidata/slug/pdl) are rebuilt from the source files/`slug_classify` each merge. Priority stays `["edgar", "wikidata", "slug", "pdl"]` (pdl last = gap-fill).
- **Idempotent:** running `merge_sectors --apply` twice produces zero further changes.
- **Acceptance gate:** `tests/test_sector_recall.py` must hold — accuracy-when-covered **≥ 0.68** (currently 73.4%), coverage **≥ 0.34** (ratcheted floor). Removing wrong labels should hold/raise accuracy; a small coverage dip from the purge is acceptable but must stay ≥ 0.34.
- **Auditable:** the cleaned `sector_wikidata.json` is committed (its diff shows exactly what was removed).
- **No `SectorExtractor` code change** — only data files (`sector_wikidata.json`, `sectors.json`) change.
- **ruff line-length 100, no semicolon one-liners (E701/E702); mypy `src/`-only.**

## Key Facts (verified)

- `scripts/sector_wikidata.json`: `{company_key: {"sector","source":"wikidata","wd_industry","wd_qid"}}`, 2,358 entries. Top junk: `pornography industry` (113 hits). `merge_sectors._load(name)` reads `scripts/<name>` and returns `{k: v["sector"] for … if v.get("sector")}` (drops `wd_industry`), so the cleaner must operate on the raw json directly, not via `_load`.
- Current `sectors.json` composition (13,624 sectored): **1,453 sourceless (hand-curated)** + 8,322 slug + 1,912 wikidata + 1,343 pdl + 594 edgar. Hand-curated sample: `1000heads, 100ms, 10xgenomics` (no `source` key).
- `merge_sectors.py` current `main` (post-PDL): builds `sources = {wikidata,edgar,naics,pdl}` + live `slug`; `priority = ["edgar","wikidata","slug","pdl"]`; already has the pure `apply_priority(seed, curated, sources, priority)`. `--apply` writes `json.dumps(sec, ensure_ascii=True, indent=1) + "\n"`.
- `apply_priority` (already in the file): for each non-`curated` seed key, first source in `priority` order with a value wins → `{"sector","domain","source"}`.
- `test_sector_recall.py`: `ACCURACY_GATE = 0.68`, `COVERAGE_GATE = 0.34`; tests `SectorExtractor` on the 699-row gold.
- pytest config has `pythonpath = ["."]` (so `scripts.*` import as namespace packages).

## File Structure

**Create:** `scripts/clean_sector_wikidata.py` (pure filter + CLI), `tests/test_clean_wikidata.py`.
**Modify:** `scripts/merge_sectors.py` (extract `rebuild_table`, lock only hand-curated), `tests/test_merge_sectors.py` (add rebuild test), `scripts/sector_wikidata.json` (cleaned), `src/ergon_tracker/registry/data/sectors.json` (re-merged), `docs/extraction-baseline.md` (record).

---

## Task 1: Wikidata post-filter (`clean_sector_wikidata.py`)

**Files:**
- Create: `scripts/clean_sector_wikidata.py`, `tests/test_clean_wikidata.py`

**Interfaces:**
- Produces:
  - `WD_JUNK_INDUSTRIES: frozenset[str]`, `SHORT_SLUG_MAX: int = 3`.
  - `clean(raw: dict) -> tuple[dict, dict]` — returns `(cleaned_raw, drop_counts)` where `cleaned_raw` keeps the FULL record for surviving keys and `drop_counts` is `{"junk_industry": int, "short_slug": int}`.
  - `main(argv) -> None` — reads/rewrites `scripts/sector_wikidata.json`; `--dry-run` previews without writing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_clean_wikidata.py
from __future__ import annotations

import pytest

cw = pytest.importorskip("scripts.clean_sector_wikidata")


def test_clean_drops_junk_and_short_slugs() -> None:
    raw = {
        "goodco": {"sector": "Biotech/Pharma", "source": "wikidata", "wd_industry": "biotechnology"},
        "harper": {"sector": "Media/Entertainment", "source": "wikidata", "wd_industry": "pornography industry"},
        "hud": {"sector": "Manufacturing/Industrial", "source": "wikidata", "wd_industry": "shipbuilding"},
        "cba": {"sector": "Banking/Finance", "source": "wikidata", "wd_industry": "banking"},  # 3-char slug
    }
    cleaned, drops = cw.clean(raw)
    assert set(cleaned) == {"goodco"}  # harper=junk, hud+cba=short-slug
    assert cleaned["goodco"]["wd_industry"] == "biotechnology"  # full record preserved
    assert drops == {"junk_industry": 1, "short_slug": 2}


def test_junk_industries_includes_pornography() -> None:
    assert "pornography industry" in cw.WD_JUNK_INDUSTRIES
    assert cw.SHORT_SLUG_MAX == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_clean_wikidata.py -v`
Expected: FAIL — module not importable.

- [ ] **Step 3: Implement `clean_sector_wikidata.py`**

```python
# scripts/clean_sector_wikidata.py
"""Purge low-confidence label-pass entries from scripts/sector_wikidata.json (offline).

The Wikidata harvest's domain pass (P856) is clean; its label pass matches short/generic company
slugs to unrelated entities (e.g. `harper`→"pornography industry", `hud`→"shipbuilding"). This drops
the obvious junk — blacklisted industries + very short slugs — and rewrites the committed json (an
auditable diff). It does NOT re-query Wikidata; the committed json is the input.

Usage:
  .venv/bin/python scripts/clean_sector_wikidata.py            # apply (rewrites the json)
  .venv/bin/python scripts/clean_sector_wikidata.py --dry-run  # preview counts only
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WD = ROOT / "scripts" / "sector_wikidata.json"

# Industries that are near-always spurious entity collisions for employers in our registry (no real
# employer here legitimately carries them). Conservative — extend only with clearly-junk industries.
WD_JUNK_INDUSTRIES: frozenset[str] = frozenset({"pornography industry"})

# Label-pass acronym collisions: slugs this short (<=3 chars) almost never match the right entity.
SHORT_SLUG_MAX: int = 3


def clean(raw: dict) -> tuple[dict, dict]:
    """Return (cleaned_raw, drop_counts). Keeps full records for survivors."""
    cleaned: dict = {}
    drops = {"junk_industry": 0, "short_slug": 0}
    for key, rec in raw.items():
        if rec.get("wd_industry") in WD_JUNK_INDUSTRIES:
            drops["junk_industry"] += 1
            continue
        if len(key) <= SHORT_SLUG_MAX:
            drops["short_slug"] += 1
            continue
        cleaned[key] = rec
    return cleaned, drops


def main(argv: list[str]) -> None:
    dry = "--dry-run" in argv
    raw = json.loads(WD.read_text())
    cleaned, drops = clean(raw)
    print(f"[clean-wd] {len(raw)} -> {len(cleaned)} (dropped junk_industry={drops['junk_industry']}, "
          f"short_slug={drops['short_slug']})")
    if dry:
        print("[clean-wd] dry-run — not written.")
        return
    WD.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n")
    print(f"[clean-wd] wrote {WD.name}")


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_clean_wikidata.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint + commit** (do NOT run it against the real json yet — that's Task 3)

```bash
.venv/bin/ruff check scripts/clean_sector_wikidata.py tests/test_clean_wikidata.py
.venv/bin/ruff format scripts/clean_sector_wikidata.py tests/test_clean_wikidata.py
git add scripts/clean_sector_wikidata.py tests/test_clean_wikidata.py
git commit -m "feat(sector): offline wikidata junk-purge filter (blacklist + short-slug)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Re-derivable merge (`rebuild_table`)

**Files:**
- Modify: `scripts/merge_sectors.py`
- Test: `tests/test_merge_sectors.py`

**Interfaces:**
- Consumes: `apply_priority` (already in the file).
- Produces: `rebuild_table(companies: dict, seed: dict, sources: dict, priority: list[str]) -> dict[str, dict]` — keeps entries with a truthy `sector` and NO `source` (hand-curated) as-is; re-derives all other keys via `apply_priority`; returns `{**hand, **filled}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_merge_sectors.py
def test_rebuild_table_locks_only_handcurated_and_rederives_sources() -> None:
    companies = {
        "hand1": {"sector": "Software/SaaS"},  # sourceless hand-curated → locked, preserved as-is
        "stalewd": {"sector": "Media/Entertainment", "source": "wikidata"},  # source-tagged, absent from sources → dropped
        "edg1": {"sector": "Insurance", "source": "edgar"},  # source-tagged, still in sources → re-derived
    }
    seed = {
        "hand1": {"domain": None}, "stalewd": {"domain": None},
        "edg1": {"domain": None}, "new": {"domain": None},
    }
    sources = {"edgar": {"edg1": "Insurance", "new": "Banking/Finance"}, "wikidata": {}, "slug": {}, "pdl": {}}
    out = ms.rebuild_table(companies, seed, sources, ["edgar", "wikidata", "slug", "pdl"])
    assert out["hand1"] == {"sector": "Software/SaaS"}  # hand-curated preserved verbatim
    assert "stalewd" not in out  # stale wikidata dropped (not hand-curated, not re-produced)
    assert out["edg1"] == {"sector": "Insurance", "domain": None, "source": "edgar"}  # re-derived
    assert out["new"] == {"sector": "Banking/Finance", "domain": None, "source": "edgar"}  # new gap-fill
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_merge_sectors.py -k rebuild_table -v`
Expected: FAIL — `rebuild_table` not defined.

- [ ] **Step 3: Add `rebuild_table` and rewire `main`**

Add the pure function above `main`:

```python
def rebuild_table(
    companies: dict, seed: dict, sources: dict, priority: list[str]
) -> dict[str, dict]:
    """Rebuild the sectors table: lock ONLY hand-curated (sourceless) entries and re-derive every
    source-tagged entry fresh from the sources, so a source correction (e.g. a purged wikidata entry)
    actually takes effect. Idempotent given the same inputs."""
    hand = {k: v for k, v in companies.items() if v.get("sector") and not v.get("source")}
    curated = {k: v["sector"] for k, v in hand.items()}
    filled = apply_priority(seed, curated, sources, priority)
    return {**hand, **filled}
```

In `main`, change the `curated` used by the accuracy report to hand-curated-only, and replace the
`apply_priority`/`update`/`added` block with a `rebuild_table` call:

```python
    curated = {k: v["sector"] for k, v in sec["companies"].items() if v.get("sector") and not v.get("source")}
```

(the accuracy-vs-curated report loop is unchanged but now compares against hand-curation only — a more
honest precision proxy). Then:

```python
    priority = ["edgar", "wikidata", "slug", "pdl"]
    sec["companies"] = rebuild_table(sec["companies"], seed, sources, priority)
    added = dict.fromkeys(priority, 0)
    for v in sec["companies"].values():
        s = v.get("source")
        if s in added:
            added[s] += 1
```

(the `total`/coverage print lines and the `if apply:` write block stay as they are.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_merge_sectors.py -v`
Expected: PASS (both the existing gap-fill test and the new rebuild test).

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check scripts/merge_sectors.py tests/test_merge_sectors.py
.venv/bin/ruff format scripts/merge_sectors.py tests/test_merge_sectors.py
git add scripts/merge_sectors.py tests/test_merge_sectors.py
git commit -m "feat(sector): re-derivable merge (lock only hand-curated; rebuild sources each run)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Clean + re-merge + measure + record

**Files:** rewrites `scripts/sector_wikidata.json`, `src/ergon_tracker/registry/data/sectors.json`; modifies `docs/extraction-baseline.md`.

- [ ] **Step 1: Unit sweep (green before touching data)**

Run: `.venv/bin/pytest tests/test_clean_wikidata.py tests/test_merge_sectors.py tests/test_sector_pdl.py -q`
Expected: all PASS.

- [ ] **Step 2: Record BEFORE composition**

Run:
```bash
.venv/bin/python -c "import json,collections; d=json.load(open('src/ergon_tracker/registry/data/sectors.json'))['companies']; print(collections.Counter(v.get('source','<hand>') for v in d.values() if v.get('sector')))"
```
Record the before counts (expect `<hand> 1453, slug 8322, wikidata 1912, pdl 1343, edgar 594`).

- [ ] **Step 3: Inspect what the cleaner will drop, then apply it**

```bash
.venv/bin/python scripts/clean_sector_wikidata.py --dry-run   # preview counts
```
If the dry-run drops far more than expected (junk_industry ~113, short_slug a few hundred at most), inspect the `wd_industry` distribution of dropped entries before proceeding; if a clearly-junk industry beyond `pornography industry` dominates the drops, add it to `WD_JUNK_INDUSTRIES` (commit that one-line change separately) — otherwise keep the blacklist minimal. Then apply:
```bash
.venv/bin/python scripts/clean_sector_wikidata.py            # rewrites sector_wikidata.json
```
Expected: `[clean-wd] 2358 -> N (dropped junk_industry=…, short_slug=…)`.

- [ ] **Step 4: Re-merge — dry-run (before/after), then apply**

```bash
.venv/bin/python scripts/merge_sectors.py            # shows added-by-source + coverage
.venv/bin/python scripts/merge_sectors.py --apply    # rebuilds sectors.json
```
Record the AFTER composition (rerun the Step-2 one-liner). Expect: hand-curated 1,453 unchanged; wikidata down by the purge count; edgar/slug/pdl ~stable (a few may shift via priority reassignment — that's expected).

- [ ] **Step 5: Idempotence check (the refactor's real test)**

```bash
.venv/bin/python scripts/merge_sectors.py --apply
git diff --stat src/ergon_tracker/registry/data/sectors.json
```
Expected: the second `--apply` produces **no change** to sectors.json (empty `git diff`). If it changes, the rebuild is non-deterministic — STOP and investigate before committing.

- [ ] **Step 6: Acceptance gate — gold benchmark**

Run: `.venv/bin/pytest tests/test_sector_recall.py -v -s`
Expected: accuracy-when-covered ≥ 0.68 (record it; expect ≥ 73.4%) and coverage ≥ 0.34. **Record both.** If accuracy dropped below 0.68 OR coverage below 0.34, STOP and investigate (the purge or a priority reassignment regressed the table).

- [ ] **Step 7: Record + commit**

Add a `### Sector — Wikidata cleanup + re-derivable merge (2026-07-08)` subsection to `docs/extraction-baseline.md`: the wikidata drop count (junk_industry + short_slug), the before/after table composition, the gold accuracy/coverage after re-merge, that the merge is now re-derivable (only hand-curated locked) + idempotent, and that hand-curation was untouched.

```bash
git add scripts/sector_wikidata.json src/ergon_tracker/registry/data/sectors.json docs/extraction-baseline.md
git commit -m "chore(sector): purge wikidata junk + rebuild sectors.json (re-derivable merge)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** (vs `2026-07-08-wikidata-cleanup-rederivable-merge-design.md`):
- Unit 1 re-derivable merge (lock only hand-curated, rebuild) → Task 2 (`rebuild_table`). ✔
- Unit 2 wikidata post-filter (blacklist + short-slug) → Task 1. ✔
- Idempotence → Task 3 Step 5. ✔
- Acceptance gate (accuracy ≥ 0.68, coverage ≥ 0.34) → Task 3 Step 6. ✔
- Before/after composition measured → Task 3 Steps 2/4. ✔
- Auditable cleaned json committed → Task 1 writes it, Task 3 commits it. ✔
- No `SectorExtractor` change; no new dep; offline → Global Constraints + file structure. ✔
- Record in baseline doc → Task 3 Step 7. ✔

**2. Placeholder scan:** No TBD/TODO. The only data-driven decision (extending `WD_JUNK_INDUSTRIES`) has an explicit inspect-then-decide rule in Task 3 Step 3 with a concrete starting value; not a placeholder. All code steps carry complete code.

**3. Type consistency:** `clean(raw) -> (cleaned, drops)` with `drops` keys `junk_industry`/`short_slug` consistent across Task 1 code + test. `rebuild_table(companies, seed, sources, priority)` signature matches its test and the `main` call. `apply_priority` reused unchanged. `WD_JUNK_INDUSTRIES`/`SHORT_SLUG_MAX` names consistent. Priority `["edgar","wikidata","slug","pdl"]` consistent throughout. ✔
