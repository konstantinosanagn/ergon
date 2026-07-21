"""Tests for the product-metric regression tripwire + the JD-capture coverage field."""

from __future__ import annotations

from ergon_tracker.index.build import build_index
from ergon_tracker.index.coverage import compute_coverage
from ergon_tracker.index.db import connect
from ergon_tracker.index.metrics_gate import (
    MetricsThresholds,
    check_metrics_regression,
    metrics_from_coverage,
)
from ergon_tracker.models import JobPosting


def _job(source: str, sid: str, company: str, title: str, *, desc=None):
    fields: dict = {}
    if desc is not None:
        fields["description_text"] = desc
    return JobPosting.create(
        source=source, source_job_id=sid, company=company, title=title, **fields
    )


# --- with_jd coverage field -------------------------------------------------------------------


def test_compute_coverage_with_jd(tmp_path):
    """with_jd counts only active rows whose snippet is non-empty (TRIM != '')."""
    jobs = [
        _job("greenhouse", "1", "Acme", "Backend Engineer", desc="Build and scale infra."),
        _job("greenhouse", "2", "Beta", "Data Scientist", desc="Own the data pipeline."),
        _job("lever", "3", "Gamma", "ML Engineer", desc=None),  # no description -> no snippet
        _job("lever", "4", "Delta", "Frontend Engineer", desc="   "),  # whitespace -> not counted
    ]
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    con = connect(p, read_only=True)
    try:
        cov = compute_coverage(con)
    finally:
        con.close()
    assert cov["active_jobs"] == 4
    assert cov["with_jd"] == 2  # only the two rows with real snippet text


def test_metrics_from_coverage_shape():
    cov = {
        "active_jobs": 100,
        "with_jd": 40,
        "with_salary": 10,
        "by_sector": {"Fintech": 60, "AI/ML": 20},  # 80 have a sector
        "by_source": {f"src{i}": (20 - i) for i in range(20)},  # 20 sources, descending counts
        "build_id": "b1",
    }
    m = metrics_from_coverage(cov)
    assert m["active_jobs"] == 100
    assert m["with_jd"] == 40 and m["jd_pct"] == 40.0
    assert m["with_salary"] == 10 and m["salary_pct"] == 10.0
    assert m["sector_pct"] == 80.0
    assert len(m["top_sources"]) == 15  # capped at top ~15
    assert m["top_sources"]["src0"] == 20  # highest-count source retained


# --- regression detection ---------------------------------------------------------------------


def _base_metrics(**over):
    m = {
        "active_jobs": 1000,
        "with_jd": 200,
        "jd_pct": 20.0,
        "with_salary": 100,
        "salary_pct": 10.0,
        "sector_pct": 90.0,
        "top_sources": {"greenhouse": 500, "lever": 300, "workday": 200},
    }
    m.update(over)
    return m


def test_clean_pair_no_regression():
    prev = _base_metrics()
    cur = _base_metrics(active_jobs=1010, jd_pct=20.5)  # tiny positive drift
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert rep.ok
    assert rep.to_signal()["regressions"] == []


def test_active_drop_trips():
    prev = _base_metrics()
    cur = _base_metrics(active_jobs=900)  # -10% > 5% threshold
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert not rep.ok
    metrics = {r.metric for r in rep.regressions}
    assert "active_jobs" in metrics


def test_active_small_drop_ok():
    prev = _base_metrics()
    cur = _base_metrics(active_jobs=970)  # -3% < 5% threshold -> fine
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert rep.ok


def test_source_crater_trips():
    prev = _base_metrics()
    cur = _base_metrics(top_sources={"greenhouse": 500, "lever": 100, "workday": 200})
    # lever 300 -> 100 == -66% > 30% threshold
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert not rep.ok
    assert any(r.metric == "source:lever" for r in rep.regressions)


def test_source_vanishing_trips():
    """A source that drops out of the current top-N reads as 0 -> a full drop -> WARN."""
    prev = _base_metrics()
    cur = _base_metrics(top_sources={"greenhouse": 500, "lever": 300})  # workday gone
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert any(r.metric == "source:workday" and r.cur == 0.0 for r in rep.regressions)


def test_jd_pct_drop_trips():
    prev = _base_metrics()
    cur = _base_metrics(jd_pct=16.0)  # -4 points > 3-point threshold
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert not rep.ok
    assert any(r.metric == "jd_pct" for r in rep.regressions)


def test_jd_pct_small_drop_ok():
    prev = _base_metrics()
    cur = _base_metrics(jd_pct=18.0)  # -2 points < 3-point threshold -> fine
    rep = check_metrics_regression(cur, prev, build_id="b2")
    assert rep.ok


def test_no_baseline_is_ok():
    cur = _base_metrics()
    for prev in (None, {}):
        rep = check_metrics_regression(cur, prev, build_id="b1")
        assert rep.ok
        assert rep.to_signal() == {"ok": True, "build_id": "b1", "regressions": []}


def test_env_threshold_override():
    prev = _base_metrics()
    cur = _base_metrics(active_jobs=970)  # -3%: fine at default 5%, trips at 2%
    th = MetricsThresholds(active_drop_pct=2.0)
    rep = check_metrics_regression(cur, prev, thresholds=th, build_id="b2")
    assert not rep.ok


# --- signal schema ----------------------------------------------------------------------------


def test_signal_schema_exact():
    prev = _base_metrics()
    cur = _base_metrics(active_jobs=800)  # -20%
    sig = check_metrics_regression(cur, prev, build_id="build-xyz").to_signal()
    assert set(sig.keys()) == {"ok", "build_id", "regressions"}
    assert sig["ok"] is False
    assert sig["build_id"] == "build-xyz"
    assert isinstance(sig["regressions"], list) and sig["regressions"]
    reg = sig["regressions"][0]
    assert set(reg.keys()) == {"metric", "prev", "cur", "delta_pct", "threshold"}
    assert isinstance(reg["metric"], str)
    for k in ("prev", "cur", "delta_pct", "threshold"):
        assert isinstance(reg[k], (int, float))
    assert reg["delta_pct"] < reg["threshold"]  # signed: dropped below the (negative) floor


def test_signal_json_serializable(tmp_path):
    import json

    prev = _base_metrics()
    cur = _base_metrics(jd_pct=10.0)
    sig = check_metrics_regression(cur, prev, build_id="b").to_signal()
    (tmp_path / "metrics_regression.json").write_text(json.dumps(sig, indent=2))
    reloaded = json.loads((tmp_path / "metrics_regression.json").read_text())
    assert reloaded["ok"] is False


# --- non-fatal / robustness -------------------------------------------------------------------


def test_malformed_prev_does_not_raise():
    cur = _base_metrics()
    for bad in (
        {"active_jobs": "lots", "jd_pct": None, "top_sources": "nope"},
        {"active_jobs": True, "top_sources": {"greenhouse": "many"}},
        {"top_sources": {"greenhouse": None}},
        {"active_jobs": float("nan")},
        [1, 2, 3],  # not even a dict
        "garbage",
    ):
        rep = check_metrics_regression(cur, bad, build_id="b")  # must not raise
        assert isinstance(rep.to_signal()["regressions"], list)


def test_zero_active_prev_no_divzero():
    prev = _base_metrics(active_jobs=0)
    cur = _base_metrics()
    rep = check_metrics_regression(cur, prev, build_id="b")  # no divide-by-zero
    assert isinstance(rep.ok, bool)
