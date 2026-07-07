"""Contract/smoke tests for the offline trainer — no fastembed, no full run."""

from __future__ import annotations

import numpy as np
import pytest

train = pytest.importorskip("scripts.train_sector_classifier", reason="run from repo root")


def test_load_corpus_drops_unlabeled(tmp_path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text(
        '{"company":"A","company_key":"a","domain":"a.ai","sector":"Fintech"}\n'
        '{"company":"B","company_key":"b","domain":"b.com","sector":null}\n'
    )
    records, labels = train.load_corpus(p)
    assert len(records) == 1 and labels == ["Fintech"]


def test_embed_records_uses_single_process(monkeypatch) -> None:
    seen = {}

    class FakeReranker:
        def embed_texts(self, texts, *, batch_size=256, parallel=None):
            seen["parallel"] = parallel
            return [[float(len(t)), 0.0, 0.0] for t in texts]

    monkeypatch.setattr(train, "get_semantic_reranker", lambda *a, **k: FakeReranker())
    mat = train.embed_records([{"company": "X", "company_key": "x", "domain": None}], batch_size=8)
    assert seen["parallel"] is None  # single-process, always (the OOM-safe contract)
    assert isinstance(mat, np.ndarray) and mat.shape[0] == 1


def test_fit_model_learns_separable(monkeypatch) -> None:
    pytest.importorskip("sklearn")
    rng = np.random.default_rng(1)
    # two well-separated blobs in a 6-dim "feature" space
    a = rng.normal(loc=+2, size=(40, 6))
    b = rng.normal(loc=-2, size=(40, 6))
    feats = np.vstack([a, b]).astype(np.float32)
    y = ["Fintech"] * 40 + ["Healthcare"] * 40
    model = train.fit_model(feats, y, folds=3, jobs=1)
    assert set(model["labels"]) == {"Fintech", "Healthcare"}
    assert model["W"].shape == (2, 6)
    assert model["cv_accuracy"] > 0.9  # separable → high CV


def test_sweep_thresholds_hits_target_precision() -> None:
    # confident correct + a few wrong-but-confident → sweep should raise tau to protect precision
    probs = np.array([[0.95, 0.05], [0.9, 0.1], [0.55, 0.45], [0.92, 0.08]])
    feats = np.eye(4, 6, dtype=np.float32)
    centroids = np.array([[1.0, 0, 0, 0, 0, 0], [0, 1.0, 0, 0, 0, 0]], dtype=np.float32)
    y_idx = np.array([0, 0, 1, 1])  # last one is a confident error
    tp, tm, ts, rep = train.sweep_thresholds(probs, feats, centroids, y_idx, target_precision=0.85)
    # a precision-meeting point WITH positive coverage exists (fire only the 0.95-prob correct row),
    # so the sweep must return it — not the empty fallback.
    assert rep["precision"] >= 0.85 - 1e-9
    assert rep["coverage"] > 0.0
    assert 0.0 <= tp <= 1.0
