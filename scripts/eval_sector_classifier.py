"""Benchmark the sector classifier with honest held-out CV evaluation vs the deterministic baseline.

Usage (honest CV — the primary evaluation):
  .venv/bin/python scripts/eval_sector_classifier.py \
      --corpus tests/fixtures/sector_corpus.jsonl [--folds 5]

Usage (old train-set scoring — prints a loud warning):
  .venv/bin/python scripts/eval_sector_classifier.py \
      --corpus tests/fixtures/sector_corpus.jsonl --score-model dist/sector_clf.npz

Usage (ceiling):
  ... --ceiling
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

BASELINE_ACCURACY = 0.724
BASELINE_COVERAGE = 0.267
BASELINE = {"accuracy_when_covered": BASELINE_ACCURACY, "coverage": BASELINE_COVERAGE}

TAU_PROB_CURVE = [0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7]


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


def _infer_model(
    model: dict,
    feats: np.ndarray,
    tau_prob: float,
    tau_margin: float,
    tau_sim: float,
) -> list[tuple[str | None, float]]:
    """Replicate SectorClassifier inference: logits -> per-class Platt -> normalize -> 3-gate."""
    W, b = model["W"], model["b"]
    pa, pb = model["platt_a"], model["platt_b"]
    cents = model["centroids"]
    logits = feats @ W.T + b
    cal = 1.0 / (1.0 + np.exp(-(pa * logits + pb)))
    probs = cal / cal.sum(axis=1, keepdims=True)
    order = np.argsort(-probs, axis=1)
    top1 = order[:, 0]
    p1 = probs[np.arange(len(probs)), top1]
    p2 = probs[np.arange(len(probs)), order[:, 1]] if probs.shape[1] > 1 else np.zeros(len(probs))
    cn = np.linalg.norm(cents, axis=1)
    cn[cn == 0] = 1.0
    fn_norm = np.linalg.norm(feats, axis=1)
    fn_norm[fn_norm == 0] = 1.0
    sim = np.einsum("ij,ij->i", feats, cents[top1]) / (fn_norm * cn[top1])
    out: list[tuple[str | None, float]] = []
    for i in range(len(probs)):
        ok = p1[i] >= tau_prob and (p1[i] - p2[i]) >= tau_margin and sim[i] >= tau_sim
        out.append((model["labels"][top1[i]] if ok else None, float(p1[i])))
    return out


def cv_risk_coverage(
    embeddings: np.ndarray,
    domains: list,
    y: list[str] | np.ndarray,
    *,
    folds: int = 5,
    jobs: int = 1,
    tau_prob: float = 0.0,
    tau_margin: float = 0.0,
    tau_sim: float = -1.0,
) -> tuple[dict, list[dict]]:
    """Honest stratified k-fold risk-coverage evaluation (no train-set leak).

    Trains on 4/5 of the data, infers on held-out 1/5 using the same 3-gate logic as
    SectorClassifier. Pools out-of-fold predictions, then returns the risk_coverage dict
    and a risk-coverage curve sweeping tau_prob.
    """
    from scripts.train_sector_classifier import fit_model
    from sklearn.model_selection import StratifiedKFold

    from ergon_tracker.extract.sector_features import assemble, cl2n

    y_arr = np.asarray(y)
    labels_sorted = sorted(set(y_arr))
    label2idx = {lab: i for i, lab in enumerate(labels_sorted)}
    y_idx = np.array([label2idx[v] for v in y_arr])

    min_class = int(np.bincount(y_idx).min())
    n_splits = max(2, min(folds, min_class))
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)

    oof_pred: list[tuple[str | None, float] | None] = [None] * len(y_arr)
    oof_gold: list[str | None] = [None] * len(y_arr)

    for tr_idx, te_idx in skf.split(embeddings, y_idx):
        _, mean = cl2n(embeddings[tr_idx])
        f_tr = assemble(embeddings[tr_idx], [domains[i] for i in tr_idx], mean)
        f_te = assemble(embeddings[te_idx], [domains[i] for i in te_idx], mean)
        model = fit_model(f_tr, list(y_arr[tr_idx]), folds=folds, jobs=jobs)
        preds = _infer_model(model, f_te, tau_prob, tau_margin, tau_sim)
        for k, i in enumerate(te_idx):
            oof_pred[i] = preds[k]
            oof_gold[i] = str(y_arr[i])

    # All positions must be filled after CV
    assert all(p is not None for p in oof_pred), "OOF predictions incomplete"
    pooled_pred: list[tuple[str | None, float]] = oof_pred  # type: ignore[assignment]
    pooled_gold: list[str] = oof_gold  # type: ignore[assignment]

    metrics = risk_coverage(pooled_pred, pooled_gold)

    # Risk-coverage curve: sweep tau_prob only (pure prob abstention)
    raw_probs = np.array([p for _, p in pooled_pred])
    raw_labs = [lab for lab, _ in pooled_pred]
    curve: list[dict] = []
    for tau in TAU_PROB_CURVE:
        fire = raw_probs >= tau
        cov = float(fire.mean())
        if cov == 0:
            curve.append({"tau_prob": tau, "coverage": 0.0, "accuracy_when_covered": 0.0})
            continue
        hits = sum(1 for i in range(len(pooled_gold)) if fire[i] and raw_labs[i] == pooled_gold[i])
        acc = hits / int(fire.sum())
        curve.append({"tau_prob": tau, "coverage": cov, "accuracy_when_covered": acc})

    return metrics, curve


def main(argv: list[str]) -> None:
    corpus: Path | None = None
    score_model_path: Path | None = None
    ceiling = False
    folds = 5
    i = 0
    while i < len(argv):
        if argv[i] == "--corpus":
            corpus = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--score-model":
            score_model_path = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--folds":
            folds = int(argv[i + 1])
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

    records, golds = load_corpus(corpus)
    emb = embed_records(records)
    domains = [r.get("domain") for r in records]

    # ── Honest held-out CV evaluation (primary) ───────────────────────────────────────────────
    print(f"\n=== Honest held-out CV ({folds}-fold stratified) — bge-small + calibrated logreg ===")
    m, curve = cv_risk_coverage(emb, domains, golds, folds=folds)
    print(
        f"  accuracy-when-covered : {m['accuracy_when_covered']:.1%}  "
        f"(baseline {BASELINE_ACCURACY:.1%})"
    )
    print(f"  coverage              : {m['coverage']:.1%}  (baseline {BASELINE_COVERAGE:.1%})")
    print(f"  macro-F1              : {m['macro_f1']:.3f}")

    # Verdict: ML beats baseline if at full-coverage accuracy >= baseline accuracy
    # AND coverage >= baseline coverage.
    verdict_beats = (
        m["accuracy_when_covered"] >= BASELINE_ACCURACY and m["coverage"] >= BASELINE_COVERAGE
    )
    verdict_word = "BEATS" if verdict_beats else "does NOT beat"
    print(f"  VERDICT: ML {verdict_word} the deterministic baseline.")

    print("\n  Risk-coverage curve (OOF, abstain by prob threshold):")
    print("    tau_prob   coverage   accuracy-when-covered")
    for row in curve:
        acc_str = f"{row['accuracy_when_covered']:6.1%}" if row["coverage"] > 0 else "     —"
        print(f"    {row['tau_prob']:.2f}       {row['coverage']:5.1%}    {acc_str}")

    # ── Optional: old train-set scoring (loud warning) ────────────────────────────────────────
    if score_model_path is not None:
        from ergon_tracker.extract.sector_clf import load_sector_model

        print()
        print("=" * 70)
        print("WARNING: train-set scoring (optimistic/leaked when corpus==training data)")
        print("         — NOT the PoC verdict")
        print("=" * 70)
        load_sector_model.cache_clear()
        clf = load_sector_model(score_model_path)
        if clf is None:
            print(f"  no model at {score_model_path} — run train_sector_classifier.py first")
        else:
            preds = clf.predict_batch(emb, domains)
            ms = risk_coverage(preds, golds)
            print(f"  accuracy-when-covered : {ms['accuracy_when_covered']:.1%}")
            print(f"  coverage              : {ms['coverage']:.1%}")
            print(f"  macro-F1              : {ms['macro_f1']:.3f}")
        print("=" * 70)

    # ── Ceiling ───────────────────────────────────────────────────────────────────────────────
    if ceiling:
        try:
            from transformers import pipeline  # noqa: F401

            print("  [ceiling] deberta zero-shot available — (run separately; dev-only reference).")
        except ImportError:
            print("  [ceiling] transformers not installed — skipping zero-shot ceiling.")


if __name__ == "__main__":
    main(sys.argv[1:])
