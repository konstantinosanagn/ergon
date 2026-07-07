"""Offline trainer for the Stage-1 sector classifier PoC (sklearn; never imported at runtime).

Usage:
  .venv/bin/python scripts/train_sector_classifier.py \
      --corpus tests/fixtures/sector_corpus.jsonl --out dist/sector_clf.npz [--sample 50] [--folds 5]

--sample N embeds only the first N labeled rows (the memory/throughput STRESS gate) and skips export;
run it before the full pass. Concurrency for sklearn CV is env-gated via ERGON_SECTOR_JOBS
(explicit int wins; else CPU-2 on CI; else 1 local).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.extract.sector_features import build_input_text  # noqa: E402
from ergon_tracker.semantic import get_semantic_reranker  # noqa: E402


def _peak_rss_mb() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes vs KB


def _jobs() -> int:
    env = os.environ.get("ERGON_SECTOR_JOBS")
    if env:
        return int(env)
    if os.environ.get("CI"):
        return max(2, (os.cpu_count() or 4) - 2)
    return 1


def load_corpus(path: Path) -> tuple[list[dict], list[str]]:
    """Read the JSONL corpus; keep only rows with a gold sector."""
    records, labels = [], []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("sector"):
            records.append(r)
            labels.append(r["sector"])
    return records, labels


def embed_records(
    records: list[dict],
    *,
    batch_size: int = 256,
    sample: int | None = None,
    log=print,
) -> np.ndarray:
    """Single-process (parallel=None), memory-bounded embedding of the input-text of each record."""
    rows = records[:sample] if sample else records
    texts = [
        build_input_text(r.get("company"), r.get("domain"), r.get("example_title")) for r in rows
    ]
    t0 = time.monotonic()
    reranker = get_semantic_reranker()
    vecs = reranker.embed_texts(texts, batch_size=batch_size, parallel=None)  # OOM-safe: no workers
    mat = np.asarray(vecs, dtype=np.float32)
    log(
        f"[embed] rows={len(rows)} dim={mat.shape[1] if mat.size else 0} "
        f"peakRSS={_peak_rss_mb():.0f}MB wall={time.monotonic() - t0:.1f}s"
    )
    return mat


def fit_model(feats: np.ndarray, y: list[str], *, folds: int, jobs: int) -> dict:
    """L2 multinomial logreg with a CV C-sweep, per-class Platt, and class centroids."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    labels = sorted(set(y))
    idx = {lab: i for i, lab in enumerate(labels)}
    y_idx = np.asarray([idx[v] for v in y])

    # choose C by stratified CV accuracy (small grid; balanced for the long tail)
    best_c, best_acc = 1.0, -1.0
    n_splits = min(folds, int(np.bincount(y_idx).min()))
    skf = StratifiedKFold(n_splits=max(2, n_splits), shuffle=True, random_state=42)
    for c in (0.25, 0.5, 1.0, 2.0, 4.0):
        clf = LogisticRegression(C=c, max_iter=2000, class_weight="balanced")
        pred = cross_val_predict(clf, feats, y_idx, cv=skf, n_jobs=jobs)
        acc = float((pred == y_idx).mean())
        if acc > best_acc:
            best_c, best_acc = c, acc

    final = LogisticRegression(C=best_c, max_iter=2000, class_weight="balanced").fit(feats, y_idx)
    # binary logreg emits one row; expand to 2 rows so the .npz is always (n_classes, feat_dim)
    W = (
        final.coef_
        if final.coef_.shape[0] == len(labels)
        else np.vstack([-final.coef_[0], final.coef_[0]])
    )
    b = (
        final.intercept_
        if final.intercept_.shape[0] == len(labels)
        else np.array([-final.intercept_[0], final.intercept_[0]])
    )

    # per-class Platt on out-of-fold decision scores (exportable as sigmoid(a*f+b))
    from sklearn.linear_model import LogisticRegression as LR1D

    dec = cross_val_predict(
        LogisticRegression(C=best_c, max_iter=2000, class_weight="balanced"),
        feats,
        y_idx,
        cv=skf,
        n_jobs=jobs,
        method="decision_function",
    )
    dec = dec if dec.ndim == 2 else np.vstack([-dec, dec]).T
    platt_a, platt_b = np.ones(len(labels), np.float32), np.zeros(len(labels), np.float32)
    for k in range(len(labels)):
        yk = (y_idx == k).astype(int)
        if 0 < yk.sum() < len(yk):
            p = LR1D().fit(dec[:, [k]], yk)
            platt_a[k], platt_b[k] = float(p.coef_[0, 0]), float(p.intercept_[0])

    centroids = np.vstack([feats[y_idx == k].mean(axis=0) for k in range(len(labels))]).astype(
        np.float32
    )
    return {
        "labels": labels,
        "W": W.astype(np.float32),
        "b": b.astype(np.float32),
        "platt_a": platt_a,
        "platt_b": platt_b,
        "centroids": centroids,
        "cv_accuracy": best_acc,
    }


def _apply(probs, platt_a, platt_b):
    cal = 1.0 / (1.0 + np.exp(-(platt_a * probs + platt_b)))
    return cal / cal.sum(axis=1, keepdims=True)


def sweep_thresholds(probs, feats, centroids, y_idx, *, target_precision: float):
    """Vectorized grid over (tau_prob, tau_margin, tau_sim); pick max-coverage point meeting precision."""
    order = np.argsort(-probs, axis=1)
    top1 = order[:, 0]
    p1 = probs[np.arange(len(probs)), top1]
    p2 = probs[np.arange(len(probs)), order[:, 1]]
    cn = np.linalg.norm(centroids, axis=1)
    cn[cn == 0] = 1.0
    fn = np.linalg.norm(feats, axis=1)
    fn[fn == 0] = 1.0
    sim = np.einsum("ij,ij->i", feats, centroids[top1]) / (fn * cn[top1])
    correct = top1 == y_idx
    best = (1.0, 0.0, -1.0, {"precision": 1.0, "coverage": 0.0})
    for tp in np.linspace(0.3, 0.95, 14):
        for tm in np.linspace(0.0, 0.4, 9):
            for ts in np.linspace(-0.2, 0.6, 9):
                fire = (p1 >= tp) & ((p1 - p2) >= tm) & (sim >= ts)
                cov = fire.mean()
                if cov == 0:
                    continue
                prec = correct[fire].mean()
                if prec >= target_precision and cov > best[3]["coverage"]:
                    best = (
                        float(tp),
                        float(tm),
                        float(ts),
                        {"precision": float(prec), "coverage": float(cov)},
                    )
    return best


def main(argv: list[str]) -> None:
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    from ergon_tracker.extract.sector_clf import save_sector_model
    from ergon_tracker.extract.sector_features import assemble, cl2n

    corpus, out, sample, folds, target = None, ROOT / "dist" / "sector_clf.npz", None, 5, 0.85
    i = 0
    while i < len(argv):
        if argv[i] == "--corpus":
            corpus = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--out":
            out = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--sample":
            sample = int(argv[i + 1])
            i += 2
        elif argv[i] == "--folds":
            folds = int(argv[i + 1])
            i += 2
        elif argv[i] == "--target-precision":
            target = float(argv[i + 1])
            i += 2
        else:
            print(f"unknown flag: {argv[i]}")
            return
    if not corpus:
        print("need --corpus")
        return

    records, y = load_corpus(corpus)
    print(f"[load] {len(records)} labeled rows, {len(set(y))} classes, jobs={_jobs()}")
    emb = embed_records(records, sample=sample)
    if sample:
        print(
            f"[stress] sample={sample} embedded OK, peakRSS={_peak_rss_mb():.0f}MB — full run is safe."
        )
        return
    _, mean = cl2n(emb)
    feats = assemble(emb, [r.get("domain") for r in records], mean)

    model = fit_model(feats, y, folds=folds, jobs=_jobs())
    idx = {lab: k for k, lab in enumerate(model["labels"])}
    y_idx = np.asarray([idx[v] for v in y])

    # out-of-fold calibrated probs for an honest threshold sweep
    skf = StratifiedKFold(
        n_splits=max(2, min(folds, int(np.bincount(y_idx).min()))), shuffle=True, random_state=42
    )
    dec = cross_val_predict(
        LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced"),
        feats,
        y_idx,
        cv=skf,
        n_jobs=_jobs(),
        method="predict_proba",
    )
    tp, tm, ts, rep = sweep_thresholds(
        dec, feats, model["centroids"], y_idx, target_precision=target
    )
    print(
        f"[sweep] tau=({tp:.2f},{tm:.2f},{ts:.2f}) precision={rep['precision']:.1%} coverage={rep['coverage']:.1%}"
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    save_sector_model(
        out,
        labels=model["labels"],
        mean=mean,
        W=model["W"],
        b=model["b"],
        platt_a=model["platt_a"],
        platt_b=model["platt_b"],
        centroids=model["centroids"],
        tau_prob=tp,
        tau_margin=tm,
        tau_sim=ts,
        embed_dim=emb.shape[1],
    )
    (out.with_suffix(".metrics.json")).write_text(
        json.dumps(
            {
                "cv_accuracy": model["cv_accuracy"],
                "threshold": {"prob": tp, "margin": tm, "sim": ts},
                "sweep": rep,
                "n_labeled": len(records),
                "n_classes": len(model["labels"]),
                "peak_rss_mb": _peak_rss_mb(),
            },
            indent=2,
        )
    )
    print(f"[done] wrote {out} + metrics")


if __name__ == "__main__":
    main(sys.argv[1:])
