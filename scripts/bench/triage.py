"""Agreement classification + the triage-ordered human audit queue (bench v2).

Compares an extractor prediction (``scripts.bench.predict.predict``'s output shape, per field in
``scripts.bench.schema.FIELDS``) against the fleet's gold label for the same row (a Task-5
``labels.jsonl`` record: ``{id, votes: {field: [...]}, gold: {field: value}, split: {field: bool}}``)
and orders the resulting per-field findings into a queue for human adjudication:

1. conflicts       -- both sides known, values disagree.
2. coverage gaps    -- exactly one side is unknown (extractor missed it, or over-claimed).
3. fleet-split      -- the labeling fleet itself failed to reach a majority on this field (the
                       gold value is unreliable regardless of what the extractor said).
4. calibration      -- a fixed-size, deterministic sample of agreements, so a human can spot-check
                       the "nothing to see here" rows too.

Rows/fields where both sides are unknown ("na") carry no signal and are never queued.
"""

from __future__ import annotations

from typing import Any

from .schema import FIELDS

__all__ = ["agreement", "build_queue"]

# Field-type groupings drive which comparator ``agreement()`` uses.
_NUMERIC_TOLERANCE_FIELDS = {"salary"}
_EXACT_RANGE_FIELDS = {"yoe"}
_BOOL_FIELDS = {"remote", "sponsorship", "visa_sponsor"}

_SALARY_TOLERANCE = 0.05  # 5%
_UNKNOWN_STRINGS = {"unknown", "n/a", "na", ""}


def _is_unknown(value: Any, field: str) -> bool:
    """Whether ``value`` (a single field's value, from either pred or gold) counts as "not
    present" for triage purposes."""
    if value is None:
        return True
    if field in _NUMERIC_TOLERANCE_FIELDS or field in _EXACT_RANGE_FIELDS:
        # predict()/labels always collapse a fully-empty range/salary to None; a non-None dict
        # here means at least one side (min/max) is populated.
        return False
    if field in _BOOL_FIELDS:
        return False
    if isinstance(value, str):
        return value.strip().lower() in _UNKNOWN_STRINGS
    return False


def _norm_categorical(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _salary_agrees(pred: Any, gold: Any) -> bool:
    for key in ("min", "max"):
        pv, gv = pred.get(key), gold.get(key)
        if pv is None and gv is None:
            continue
        if pv is None or gv is None:
            return False
        base = max(abs(pv), abs(gv), 1)
        if abs(pv - gv) / base > _SALARY_TOLERANCE:
            return False
    pc, gc = pred.get("currency"), gold.get("currency")
    return not (pc and gc and str(pc).strip().lower() != str(gc).strip().lower())


def _yoe_agrees(pred: Any, gold: Any) -> bool:
    return bool(pred.get("min") == gold.get("min") and pred.get("max") == gold.get("max"))


def agreement(pred: dict[str, Any], gold: dict[str, Any], field: str) -> str:
    """Classify one ``field`` of one row: ``"agree"``, ``"conflict"``, ``"coverage"``, or
    ``"na"``, comparing ``pred[field]`` (extractor) against ``gold[field]`` (fleet majority)."""
    p, g = pred.get(field), gold.get(field)
    p_unknown, g_unknown = _is_unknown(p, field), _is_unknown(g, field)

    if p_unknown and g_unknown:
        return "na"
    if p_unknown != g_unknown:
        return "coverage"

    if field in _NUMERIC_TOLERANCE_FIELDS:
        return "agree" if _salary_agrees(p, g) else "conflict"
    if field in _EXACT_RANGE_FIELDS:
        return "agree" if _yoe_agrees(p, g) else "conflict"
    if field in _BOOL_FIELDS:
        return "agree" if bool(p) == bool(g) else "conflict"
    return "agree" if _norm_categorical(p) == _norm_categorical(g) else "conflict"


def build_queue(
    rows: list[dict[str, Any]],
    preds: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    *,
    calib: int = 100,
) -> list[dict[str, Any]]:
    """The triage-ordered human audit queue: conflicts, then coverage gaps, then fleet-split
    fields, then a fixed-size deterministic sample of agreements for calibration.

    ``rows``, ``preds``, ``labels`` are parallel, same-length, positionally matched (one row's
    corpus record / extractor prediction / Task-5 label record per index). Rows where every field
    agrees are never queued except via the calibration sample.
    """
    conflicts: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    fleet_split: list[dict[str, Any]] = []
    agreements: list[dict[str, Any]] = []

    for row, pred, label in zip(rows, preds, labels, strict=True):
        rid = row.get("id", label.get("id", ""))
        url = row.get("url") or row.get("apply_url") or ""
        gold = label.get("gold") or {}
        split = label.get("split") or {}

        for field in FIELDS:
            item = {
                "id": rid,
                "field": field,
                "extractor_value": pred.get(field),
                "fleet_value": gold.get(field),
                "url": url,
            }
            if split.get(field):
                fleet_split.append({**item, "reason": "fleet-split"})
                continue

            cls = agreement(pred, gold, field)
            if cls == "conflict":
                conflicts.append({**item, "reason": "conflict"})
            elif cls == "coverage":
                coverage.append({**item, "reason": "coverage"})
            elif cls == "agree":
                agreements.append({**item, "reason": "agree"})
            # "na": both sides unknown -- nothing to adjudicate, never queued.

    # Deterministic calibration sample: the first `calib` agreements in input order (row-major,
    # then FIELDS order). No randomness/wall-clock so the queue is reproducible run to run.
    calibration = [{**a, "reason": "calibration"} for a in agreements[:calib]]

    return conflicts + coverage + fleet_split + calibration
