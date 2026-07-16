"""Tests for scripts.bench.triage: agreement classification + the triage-ordered audit queue.

All inputs are synthetic — no network, no real labels. `predict()`-shaped prediction dicts and
Task-5-shaped label records (`{id, gold: {field: value}, split: {field: bool}}`).
"""

from __future__ import annotations

import copy
from typing import Any

from scripts.bench.schema import FIELDS
from scripts.bench.triage import agreement, build_queue

# A fully-agreeing baseline: every field present and identical between pred/gold, so a row built
# from this (with zero overrides) contributes only "agree" classifications.
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


def test_baseline_pred_and_gold_cover_every_field():
    assert set(_BASELINE) == set(FIELDS)


# ---------------------------------------------------------------------------
# agreement() unit tests
# ---------------------------------------------------------------------------


def test_agreement_categorical_exact_match():
    pred = _value({})
    gold = _value({})
    assert agreement(pred, gold, "level") == "agree"


def test_agreement_categorical_conflict():
    pred = _value({"level": "senior"})
    gold = _value({"level": "junior"})
    assert agreement(pred, gold, "level") == "conflict"


def test_agreement_coverage_when_one_side_unknown():
    pred = _value({"sector": None})
    gold = _value({"sector": "Software/SaaS"})
    assert agreement(pred, gold, "sector") == "coverage"


def test_agreement_na_when_both_unknown():
    pred = _value({"sector": None})
    gold = _value({"sector": None})
    assert agreement(pred, gold, "sector") == "na"


def test_agreement_salary_within_5pct_tolerance_agrees():
    pred = _value({"salary": {"min": 150000, "max": 180000, "currency": "USD"}})
    gold = _value({"salary": {"min": 151000, "max": 180000, "currency": "USD"}})
    assert agreement(pred, gold, "salary") == "agree"


def test_agreement_salary_outside_tolerance_conflicts():
    pred = _value({"salary": {"min": 150000, "max": 180000, "currency": "USD"}})
    gold = _value({"salary": {"min": 200000, "max": 180000, "currency": "USD"}})
    assert agreement(pred, gold, "salary") == "conflict"


def test_agreement_yoe_requires_exact_match():
    pred = _value({"yoe": {"min": 5, "max": 8}})
    gold = _value({"yoe": {"min": 6, "max": 8}})
    assert agreement(pred, gold, "yoe") == "conflict"
    gold_exact = _value({"yoe": {"min": 5, "max": 8}})
    assert agreement(pred, gold_exact, "yoe") == "agree"


def test_agreement_bool_field_exact_match():
    pred = _value({"remote": True})
    gold = _value({"remote": False})
    assert agreement(pred, gold, "remote") == "conflict"


# ---------------------------------------------------------------------------
# build_queue(): triage ordering over 4 synthetic rows, one per class
# ---------------------------------------------------------------------------


def _row(rid: str) -> dict[str, Any]:
    return {"id": rid, "url": f"https://example.com/{rid}"}


def test_build_queue_orders_conflicts_coverage_split_then_calibration():
    # Row A: a single conflict on "level"; every other field agrees.
    pred_a = _value({"level": "senior"})
    gold_a = _value({"level": "junior"})
    label_a = {"id": "row-a", "gold": gold_a, "split": {}}

    # Row B: a single coverage gap on "sector" (extractor missed it, fleet has it).
    pred_b = _value({"sector": None})
    gold_b = _value({"sector": "Software/SaaS"})
    label_b = {"id": "row-b", "gold": gold_b, "split": {}}

    # Row C: a single fleet-split on "country" (fleet tied -> gold is None, split=True). Must be
    # classified as fleet-split, NOT as a coverage gap, even though gold looks "unknown".
    pred_c = _value({"country": "United States"})
    gold_c = _value({"country": None})
    label_c = {"id": "row-c", "gold": gold_c, "split": {"country": True}}

    # Row D: everything agrees, including a salary value within the 5% tolerance band.
    pred_d = _value({"salary": {"min": 150000, "max": 180000, "currency": "USD"}})
    gold_d = _value({"salary": {"min": 151000, "max": 180000, "currency": "USD"}})
    label_d = {"id": "row-d", "gold": gold_d, "split": {}}

    rows = [_row("row-a"), _row("row-b"), _row("row-c"), _row("row-d")]
    preds = [pred_a, pred_b, pred_c, pred_d]
    labels = [label_a, label_b, label_c, label_d]

    queue = build_queue(rows, preds, labels, calib=1)

    # Exactly one item per triage class, plus one calibration sample (calib=1).
    assert len(queue) == 4
    reasons = [item["reason"] for item in queue]
    assert reasons == ["conflict", "coverage", "fleet-split", "calibration"]

    conflict_item, coverage_item, split_item, calib_item = queue

    assert conflict_item["id"] == "row-a" and conflict_item["field"] == "level"
    assert conflict_item["extractor_value"] == "senior"
    assert conflict_item["fleet_value"] == "junior"
    assert conflict_item["url"] == "https://example.com/row-a"

    assert coverage_item["id"] == "row-b" and coverage_item["field"] == "sector"
    assert coverage_item["extractor_value"] is None
    assert coverage_item["fleet_value"] == "Software/SaaS"

    assert split_item["id"] == "row-c" and split_item["field"] == "country"
    assert split_item["extractor_value"] == "United States"
    assert split_item["fleet_value"] is None

    # The calibration sample is deterministic (first agreement encountered in input order): row A
    # is processed first, "level" is a conflict so it's skipped, and "sector" (next in FIELDS
    # order) is the first field on row A that agrees.
    assert calib_item["id"] == "row-a" and calib_item["field"] == "sector"
    assert calib_item["extractor_value"] == "Software/SaaS"
    assert calib_item["fleet_value"] == "Software/SaaS"


def test_build_queue_agreements_excluded_except_calibration_sample():
    # A single, fully-agreeing row: with calib=0 nothing from it should reach the queue at all.
    pred = _value({})
    gold = _value({})
    label = {"id": "row-x", "gold": gold, "split": {}}
    queue = build_queue([_row("row-x")], [pred], [label], calib=0)
    assert queue == []


def test_build_queue_calibration_sample_is_deterministic_across_runs():
    pred = _value({})
    gold = _value({})
    label = {"id": "row-x", "gold": gold, "split": {}}
    rows, preds, labels = [_row("row-x")], [pred], [label]
    q1 = build_queue(rows, preds, labels, calib=3)
    q2 = build_queue(rows, preds, labels, calib=3)
    assert q1 == q2


def test_build_queue_both_unknown_is_never_queued():
    # "na" fields (both pred and gold unknown) must never appear, even in the calibration pool.
    pred = _value({"sector": None})
    gold = _value({"sector": None})
    label = {"id": "row-x", "gold": gold, "split": {}}
    queue = build_queue([_row("row-x")], [pred], [label], calib=1000)
    assert all(item["field"] != "sector" for item in queue)
