"""Benchmark-driven gate for the company-sector (industry) classifier.

700 DISTINCT companies (``scripts/build_sector_corpus.py``), blind-labeled for industry from the
company name/domain. ``SectorExtractor`` classifies via a curated company->sector gazetteer +
name-keyword rules; it returns None ("unknown") rather than guess. So two numbers matter:
  * **accuracy-when-covered** — when the extractor returns a sector, how often is it right?
  * **coverage** — how often does it return a sector at all? Inherently limited: opaque startup names
    ("dv01", "unit", "flox") carry no industry signal, so a large unknown share is expected and
    correct (a wrong guess is worse than None). Gated as a low floor to catch regressions only.

Record format: ``{"company": ..., "company_key": ..., "domain": ..., "sector": "Fintech"|null, ...}``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.sector import SectorExtractor

CORPUS_PATH = Path(__file__).parent / "fixtures" / "sector_corpus.jsonl"

ACCURACY_GATE = 0.68  # accuracy when the extractor returns a sector (measured 0.724)
COVERAGE_GATE = 0.22  # low floor — coverage is name-limited by design (measured 0.267)

_EX = SectorExtractor()


def _load() -> list[dict]:
    return [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]


def _predict(r: dict) -> str | None:
    return _EX.extract(
        ExtractInput(title="x", company=r["company"], company_key=r["company_key"], company_domain=r.get("domain"))
    )


def test_corpus_is_substantial() -> None:
    records = _load()
    assert len(records) >= 400, f"only {len(records)} companies"
    assert sum(1 for r in records if r["sector"]) >= 300, "too few gold-known companies"


def test_accuracy_when_covered() -> None:
    records = _load()
    both = [r for r in records if r["sector"] and _predict(r)]
    hits = sum(1 for r in both if _predict(r) == r["sector"])
    acc = hits / len(both) if both else 1.0
    print(f"\nsector accuracy-when-covered: {hits}/{len(both)} = {acc:.1%}")
    if acc < ACCURACY_GATE:
        miss = [(r["company"], r["sector"], _predict(r)) for r in both if _predict(r) != r["sector"]][:10]
        detail = "\n".join(f"  {c!r} want={w} got={g}" for c, w, g in miss)
        pytest.fail(f"accuracy {acc:.1%} below gate {ACCURACY_GATE:.0%}.\n{detail}")


def test_coverage_floor() -> None:
    records = _load()
    covered = sum(1 for r in records if _predict(r))
    cov = covered / len(records)
    print(f"sector coverage: {covered}/{len(records)} = {cov:.1%}")
    if cov < COVERAGE_GATE:
        pytest.fail(f"coverage {cov:.1%} regressed below floor {COVERAGE_GATE:.0%}")
