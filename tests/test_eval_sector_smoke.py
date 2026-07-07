from __future__ import annotations

import pytest

ev = pytest.importorskip("scripts.eval_sector_classifier", reason="run from repo root")


def test_risk_coverage_math() -> None:
    preds = [("Fintech", 0.9), (None, 0.4), ("Healthcare", 0.8), ("Fintech", 0.7)]
    golds = ["Fintech", "Healthcare", "Healthcare", "Banking/Finance"]
    m = ev.risk_coverage(preds, golds)
    assert m["coverage"] == pytest.approx(3 / 4)
    assert m["accuracy_when_covered"] == pytest.approx(2 / 3)  # 2 of 3 covered are right
    assert 0.0 <= m["macro_f1"] <= 1.0


def test_cv_risk_coverage_structure() -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("sklearn")

    rng = np.random.default_rng(42)
    embed_dim = 8
    n_per_class = 20
    # 3 well-separated gaussian blobs (linearly separable)
    centers = np.array(
        [
            [5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    blobs = [rng.normal(loc=centers[k], scale=0.3, size=(n_per_class, embed_dim)) for k in range(3)]
    emb = np.vstack(blobs).astype(np.float32)
    class_names = ["Alpha", "Beta", "Gamma"]
    labels = [class_names[k] for k in range(3) for _ in range(n_per_class)]
    n = len(labels)
    domains: list = [None] * n

    result, curve = ev.cv_risk_coverage(emb, domains, labels, folds=3)

    # Structure checks
    assert isinstance(result, dict)
    for key in ("accuracy_when_covered", "coverage", "macro_f1", "per_class"):
        assert key in result, f"missing key: {key}"
    assert isinstance(curve, list)
    assert len(curve) > 0
    for row in curve:
        for key in ("tau_prob", "coverage", "accuracy_when_covered"):
            assert key in row, f"curve row missing key: {key}"

    # tau=0.0 must be full coverage (no abstentions at zero threshold)
    tau0_row = next(r for r in curve if r["tau_prob"] == 0.0)
    assert tau0_row["coverage"] == pytest.approx(1.0), "expected full coverage at tau_prob=0.0"

    # On linearly separable data the held-out accuracy should be high
    assert result["accuracy_when_covered"] > 0.8, (
        f"expected >0.8 accuracy on separable blobs, got {result['accuracy_when_covered']:.3f}"
    )
