"""The JD-coverage gate must be evaluated on the RECONCILED artifact, not the pre-merge build.

The 2026-07-28 deadlock: `_gated_publish` read jd_coverage on `db_tmp`, but the Tier-3 detail merge
that fills `snippet` runs afterwards and is itself gated on the result. A full re-crawl replaces
carried rows with list-only ones, so pre-merge coverage collapses by construction; the baseline
meanwhile stays pinned high because only carry-forward builds ever publish. Every non-join build
failed for 42 consecutive days. These tests pin the fix and, just as importantly, pin that the
protection the gate was written for still works.
"""

from __future__ import annotations

from pathlib import Path

from ergon.index.db import connect, fresh_db
from ergon.index.gates import evaluate_gates, evaluate_jd_coverage

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


def _index(path: Path, active: int, with_jd: int) -> None:
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


def _merge_detail(path: Path, upto: int) -> None:
    """Stand-in for the Tier-3 detail merge: fill `snippet` on rows that were list-only."""
    con = connect(path)
    con.execute(
        "UPDATE jobs SET snippet='merged jd body' WHERE snippet IS NULL AND id IN "
        f"({','.join(repr(f'j{i}') for i in range(upto))})"
    )
    con.commit()
    con.close()


def test_the_deadlock_shape_publishes_after_the_merge(tmp_path: Path) -> None:
    """A full re-crawl: 59% pre-merge against a 93% baseline, restored to 93% by the merge.

    Pre-merge this is a 34pt drop and would fail. Post-merge it is flat and must pass — otherwise
    no full crawl can ever publish again.
    """
    db = tmp_path / "recrawl.sqlite"
    _index(db, active=100, with_jd=59)

    pre = evaluate_jd_coverage(db, prev_jd_pct=93.0)
    assert not pre.passed, "pre-merge coverage should look like a collapse — that was the trap"

    _merge_detail(db, upto=93)

    post = evaluate_jd_coverage(db, prev_jd_pct=93.0)
    assert post.passed
    assert "93.0%" in post.detail


def test_a_genuine_collapse_still_fails_after_the_merge(tmp_path: Path) -> None:
    """The protection must survive the fix: a gutted detail sidecar merges nothing and still fails."""
    db = tmp_path / "gutted.sqlite"
    _index(db, active=100, with_jd=40)
    _merge_detail(db, upto=0)  # sidecar contributed nothing

    res = evaluate_jd_coverage(db, prev_jd_pct=93.0)
    assert not res.passed
    assert "40.0%" in res.detail


def test_partial_merge_below_threshold_still_fails(tmp_path: Path) -> None:
    """A merge that only partly recovers must not be enough to wave a real regression through.

    Recovers to 60% — over the 15pt relative limit AND under the absolute healthy floor, so
    neither rule lets it through. (70% would now pass on the floor, which is intended: that is a
    healthy index, not a collapse.)
    """
    db = tmp_path / "partial.sqlite"
    _index(db, active=100, with_jd=40)
    _merge_detail(db, upto=60)

    assert not evaluate_jd_coverage(db, prev_jd_pct=93.0).passed


def test_include_jd_false_omits_the_gate_entirely(tmp_path: Path) -> None:
    """The structural pre-merge phase must not report a jd_coverage verdict at all.

    Reporting a pass would be worse than omitting it — gates.json would claim JD was checked.
    """
    db = tmp_path / "structural.sqlite"
    _index(db, active=100, with_jd=10)

    rep = evaluate_gates(db, prev_row_count=100, prev_jd_pct=93.0, include_jd=False)
    assert not any(r.name == "jd_coverage" for r in rep.results)
    assert rep.passed, "structural gates alone should pass on a valid index"

    with_jd = evaluate_gates(db, prev_row_count=100, prev_jd_pct=93.0, include_jd=True)
    assert any(r.name == "jd_coverage" for r in with_jd.results)
    assert not with_jd.passed


def test_no_baseline_passes(tmp_path: Path) -> None:
    """First build: nothing to regress against."""
    db = tmp_path / "first.sqlite"
    _index(db, active=10, with_jd=0)
    res = evaluate_jd_coverage(db, prev_jd_pct=None)
    assert res.passed
    assert "no baseline" in res.detail


def test_recovery_build_passes(tmp_path: Path) -> None:
    """One-directional: climbing coverage is never blocked."""
    db = tmp_path / "recovery.sqlite"
    _index(db, active=100, with_jd=85)
    assert evaluate_jd_coverage(db, prev_jd_pct=40.0).passed


def test_empty_index_does_not_divide_by_zero(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    _index(db, active=0, with_jd=0)
    res = evaluate_jd_coverage(db, prev_jd_pct=93.0)
    assert not res.passed  # 0% against a 93% baseline is a collapse, not a pass
    assert "0.0%" in res.detail


# --- the absolute healthy floor ---------------------------------------------------------------
# The relative rule compares a FRESH full crawl against a MATURED carry-forward baseline, which is
# not like-for-like: on 2026-09-09 a real re-crawl landed at 76.0% vs a 93.39% baseline with
# 316,859 rows not yet in the detail sidecar, and was blocked for drain lag rather than data loss.
# The floor says a high ABSOLUTE coverage cannot be a collapse. Both historical incidents bottomed
# far below it, so the protection is unchanged.


def test_full_recrawl_dip_passes_on_the_absolute_floor(tmp_path: Path) -> None:
    """The exact production shape that was blocked: 76% against a 93.39% baseline."""
    db = tmp_path / "recrawl76.sqlite"
    _index(db, active=1000, with_jd=760)
    res = evaluate_jd_coverage(db, prev_jd_pct=93.39)
    assert res.passed
    assert "healthy floor" in res.detail


def test_the_2026_07_26_collapse_is_still_blocked(tmp_path: Path) -> None:
    """The incident the gate exists for: 85.18% -> 47.59%. Far below the floor; still fails."""
    db = tmp_path / "collapse.sqlite"
    _index(db, active=1000, with_jd=476)
    assert not evaluate_jd_coverage(db, prev_jd_pct=85.18).passed


def test_just_below_the_floor_fails(tmp_path: Path) -> None:
    """The floor is a real boundary, not a rubber stamp."""
    db = tmp_path / "below.sqlite"
    _index(db, active=1000, with_jd=699)  # 69.9%
    assert not evaluate_jd_coverage(db, prev_jd_pct=93.39).passed


def test_exactly_at_the_floor_passes(tmp_path: Path) -> None:
    db = tmp_path / "at.sqlite"
    _index(db, active=1000, with_jd=700)  # 70.0%
    assert evaluate_jd_coverage(db, prev_jd_pct=93.39).passed


def test_the_floor_does_not_mask_a_collapse_from_a_low_baseline(tmp_path: Path) -> None:
    """A build already below the floor gets no escape hatch — the relative rule governs."""
    db = tmp_path / "lowbase.sqlite"
    _index(db, active=1000, with_jd=400)  # 40% against a 76% baseline
    assert not evaluate_jd_coverage(db, prev_jd_pct=76.0).passed
