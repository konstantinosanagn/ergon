"""Gold-set regression tests: extractor accuracy must not drop below locked baselines.

Thresholds are set a margin below the 2026-06-16 baseline (see docs/extraction-baseline.md).
Phase 2 work should RAISE these as fields improve. country/yoe are intentionally low — they
are the known-weak fields targeted next.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from eval_extraction import evaluate  # noqa: E402

GOLD = ROOT / "tests" / "data" / "gold.jsonl"


@pytest.fixture(scope="module")
def report() -> dict:
    rows = [json.loads(line) for line in GOLD.read_text().split("\n") if line.strip()]
    return evaluate(rows)


def test_gold_set_is_present_and_sized(report: dict) -> None:
    assert report["n"] >= 150


# Thresholds locked to the 500-row consensus baseline (runs/2026-06-16-gold-500/), a margin
# below measured. The earlier 162-row numbers were optimistic on level/sector.
def test_level_quality(report: dict) -> None:
    # Error-pattern fixes (IC-vs-people manager, associate, dev-rep, locale) lifted 0.87->0.95.
    assert report["level_accuracy"] >= 0.92
    assert report["level_macro_f1"] >= 0.90


def test_sector_quality(report: dict) -> None:
    # Corrected misclassified companies in sectors.json: 0.80 -> 0.95.
    assert report["sector_accuracy"] >= 0.92


def test_city_quality(report: dict) -> None:
    assert report["city_accuracy"] >= 0.90


def test_country_quality(report: dict) -> None:
    assert report["country_accuracy"] >= 0.88


def test_comp_quality(report: dict) -> None:
    assert report["comp_f1"] >= 0.92
    assert (report["comp_value_within_5pct"] or 0) >= 0.92


def test_yoe_quality_500(report: dict) -> None:
    assert report["yoe_f1"] >= 0.92
    assert (report["yoe_exact"] or 0) >= 0.90


def test_yoe_quality(report: dict) -> None:
    # Measured on cue-windowed text (head-truncation had hidden ~97% of the signal).
    assert report["yoe_f1"] >= 0.85
    assert (report["yoe_exact"] or 0) >= 0.90
