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
_STATS_DDL = (
    "CREATE TABLE source_stats(source TEXT NOT NULL, checked INTEGER NOT NULL, "
    "candidates INTEGER NOT NULL, departed INTEGER NOT NULL, expired INTEGER NOT NULL, "
    "confirmed_alive INTEGER NOT NULL, unconfirmed INTEGER NOT NULL, errored INTEGER NOT NULL)"
)
_STATS_COLS = (
    "source",
    "checked",
    "candidates",
    "departed",
    "expired",
    "confirmed_alive",
    "unconfirmed",
    "errored",
)


def _mk_shard(
    tmp_path,
    name,
    rows,
    deltas=None,
    source_stats=None,
    *,
    with_delta_table=True,
    with_stats_table=True,
):
    """rows: list of (id, expired_at, reason) tuples. deltas: list of
    (source, board_token, added_ids_json, idset_hash, computed_at) tuples. source_stats: list of
    dicts with a subset of ``_STATS_COLS`` keys (missing keys default 0), mirroring
    ``freshness_sweep.py``'s ``_stats_rows`` -- one row per source THIS SHARD saw.
    ``with_delta_table``/``with_stats_table`` False builds a legacy shard that predates that table
    entirely (to exercise the merge's per-table resilience)."""
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
    if with_stats_table:
        con.execute(_STATS_DDL)
        con.executemany(
            f"INSERT INTO source_stats ({', '.join(_STATS_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _STATS_COLS)})",
            [
                tuple(row[c] if c == "source" else row.get(c, 0) for c in _STATS_COLS)
                for row in (source_stats or [])
            ],
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
        "_expiry_alarms": [],  # these legacy-style shards carried no source_stats table
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


# --- source_stats: the expiry-rate monitor's cross-shard aggregation ---------------------------


def test_merge_sums_source_stats_across_shards(tmp_path):
    """Two shards each covering a DISJOINT slice of the same source's boards contribute PARTIAL
    counts; the merged ``source_stats`` table must hold the SUMMED grand total, not either shard's
    slice alone (unlike expired_ids/board_deltas, this is arithmetic, not OR-IGNORE dedup)."""
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        source_stats=[
            {"source": "taleo", "checked": 5, "candidates": 5, "expired": 3, "confirmed_alive": 2}
        ],
    )
    s1 = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [],
        source_stats=[
            {"source": "taleo", "checked": 5, "candidates": 5, "expired": 1, "confirmed_alive": 4}
        ],
    )
    out = tmp_path / "index-freshness.sqlite"
    mfs.merge_shards([s0, s1], out)

    con = sqlite3.connect(str(out))
    try:
        row = con.execute(
            "SELECT checked, candidates, expired, confirmed_alive FROM source_stats "
            "WHERE source='taleo'"
        ).fetchone()
    finally:
        con.close()
    assert row == (10, 10, 4, 6)  # summed, not either shard's own 5/5/3/2 or 5/5/1/4


def test_merge_source_stats_covers_multiple_sources_independently(tmp_path):
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        source_stats=[
            {"source": "taleo", "checked": 5, "candidates": 5, "expired": 3},
            {"source": "adp", "checked": 2, "candidates": 2, "expired": 0},
        ],
    )
    out = tmp_path / "index-freshness.sqlite"
    mfs.merge_shards([s0], out)
    con = sqlite3.connect(str(out))
    try:
        sources = {r[0] for r in con.execute("SELECT source FROM source_stats")}
    finally:
        con.close()
    assert sources == {"taleo", "adp"}


def test_merge_source_stats_is_idempotent_on_rerun(tmp_path):
    """A merge re-run over the SAME shard set must not double the summed counts (recompute-from-
    scratch, not an incremental add -- see merge_shards' docstring)."""
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        source_stats=[{"source": "taleo", "checked": 5, "candidates": 5, "expired": 3}],
    )
    out = tmp_path / "index-freshness.sqlite"
    mfs.merge_shards([s0], out)
    mfs.merge_shards([s0], out)  # re-run
    con = sqlite3.connect(str(out))
    try:
        row = con.execute(
            "SELECT expired FROM source_stats WHERE source='taleo'"
        ).fetchone()
        n = con.execute("SELECT COUNT(*) FROM source_stats").fetchone()[0]
    finally:
        con.close()
    assert row == (3,)  # not doubled to 6
    assert n == 1  # not duplicated to 2 rows


def test_merge_legacy_shard_without_source_stats_table_still_contributes_other_tables(tmp_path):
    """A legacy shard predating source_stats merges its expired_ids/board_deltas fine; its missing
    source_stats table is skipped with a warning, never aborting the merge (mirrors the existing
    board_deltas legacy-shard resilience test above)."""
    legacy = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [("a", "2026-07-18T00:00:00Z", "departed_board")],
        with_stats_table=False,
    )
    modern = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [("b", "2026-07-18T00:05:00Z", "departed_board")],
        source_stats=[{"source": "taleo", "checked": 5, "candidates": 5, "expired": 1}],
    )
    out = tmp_path / "index-freshness.sqlite"
    stats = mfs.merge_shards([legacy, modern], out)
    assert stats["_total"] == 2

    con = sqlite3.connect(str(out))
    try:
        sources = {r[0] for r in con.execute("SELECT source FROM source_stats")}
    finally:
        con.close()
    assert sources == {"taleo"}


# --- expiry-rate alarm evaluated on the MERGED cross-shard totals ------------------------------


def test_merge_fires_expiry_alarm_only_visible_after_summing_shards(tmp_path, caplog):
    """Neither shard's OWN slice crosses the count floor (3 each, floor is 5) or looks alarming in
    isolation, but their SUM (6 expired / 6 candidates = rate 1.0) does. Proves the alarm is
    evaluated on the merged totals, not per-shard (see check_expiry_alarms' own docstring on why a
    per-shard rate is untrustworthy)."""
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        source_stats=[{"source": "taleo", "checked": 3, "candidates": 3, "expired": 3}],
    )
    s1 = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [],
        source_stats=[{"source": "taleo", "checked": 3, "candidates": 3, "expired": 3}],
    )
    out = tmp_path / "index-freshness.sqlite"
    with caplog.at_level("WARNING"):
        stats = mfs.merge_shards([s0, s1], out)
    assert stats["_expiry_alarms"] == ["taleo"]
    assert any("taleo" in r.message and "EXPIRY RATE ALARM" in r.message for r in caplog.records)


def test_merge_no_alarm_when_merged_rate_stays_under_threshold(tmp_path, caplog):
    s0 = _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        source_stats=[{"source": "oracle", "checked": 50, "candidates": 50, "expired": 5}],
    )
    s1 = _mk_shard(
        tmp_path,
        "index-freshness-shard-1.sqlite",
        [],
        source_stats=[{"source": "oracle", "checked": 50, "candidates": 50, "expired": 5}],
    )
    out = tmp_path / "index-freshness.sqlite"
    with caplog.at_level("WARNING"):
        stats = mfs.merge_shards([s0, s1], out)
    assert stats["_expiry_alarms"] == []
    assert not any("EXPIRY RATE ALARM" in r.message for r in caplog.records)


def test_main_prints_expiry_alarm_summary_line(tmp_path, capsys):
    _mk_shard(
        tmp_path,
        "index-freshness-shard-0.sqlite",
        [],
        source_stats=[{"source": "taleo", "checked": 10, "candidates": 10, "expired": 9}],
    )
    out = tmp_path / "index-freshness.sqlite"
    rc = mfs.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "EXPIRY RATE ALARM: 1 source(s) spiked -- taleo" in out_text


def test_main_prints_no_alarm_line_when_nothing_fires(tmp_path, capsys):
    _mk_shard(tmp_path, "index-freshness-shard-0.sqlite", [])
    out = tmp_path / "index-freshness.sqlite"
    rc = mfs.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert "EXPIRY RATE ALARM" not in capsys.readouterr().out
