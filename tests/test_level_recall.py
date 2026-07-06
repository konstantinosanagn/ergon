"""Benchmark-driven accuracy/macro-F1 gate for the seniority-level classifier.

``level.py``'s extractor is TITLE-driven (``infer_level(title)``; the description/years fallbacks live
downstream in the build pipeline and are tested separately). So this corpus is real ``(title,
description)`` postings fetched from live boards (``scripts/build_level_corpus.py``), blind-labeled
for the level the TITLE conveys — an unmarked title is ``unknown`` (not upgraded from the
description). Multi-class, so the metrics are accuracy and macro-F1, not precision/recall.

Record format: ``{"title": ..., "level": "senior", "src": ...}`` where ``level`` is one of the 12
``JobLevel`` values.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from ergon_tracker.extract.level import infer_level

CORPUS_PATH = Path(__file__).parent / "fixtures" / "level_corpus.jsonl"

# Ratcheting gates — a margin below the measured numbers (2026-07-06, 900-posting ENTERPRISE-title
# corpus: accuracy 82.2%, macro-F1 0.738). NB this is harder than the old startup-heavy 500-row gold
# (0.954): enterprise titles carry ambiguous rungs (bare "Associate", IC-"X Manager", "Supervisor",
# dual-rank "Analyst/Sr Analyst", numeric ladders) where humans and a deterministic classifier
# reasonably differ. Raise as level.py improves.
ACCURACY_GATE = 0.78
MACRO_F1_GATE = 0.68


def _load() -> list[dict]:
    return [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]


def _predict(r: dict) -> str:
    return infer_level(r["title"]).value


def _macro_f1(records: list[dict]) -> tuple[float, dict[str, float]]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    labels = set()
    for r in records:
        want, got = r["level"], _predict(r)
        labels |= {want, got}
        if want == got:
            tp[want] += 1
        else:
            fp[got] += 1
            fn[want] += 1
    per: dict[str, float] = {}
    for c in labels:
        p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) else 0.0
        rec = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) else 0.0
        per[c] = 2 * p * rec / (p + rec) if (p + rec) else 0.0
    # Macro over classes PRESENT IN GOLD (a class only the model predicted shouldn't dilute).
    gold_classes = {r["level"] for r in records}
    macro = sum(per[c] for c in gold_classes) / len(gold_classes) if gold_classes else 0.0
    return macro, per


def test_corpus_is_substantial() -> None:
    records = _load()
    assert len(records) >= 400, f"only {len(records)} records in corpus"
    assert len({r["level"] for r in records}) >= 7, "too few distinct levels represented"


def test_accuracy() -> None:
    records = _load()
    hits = sum(1 for r in records if _predict(r) == r["level"])
    acc = hits / len(records)
    print(f"\nlevel accuracy: {hits}/{len(records)} = {acc:.1%}")
    if acc < ACCURACY_GATE:
        miss = [r for r in records if _predict(r) != r["level"]][:10]
        detail = "\n".join(f"  want={r['level']} got={_predict(r)} :: {r['title']!r}" for r in miss)
        pytest.fail(f"accuracy {acc:.1%} below gate {ACCURACY_GATE:.0%}.\n{detail}")


def test_macro_f1() -> None:
    records = _load()
    macro, per = _macro_f1(records)
    print(f"level macro-F1: {macro:.3f}")
    print("  per-class F1:", {c: round(v, 2) for c, v in sorted(per.items())})
    if macro < MACRO_F1_GATE:
        pytest.fail(f"macro-F1 {macro:.3f} below gate {MACRO_F1_GATE:.2f}. per-class: {per}")
