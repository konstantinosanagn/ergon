"""Benchmark-driven recall/precision gate for the salary extractor.

The corpus (``tests/fixtures/salary_corpus.jsonl``) is built from real US
pay-transparency snippets fetched from Greenhouse/Lever postings whose index rows
had no structured salary, hand spot-checked, plus a small set of synthetic
windows covering formats rare in the crawl (Workday "Pay Range" blocks,
cue-colon amounts without a currency symbol, per-state multi-range blocks) and
mandated negatives (401(k) matches, bonuses, funding figures, "$0 copay", ages).

Record format: ``{"text": ..., "expect": {"min": X, "max": Y, "interval": ...}}``
with ``expect: null`` for negatives. Gates: recall >= 90% on positives (min/max
within 5% and the right interval) and 100% on negatives (no extraction at all).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.comp import parse_salary

CORPUS_PATH = Path(__file__).parent / "fixtures" / "salary_corpus.jsonl"

RECALL_GATE = 0.90
PRECISION_GATE = 1.00
TOLERANCE = 0.05


def _load() -> tuple[list[dict], list[dict]]:
    records = [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line]
    positives = [r for r in records if r["expect"]]
    negatives = [r for r in records if not r["expect"]]
    return positives, negatives


def _close(got: float | None, want: float | None) -> bool:
    if want is None:
        return got is None
    return got is not None and abs(got - want) <= TOLERANCE * want


def _matches(record: dict) -> bool:
    out = parse_salary(record["text"])
    if out is None:
        return False
    want = record["expect"]
    return (
        _close(out.min_amount, want["min"])
        and _close(out.max_amount, want["max"])
        and out.interval is not None
        and out.interval.value == want["interval"]
    )


def test_corpus_is_substantial() -> None:
    positives, negatives = _load()
    assert len(positives) >= 150, f"only {len(positives)} positives in corpus"
    assert len(negatives) >= 40, f"only {len(negatives)} negatives in corpus"


def test_recall_on_positives() -> None:
    positives, _ = _load()
    hits = sum(1 for r in positives if _matches(r))
    recall = hits / len(positives)
    print(f"\nsalary corpus recall: {hits}/{len(positives)} = {recall:.1%}")
    if recall < RECALL_GATE:
        misses = [r for r in positives if not _matches(r)][:5]
        detail = "\n".join(
            f"  want={r['expect']} got={parse_salary(r['text'])} :: {r['text'][:120]!r}"
            for r in misses
        )
        pytest.fail(
            f"recall {recall:.1%} below gate {RECALL_GATE:.0%} "
            f"({hits}/{len(positives)}). first misses:\n{detail}"
        )


def test_precision_on_negatives() -> None:
    _, negatives = _load()
    false_pos = [r for r in negatives if parse_salary(r["text"]) is not None]
    precision = (len(negatives) - len(false_pos)) / len(negatives)
    print(
        f"\nsalary corpus precision: {len(negatives) - len(false_pos)}/{len(negatives)} = {precision:.1%}"
    )
    if precision < PRECISION_GATE:
        detail = "\n".join(
            f"  got={parse_salary(r['text'])} :: {r['text'][:120]!r}" for r in false_pos[:5]
        )
        pytest.fail(
            f"precision {precision:.1%} below gate {PRECISION_GATE:.0%} "
            f"({len(false_pos)} false positives). first offenders:\n{detail}"
        )
