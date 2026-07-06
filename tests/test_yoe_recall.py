"""Benchmark-driven recall/precision gate for the years-of-experience extractor.

Built like the salary/degree corpora: real "<number> years/months" windows fetched from live
postings (``scripts/build_yoe_corpus.py``) with a net *wider* than ``yoe.py`` (so its true recall
gaps show), blind-labeled from the text's meaning for the required experience quantity
(``{min, max}`` in whole years, months floored), plus FP-trap negatives — vesting, company age,
calendar spans, ages, "N years ago", contract/leave lengths.

Record format::

    {"text": ..., "expect": {"min": 5, "max": null}, "src": ...}
    {"text": ..., "expect": null, "src": ...}   # negative: extractor must return (None, None)

Two gates: recall on positives (exact min AND max) and precision on negatives (no extraction).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.yoe import YoeExtractor

CORPUS_PATH = Path(__file__).parent / "fixtures" / "yoe_corpus.jsonl"

# Ratcheting gates — a margin below the measured numbers (2026-07-06, 539-record English corpus:
# recall 97.8%, precision 87.7%); raise as yoe.py improves. NOTE the negative set is *adversarially
# enriched* with company-age/tenure numbers the broad net surfaced, so 87.7% is a worst case — field
# precision is higher. Recall is the field-representative axis.
RECALL_GATE = 0.95
PRECISION_GATE = 0.85

_EX = YoeExtractor()


def _extract(text: str) -> tuple[int | None, int | None]:
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


def test_recall_on_positives() -> None:
    positives, _ = _load()
    hits, misses = 0, []
    for r in positives:
        got = _extract(r["text"])
        if got == (r["expect"]["min"], r["expect"]["max"]):
            hits += 1
        else:
            misses.append(r)
    recall = hits / len(positives)
    print(f"\nyoe recall: {hits}/{len(positives)} = {recall:.1%}")
    if recall < RECALL_GATE:
        detail = "\n".join(
            f"  want=({r['expect']['min']},{r['expect']['max']}) got={_extract(r['text'])} "
            f":: {r['text'][:110]!r}"
            for r in misses[:8]
        )
        pytest.fail(f"recall {recall:.1%} below gate {RECALL_GATE:.0%} ({hits}/{len(positives)}).\n{detail}")


def test_precision_on_negatives() -> None:
    _, negatives = _load()
    false_pos = [r for r in negatives if _extract(r["text"]) != (None, None)]
    precision = (len(negatives) - len(false_pos)) / len(negatives)
    print(f"yoe precision: {len(negatives) - len(false_pos)}/{len(negatives)} = {precision:.1%}")
    if precision < PRECISION_GATE:
        detail = "\n".join(f"  got={_extract(r['text'])} :: {r['text'][:110]!r}" for r in false_pos[:8])
        pytest.fail(
            f"precision {precision:.1%} below gate {PRECISION_GATE:.0%} "
            f"({len(false_pos)} false positives).\n{detail}"
        )
