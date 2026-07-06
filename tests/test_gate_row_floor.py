"""Row-floor gate hardening: a MISSING previous snapshot must not weaken the floor to >0.

Before this, if the live prev index failed to download, ``prev_row_count`` was None and the
row_floor gate degraded to "rows > 0" — so a fresh-only collapse (e.g. the 2026-07-05 carry-forward
freeze) could have PUBLISHED over a good ~1.44M snapshot. The fix recovers a durable floor from the
last successfully published row count in history.jsonl (restored from the release every CI run), and
only allows a below-floor publish when ``ERGON_ALLOW_COLD_START`` is set (genuine first build/reset).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ergon_tracker.index.db import connect, fresh_db
from ergon_tracker.index.gates import evaluate_gates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_index import _last_published_rows  # noqa: E402

_REQ = ("greenhouse", "A", "unknown", "mid", "fulltime", "2026-07-01", "2026-07-01", "2026-07-01", "b0")


def _tiny_index(path: Path, n: int) -> None:
    """A schema-valid index with ``n`` jobs (the 'collapse' artifact under test)."""
    fresh_db(path)
    con = connect(path)
    con.execute("INSERT INTO companies(company_key,display_name) VALUES('a','A')")
    for i in range(n):
        con.execute(
            "INSERT INTO jobs(id,content_hash,company_key,title,source,company,remote,level,"
            "employment_type,first_seen,last_seen,fetched_at,build_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"j{i}", f"h{i}", "a", f"t{i}", *_REQ),
        )
    con.commit()
    con.close()


def _floor(rep):
    return next(r for r in rep.results if r.name == "row_floor")


def test_missing_prev_with_history_blocks_collapse(tmp_path: Path) -> None:
    """Live prev absent (download failed) but history knows the last size -> collapse FAILS."""
    db = tmp_path / "collapse.sqlite"
    _tiny_index(db, 5)
    rep = evaluate_gates(db, prev_row_count=None, last_known_rows=1_456_461)
    assert not _floor(rep).passed
    assert "history[live prev MISSING]" in _floor(rep).detail


def test_genuine_cold_start_passes(tmp_path: Path) -> None:
    """No live prev AND no history -> a true first build publishes on rows > 0."""
    db = tmp_path / "cold.sqlite"
    _tiny_index(db, 5)
    rep = evaluate_gates(db, prev_row_count=None, last_known_rows=None)
    assert _floor(rep).passed


def test_cold_start_override_allows_below_floor(tmp_path: Path) -> None:
    """Explicit operator override permits an intentional shrink/reset below the historical floor."""
    db = tmp_path / "reset.sqlite"
    _tiny_index(db, 5)
    rep = evaluate_gates(db, prev_row_count=None, last_known_rows=1_456_461, allow_cold_start=True)
    assert _floor(rep).passed
    assert "override" in _floor(rep).detail


def test_live_prev_still_used_when_present(tmp_path: Path) -> None:
    """When the live prev IS present its count is the basis (unchanged normal behavior)."""
    db = tmp_path / "ok.sqlite"
    _tiny_index(db, 5)
    rep = evaluate_gates(db, prev_row_count=6, last_known_rows=None)  # floor = int(6*0.75) = 4
    assert _floor(rep).passed
    assert "prev 6" in _floor(rep).detail


def test_last_published_rows_ignores_failed_and_missing(tmp_path: Path) -> None:
    h = tmp_path / "history.jsonl"
    h.write_text(
        "\n".join(
            [
                json.dumps({"total_jobs": 1_456_461, "published": True}),
                json.dumps({"total_jobs": 341_113, "published": False}),  # a failed build — ignored
            ]
        )
    )
    assert _last_published_rows(h) == 1_456_461
    assert _last_published_rows(tmp_path / "nope.jsonl") is None
