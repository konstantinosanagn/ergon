"""Tests for scripts.bench.scoring: Wilson CI, per-field metrics, and the provider matrix.

All inputs are synthetic -- no network. Rows are ``{"source", "gold", "pred"}``, where "gold" and
"pred" are full per-field dicts in the same shape used throughout bench v2 (``predict()`` output /
Task-5 label ``gold``), see ``scripts.bench.scoring`` module docstring for the exact contract.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from scripts.bench.scoring import provider_matrix, score_field, wilson_ci

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


def _row(source: str, gold: dict[str, Any], pred: dict[str, Any]) -> dict[str, Any]:
    return {"source": source, "gold": gold, "pred": pred}


# ---------------------------------------------------------------------------
# wilson_ci()
# ---------------------------------------------------------------------------


def test_wilson_ci_n_zero_returns_zero_interval():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_bounds_sanity_8_of_10():
    lo, hi = wilson_ci(8, 10)
    # Interval brackets the point estimate and stays inside [0, 1].
    assert 0.0 <= lo < 0.8 < hi <= 1.0
    # Known Wilson values for k=8, n=10, z=1.96 (~95%).
    assert math.isclose(lo, 0.4904, abs_tol=1e-3)
    assert math.isclose(hi, 0.9436, abs_tol=1e-3)


def test_wilson_ci_perfect_score_upper_bound_is_one():
    lo, hi = wilson_ci(10, 10)
    assert hi == 1.0
    assert 0.0 < lo < 1.0


def test_wilson_ci_zero_score_lower_bound_is_zero():
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0
    assert 0.0 < hi < 1.0


def test_wilson_ci_widens_as_n_shrinks():
    lo_big, hi_big = wilson_ci(80, 100)
    lo_small, hi_small = wilson_ci(8, 10)
    assert (hi_small - lo_small) > (hi_big - lo_big)


# ---------------------------------------------------------------------------
# score_field(): categorical field, one known error
# ---------------------------------------------------------------------------


def test_score_field_categorical_one_error_out_of_ten_gives_accuracy_point_nine():
    rows = []
    for i in range(10):
        gold = _value({})
        pred = _value({"level": "junior"}) if i == 0 else _value({})
        rows.append(_row("greenhouse", gold, pred))

    result = score_field("level", rows)

    assert result["n"] == 10
    assert result["coverage"] == 1.0
    assert math.isclose(result["accuracy"], 0.9)
    assert math.isclose(result["recall"], 0.9)
    assert math.isclose(result["precision"], 0.9)  # extractor asserted on all 10 rows too
    lo, hi = result["ci"]
    assert lo <= 0.9 <= hi
    assert (lo, hi) == wilson_ci(9, 10)


# ---------------------------------------------------------------------------
# score_field(): numeric/range field, within-tolerance counted correct
# ---------------------------------------------------------------------------


def test_score_field_salary_within_5pct_tolerance_counted_correct():
    gold = _value({"salary": {"min": 150000, "max": 180000, "currency": "USD"}})
    pred_close = _value({"salary": {"min": 151000, "max": 180000, "currency": "USD"}})  # ~0.67%
    pred_far = _value({"salary": {"min": 200000, "max": 180000, "currency": "USD"}})  # way off

    rows = [
        _row("lever", gold, pred_close),
        _row("lever", gold, pred_far),
    ]
    result = score_field("salary", rows)

    assert result["n"] == 2
    assert result["coverage"] == 1.0
    assert math.isclose(result["accuracy"], 0.5)
    assert (result["ci"]) == wilson_ci(1, 2)


def test_score_field_yoe_exact_range_match_required():
    gold = _value({"yoe": {"min": 5, "max": 8}})
    pred_exact = _value({"yoe": {"min": 5, "max": 8}})
    pred_off_by_one = _value({"yoe": {"min": 6, "max": 8}})

    rows = [
        _row("ashby", gold, pred_exact),
        _row("ashby", gold, pred_off_by_one),
    ]
    result = score_field("yoe", rows)
    assert math.isclose(result["accuracy"], 0.5)


# ---------------------------------------------------------------------------
# coverage vs. precision: must never be conflated
# ---------------------------------------------------------------------------


def test_coverage_separated_from_precision_high_nulls_perfect_extraction():
    # 10 rows: gold is stated on only 2 of them ("sector" null on the other 8). Where gold IS
    # stated, the extractor's prediction matches exactly (perfect extraction on the covered
    # slice). The extractor also predicts nothing (None) on the 8 gold-null rows -- it never
    # over-claims.
    rows = []
    for i in range(10):
        if i < 2:
            gold = _value({"sector": "Software/SaaS"})
            pred = _value({"sector": "Software/SaaS"})
        else:
            gold = _value({"sector": None})
            pred = _value({"sector": None})
        rows.append(_row("icims", gold, pred))

    result = score_field("sector", rows)

    assert result["n"] == 10
    assert math.isclose(result["coverage"], 0.2)  # low: data availability, not extractor's fault
    assert math.isclose(result["accuracy"], 1.0)  # high: perfect on the covered slice
    assert math.isclose(result["recall"], 1.0)
    assert math.isclose(
        result["precision"], 1.0
    )  # extractor asserted 2/2 correctly, 0 hallucinated


def test_precision_penalized_by_hallucinated_value_gold_unstated():
    # Extractor asserts a value on every row; gold is stated on only half. On the gold-unstated
    # half, the assertion cannot be verified -- it counts against precision (not toward coverage
    # or accuracy, which are both 100% on the rows that DO have gold).
    rows = []
    for i in range(4):
        if i < 2:
            gold = _value({"sector": "Software/SaaS"})
            pred = _value({"sector": "Software/SaaS"})
        else:
            gold = _value({"sector": None})
            pred = _value({"sector": "Software/SaaS"})  # hallucinated: no gold to confirm
        rows.append(_row("icims", gold, pred))

    result = score_field("sector", rows)

    assert math.isclose(result["coverage"], 0.5)
    assert math.isclose(result["accuracy"], 1.0)  # perfect on the 2 covered rows
    assert math.isclose(result["precision"], 0.5)  # 2 correct / 4 asserted


# ---------------------------------------------------------------------------
# score_field(): tri-state bool|None field (sponsorship)
# ---------------------------------------------------------------------------


def test_score_field_tri_state_sponsorship_null_is_uncovered():
    gold_stated = _value({"sponsorship": True})
    gold_unstated = _value({"sponsorship": None})

    rows = [
        _row("workday", gold_stated, _value({"sponsorship": True})),
        _row("workday", gold_unstated, _value({"sponsorship": None})),
    ]
    result = score_field("sponsorship", rows)

    assert result["n"] == 2
    assert math.isclose(result["coverage"], 0.5)
    assert math.isclose(result["accuracy"], 1.0)


# ---------------------------------------------------------------------------
# provider_matrix()
# ---------------------------------------------------------------------------


def test_provider_matrix_splits_by_source():
    gold = _value({})
    correct_pred = _value({})
    wrong_pred = _value({"level": "junior"})

    rows = [
        _row("greenhouse", gold, correct_pred),
        _row("greenhouse", gold, correct_pred),
        _row("lever", gold, wrong_pred),
    ]
    matrix = provider_matrix("level", rows)

    assert set(matrix) == {"greenhouse", "lever"}
    assert matrix["greenhouse"]["n"] == 2
    assert math.isclose(matrix["greenhouse"]["accuracy"], 1.0)
    assert matrix["lever"]["n"] == 1
    assert math.isclose(matrix["lever"]["accuracy"], 0.0)


def test_provider_matrix_each_entry_matches_score_field_on_its_slice():
    gold = _value({})
    rows = [
        _row("greenhouse", gold, _value({})),
        _row("lever", gold, _value({"level": "junior"})),
        _row("lever", gold, _value({})),
    ]
    matrix = provider_matrix("level", rows)
    lever_rows = [r for r in rows if r["source"] == "lever"]
    assert matrix["lever"] == score_field("level", lever_rows)
