"""The exported .npz must reproduce the model's decision with numpy only — no sklearn at runtime."""

from __future__ import annotations

import numpy as np
import pytest

from ergon_tracker.extract.sector_clf import load_sector_model, save_sector_model


def _tiny_model(tmp_path):
    # 2 classes, embed_dim=3. Hand-built weights so the expected decision is analytic.
    labels = np.array(["Fintech", "Healthcare"])
    mean = np.zeros(3, dtype=np.float32)
    W = np.array(
        [[5.0, 0, 0] + [0] * 6, [0, 5.0, 0] + [0] * 6], dtype=np.float32
    )  # feat = 3 + 6 TLD
    b = np.zeros(2, dtype=np.float32)
    platt_a = np.ones(2, dtype=np.float32)
    platt_b = np.zeros(2, dtype=np.float32)
    centroids = np.array([[1.0, 0, 0] + [0] * 6, [0, 1.0, 0] + [0] * 6], dtype=np.float32)
    p = tmp_path / "m.npz"
    save_sector_model(
        p,
        labels=labels,
        mean=mean,
        W=W,
        b=b,
        platt_a=platt_a,
        platt_b=platt_b,
        centroids=centroids,
        tau_prob=0.6,
        tau_margin=0.1,
        tau_sim=0.5,
        embed_dim=3,
    )
    load_sector_model.cache_clear()
    return load_sector_model(p)


def test_predicts_confident_class(tmp_path) -> None:
    clf = _tiny_model(tmp_path)
    label, prob = clf.predict(np.array([10.0, 0.0, 0.0], dtype=np.float32), domain=None)
    assert label == "Fintech"
    assert prob > 0.6


def test_abstains_when_ambiguous(tmp_path) -> None:
    clf = _tiny_model(tmp_path)
    # symmetric embedding -> near-tie -> margin gate fails -> abstain (None)
    label, _ = clf.predict(np.array([1.0, 1.0, 0.0], dtype=np.float32), domain=None)
    assert label is None


def test_missing_file_returns_none() -> None:
    load_sector_model.cache_clear()
    assert load_sector_model("/nonexistent/model.npz") is None


from ergon_tracker.extract.sector_features import assemble, cl2n  # noqa: E402


def test_predicted_class_matches_sklearn(tmp_path) -> None:
    pytest.importorskip("sklearn")
    from sklearn.linear_model import LogisticRegression

    rng = np.random.default_rng(0)
    embed_dim = 8
    emb = rng.normal(size=(90, embed_dim)).astype(np.float32)
    y = (emb[:, 0] * 2 + emb[:, 1] - emb[:, 2]).astype(int) % 3  # 3 classes
    labels = np.array(["A", "B", "C"])
    _, mean = cl2n(emb)
    feats = assemble(emb, [None] * len(emb), mean)  # real feature path (embed_dim + TLD block)

    lr = LogisticRegression(max_iter=2000).fit(feats, y)
    p = tmp_path / "sk.npz"
    save_sector_model(
        p,
        labels=labels,
        mean=mean,
        W=lr.coef_,
        b=lr.intercept_,
        platt_a=np.ones(3, dtype=np.float32),
        platt_b=np.zeros(3, dtype=np.float32),
        centroids=np.zeros((3, feats.shape[1]), dtype=np.float32),
        tau_prob=0.0,
        tau_margin=0.0,
        tau_sim=-1.0,
        embed_dim=embed_dim,  # never abstain
    )
    load_sector_model.cache_clear()
    clf = load_sector_model(p)
    got = [lab for lab, _ in clf.predict_batch(emb, [None] * len(emb))]
    want = [labels[i] for i in lr.predict(feats)]
    assert got == want  # exact class parity through the real feature + inference path
