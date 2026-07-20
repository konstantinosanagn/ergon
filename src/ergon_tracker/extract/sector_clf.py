"""Numpy-only sector classifier: load an exported .npz and reproduce its calibrated, abstaining
decision. No sklearn, no fastembed here — the caller supplies the embedding. Tolerant of a missing
artifact (returns None), mirroring ``load_sector_index``.

--------------------------------------------------------------------------------------------------
WHY THIS CLASSIFIER IS INTENTIONALLY *NOT* WIRED INTO ``enrich_in_place``
--------------------------------------------------------------------------------------------------
This model is a parked PoC. It is deliberately left un-wired: ``enrich.py`` does NOT call it and the
gazetteer-only ``SectorExtractor`` remains the sole sector source. The reason is data, not code:

  * Honest 5-fold CV precision on gazetteer-MISS rows (the only rows this model would ever decide) is
    ~29% — far below the 0.68 sector gate. Wiring it would stamp ~360k WRONG sectors onto the index.
  * Root cause is label scarcity: ~25 labeled rows/class. It would need roughly 10–50× more labels
    to reach an honest CV precision of ~55–60% before wiring is worth revisiting.
  * The gazetteer ``SectorExtractor`` is precision-first: it emits "unknown" rather than guess. That
    is the correct current behavior; a low-precision guesser is strictly worse than an honest blank.

Do NOT wire this into the enrich path until an honest held-out precision clears the gate. The guards
below (TLD-vocab drift, embed-dim, swept-vs-served calibration parity) exist so that IF someone wires
it later they fail LOUD on a stale artifact instead of silently corrupting sectors.

NOTE: any committed ``dist/sector_clf.npz`` must be regenerated in CI with the *current* trainer
before it could ever be served — older artifacts were tuned under a mismatched calibration transform
(see ``scripts/train_sector_classifier.py`` and ``platt_normalize`` below).
--------------------------------------------------------------------------------------------------
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

# Keys always present in a well-formed artifact.
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
# Optional keys (added later; older artifacts may lack them). Default to NaN when absent.
_OPTIONAL_KEYS = (
    "sweep_coverage",
    "sweep_precision",
)


def platt_normalize(  # noqa: UP037
    scores: np.ndarray,
    platt_a: np.ndarray,
    platt_b: np.ndarray,
) -> "np.ndarray":  # noqa: UP037
    """Per-class Platt sigmoid on decision scores/logits, then renormalize across classes.

    This is the SINGLE source of truth for the probability transform. Both the offline trainer's
    threshold sweep and this runtime inference call it, so the coverage the sweep reports is the
    coverage the served model actually fires at. Keeping these identical is bug-(b)'s fix: previously
    the trainer swept plain softmax while inference served this Platt-normalized distribution, so a
    threshold tuned for ~1% coverage fired on 50–70% of live rows.
    """
    import numpy as np

    cal = 1.0 / (1.0 + np.exp(-(platt_a * scores + platt_b)))
    return (cal / cal.sum(axis=1, keepdims=True)).astype(np.float32)


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
        # Coverage/precision the trainer's sweep reported for this artifact (NaN if not persisted).
        # These let a caller regression-check the live firing rate against the tuned coverage.
        self.sweep_coverage = float(data.get("sweep_coverage", float("nan")))
        self.sweep_precision = float(data.get("sweep_precision", float("nan")))

        # ── Guard (a): TLD-vocab drift ────────────────────────────────────────────────────────
        # ``W`` was trained on a feature layout whose TLD one-hot block order/size comes from
        # ``TLD_VOCAB`` (a sorted view of ``SECTOR_TLD_GROUPS``). If someone edits the groups, the
        # runtime TLD block silently reorders/resizes while ``W`` still expects the old layout —
        # corrupt predictions, no error. Fail loud instead.
        stored_vocab = tuple(str(x) for x in data["tld_vocab"])
        if stored_vocab != tuple(TLD_VOCAB):
            raise ValueError(
                "sector_clf.npz TLD vocab drift — retrain: "
                f"stored tld_vocab={stored_vocab} != current TLD_VOCAB={tuple(TLD_VOCAB)}"
            )
        # ── Guard: embedding-width / feature-width consistency ────────────────────────────────
        # Feature width = embedding dims + the TLD one-hot block. A mismatch means the artifact's
        # ``embed_dim`` disagrees with the weight matrix it shipped with.
        expected_feat_dim = self.embed_dim + len(TLD_VOCAB)
        if self.W.shape[1] != expected_feat_dim:
            raise ValueError(
                "sector_clf.npz feature-width mismatch — retrain: "
                f"W.shape[1]={self.W.shape[1]} != embed_dim({self.embed_dim}) + "
                f"len(TLD_VOCAB)({len(TLD_VOCAB)}) = {expected_feat_dim}"
            )

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
        # SAME transform the trainer sweeps on (see ``platt_normalize``) — keeps swept coverage and
        # live firing rate identical; this is the served side of bug-(b)'s fix.
        probs = platt_normalize(logits, self.platt_a, self.platt_b)
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
    sweep_coverage: float = float("nan"),
    sweep_precision: float = float("nan"),
) -> None:
    """Persist the model as a compressed .npz (auto-ships under registry/data if placed there).

    ``sweep_coverage``/``sweep_precision`` record what the trainer's threshold sweep reported for
    the persisted (tau_prob, tau_margin, tau_sim). Because the sweep and inference now share
    ``platt_normalize``, a served model's firing rate should match ``sweep_coverage`` — the
    regression guard for bug (b).
    """
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
        sweep_coverage=np.float32(sweep_coverage),
        sweep_precision=np.float32(sweep_precision),
    )


@lru_cache(maxsize=2)
def load_sector_model(path: "str | Path") -> SectorClassifier | None:  # noqa: UP037
    """Load an exported model; return None if the file is absent (Tier-2 then simply doesn't fire)."""
    import numpy as np

    p = Path(path)
    if not p.exists():
        return None
    with np.load(p, allow_pickle=False) as data:
        payload: dict[str, Any] = {k: data[k] for k in _KEYS}
        for k in _OPTIONAL_KEYS:  # tolerate older artifacts lacking the sweep-metric keys
            if k in data.files:
                payload[k] = data[k]
        return SectorClassifier(payload)
