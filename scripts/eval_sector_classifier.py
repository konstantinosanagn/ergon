"""Benchmark the exported sector classifier vs the deterministic baseline (72.4% acc / 26.7% cov).

Usage:
  .venv/bin/python scripts/eval_sector_classifier.py \
      --corpus tests/fixtures/sector_corpus.jsonl --model dist/sector_clf.npz [--ceiling]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.train_sector_classifier import embed_records, load_corpus  # noqa: E402

from ergon_tracker.extract.sector_clf import load_sector_model  # noqa: E402

BASELINE = {"accuracy_when_covered": 0.724, "coverage": 0.267}


def _f1(tp: int, fp: int, fn: int) -> float:
    return 0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn)


def risk_coverage(preds: list[tuple[str | None, float]], golds: list[str]) -> dict:
    covered = [(p, g) for (p, _), g in zip(preds, golds, strict=False) if p is not None]
    hits = sum(1 for p, g in covered if p == g)
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    for (p, _), g in zip(preds, golds, strict=False):
        if p is None:
            continue
        if p == g:
            tp[g] += 1
        else:
            fp[p] += 1
            fn[g] += 1
    classes = set(tp) | set(fp) | set(fn)
    macro = float(np.mean([_f1(tp[c], fp[c], fn[c]) for c in classes])) if classes else 0.0
    return {
        "coverage": len(covered) / len(preds) if preds else 0.0,
        "accuracy_when_covered": hits / len(covered) if covered else 0.0,
        "macro_f1": macro,
        "per_class": {c: _f1(tp[c], fp[c], fn[c]) for c in sorted(classes)},
    }


def main(argv: list[str]) -> None:
    corpus, model_path, ceiling = None, ROOT / "dist" / "sector_clf.npz", False
    i = 0
    while i < len(argv):
        if argv[i] == "--corpus":
            corpus = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--model":
            model_path = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--ceiling":
            ceiling = True
            i += 1
        else:
            print(f"unknown flag: {argv[i]}")
            return
    if not corpus:
        print("need --corpus")
        return
    load_sector_model.cache_clear()
    clf = load_sector_model(model_path)
    if clf is None:
        print(f"no model at {model_path} — run train_sector_classifier.py first")
        return

    records, golds = load_corpus(corpus)
    emb = embed_records(records)
    preds = clf.predict_batch(emb, [r.get("domain") for r in records])
    m = risk_coverage(preds, golds)
    print("\n=== Stage-1 PoC: bge-small + calibrated logreg ===")
    print(
        f"  accuracy-when-covered : {m['accuracy_when_covered']:.1%}  "
        f"(baseline {BASELINE['accuracy_when_covered']:.1%})"
    )
    print(f"  coverage              : {m['coverage']:.1%}  (baseline {BASELINE['coverage']:.1%})")
    print(f"  macro-F1              : {m['macro_f1']:.3f}")
    verdict = (
        "BEATS"
        if m["accuracy_when_covered"] >= BASELINE["accuracy_when_covered"]
        and m["coverage"] > BASELINE["coverage"]
        else "does NOT beat"
    )
    print(f"  VERDICT: ML {verdict} the deterministic baseline.")
    if ceiling:
        try:
            from transformers import pipeline  # noqa: F401

            print("  [ceiling] deberta zero-shot available — (run separately; dev-only reference).")
        except ImportError:
            print("  [ceiling] transformers not installed — skipping zero-shot ceiling.")


if __name__ == "__main__":
    main(sys.argv[1:])
