"""The publish gates must anchor on a high-water mark, not on the last publish.

Both gates are one-directional ("fail only on a DROP") and both read their baseline from the last
published build. That is a ratchet: on 2026-09-10 the JD gate let a 72% build through via the
healthy floor, the baseline became 72%, and from then on a 4pt/day slide passed the relative test
forever. The active-row floor has the same shape at 0.9/day.

``_gate_baselines`` now returns the best jd_pct over the last 30 published builds and the best
active_jobs over the last 7. The last-build reader is left alone for the WARN-only metrics
tripwire, which is meant to be day-over-day.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_index import (  # noqa: E402
    _ACTIVE_BASELINE_WINDOW,
    _JD_BASELINE_WINDOW,
    _gate_baselines,
    _last_published_metrics,
)

from ergon.index.db import connect, fresh_db  # noqa: E402
from ergon.index.gates import evaluate_active_floor, evaluate_jd_coverage  # noqa: E402

_TS = "2026-09-01T00:00:00+00:00"


def _history(path: Path, rows: list[dict | str]) -> Path:
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
    return path


def _pub(jd: float, active: int = 1000) -> dict:
    return {"published": True, "metrics": {"jd_pct": jd, "active_jobs": active}}


def _index(path: Path, active: int, with_jd: int) -> Path:
    fresh_db(path)
    con = connect(path)
    con.execute("INSERT INTO companies(company_key,display_name) VALUES('a','A')")
    con.executemany(
        "INSERT INTO jobs(id,content_hash,company_key,title,source,company,remote,level,"
        "employment_type,first_seen,last_seen,fetched_at,build_id,status,snippet) "
        "VALUES(?,?,'a',?,'greenhouse','A','unknown','mid','fulltime',?,?,?,'b0','active',?)",
        [
            (f"j{i}", f"h{i}", f"t{i}", _TS, _TS, _TS, "jd" if i < with_jd else None)
            for i in range(active)
        ],
    )
    con.commit()
    con.close()
    return path


def test_the_ratchet_scenario_is_closed(tmp_path: Path) -> None:
    """93.4 for weeks, then 76.0 and 72.4 published via the floor: the baseline must stay 93.4."""
    h = _history(tmp_path / "h.jsonl", [_pub(93.4)] * 10 + [_pub(76.0), _pub(72.4)])
    assert _last_published_metrics(h) == {
        "jd_pct": 72.4,
        "active_jobs": 1000,
    }  # tripwire: unchanged
    base = _gate_baselines(h)
    assert base["jd_pct"] == 93.4
    # Yesterday published at 72.4; today 69% is a 3.4pt day-over-day drop that the OLD baseline
    # would wave through. Against the high-water mark it is a 24pt collapse under the floor: fail.
    assert not evaluate_jd_coverage(
        _index(tmp_path / "a.sqlite", 1000, 690), prev_jd_pct=base["jd_pct"]
    ).passed
    # A genuine recovery still passes.
    assert evaluate_jd_coverage(
        _index(tmp_path / "b.sqlite", 1000, 800), prev_jd_pct=base["jd_pct"]
    ).passed


def test_active_floor_uses_a_weekly_high_water_mark(tmp_path: Path) -> None:
    """A 9%/day leak passed the 0.9 day-over-day test forever; against the week's best it cannot."""
    days = [1000, 910, 828, 754]  # each day 91% of the last
    h = _history(tmp_path / "h.jsonl", [_pub(90.0, a) for a in days])
    base = _gate_baselines(h)
    assert base["active_jobs"] == 1000
    today = _index(tmp_path / "c.sqlite", 686, 686)  # 91% of 754, 69% of the week's best
    assert evaluate_active_floor(today, prev_active=754).passed, "day-over-day would have passed it"
    assert not evaluate_active_floor(today, prev_active=base["active_jobs"]).passed


def test_windows_are_respected(tmp_path: Path) -> None:
    old_jd = [_pub(99.0)] + [_pub(80.0)] * _JD_BASELINE_WINDOW
    assert _gate_baselines(_history(tmp_path / "j.jsonl", old_jd))["jd_pct"] == 80.0
    old_act = [_pub(80.0, 5000)] + [_pub(80.0, 1000)] * _ACTIVE_BASELINE_WINDOW
    assert _gate_baselines(_history(tmp_path / "a.jsonl", old_act))["active_jobs"] == 1000


def test_unpublished_malformed_and_missing_rows_are_ignored(tmp_path: Path) -> None:
    rows: list[dict | str] = [
        {"published": False, "metrics": {"jd_pct": 99.0, "active_jobs": 9999}},  # a failed build
        "not json at all",
        {"published": True},  # no metrics block
        {"published": True, "metrics": {"jd_pct": "n/a", "active_jobs": 2.5}},  # wrong types
        _pub(85.0, 1200),
    ]
    assert _gate_baselines(_history(tmp_path / "h.jsonl", rows)) == {
        "jd_pct": 85.0,
        "active_jobs": 1200,
    }


def test_no_history_means_no_baseline(tmp_path: Path) -> None:
    assert _gate_baselines(tmp_path / "absent.jsonl") == {"jd_pct": None, "active_jobs": None}
    assert evaluate_jd_coverage(_index(tmp_path / "d.sqlite", 10, 1), prev_jd_pct=None).passed
