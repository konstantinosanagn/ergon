"""Semantic reranking (Layer 2) — optional, embeddings-based.

Lexical BM25 (the default :mod:`ergon_tracker.ranking`) matches *tokens*. It can't tell that
"ML engineer" and "Machine Learning" mean the same thing, or rank a role for the *meaning* of
"fintech using Rust, remote-friendly". This module adds that, using small CPU embedding models
via `fastembed` (ONNX) — no GPU, no API, fully local. It is an **opt-in extra**::

    pip install 'ergon-tracker[semantic]'

and is engaged per search with ``semantic=True``. It implements the
:class:`ergon_tracker.ranking.Reranker` protocol, so it plugs into the existing ranking seam:
lexical BM25 narrows to a top-K candidate set, then embeddings reorder it by cosine similarity.

The model (~130 MB) downloads once on first use and is cached by `fastembed`. The model is
loaded lazily and memoized, so importing this module never downloads anything.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .config import get_env

if TYPE_CHECKING:
    from .models import JobPosting

__all__ = ["SemanticReranker", "get_semantic_reranker", "DEFAULT_MODEL"]

# Default: BAAI/bge-small-en-v1.5 — ~67 MB on disk, 384-dim. fastembed already ships it as a
# quantized ONNX build (quantization is on by default — that's the whole point of fastembed over
# sentence-transformers/PyTorch), so there is no separate "quantize it ourselves" step.
# Two free, no-accuracy-loss tuning knobs are exposed via env / .env instead:
#   ERGON_SEMANTIC_MODEL   — swap in a smaller/quantized variant (e.g. a *Q model) or a bigger one
#   ERGON_SEMANTIC_THREADS — ONNX Runtime intra-op threads (cap CPU use, or raise for throughput)
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


def _default_model() -> str:
    return get_env("ERGON_SEMANTIC_MODEL") or DEFAULT_MODEL


# How much of each posting to embed. Title carries the most signal; we prepend it and add a
# bounded slice of the description so long postings don't dominate or slow things down.
_DESC_CHARS = 600


def _job_text(job: JobPosting) -> str:
    parts = [job.title or ""]
    if job.department:
        parts.append(job.department)
    if job.description_text:
        parts.append(job.description_text[:_DESC_CHARS])
    return " — ".join(p for p in parts if p)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SemanticReranker:
    """Embeddings reranker: scores jobs by cosine similarity to the query.

    Implements the ``Reranker`` protocol (``rerank(query, jobs) -> list[float]``). The embedding
    model is created lazily on first ``rerank`` call so constructing this object is cheap.
    """

    def __init__(self, model_name: str | None = None, threads: int | None = None) -> None:
        self.model_name = model_name or _default_model()
        self.threads = threads
        self._model: Any = None  # set lazily to a fastembed TextEmbedding in _ensure_model

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError(
                "Semantic search needs the optional extra: pip install 'ergon-tracker[semantic]'"
            ) from exc
        kwargs: dict[str, Any] = {"model_name": self.model_name}
        threads = self.threads if self.threads is not None else get_env("ERGON_SEMANTIC_THREADS")
        if threads:
            kwargs["threads"] = int(threads)
        self._model = TextEmbedding(**kwargs)

    def rerank(self, query: str, jobs: list[JobPosting]) -> list[float]:
        if not jobs:
            return []
        self._ensure_model()
        assert self._model is not None
        texts = [_job_text(j) for j in jobs]
        # fastembed returns numpy arrays; tolist() keeps this dependency-light (no numpy import).
        query_vec = next(iter(self._model.embed([query]))).tolist()
        doc_vecs = [v.tolist() for v in self._model.embed(texts)]
        return [_cosine(query_vec, dv) for dv in doc_vecs]


@lru_cache(maxsize=4)
def get_semantic_reranker(model_name: str | None = None) -> SemanticReranker:
    """Return a memoized :class:`SemanticReranker` (one model load per process, per model name).

    ``model_name=None`` resolves to ``ERGON_SEMANTIC_MODEL`` or :data:`DEFAULT_MODEL`. Raises
    ``ImportError`` with install guidance the first time it embeds if the ``semantic`` extra is
    not installed.
    """
    return SemanticReranker(model_name)
