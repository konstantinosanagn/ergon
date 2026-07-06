"""Benchmark-driven recall/precision gate for the degree extractor.

The corpus (``tests/fixtures/degree_corpus.jsonl``) is built the same way as the salary corpus:
real education-requirement windows fetched from live Greenhouse/Lever/… postings
(``scripts/build_degree_corpus.py``), extracted with a net *wider* than ``degree.py`` so the
benchmark can see the extractor's true recall gaps, then **blind-labeled from the text's meaning**
(not from ``degree.py``'s rules) for ``degree_min`` + ``degree_required``, plus FP-trap negatives
("MS Office", "high degree of autonomy", "Boston, MA", "360 degree", tuition reimbursement).

Record format::

    {"text": ..., "expect": {"degree_min": "bachelor", "degree_required": true|false|null}, "src": ...}
    {"text": ..., "expect": null, "src": ...}   # negative: extractor must return (None, None)

Three gates (degree has two axes of correctness, unlike salary's one):
  * **level recall** — ``degree_min`` exactly correct on positives. Deterministic gazetteer, so held high.
  * **scope accuracy** — ``degree_required`` correct on positives whose gold scope is *stated*
    (required/preferred). This is the genuinely hard half (published systems ~74%); gated honestly
    and ratcheted, never faked.
  * **precision** — negatives must yield no extraction at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.degree import DegreeExtractor

CORPUS_PATH = Path(__file__).parent / "fixtures" / "degree_corpus.jsonl"

# Ratcheting gates — a margin below the measured numbers (2026-07-05, 402-record English corpus:
# level recall 88.6%, scope 61.1%, precision 99.5%); raise as degree.py improves.
LEVEL_RECALL_GATE = 0.85  # degree_min (the level) — production-grade
SCOPE_ACC_GATE = 0.58  # degree_required (required-vs-preferred) — the genuinely hard half; honest,
# low, ratcheted. NOT yet fit-rubric-grade: consumers should treat degree_required as advisory.
PRECISION_GATE = 0.98  # one irreducible FP: a maritime "Master's <orders>" (ship-rank word sense)

_EX = DegreeExtractor()


def _extract(text: str) -> tuple[str | None, bool | None]:
    return _EX.extract(ExtractInput(title="Role", description_text=text))


def _load() -> tuple[list[dict], list[dict]]:
    records = [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]
    positives = [r for r in records if r["expect"]]
    negatives = [r for r in records if not r["expect"]]
    return positives, negatives


def test_corpus_is_substantial() -> None:
    positives, negatives = _load()
    assert len(positives) >= 150, f"only {len(positives)} positives in corpus"
    assert len(negatives) >= 40, f"only {len(negatives)} negatives in corpus"


def test_level_recall_on_positives() -> None:
    positives, _ = _load()
    hits = 0
    misses: list[dict] = []
    for r in positives:
        got_level, _ = _extract(r["text"])
        if got_level == r["expect"]["degree_min"]:
            hits += 1
        else:
            misses.append(r)
    recall = hits / len(positives)
    print(f"\ndegree level recall: {hits}/{len(positives)} = {recall:.1%}")
    if recall < LEVEL_RECALL_GATE:
        detail = "\n".join(
            f"  want={r['expect']['degree_min']} got={_extract(r['text'])[0]} :: {r['text'][:110]!r}"
            for r in misses[:8]
        )
        pytest.fail(
            f"level recall {recall:.1%} below gate {LEVEL_RECALL_GATE:.0%} "
            f"({hits}/{len(positives)}). first misses:\n{detail}"
        )


def test_scope_accuracy_on_positives() -> None:
    """Scope (required/preferred) only scored where the gold scope is stated AND the level is right
    (a wrong level makes scope moot). ``None`` golds — degree present but scope unstated — are
    reported but excluded from the gate."""
    positives, _ = _load()
    scored = [r for r in positives if r["expect"]["degree_required"] is not None]
    correct = 0
    wrong: list[dict] = []
    for r in scored:
        got_level, got_scope = _extract(r["text"])
        if got_level == r["expect"]["degree_min"] and got_scope == r["expect"]["degree_required"]:
            correct += 1
        else:
            wrong.append(r)
    acc = correct / len(scored) if scored else 1.0
    print(f"degree scope accuracy: {correct}/{len(scored)} = {acc:.1%}")
    if acc < SCOPE_ACC_GATE:
        detail = "\n".join(
            f"  want=({r['expect']['degree_min']},{r['expect']['degree_required']}) "
            f"got={_extract(r['text'])} :: {r['text'][:100]!r}"
            for r in wrong[:8]
        )
        pytest.fail(
            f"scope accuracy {acc:.1%} below gate {SCOPE_ACC_GATE:.0%} "
            f"({correct}/{len(scored)}). first misses:\n{detail}"
        )


def test_precision_on_negatives() -> None:
    _, negatives = _load()
    false_pos = [r for r in negatives if _extract(r["text"]) != (None, None)]
    precision = (len(negatives) - len(false_pos)) / len(negatives)
    print(f"degree precision: {len(negatives) - len(false_pos)}/{len(negatives)} = {precision:.1%}")
    if precision < PRECISION_GATE:
        detail = "\n".join(f"  got={_extract(r['text'])} :: {r['text'][:110]!r}" for r in false_pos[:8])
        pytest.fail(
            f"precision {precision:.1%} below gate {PRECISION_GATE:.0%} "
            f"({len(false_pos)} false positives). first offenders:\n{detail}"
        )
