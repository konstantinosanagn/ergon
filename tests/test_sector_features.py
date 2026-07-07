from __future__ import annotations

import numpy as np  # noqa: E402

from ergon_tracker.extract.sector_features import (
    TLD_VOCAB,
    assemble,
    build_input_text,
    cl2n,
    tld_features,
)


def test_input_text_combines_name_domain_title() -> None:
    # domain contributes its registrable label WITHOUT the TLD (the TLD is a separate feature).
    txt = build_input_text("Acme Corp", "careers.acme-bank.com", "Senior Risk Analyst")
    assert "Acme Corp" in txt
    assert "acme-bank" in txt
    assert ".com" not in txt
    assert "Senior Risk Analyst" in txt


def test_input_text_tolerates_missing_fields() -> None:
    assert build_input_text(None, None, None) == ""
    assert build_input_text("Acme", None, None) == "Acme"


def test_tld_features_are_fixed_width_and_grouped() -> None:
    vec = tld_features("foo.ai")
    assert len(vec) == len(TLD_VOCAB)
    assert sum(vec) == 1.0  # .ai lights exactly its group
    assert tld_features(None) == [0.0] * len(TLD_VOCAB)  # no domain → all-zero
    assert tld_features("foo.unknown-tld") == [0.0] * len(TLD_VOCAB)


def test_cl2n_centers_then_unit_normalizes() -> None:
    mat = np.array([[3.0, 0.0], [0.0, 4.0], [1.0, 1.0]], dtype=np.float32)
    out, mean = cl2n(mat)
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)  # rows are unit
    np.testing.assert_allclose(mean, mat.mean(axis=0), atol=1e-5)  # mean is the column mean


def test_cl2n_reuses_supplied_mean() -> None:
    train = np.array([[2.0, 0.0], [0.0, 2.0]], dtype=np.float32)
    _, mean = cl2n(train)
    test = np.array([[5.0, 1.0]], dtype=np.float32)
    out, mean2 = cl2n(test, mean)
    np.testing.assert_allclose(mean2, mean)  # supplied mean is passed through, not recomputed
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)


def test_assemble_appends_tld_onehot() -> None:
    emb = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)  # embed_dim=4
    _, mean = cl2n(emb)
    feats = assemble(emb, ["foo.ai"], mean)
    assert feats.shape == (1, 4 + len(TLD_VOCAB))
    assert feats.dtype == np.float32
    assert feats[0, 4:].sum() == 1.0  # the appended TLD block is the one-hot
