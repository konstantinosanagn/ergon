"""JD-coverage publish gate: a build whose JD-text capture % COLLAPSED vs the last published build
must FAIL and not publish — while a recovery build (climbing coverage) must still PASS.

The 2026-07-27 incident: a full-crawl index with MORE rows but COLLAPSED JD coverage (76%->40%)
published over a good one, because the publish gate enforced only a row-count floor. This gate is the
relative, one-directional hard stop that would have blocked it. Production is currently mid-recovery
(the drain refilling coverage 40->76->85), so the must-not-break case is that a CLIMBING build passes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ergon_tracker.index.db import connect, fresh_db
from ergon_tracker.index.gates import (
    _DEF_JD_MAX_DROP_PCT,
    evaluate_gates,
    jd_gate_drop_pct_from_env,
)

_REQ = (
    "greenhouse",
    "A",
    "unknown",
    "mid",
    "fulltime",
    "2026-07-01",
    "2026-07-01",
    "2026-07-01",
    "b0",
)


def _jd_index(path: Path, active: int, with_jd: int) -> None:
    """A schema-valid index with ``active`` active jobs, ``with_jd`` of them carrying a snippet."""
    fresh_db(path)
    con = connect(path)
    con.execute("INSERT INTO companies(company_key,display_name) VALUES('a','A')")
    for i in range(active):
        snippet = "a real job description" if i < with_jd else None
        con.execute(
            "INSERT INTO jobs(id,content_hash,company_key,title,source,company,remote,level,"
            "employment_type,first_seen,last_seen,fetched_at,build_id,snippet) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"j{i}", f"h{i}", "a", f"t{i}", *_REQ, snippet),
        )
    con.commit()
    con.close()


def _jd(rep):
    return next(r for r in rep.results if r.name == "jd_coverage")


def test_jd_collapse_vs_prior_fails(tmp_path: Path) -> None:
    """The real incident shape: baseline 76%, this build 40% -> a 36pt drop -> gate FAILS."""
    db = tmp_path / "collapse.sqlite"
    _jd_index(db, active=100, with_jd=40)  # jd_pct = 40.0
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=76.0)
    assert not _jd(rep).passed
    assert not rep.passed  # the whole report fails -> publish blocked
    assert "40.0%" in _jd(rep).detail and "baseline 76.0%" in _jd(rep).detail


def test_jd_recovery_increase_passes(tmp_path: Path) -> None:
    """MUST-NOT-BREAK: the drain refilling coverage 40->76 INCREASES jd_pct -> gate PASSES."""
    db = tmp_path / "recovery.sqlite"
    _jd_index(db, active=100, with_jd=76)  # jd_pct = 76.0, up from a 40.0 baseline
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=40.0)
    assert _jd(rep).passed
    assert rep.passed


def test_jd_further_recovery_passes(tmp_path: Path) -> None:
    """A later recovery step 76->85 also climbs -> still PASSES (no false-fail on the ramp)."""
    db = tmp_path / "recovery2.sqlite"
    _jd_index(db, active=100, with_jd=85)  # jd_pct = 85.0, up from 76.0
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=76.0)
    assert _jd(rep).passed


def test_jd_small_daily_dip_within_threshold_passes(tmp_path: Path) -> None:
    """Ordinary daily churn (76% -> 74%, a 2pt dip) is under the 15pt threshold -> PASSES."""
    db = tmp_path / "dip.sqlite"
    _jd_index(db, active=100, with_jd=74)  # jd_pct = 74.0
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=76.0)
    assert _jd(rep).passed


def test_jd_no_baseline_passes(tmp_path: Path) -> None:
    """No last-published baseline (first build / missing history) -> never false-fail -> PASSES."""
    db = tmp_path / "cold.sqlite"
    _jd_index(db, active=100, with_jd=10)  # low coverage, but nothing to compare against
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=None)
    assert _jd(rep).passed
    assert "no baseline" in _jd(rep).detail


def test_jd_drop_exactly_at_threshold_passes(tmp_path: Path) -> None:
    """Boundary: a drop of exactly the threshold (15pt) is tolerated (only > 15 fails)."""
    db = tmp_path / "edge.sqlite"
    _jd_index(db, active=100, with_jd=61)  # 76 - 61 = 15.0 == threshold
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=76.0)
    assert _jd(rep).passed


def test_jd_custom_threshold_tightens_gate(tmp_path: Path) -> None:
    """An env-supplied tighter threshold (5pt) catches a drop the default 15pt would tolerate."""
    db = tmp_path / "tight.sqlite"
    _jd_index(db, active=100, with_jd=68)  # 76 - 68 = 8pt drop
    assert evaluate_gates(db, prev_row_count=100, prev_jd_pct=76.0).passed  # default 15 -> pass
    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=76.0, jd_max_drop_pct=5.0)
    assert not _jd(rep).passed  # tighter 5pt -> fail


def test_env_threshold_override_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate threshold is env-tunable (ERGON_METRICS_JD_GATE_DROP_PCT); a bad value falls back."""
    monkeypatch.delenv("ERGON_METRICS_JD_GATE_DROP_PCT", raising=False)
    assert jd_gate_drop_pct_from_env() == _DEF_JD_MAX_DROP_PCT
    monkeypatch.setenv("ERGON_METRICS_JD_GATE_DROP_PCT", "8.5")
    assert jd_gate_drop_pct_from_env() == 8.5
    monkeypatch.setenv("ERGON_METRICS_JD_GATE_DROP_PCT", "not-a-number")
    assert jd_gate_drop_pct_from_env() == _DEF_JD_MAX_DROP_PCT
