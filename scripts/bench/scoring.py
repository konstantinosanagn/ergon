"""Metrics engine: per-field accuracy/precision/recall/coverage + Wilson confidence intervals,
and the per-provider breakdown (bench v2, Task 9).

Row shape consumed by ``score_field``/``provider_matrix`` mirrors the ``predict()``-shaped
prediction dict and Task-5-shaped fleet-gold dict used throughout the pipeline (``triage.py``,
``resolve_gold.py``): each row is

    {"source": <str>, "gold": {field: value, ...}, "pred": {field: value, ...}}

i.e. ``row["gold"]`` and ``row["pred"]`` are full per-field dicts keyed by
``scripts.bench.schema.FIELDS`` (the same shape as a Task-5 label's ``gold`` and a ``predict()``
output). In production this row is assembled from a Task-8 ``resolve()`` output by taking, per
field, ``{"value": ...}`` as gold and ``{"extractor_value": ...}`` as pred; scoring itself only
ever needs the (gold, pred) pair for one field at a time and is agnostic to ``review_state``.

``agreement()`` (``triage.py``) is reused as the ONLY value-equality oracle, so scoring and triage
always agree on what "correct" means (numeric-tolerance for salary, exact-range for yoe,
bool-exact for remote/sponsorship/visa_sponsor, null-aware categorical-exact otherwise).

Metric definitions (load-bearing for later tasks and the report — do not silently change):

- ``coverage`` = fraction of rows where GOLD is stated (non-null/non-"unknown" for that field).
  Pure data-availability; independent of what the extractor did.
- ``accuracy`` = restricted to the gold-stated (covered) rows, the fraction the extractor got
  right (``agreement(pred, gold, field) == "agree"``). This is value-accuracy computed ONLY on the
  covered slice — a field with high nulls but perfect extraction on the rows it does have gold for
  shows high accuracy and low coverage, never low accuracy from missing gold.
- ``recall`` = same covered-slice framing as accuracy, restated in stated/detection terms: of the
  rows where gold is stated, the fraction the extractor both asserted a value for AND got right.
  Because "got it right" already requires the extractor to have asserted a (matching) value,
  recall and accuracy are numerically identical by construction — accuracy is the headline
  "value correctness" framing, recall is the "detection completeness" framing paired with
  precision. Both are TP / n_covered where TP = rows with gold stated, extractor asserted, and
  values agree.
- ``precision`` = restricted to rows where the EXTRACTOR asserted a value (predicted non-null/
  non-"unknown"), the fraction that were correct (TP / n_asserted). A prediction made where gold
  is unstated cannot be verified and counts against precision (not toward it) since there's no
  gold to confirm it — it is never counted as a coverage or accuracy miss.
- ``ci`` = ``wilson_ci(TP, n_covered)`` — the confidence interval around ``accuracy``/``recall``.

TP (true positives) never counts a row where gold is unstated, so coverage and precision are
always independent: coverage never penalizes precision, and precision never inflates coverage.
"""

from __future__ import annotations

import math
from typing import Any

from .triage import agreement

__all__ = ["wilson_ci", "score_field", "provider_matrix"]


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials at confidence implied by ``z``
    (default ``z=1.96`` -> ~95%). ``n == 0`` -> ``(0.0, 0.0)`` (no data, no interval)."""
    if n == 0:
        return (0.0, 0.0)

    p_hat = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = p_hat + z2 / (2 * n)
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z2 / (4 * n)) / n)

    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def _is_stated(value: Any, field: str) -> bool:
    """Whether ``value`` counts as a stated (non-null/non-"unknown") value for ``field``, reusing
    ``agreement`` as the sole oracle: comparing a value against itself is "na" iff that value is
    unstated for this field's type (see ``triage._is_unknown``'s null-aware rules per field type),
    and otherwise always "agree" (any value trivially equals itself)."""
    return agreement({field: value}, {field: value}, field) != "na"


def score_field(field: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-field metrics over ``rows`` (each ``{"source", "gold", "pred"}``, see module docstring
    for the exact shape): ``{n, accuracy, precision, recall, coverage, ci}``."""
    n = len(rows)
    n_covered = 0
    n_asserted = 0
    n_correct = 0

    for row in rows:
        gold, pred = row["gold"], row["pred"]
        gold_value, pred_value = gold.get(field), pred.get(field)

        gold_stated = _is_stated(gold_value, field)
        pred_stated = _is_stated(pred_value, field)
        correct = agreement(pred, gold, field) == "agree"

        if gold_stated:
            n_covered += 1
        if pred_stated:
            n_asserted += 1
        if correct:
            n_correct += 1

    coverage = n_covered / n if n else 0.0
    accuracy = n_correct / n_covered if n_covered else 0.0
    recall = accuracy
    precision = n_correct / n_asserted if n_asserted else 0.0
    ci = wilson_ci(n_correct, n_covered)

    return {
        "n": n,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "coverage": coverage,
        "ci": ci,
    }


def provider_matrix(field: str, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``score_field`` broken out per ``row["source"]`` — the headline per-ATS diagnostic."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(row.get("source", ""), []).append(row)
    return {source: score_field(field, source_rows) for source, source_rows in by_source.items()}
