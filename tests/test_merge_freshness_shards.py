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
_DELTA_DDL = (
    "CREATE TABLE board_deltas(source TEXT NOT NULL, board_token TEXT NOT NULL, "
    "added_ids TEXT NOT NULL, idset_hash TEXT NOT NULL, computed_at TEXT NOT NULL, "
    "PRIMARY KEY (source, board_token))"
)


def _mk_shard(tmp_path, name, rows, deltas=None, *, with_delta_table=True):
    """rows: list of (id, expired_at, reason) tuples. deltas: list of
    (source, board_token, added_ids_json, idset_hash, computed_at) tuples. ``with_delta_table``
    False builds a legacy shard that predates the board_deltas table entirely (to exercise the
    merge's per-table resilience)."""
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.execute(_DDL)
    con.executemany("INSERT INTO expired_ids (id, expired_at, reason) VALUES (?, ?, ?)", rows)
    if with_delta_table:
        con.execute(_DELTA_DDL)
        con.executemany(
            "INSERT INTO board_deltas (source, board_token, added_ids, idset_hash, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            deltas or [],
        )
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
        "_total_deltas": 0,  # these shards carried an empty board_deltas table
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
    assert stats["_total_deltas"] == 0
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
    assert "merged 2 shard(s), 2 expired_ids rows, 0 board_deltas rows" in out_text
    con = sqlite3.connect(str(out))
    ids = {r[0] for r in con.execute("SELECT id FROM expired_ids").fetchall()}
    con.close()
    assert ids == {"a", "b"}


def test_merge_unions_board_deltas_across_shards(tmp_path):
    """The Phase-2 board_deltas table is unioned alongside expired_ids: two shards each carrying a
    delta for a DISJOINT board contribute both, and the combined sidecar carries both rows intact
    (added_ids/idset_hash/computed_at preserved verbatim)."""
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [("a", "2026-07-18T00:00:00Z", "departed_board")],
        deltas=[("greenhouse", "acme", '["r1", "r2"]', "hash-acme", "2026-07-18T00:00:00Z")],
    )
    s1 = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [],
        deltas=[("lever", "beta", "[]", "hash-beta", "2026-07-18T00:05:00Z")],
    )
    out = tmp_path / "index-freshness.sqlite"
    stats = mfs.merge_shards([s0, s1], out)

    assert stats["_total"] == 1  # one expired id ('a')
    assert stats["_total_deltas"] == 2  # one delta per shard

    con = sqlite3.connect(str(out))
    try:
        rows = {
            (r[0], r[1]): (r[2], r[3], r[4])
            for r in con.execute(
                "SELECT source, board_token, added_ids, idset_hash, computed_at FROM board_deltas"
            )
        }
    finally:
        con.close()
    assert rows == {
        ("greenhouse", "acme"): ('["r1", "r2"]', "hash-acme", "2026-07-18T00:00:00Z"),
        ("lever", "beta"): ("[]", "hash-beta", "2026-07-18T00:05:00Z"),
    }


def test_merge_board_deltas_or_ignore_on_duplicate_board(tmp_path):
    """A (source, board_token) claimed by an earlier shard wins under OR IGNORE -- a coincidental
    cross-shard duplicate is idempotent, never a PRIMARY KEY crash."""
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        deltas=[("greenhouse", "acme", '["r1"]', "hash-first", "t0")],
    )
    s1 = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [],
        deltas=[("greenhouse", "acme", '["r1", "r2"]', "hash-second", "t1")],
    )
    out = tmp_path / "index-freshness.sqlite"
    stats = mfs.merge_shards([s0, s1], out)
    assert stats["_total_deltas"] == 1  # second shard's dup is ignored

    con = sqlite3.connect(str(out))
    try:
        row = con.execute(
            "SELECT added_ids, idset_hash FROM board_deltas WHERE source='greenhouse'"
        ).fetchone()
    finally:
        con.close()
    assert row == ('["r1"]', "hash-first")  # first shard (sorted order) won


def test_merge_legacy_shard_without_delta_table_still_contributes_expired(tmp_path):
    """A legacy shard predating board_deltas (only expired_ids) merges its expired_ids fine; its
    missing board_deltas table is skipped with a warning, never aborting the merge."""
    legacy = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [("a", "2026-07-18T00:00:00Z", "departed_board")],
        with_delta_table=False,
    )
    modern = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [("b", "2026-07-18T00:05:00Z", "departed_board")],
        deltas=[("greenhouse", "acme", "[]", "hash-acme", "t0")],
    )
    out = tmp_path / "index-freshness.sqlite"
    stats = mfs.merge_shards([legacy, modern], out)

    assert stats["_total"] == 2  # both expired ids present despite the legacy shard's missing table
    assert stats["_total_deltas"] == 1  # only the modern shard had a delta

    con = sqlite3.connect(str(out))
    try:
        ids = {r[0] for r in con.execute("SELECT id FROM expired_ids")}
        deltas = {r[0] for r in con.execute("SELECT source FROM board_deltas")}
    finally:
        con.close()
    assert ids == {"a", "b"}
    assert deltas == {"greenhouse"}


def test_empty_shard_writes_zero_rows(tmp_path, capsys):
    """A shard with zero departed ids (the common case -- most boards have nothing new to
    expire) still merges cleanly and contributes 0 rows, matching freshness_sweep.py's
    always-create-the-table-even-empty sidecar contract."""
    _mk_shard(tmp_path, "index-freshness-shard-0.sqlite", [])
    out = tmp_path / "index-freshness.sqlite"
    rc = mfs.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert "merged 1 shard(s), 0 expired_ids rows, 0 board_deltas rows" in capsys.readouterr().out
    con = sqlite3.connect(str(out))
    n = con.execute("SELECT COUNT(*) FROM expired_ids").fetchone()[0]
    con.close()
    assert n == 0
