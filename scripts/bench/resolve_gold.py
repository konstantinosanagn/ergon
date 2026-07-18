"""Fuse fleet labels, extractor predictions, and human corrections into resolved gold
(bench v2, Task 8).

Three inputs, one gold value per (row, field):
  - labels: Task-5 fleet labels, one record per row -- ``{id, gold: {field: value}, split:
    {field: bool}}``.
  - preds: ``scripts.bench.predict.predict()`` output, one dict per row (parallel to ``labels``,
    matched positionally -- same pairing convention used throughout the pipeline, e.g.
    ``triage.build_queue``).
  - corrections: Task-7 human-auditor verdicts, records ``{id, field, verdict:
    "extractor"|"fleet"|"correct", value?, note?}``.

Precedence per (row, field):
  1. A human correction wins if one exists for that (id, field): verdict "correct" uses its
     ``value``, "extractor" uses the prediction, "fleet" uses the fleet gold. -> review_state
     "human".
  2. Else, if the extractor and fleet AGREE (``triage.agreement`` returns "agree"), auto-accept
     that value. -> review_state "auto".
  3. Else, fall back to the fleet-majority gold. review_state is "unreviewed" when this was a real
     disagreement nobody adjudicated (``agreement`` classified it "conflict"/"coverage", or the
     fleet itself split on this field) -- it flags a field that needed a human look and never got
     one. review_state is "fleet" when there was nothing to review at all (``agreement`` ==
     "na": both extractor and fleet are unknown, so the fleet value -- None -- is trivially
     correct-by-absence).

``resolve()`` keeps ``extractor_value``/``fleet_value`` alongside each resolved field so
``calibration_stats()`` can recompute what the PRE-correction state would have been (an
auto-accepted agreement, or a fleet-fallback) without needing the original labels/preds again.
"""

from __future__ import annotations

from typing import Any

from .schema import FIELDS
from .triage import agreement

__all__ = ["resolve", "calibration_stats"]


def _index_corrections(corrections: list[dict[str, Any]]) -> dict[tuple[Any, str], dict[str, Any]]:
    """(id, field) -> the correction record. Later entries win on a duplicate key (last verdict
    filed stands)."""
    out: dict[tuple[Any, str], dict[str, Any]] = {}
    for c in corrections:
        out[(c["id"], c["field"])] = c
    return out


def resolve(
    labels: list[dict[str, Any]],
    preds: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fuse ``labels`` (fleet), ``preds`` (extractor, parallel/positionally matched to
    ``labels``), and ``corrections`` (human) into one resolved row per input row.

    Each returned row is ``{"id": ..., "fields": {field: {"value", "review_state",
    "extractor_value", "fleet_value"}}}``, one entry per ``scripts.bench.schema.FIELDS``.
    """
    by_key = _index_corrections(corrections)
    resolved: list[dict[str, Any]] = []

    for label, pred in zip(labels, preds, strict=True):
        rid = label["id"]
        gold = label.get("gold") or {}
        split = label.get("split") or {}
        fields: dict[str, Any] = {}

        for field in FIELDS:
            extractor_value = pred.get(field)
            fleet_value = gold.get(field)
            correction = by_key.get((rid, field))

            # Only the three adjudication verdicts set gold. A non-adjudication verdict
            # ("comment"/"skip"/"ambiguous" from the auditor) carries just a note/observation, not a
            # decision -- treat it as if there were no correction and fall through to the automatic
            # resolution below, so a comment-only record can never crash resolve.
            adjudicated = correction is not None and correction.get("verdict") in (
                "correct",
                "extractor",
                "fleet",
            )
            if adjudicated:
                assert correction is not None
                verdict = correction["verdict"]
                if verdict == "correct":
                    value = correction.get("value")
                elif verdict == "extractor":
                    value = extractor_value
                else:  # "fleet"
                    value = fleet_value
                review_state = "human"
            elif split.get(field):
                value = fleet_value
                review_state = "unreviewed"
            else:
                cls = agreement(pred, gold, field)
                if cls == "agree":
                    value = extractor_value
                    review_state = "auto"
                elif cls == "na":
                    value = fleet_value
                    review_state = "fleet"
                else:  # "conflict" or "coverage"
                    value = fleet_value
                    review_state = "unreviewed"

            fields[field] = {
                "value": value,
                "review_state": review_state,
                "extractor_value": extractor_value,
                "fleet_value": fleet_value,
            }

        resolved.append({"id": rid, "fields": fields})

    return resolved


def _was_pre_correction_agreement(entry: dict[str, Any], field: str) -> bool:
    """Whether, ignoring any human correction, ``triage.agreement`` would have classified this
    field's extractor/fleet pair as ``"agree"`` (i.e. it would have auto-accepted)."""
    synth_pred = {field: entry["extractor_value"]}
    synth_gold = {field: entry["fleet_value"]}
    return agreement(synth_pred, synth_gold, field) == "agree"


def calibration_stats(
    resolved: list[dict[str, Any]], corrections: list[dict[str, Any]]
) -> dict[str, Any]:
    """Label-quality calibration over the human-audited slice (every record in ``corrections``).

    For each correction, recompute what the PRE-correction resolution would have been (an
    auto-accepted agreement, or a fleet-fallback) from the resolved row's stored
    extractor_value/fleet_value, then compare against the human's verdict:

    - "confirmed": the human's verdict matches the pre-correction value. For an auto-accepted
      agreement, extractor_value == fleet_value already, so either an "extractor" or "fleet"
      verdict confirms it (only "correct" overturns it). For a fleet-fallback, only a "fleet"
      verdict confirms it (the human agreeing the fleet default was right after all).
    - ``human_verified_fraction``: confirmed / total corrections -- how often the human's check
      matched what auto-resolution had already picked.
    - ``false_agreement_rate``: restricted to the corrections that were checking an
      auto-accepted agreement (extractor == fleet), the fraction the human overturned -- the
      measured rate at which "extractor and fleet agreed" was nonetheless wrong.

    Corrections referencing an (id, field) not present in ``resolved`` are skipped (not counted
    in either denominator).
    """
    by_id = {row["id"]: row for row in resolved}

    n_total = 0
    n_confirmed = 0
    n_agree_checked = 0
    n_agree_overturned = 0

    for c in corrections:
        row = by_id.get(c["id"])
        if row is None:
            continue
        entry = row["fields"].get(c["field"])
        if entry is None:
            continue

        n_total += 1
        was_agreement = _was_pre_correction_agreement(entry, c["field"])
        verdict = c["verdict"]

        if was_agreement:
            confirmed = verdict in ("extractor", "fleet")
            n_agree_checked += 1
            if not confirmed:
                n_agree_overturned += 1
        else:
            confirmed = verdict == "fleet"

        if confirmed:
            n_confirmed += 1

    return {
        "n_corrections": n_total,
        "n_confirmed": n_confirmed,
        "human_verified_fraction": (n_confirmed / n_total) if n_total else 0.0,
        "n_agreements_checked": n_agree_checked,
        "n_agreements_overturned": n_agree_overturned,
        "false_agreement_rate": (n_agree_overturned / n_agree_checked) if n_agree_checked else 0.0,
    }
