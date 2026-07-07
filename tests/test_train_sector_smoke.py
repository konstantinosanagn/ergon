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
