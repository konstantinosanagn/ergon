"""Tests for scripts.bench.resolve_gold: fuse fleet labels + extractor predictions + human
corrections into resolved gold, and compute label-quality calibration stats.

All inputs are synthetic -- no network. Same shapes used throughout bench-v2: `predict()`-shaped
prediction dicts, Task-5-shaped label records (`{id, gold: {field: value}, split: {field:
bool}}`), and Task-7-shaped correction records (`{id, field, verdict:
"extractor"|"fleet"|"correct", value?, note?}`).
"""

from __future__ import annotations

import copy
from typing import Any

from scripts.bench.resolve_gold import calibration_stats, resolve
from scripts.bench.schema import FIELDS

_BASELINE: dict[str, Any] = {
    "level": "senior",
    "sector": "Software/SaaS",
    "country": "United States",
    "city": "New York",
    "remote": False,
    "employment_type": "full_time",
    "salary": {"min": 150000, "max": 180000, "currency": "USD"},
    "yoe": {"min": 5, "max": 8},
    "degree": "bachelor",
    "sponsorship": True,
    "posted_at": "2024-01-01",
    "visa_sponsor": True,
}


def _value(overrides: dict[str, Any]) -> dict[str, Any]:
    v = copy.deepcopy(_BASELINE)
    v.update(overrides)
    return v


def _label(rid: str, gold: dict[str, Any], split: dict[str, bool] | None = None) -> dict[str, Any]:
    return {"id": rid, "gold": gold, "split": split or {}}


def _field(resolved_rows: list[dict[str, Any]], rid: str, field: str) -> dict[str, Any]:
    (row,) = [r for r in resolved_rows if r["id"] == rid]
    return row["fields"][field]


# ---------------------------------------------------------------------------
# resolve(): precedence
# ---------------------------------------------------------------------------


def test_resolve_baseline_covers_every_field():
    assert set(_BASELINE) == set(FIELDS)


def test_resolve_auto_accepts_when_extractor_and_fleet_agree():
    pred = _value({})
    gold = _value({})
    resolved = resolve([_label("row-1", gold)], [pred], [])

    entry = _field(resolved, "row-1", "level")
    assert entry["value"] == "senior"
    assert entry["review_state"] == "auto"


def test_resolve_falls_back_to_fleet_on_unreviewed_conflict():
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    resolved = resolve([_label("row-1", gold)], [pred], [])

    entry = _field(resolved, "row-1", "level")
    assert entry["value"] == "junior"  # fleet-majority gold
    assert entry["review_state"] == "unreviewed"


def test_resolve_falls_back_to_fleet_on_unreviewed_coverage_gap():
    pred = _value({"sector": None})
    gold = _value({"sector": "Software/SaaS"})
    resolved = resolve([_label("row-1", gold)], [pred], [])

    entry = _field(resolved, "row-1", "sector")
    assert entry["value"] == "Software/SaaS"
    assert entry["review_state"] == "unreviewed"


def test_resolve_falls_back_to_fleet_on_fleet_split():
    pred = _value({"country": "United States"})
    gold = _value({"country": None})
    resolved = resolve([_label("row-1", gold, split={"country": True})], [pred], [])

    entry = _field(resolved, "row-1", "country")
    assert entry["value"] is None  # fleet gold, which is None (tied vote)
    assert entry["review_state"] == "unreviewed"


def test_resolve_tags_fleet_when_both_sides_unknown():
    pred = _value({"sector": None})
    gold = _value({"sector": None})
    resolved = resolve([_label("row-1", gold)], [pred], [])

    entry = _field(resolved, "row-1", "sector")
    assert entry["value"] is None
    assert entry["review_state"] == "fleet"


def test_resolve_human_correction_correct_verdict_uses_its_own_value():
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    corrections = [{"id": "row-1", "field": "level", "verdict": "correct", "value": "staff"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)

    entry = _field(resolved, "row-1", "level")
    assert entry["value"] == "staff"
    assert entry["review_state"] == "human"


def test_resolve_human_correction_extractor_verdict_uses_prediction():
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    corrections = [{"id": "row-1", "field": "level", "verdict": "extractor"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)

    entry = _field(resolved, "row-1", "level")
    assert entry["value"] == "senior"
    assert entry["review_state"] == "human"


def test_resolve_human_correction_fleet_verdict_uses_fleet_gold():
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    corrections = [{"id": "row-1", "field": "level", "verdict": "fleet"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)

    entry = _field(resolved, "row-1", "level")
    assert entry["value"] == "junior"
    assert entry["review_state"] == "human"


def test_resolve_human_correction_wins_even_over_agreement():
    # Extractor and fleet agree, but a human still filed a correction (e.g. spot-checked a
    # calibration sample and found both wrong) -- the correction still wins.
    pred = _value({})
    gold = _value({})
    corrections = [{"id": "row-1", "field": "level", "verdict": "correct", "value": "staff"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)

    entry = _field(resolved, "row-1", "level")
    assert entry["value"] == "staff"
    assert entry["review_state"] == "human"


def test_resolve_correction_only_affects_its_own_field():
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    corrections = [{"id": "row-1", "field": "level", "verdict": "correct", "value": "staff"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)

    # Every other field on row-1 is untouched by the correction and still resolves via
    # auto-accept (pred == gold on all fields except "level").
    other = _field(resolved, "row-1", "sector")
    assert other["value"] == "Software/SaaS"
    assert other["review_state"] == "auto"


def test_resolve_returns_one_row_per_input_row_all_fields_present():
    resolved = resolve([_label("row-1", _value({}))], [_value({})], [])
    assert len(resolved) == 1
    assert set(resolved[0]["fields"]) == set(FIELDS)


def test_resolve_multiple_rows_positionally_matched():
    labels = [_label("row-1", _value({})), _label("row-2", _value({"level": "junior"}))]
    preds = [_value({}), _value({"level": "senior"})]
    resolved = resolve(labels, preds, [])

    assert _field(resolved, "row-1", "level")["review_state"] == "auto"
    assert _field(resolved, "row-2", "level")["review_state"] == "unreviewed"
    assert _field(resolved, "row-2", "level")["value"] == "junior"


# ---------------------------------------------------------------------------
# calibration_stats()
# ---------------------------------------------------------------------------


def test_calibration_stats_empty_corrections_is_zero_safe():
    resolved = resolve([_label("row-1", _value({}))], [_value({})], [])
    stats = calibration_stats(resolved, [])
    assert stats["n_corrections"] == 0
    assert stats["human_verified_fraction"] == 0.0
    assert stats["n_agreements_checked"] == 0
    assert stats["false_agreement_rate"] == 0.0


def test_calibration_stats_confirmed_agreement_counts_as_verified_and_not_overturned():
    # A calibration-sample item: extractor and fleet agreed, human spot-checked and confirmed
    # (verdict "extractor" -- since pred == gold here, "fleet" would mean the same thing).
    labels = [_label("row-1", _value({}))]
    preds = [_value({})]
    corrections = [{"id": "row-1", "field": "level", "verdict": "extractor"}]
    resolved = resolve(labels, preds, corrections)
    stats = calibration_stats(resolved, corrections)

    assert stats["n_corrections"] == 1
    assert stats["n_confirmed"] == 1
    assert stats["human_verified_fraction"] == 1.0
    assert stats["n_agreements_checked"] == 1
    assert stats["n_agreements_overturned"] == 0
    assert stats["false_agreement_rate"] == 0.0


def test_calibration_stats_overturned_agreement_is_a_false_agreement():
    # Extractor and fleet agreed ("senior") but the human found the real answer was "staff" --
    # exactly the false-agreement case calibration exists to measure.
    labels = [_label("row-1", _value({}))]
    preds = [_value({})]
    corrections = [{"id": "row-1", "field": "level", "verdict": "correct", "value": "staff"}]
    resolved = resolve(labels, preds, corrections)
    stats = calibration_stats(resolved, corrections)

    assert stats["n_corrections"] == 1
    assert stats["n_confirmed"] == 0
    assert stats["human_verified_fraction"] == 0.0
    assert stats["n_agreements_checked"] == 1
    assert stats["n_agreements_overturned"] == 1
    assert stats["false_agreement_rate"] == 1.0


def test_calibration_stats_confirmed_fleet_fallback_counts_as_verified_but_not_an_agreement_check():
    # A conflict item: extractor and fleet disagreed, human checked and confirmed fleet was
    # right. Verified, but must NOT count toward the agreement-checked denominator (it was never
    # an auto-accepted agreement).
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    corrections = [{"id": "row-1", "field": "level", "verdict": "fleet"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)
    stats = calibration_stats(resolved, corrections)

    assert stats["n_confirmed"] == 1
    assert stats["human_verified_fraction"] == 1.0
    assert stats["n_agreements_checked"] == 0
    assert stats["false_agreement_rate"] == 0.0


def test_calibration_stats_overturned_fleet_fallback_is_not_verified_but_not_a_false_agreement():
    # Same conflict item, but the human sided with the extractor instead -- fleet's default was
    # wrong. Not verified. Still not an agreement check (extractor/fleet never agreed here).
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    corrections = [{"id": "row-1", "field": "level", "verdict": "extractor"}]
    resolved = resolve([_label("row-1", gold)], [pred], corrections)
    stats = calibration_stats(resolved, corrections)

    assert stats["n_confirmed"] == 0
    assert stats["human_verified_fraction"] == 0.0
    assert stats["n_agreements_checked"] == 0
    assert stats["n_agreements_overturned"] == 0


def test_calibration_stats_mixed_slice_fractions():
    # Two agreement checks (one confirmed, one overturned) + one fleet-fallback check
    # (confirmed) -> exercise the combined fraction math.
    labels = [
        _label("row-1", _value({})),
        _label("row-2", _value({})),
        _label("row-3", _value({"level": "junior"})),
    ]
    preds = [_value({}), _value({}), _value({"level": "senior"})]
    corrections = [
        {"id": "row-1", "field": "level", "verdict": "extractor"},  # confirmed agreement
        {
            "id": "row-2",
            "field": "level",
            "verdict": "correct",
            "value": "staff",
        },  # overturned agreement
        {"id": "row-3", "field": "level", "verdict": "fleet"},  # confirmed fleet-fallback
    ]
    resolved = resolve(labels, preds, corrections)
    stats = calibration_stats(resolved, corrections)

    assert stats["n_corrections"] == 3
    assert stats["n_confirmed"] == 2
    assert stats["human_verified_fraction"] == 2 / 3
    assert stats["n_agreements_checked"] == 2
    assert stats["n_agreements_overturned"] == 1
    assert stats["false_agreement_rate"] == 0.5


def test_calibration_stats_ignores_correction_for_unknown_row():
    resolved = resolve([_label("row-1", _value({}))], [_value({})], [])
    corrections = [{"id": "row-missing", "field": "level", "verdict": "extractor"}]
    stats = calibration_stats(resolved, corrections)
    assert stats["n_corrections"] == 0
