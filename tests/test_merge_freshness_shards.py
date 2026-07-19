"""Tests for scripts/merge_freshness_shards.py -- unions per-shard freshness-sweep sidecars
(the daily host-sharded matrix's `index-freshness-shard-N.sqlite` outputs) into one combined
`index-freshness.sqlite`, for the freshness-sweep workflow's `merge` job. Schema DDL is the pinned
``expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)`` contract also written
by `scripts/freshness_sweep.py` and read by `ergon_tracker.index.build.apply_freshness_expiries`.
"""

from __future__ import annotations

import sqlite3

import pytest

mfs = pytest.importorskip("scripts.merge_freshness_shards", reason="run from repo root")

_DDL = "CREATE TABLE expired_ids(id TEXT PRIMARY KEY, expired_at TEXT NOT NULL, reason TEXT)"


def _mk_shard(tmp_path, name, rows):
    """rows: list of (id, expired_at, reason) tuples."""
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.execute(_DDL)
    con.executemany("INSERT INTO expired_ids (id, expired_at, reason) VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return p


def test_find_shard_dbs_globs_and_sorts(tmp_path):
    _mk_shard(tmp_path, "index-freshness-shard-1.sqlite", [])
    _mk_shard(tmp_path, "index-freshness-shard-0.sqlite", [])
    _mk_shard(tmp_path, "not-a-shard.sqlite", [])
    found = mfs.find_shard_dbs(tmp_path)
    assert [p.name for p in found] == [
        "index-freshness-shard-0.sqlite",
        "index-freshness-shard-1.sqlite",
    ]


def test_merge_unions_disjoint_and_overlapping_ids(tmp_path):
    """3 tiny shards: two disjoint ids each, plus one id ('dup') shared by shards 0 and 2 with
    identical semantics (departed-board rows carry no per-shard-specific meaning worth
    reconciling -- see merge_shards' docstring). The union must contain exactly the 5 distinct
    ids, and the shared id must survive with a single, well-formed row (not duplicated, not
    dropped)."""
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [
            ("a", "2026-07-18T00:00:00Z", "departed_board"),
            ("dup", "2026-07-18T00:00:00Z", "departed_board"),
        ],
    )
    s1 = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [("b", "2026-07-18T00:05:00Z", "departed_board")],
    )
    s2 = _mk_shard(
        tmp_path,
        "index-freshness-shard-2.sqlite",
        [
            ("c", "2026-07-18T00:10:00Z", "departed_board"),
            ("dup", "2026-07-18T00:10:00Z", "departed_board"),  # same id, identical semantics
        ],
    )
    out = tmp_path / "index-freshness.sqlite"
    stats = mfs.merge_shards([s0, s1, s2], out)

    # shard 0 contributes 2 (a, dup); shard 1 contributes 1 (b); shard 2 contributes 1 (c) since
    # its "dup" is ignored as an already-claimed id (OR IGNORE) -- 4 net rows total, 5 shard rows.
    assert stats == {
        "index-freshness-shard-0.sqlite": 2,
        "index-freshness-shard-1.sqlite": 1,
        "index-freshness-shard-2.sqlite": 1,
        "_total": 4,
    }

    con = sqlite3.connect(str(out))
    try:
        ids = {r[0] for r in con.execute("SELECT id FROM expired_ids").fetchall()}
        assert ids == {"a", "b", "c", "dup"}  # union is correct: no loss, no duplication

        # the output db is a valid, well-formed sidecar matching the pinned contract
        row = con.execute(
            "SELECT id, expired_at, reason FROM expired_ids WHERE id='dup'"
        ).fetchone()
        assert row[0] == "dup"
        assert row[1] is not None
        assert row[2] == "departed_board"

        cols = {r[1] for r in con.execute("PRAGMA table_info(expired_ids)").fetchall()}
        assert cols == {"id", "expired_at", "reason"}
    finally:
        con.close()


def test_merge_is_idempotent_on_rerun(tmp_path):
    s0 = _mk_shard(
        tmp_path, "index-freshness-shard-0.sqlite", [("a", "2026-07-18T00:00:00Z", "reason")]
    )
    out = tmp_path / "index-freshness.sqlite"
    mfs.merge_shards([s0], out)
    mfs.merge_shards([s0], out)  # re-run (e.g. after a retry) must not error or double the row
    con = sqlite3.connect(str(out))
    n = con.execute("SELECT COUNT(*) FROM expired_ids").fetchone()[0]
    con.close()
    assert n == 1


def test_bad_shard_is_skipped_not_aborted(tmp_path):
    """A truncated/corrupt shard file is skipped with a warning; the good shards still merge."""
    good = _mk_shard(
        tmp_path, "index-freshness-shard-0.sqlite", [("a", "2026-07-18T00:00:00Z", "reason")]
    )
    bad = tmp_path / "index-freshness-shard-1.sqlite"
    bad.write_bytes(b"not a sqlite database at all")
    out = tmp_path / "index-freshness.sqlite"
    stats = mfs.merge_shards([good, bad], out)
    assert stats["index-freshness-shard-0.sqlite"] == 1
    assert "index-freshness-shard-1.sqlite" not in stats  # skipped
    assert stats["_total"] == 1
    con = sqlite3.connect(str(out))
    ids = {r[0] for r in con.execute("SELECT id FROM expired_ids").fetchall()}
    con.close()
    assert ids == {"a"}


def test_merge_no_shards_found_returns_error(tmp_path, capsys):
    rc = mfs.main(["--shards-dir", str(tmp_path), "--out", str(tmp_path / "out.sqlite")])
    assert rc == 1
    assert "no index-freshness-shard-*.sqlite files found" in capsys.readouterr().err


def test_main_end_to_end(tmp_path, capsys):
    _mk_shard(tmp_path, "index-freshness-shard-0.sqlite", [("a", "2026-07-18T00:00:00Z", "reason")])
    _mk_shard(tmp_path, "index-freshness-shard-1.sqlite", [("b", "2026-07-18T00:00:00Z", "reason")])
    out = tmp_path / "combined" / "index-freshness.sqlite"
    rc = mfs.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    out_text = capsys.readouterr().out
    assert "merged 2 shard(s), 2 rows" in out_text
    con = sqlite3.connect(str(out))
    ids = {r[0] for r in con.execute("SELECT id FROM expired_ids").fetchall()}
    con.close()
    assert ids == {"a", "b"}


def test_empty_shard_writes_zero_rows(tmp_path, capsys):
    """A shard with zero departed ids (the common case -- most boards have nothing new to
    expire) still merges cleanly and contributes 0 rows, matching freshness_sweep.py's
    always-create-the-table-even-empty sidecar contract."""
    _mk_shard(tmp_path, "index-freshness-shard-0.sqlite", [])
    out = tmp_path / "index-freshness.sqlite"
    rc = mfs.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert "merged 1 shard(s), 0 rows" in capsys.readouterr().out
    con = sqlite3.connect(str(out))
    n = con.execute("SELECT COUNT(*) FROM expired_ids").fetchone()[0]
    con.close()
    assert n == 0
