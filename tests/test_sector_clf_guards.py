"""Defensive guards for the (intentionally un-wired) sector classifier.

These tests protect against silent artifact corruption if the model is ever wired into enrich:
  * bug (a): TLD-vocab / feature-width drift between the trained ``W`` and the runtime feature layout.
  * bug (b): the trainer's threshold sweep and inference must use the SAME probability transform, so
    the coverage the sweep reports equals the rate the served model actually fires at.

All synthetic — no fastembed, no real ``dist/sector_clf.npz`` (never retrain/embed locally).
"""

from __future__ import annotations

import numpy as np
import pytest

from ergon_tracker.extract.sector_clf import (
    load_sector_model,
    platt_normalize,
    save_sector_model,
)
from ergon_tracker.extract.sector_features import TLD_VOCAB, assemble, cl2n


def _save(tmp_path, *, tld_vocab=TLD_VOCAB, embed_dim=3, **overrides):
    """Persist a tiny 2-class artifact; overridable to inject drift."""
    n_tld = len(TLD_VOCAB)
    feat_dim = embed_dim + n_tld
    kwargs = {
        "labels": np.array(["Fintech", "Healthcare"]),
        "mean": np.zeros(embed_dim, dtype=np.float32),
        "W": np.zeros((2, feat_dim), dtype=np.float32),
        "b": np.zeros(2, dtype=np.float32),
        "platt_a": np.ones(2, dtype=np.float32),
        "platt_b": np.zeros(2, dtype=np.float32),
        "centroids": np.zeros((2, feat_dim), dtype=np.float32),
        "tau_prob": 0.6,
        "tau_margin": 0.1,
        "tau_sim": 0.5,
        "embed_dim": embed_dim,
        "tld_vocab": tld_vocab,
    }
    kwargs.update(overrides)
    p = tmp_path / "m.npz"
    save_sector_model(p, **kwargs)
    load_sector_model.cache_clear()
    return p


# ── Guard (a): TLD-vocab / feature-width drift ────────────────────────────────────────────────


def test_matching_tld_vocab_loads(tmp_path) -> None:
    p = _save(tmp_path)
    clf = load_sector_model(p)
    assert clf is not None
    assert clf.labels == ["Fintech", "Healthcare"]


def test_tld_vocab_drift_raises(tmp_path) -> None:
    # someone reordered/edited SECTOR_TLD_GROUPS after training -> stored vocab no longer matches
    drifted = ("zzz",) + tuple(TLD_VOCAB)
    p = _save(tmp_path, tld_vocab=drifted)
    with pytest.raises(ValueError, match="TLD vocab drift"):
        load_sector_model(p)


def test_feature_width_mismatch_raises(tmp_path) -> None:
    # embed_dim claims 5 but W was built for embed_dim=3 -> width disagreement
    n_tld = len(TLD_VOCAB)
    feat_dim = 3 + n_tld
    p = tmp_path / "bad.npz"
    save_sector_model(
        p,
        labels=np.array(["Fintech", "Healthcare"]),
        mean=np.zeros(5, dtype=np.float32),
        W=np.zeros((2, feat_dim), dtype=np.float32),  # width for embed_dim=3
        b=np.zeros(2, dtype=np.float32),
        platt_a=np.ones(2, dtype=np.float32),
        platt_b=np.zeros(2, dtype=np.float32),
        centroids=np.zeros((2, feat_dim), dtype=np.float32),
        tau_prob=0.6,
        tau_margin=0.1,
        tau_sim=0.5,
        embed_dim=5,  # <-- lie
    )
    load_sector_model.cache_clear()
    with pytest.raises(ValueError, match="feature-width mismatch"):
        load_sector_model(p)


# ── Guard (b): swept coverage == live firing rate (calibration parity) ─────────────────────────


def test_swept_coverage_matches_live_firing_rate(tmp_path) -> None:
    """Regression guard for bug (b).

    Build a synthetic model + inputs, run the trainer's own sweep on the Platt-normalized transform,
    persist the swept coverage, then run inference on the same inputs. Because sweep and inference
    share ``platt_normalize``, the fraction of rows the served model fires on MUST equal the swept
    coverage. If someone reverts the trainer to sweep plain softmax (the bug), the two diverge and
    this fails.
    """
    train = pytest.importorskip(
        "scripts.train_sector_classifier", reason="run from repo root"
    )

    rng = np.random.default_rng(7)
    embed_dim, n, k = 6, 120, 3
    emb = rng.normal(size=(n, embed_dim)).astype(np.float32)
    domains = [None] * n
    _, mean = cl2n(emb)
    feats = assemble(emb, domains, mean)  # real feature path (embed_dim + TLD block)
    feat_dim = feats.shape[1]

    # A model with real structure so predictions are mostly-but-not-all correct.
    W = rng.normal(size=(k, feat_dim)).astype(np.float32)
    b = np.zeros(k, dtype=np.float32)
    platt_a = np.full(k, 1.3, dtype=np.float32)
    platt_b = np.full(k, -0.2, dtype=np.float32)
    logits = feats @ W.T + b
    probs = platt_normalize(logits, platt_a, platt_b)
    # gold labels: mostly argmax, flip ~30% so a mid threshold yields partial (not full) coverage.
    y_idx = probs.argmax(axis=1)
    flip = rng.random(n) < 0.3
    y_idx = np.where(flip, (y_idx + 1) % k, y_idx).astype(int)
    centroids = np.vstack(
        [feats[y_idx == c].mean(axis=0) if (y_idx == c).any() else np.zeros(feat_dim) for c in range(k)]
    ).astype(np.float32)

    # Sweep on the SAME transform inference serves (probs == platt_normalize(logits, ...)).
    tp, tm, ts, rep = train.sweep_thresholds(
        probs, feats, centroids, y_idx, target_precision=0.75
    )
    assert 0.0 < rep["coverage"] < 1.0  # a meaningful partial-coverage operating point

    p = tmp_path / "cal.npz"
    save_sector_model(
        p,
        labels=np.array([f"C{c}" for c in range(k)]),
        mean=mean,
        W=W,
        b=b,
        platt_a=platt_a,
        platt_b=platt_b,
        centroids=centroids,
        tau_prob=tp,
        tau_margin=tm,
        tau_sim=ts,
        embed_dim=embed_dim,
        sweep_coverage=rep["coverage"],
        sweep_precision=rep["precision"],
    )
    load_sector_model.cache_clear()
    clf = load_sector_model(p)
    assert clf is not None

    preds = clf.predict_batch(emb, domains)
    live_firing = np.mean([lab is not None for lab, _ in preds])

    # stored coverage round-trips through float32 (hence the loose rel tol here)...
    assert clf.sweep_coverage == pytest.approx(rep["coverage"], rel=1e-5)
    # ...but the live firing rate equals the swept coverage to full float64 precision, because sweep
    # and inference apply the identical ``platt_normalize`` transform to identical inputs. Reverting
    # the trainer to sweep plain softmax (bug b) would blow this gap open (~1% vs ~50%).
    assert live_firing == pytest.approx(rep["coverage"], abs=1e-9)
    assert live_firing == pytest.approx(clf.sweep_coverage, abs=1e-4)


def test_optional_sweep_keys_default_nan_when_absent(tmp_path) -> None:
    # artifact saved without sweep metrics still loads; attributes default to NaN.
    p = _save(tmp_path)  # no sweep_coverage/sweep_precision passed
    clf = load_sector_model(p)
    assert clf is not None
    assert np.isnan(clf.sweep_coverage) and np.isnan(clf.sweep_precision)
