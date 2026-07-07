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
