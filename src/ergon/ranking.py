"""Relevance ranking for search results (deterministic, dependency-free).

After fetch + filter + dedup, results arrive in *source order* — which has nothing to do
with how well a posting matches the user's query. Without ranking, a search for ``"engineer"``
can surface an "Account Executive" role just because its *description* happens to mention
engineering. This module fixes precision while preserving recall: every candidate that passed
the filter is kept, but they are ordered by how well they actually match.

Design (deterministic-first, no ML, runs anywhere):

* **Field-weighted BM25 (BM25F-style).** Each field (title, department, company, description)
  is scored with its own Okapi BM25 — its own length normalization and IDF — and the per-field
  scores are combined as a weighted sum, with the title weighted far above the rest. Because a
  short title saturates fast and a long description normalizes against long descriptions, a
  title match dominates a description-only match, and keyword-stuffing the description can't
  masquerade as a title hit. BM25's k1/b give term-frequency saturation and length norm.
* **Stable.** Ties keep their prior order (authority/recency from dedup), so ranking only ever
  *improves* ordering, never scrambles equally-relevant results.

A pluggable :data:`Reranker` seam lets a stronger model (e.g. a cross-encoder such as ZeroEntropy
``zerank``) reorder the top-K later, installed as an optional extra — without touching the core.
By default no reranker is registered and ranking is pure lexical BM25.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .models import JobPosting

__all__ = ["rank", "score_text", "Reranker", "register_reranker"]

# BM25 hyperparameters (standard defaults).
_K1 = 1.5
_B = 0.75

# Per-field weights applied to each field's own BM25 score. Title dominates.
_FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("title", 5.0),
    ("department", 2.0),
    ("company", 2.0),
    ("description_text", 1.0),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _field_tokens(job: JobPosting, field: str) -> list[str]:
    return _tokenize(getattr(job, field) or "")


def _bm25_field(q_terms: set[str], field_docs: list[list[str]]) -> list[float]:
    """Okapi BM25 score per doc for one field, computed over that field's own corpus stats."""
    n = len(field_docs)
    tfs = [Counter(d) for d in field_docs]
    doc_len = [len(d) for d in field_docs]
    nonempty = [dl for dl in doc_len if dl] or [1]
    avgdl = sum(nonempty) / len(nonempty)
    df = {t: sum(1 for tf in tfs if t in tf) for t in q_terms}
    idf = {t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms}

    scores: list[float] = []
    for i in range(n):
        tf = tfs[i]
        dl = doc_len[i] or 1
        s = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if not f:
                continue
            denom = f + _K1 * (1.0 - _B + _B * dl / avgdl)
            s += idf[t] * (f * (_K1 + 1.0)) / denom
        scores.append(s)
    return scores


# --------------------------------------------------------------------------------------------
# Pluggable reranker seam (e.g. a cross-encoder like ZeroEntropy zerank). Optional; off by
# default. A backend implements `rerank(query, jobs) -> list[float]` returning a score per job.
# --------------------------------------------------------------------------------------------
@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, jobs: list[JobPosting]) -> list[float]:
        """Return a relevance score per job (same order/length as ``jobs``)."""
        ...


_RERANKER: Reranker | None = None


def register_reranker(reranker: Reranker | None) -> None:
    """Install (or clear, with ``None``) a stronger reranker used after lexical BM25.

    Optional extras (e.g. a zerank backend) call this to plug in a cross-encoder. The core
    never imports a model; this keeps the default install light and fully deterministic.
    """
    global _RERANKER
    _RERANKER = reranker


def score_text(query: str, jobs: list[JobPosting]) -> list[float]:
    """Compute field-weighted BM25 scores for ``jobs`` against ``query`` (one score per job).

    Each field is scored with its own BM25 over the candidate set, then combined as a weighted
    sum (title weighted highest). Pure function; no network, no model. Empty query -> all zeros.
    """
    q_terms = set(_tokenize(query))
    n = len(jobs)
    if not q_terms or n == 0:
        return [0.0] * n

    totals = [0.0] * n
    for field, weight in _FIELD_WEIGHTS:
        field_docs = [_field_tokens(j, field) for j in jobs]
        if not any(field_docs):
            continue
        field_scores = _bm25_field(q_terms, field_docs)
        for i, s in enumerate(field_scores):
            totals[i] += weight * s
    return totals


def rank(
    jobs: list[JobPosting], query: str | None, *, reranker: Reranker | None = None
) -> list[JobPosting]:
    """Return ``jobs`` ordered by relevance to ``query`` (descending), setting ``job.score``.

    Stable: equally-scored jobs keep their incoming order (authority/recency from dedup). With
    no query (or no jobs) the list is returned unchanged and scores are left as-is. A reranker
    (the ``reranker`` argument, else the process-wide one from :func:`register_reranker`)
    reorders the lexical top-K for a final precision pass.
    """
    if not query or len(jobs) <= 1:
        return jobs

    scores = score_text(query, jobs)
    for job, sc in zip(jobs, scores, strict=True):
        job.score = sc

    # Optional stronger reranker over the lexical top-K (kept small for cost). A per-call
    # reranker wins over the global default so concurrent searches never share mutable state.
    active = reranker if reranker is not None else _RERANKER
    if active is not None:
        top_k = min(len(jobs), 100)
        order = sorted(range(len(jobs)), key=lambda i: scores[i], reverse=True)
        head_idx = order[:top_k]
        head_jobs = [jobs[i] for i in head_idx]
        try:
            re_scores = active.rerank(query, head_jobs)
            for job, sc in zip(head_jobs, re_scores, strict=True):
                job.score = sc
        except Exception:  # noqa: BLE001 - a reranker failure must not break search
            pass

    # Stable sort by score desc (Python sort is stable, so ties keep prior order).
    return sorted(jobs, key=lambda j: j.score if j.score is not None else 0.0, reverse=True)
