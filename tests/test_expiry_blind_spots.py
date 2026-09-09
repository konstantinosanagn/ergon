"""The two blind spots that let a mass expiry run unseen.

1. ``row_floor`` counts EVERY row, and every expiry path flips ``status`` in place rather than
   deleting — so a 100% expiry leaves ``COUNT(*)`` unchanged and the gate cannot see it. By
   construction, not by accident. ``jd_coverage`` is a ratio over active rows, so a uniform expiry
   can even improve it. ``evaluate_active_floor`` is the gate that watches the count the expiry
   paths actually move.

2. ``liveness.process_board`` skipped only on a ``None`` board. An EMPTY id-set counted as a fully
   successful fetch of an empty board, and 14 providers return ``[]`` on a swallowed 429/5xx/
   timeout rather than raising — so that is exactly the shape a transient failure takes. Two
   consecutive weekly runs then expired the whole board.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio

from ergon.index.db import connect, fresh_db
from ergon.index.gates import _DEF_ACTIVE_MIN_RATIO, evaluate_active_floor, evaluate_gates
from ergon.index.liveness import open_liveness, reconcile_liveness_tier

_DAY0 = "2026-07-01T00:00:00+00:00"
_REQ = ("greenhouse", "A", "unknown", "mid", "fulltime", _DAY0, _DAY0, _DAY0, "b0")


def _index(path: Path, total: int, active: int) -> None:
    """``total`` rows, of which ``active`` are active and the rest already expired."""
    fresh_db(path)
    con = connect(path)
    con.execute("INSERT INTO companies(company_key,display_name) VALUES('a','A')")
    for i in range(total):
        con.execute(
            "INSERT INTO jobs(id,content_hash,company_key,title,source,company,remote,level,"
            "employment_type,first_seen,last_seen,fetched_at,build_id,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"j{i}", f"h{i}", "a", f"t{i}", *_REQ, "active" if i < active else "expired"),
        )
    con.commit()
    con.close()


# --- 1. the active-row gate --------------------------------------------------------------------


def test_row_floor_is_blind_to_a_total_expiry(tmp_path: Path) -> None:
    """Documents the blind spot itself: every row expired, row_floor still passes.

    This is why a second gate is needed — it is not a bug in row_floor, it is what row_floor
    measures.
    """
    db = tmp_path / "wiped.sqlite"
    _index(db, total=1000, active=0)  # 1000 rows, none of them active
    rep = evaluate_gates(db, prev_row_count=1000, include_jd=False)
    row_floor = next(r for r in rep.results if r.name == "row_floor")
    assert row_floor.passed, "row_floor counts all rows, so a 100% expiry is invisible to it"


def test_active_floor_catches_the_total_expiry(tmp_path: Path) -> None:
    db = tmp_path / "wiped.sqlite"
    _index(db, total=1000, active=0)
    res = evaluate_active_floor(db, prev_active=1000)
    assert not res.passed
    assert "0 active" in res.detail


def test_active_floor_catches_a_partial_mass_expiry(tmp_path: Path) -> None:
    """Half the board expired — well past ordinary churn."""
    db = tmp_path / "half.sqlite"
    _index(db, total=1000, active=500)
    assert not evaluate_active_floor(db, prev_active=1000).passed


def test_ordinary_daily_churn_passes(tmp_path: Path) -> None:
    """Observed production churn is ~0.2%/day; the gate must not fire on normal operation."""
    db = tmp_path / "churn.sqlite"
    _index(db, total=1000, active=995)
    assert evaluate_active_floor(db, prev_active=1000).passed


def test_active_floor_boundary(tmp_path: Path) -> None:
    db = tmp_path / "edge.sqlite"
    _index(db, total=1000, active=int(1000 * _DEF_ACTIVE_MIN_RATIO))
    assert evaluate_active_floor(db, prev_active=1000).passed
    _index(db, total=1000, active=int(1000 * _DEF_ACTIVE_MIN_RATIO) - 1)
    assert not evaluate_active_floor(db, prev_active=1000).passed


def test_no_baseline_passes(tmp_path: Path) -> None:
    """A missing basis is not evidence of loss — same discipline as row_floor's cold start."""
    db = tmp_path / "first.sqlite"
    _index(db, total=10, active=10)
    assert evaluate_active_floor(db, prev_active=None).passed


def test_growth_passes(tmp_path: Path) -> None:
    db = tmp_path / "grew.sqlite"
    _index(db, total=2000, active=2000)
    assert evaluate_active_floor(db, prev_active=1000).passed


# --- 2. the liveness empty-board valve ----------------------------------------------------------


def _liveness_index(path: Path, n: int, source: str = "jazzhr") -> str:
    fresh_db(path)
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token) "
        "VALUES (:id, :ch, :src, 'A', 'T', 'unknown', 'mid', 'full_time', 'active', "
        ":ts, :ts, :ts, 'b0', 'acme')",
        [{"id": f"x{i}", "ch": f"h{i}", "src": source, "ts": _DAY0} for i in range(n)],
    )
    con.commit()
    con.close()
    return str(path)


def _expired(idx: str) -> int:
    con = sqlite3.connect(idx)
    try:
        return con.execute("SELECT COUNT(*) FROM jobs WHERE status='expired'").fetchone()[0]
    finally:
        con.close()


def _sweep(tmp_path: Path, idx: str, runs: int) -> dict:
    """Run liveness `runs` times against a board that always comes back EMPTY."""
    liv = str(tmp_path / "liv.sqlite")
    open_liveness(liv).close()

    async def fetch_board(source: str, token: str) -> set[str]:
        return set()  # what a provider that swallows a 429/5xx into [] produces

    async def fetch_detail(ref):  # noqa: ANN001, ANN202
        raise AssertionError("jazzhr has no per-posting confirm")

    stats: dict = {}
    for i in range(runs):
        day = f"2026-07-{8 + i * 7:02d}T00:00:00+00:00"
        stats = anyio.run(
            lambda d=day: reconcile_liveness_tier(
                liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: d
            )
        )
    return stats


def test_persistently_empty_board_never_expires_a_sizeable_board(tmp_path: Path) -> None:
    """The audit's shape: a swallowed failure returns [] on two consecutive weekly runs.

    Pre-valve that reached dead_streak 2 and expired 100% of the board while reporting
    boards_failed=0. The board must now be held undetermined instead.
    """
    idx = _liveness_index(tmp_path / "big.sqlite", 50)
    stats = _sweep(tmp_path, idx, runs=2)
    assert _expired(idx) == 0, "an empty board must not expire a sizeable board's rows"
    assert stats["boards_undetermined"] >= 1
    assert stats["flipped_dead"] == 0


def test_small_board_still_expires(tmp_path: Path) -> None:
    """The guard is scoped to sizeable boards on purpose.

    A handful of rows going empty is ordinary closure, not a mass expiry, and guarding it would
    mean a genuinely-closed small board could never clear.
    """
    idx = _liveness_index(tmp_path / "small.sqlite", 3)
    _sweep(tmp_path, idx, runs=2)
    assert _expired(idx) == 3
