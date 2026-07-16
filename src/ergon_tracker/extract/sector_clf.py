"""Numpy-only sector classifier: load an exported .npz and reproduce its calibrated, abstaining
decision. No sklearn, no fastembed here — the caller supplies the embedding. Tolerant of a missing
artifact (returns None), mirroring ``load_sector_index``.
"""
# noqa: UP037
# Quoted annotations are intentional: numpy is lazy-imported inside functions, so annotations
# can't reference np directly. All annotation strings are evaluated later at runtime.

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .sector_features import TLD_VOCAB, tld_features

if TYPE_CHECKING:
    import numpy as np

_KEYS = (
    "labels",
    "mean",
    "W",
    "b",
    "platt_a",
    "platt_b",
    "centroids",
    "tau_prob",
    "tau_margin",
    "tau_sim",
    "tld_vocab",
    "embed_dim",
)


class SectorClassifier:
    """Holds the exported arrays and applies the decision rule with numpy."""

    def __init__(self, data: dict[str, Any]) -> None:
        import numpy as np

        self.labels: list[str] = [str(x) for x in data["labels"]]
        self.mean = np.asarray(data["mean"], dtype=np.float32)
        self.W = np.asarray(data["W"], dtype=np.float32)
        self.b = np.asarray(data["b"], dtype=np.float32)
        self.platt_a = np.asarray(data["platt_a"], dtype=np.float32)
        self.platt_b = np.asarray(data["platt_b"], dtype=np.float32)
        self.centroids = np.asarray(data["centroids"], dtype=np.float32)
        self.tau_prob = float(data["tau_prob"])
        self.tau_margin = float(data["tau_margin"])
        self.tau_sim = float(data["tau_sim"])
        self.embed_dim = int(data["embed_dim"])
        # centroid norms precomputed for the cosine gate
        self._cnorm = np.linalg.norm(self.centroids, axis=1)
        self._cnorm[self._cnorm == 0] = 1.0

    def _features(  # noqa: UP037
        self,
        embeddings: np.ndarray,
        domains: "list[str | None]",  # noqa: UP037
    ) -> "np.ndarray":  # noqa: UP037
        import numpy as np

        x = np.asarray(embeddings, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        c = x - self.mean
        n = np.linalg.norm(c, axis=1, keepdims=True)
        n[n == 0] = 1.0
        normed = c / n
        tld = np.asarray([tld_features(d) for d in domains], dtype=np.float32)
        return np.hstack([normed, tld]).astype(np.float32)

    def predict_batch(  # noqa: UP037
        self,
        embeddings: np.ndarray,
        domains: "list[str | None]",  # noqa: UP037
    ) -> "list[tuple[str | None, float]]":  # noqa: UP037
        import numpy as np

        feats = self._features(embeddings, domains)
        logits = feats @ self.W.T + self.b  # (n, n_classes)
        cal = 1.0 / (1.0 + np.exp(-(self.platt_a * logits + self.platt_b)))
        probs = cal / cal.sum(axis=1, keepdims=True)
        order = np.argsort(-probs, axis=1)
        top1 = order[:, 0]
        p1 = probs[np.arange(len(probs)), top1]
        p2 = (
            probs[np.arange(len(probs)), order[:, 1]]
            if probs.shape[1] > 1
            else np.zeros(len(probs))
        )
        # cosine(features, chosen centroid)
        fnorm = np.linalg.norm(feats, axis=1)
        fnorm[fnorm == 0] = 1.0
        sim = np.einsum("ij,ij->i", feats, self.centroids[top1]) / (fnorm * self._cnorm[top1])
        out: list[tuple[str | None, float]] = []
        for i in range(len(probs)):
            ok = (
                p1[i] >= self.tau_prob
                and (p1[i] - p2[i]) >= self.tau_margin
                and sim[i] >= self.tau_sim
            )
            out.append((self.labels[top1[i]] if ok else None, float(p1[i])))
        return out

    def predict(  # noqa: UP037
        self,
        embedding: np.ndarray,
        domain: str | None,  # noqa: UP037
    ) -> "tuple[str | None, float]":  # noqa: UP037
        return self.predict_batch(embedding, [domain])[0]


def save_sector_model(  # noqa: UP037
    path: "str | Path",  # noqa: UP037
    *,
    labels: Any,
    mean: Any,
    W: Any,
    b: Any,
    platt_a: Any,
    platt_b: Any,
    centroids: Any,
    tau_prob: float,
    tau_margin: float,
    tau_sim: float,
    embed_dim: int,
    tld_vocab: tuple[str, ...] = TLD_VOCAB,
) -> None:
    """Persist the model as a compressed .npz (auto-ships under registry/data if placed there)."""
    import numpy as np

    np.savez_compressed(
        path,
        labels=np.asarray(labels),
        mean=np.asarray(mean, dtype=np.float32),
        W=np.asarray(W, dtype=np.float32),
        b=np.asarray(b, dtype=np.float32),
        platt_a=np.asarray(platt_a, dtype=np.float32),
        platt_b=np.asarray(platt_b, dtype=np.float32),
        centroids=np.asarray(centroids, dtype=np.float32),
        tau_prob=np.float32(tau_prob),
        tau_margin=np.float32(tau_margin),
        tau_sim=np.float32(tau_sim),
        tld_vocab=np.asarray(list(tld_vocab)),
        embed_dim=np.int64(embed_dim),
    )


@lru_cache(maxsize=2)
def load_sector_model(path: "str | Path") -> SectorClassifier | None:  # noqa: UP037
    """Load an exported model; return None if the file is absent (Tier-2 then simply doesn't fire)."""
    import numpy as np

    p = Path(path)
    if not p.exists():
        return None
    with np.load(p, allow_pickle=False) as data:
        return SectorClassifier({k: data[k] for k in _KEYS})
