"""Tests for scripts/merge_vectors_shards.py -- unions disjoint per-shard rich embedding sidecars
(the sharded-embedding matrix's `index-vectors-shard-N.sqlite` outputs) into one combined
`index-vectors.sqlite`. Reuses `_ensure_schema` for the schema, no duplicated DDL.
"""

from __future__ import annotations

import sqlite3

import pytest

from ergon_tracker.index.rich import _ensure_schema

mvs = pytest.importorskip("scripts.merge_vectors_shards", reason="run from repo root")


def _mk_shard(tmp_path, name, rows, meta=None):
    """rows: list of (id, sig, scale, vec_bytes). meta: optional {key: value} dict."""
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    _ensure_schema(con)
    con.executemany(
        "INSERT INTO job_vectors (id, sig, scale, vec) VALUES (?, ?, ?, ?)", rows
    )
    for k, v in (meta or {}).items():
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (k, v))
    con.commit()
    con.close()
    return p


def test_find_shard_dbs_globs_and_sorts(tmp_path):
    _mk_shard(tmp_path, "index-vectors-shard-1.sqlite", [])
    _mk_shard(tmp_path, "index-vectors-shard-0.sqlite", [])
    _mk_shard(tmp_path, "not-a-shard.sqlite", [])
    found = mvs.find_shard_dbs(tmp_path)
    assert [p.name for p in found] == [
        "index-vectors-shard-0.sqlite",
        "index-vectors-shard-1.sqlite",
    ]


def test_merge_unions_disjoint_shard_rows(tmp_path):
    s0 = _mk_shard(
        tmp_path,
        "index-vectors-shard-0.sqlite",
        [
            ("a", "sa", 0.1, b"\x01\x02\x03"),
            ("b", "sb", 0.2, b"\x04\x05\x06"),
        ],
        meta={"schema_version": "3", "model": "bge-small", "dim": "3", "quant": "int8"},
    )
    s1 = _mk_shard(
        tmp_path,
        "index-vectors-shard-1.sqlite",
        [("c", "sc", 0.3, b"\x07\x08\x09")],
        meta={"schema_version": "3", "model": "bge-small", "dim": "3", "quant": "int8"},
    )
    out = tmp_path / "index-vectors.sqlite"
    stats = mvs.merge_shards([s0, s1], out)
    assert stats == {
        "index-vectors-shard-0.sqlite": 2,
        "index-vectors-shard-1.sqlite": 1,
        "_total": 3,
    }

    con = sqlite3.connect(str(out))
    rows = {
        r[0]: (r[1], r[2], r[3])
        for r in con.execute("SELECT id, sig, scale, vec FROM job_vectors ORDER BY id").fetchall()
    }
    assert rows == {
        "a": ("sa", 0.1, b"\x01\x02\x03"),
        "b": ("sb", 0.2, b"\x04\x05\x06"),
        "c": ("sc", 0.3, b"\x07\x08\x09"),
    }
    # meta carried over from the shards
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    con.close()
    assert meta["model"] == "bge-small"
    assert meta["dim"] == "3"
    assert meta["quant"] == "int8"
    assert meta["schema_version"] == "3"


def test_meta_carried_from_first_shard_not_clobbered(tmp_path):
    """First shard with a key wins (INSERT OR IGNORE); a later shard can't clobber it."""
    s0 = _mk_shard(
        tmp_path, "index-vectors-shard-0.sqlite", [("a", "sa", 1.0, b"\x01")], meta={"model": "first"}
    )
    s1 = _mk_shard(
        tmp_path, "index-vectors-shard-1.sqlite", [("b", "sb", 1.0, b"\x02")], meta={"model": "second"}
    )
    out = tmp_path / "index-vectors.sqlite"
    mvs.merge_shards([s0, s1], out)
    con = sqlite3.connect(str(out))
    model = con.execute("SELECT value FROM meta WHERE key='model'").fetchone()[0]
    con.close()
    assert model == "first"


def test_merge_is_idempotent_on_rerun(tmp_path):
    s0 = _mk_shard(tmp_path, "index-vectors-shard-0.sqlite", [("a", "sa", 1.0, b"\x01")])
    out = tmp_path / "index-vectors.sqlite"
    mvs.merge_shards([s0], out)
    mvs.merge_shards([s0], out)  # re-run (e.g. after a retry) must not error or double the row
    con = sqlite3.connect(str(out))
    n = con.execute("SELECT COUNT(*) FROM job_vectors").fetchone()[0]
    con.close()
    assert n == 1


def test_bad_shard_is_skipped_not_aborted(tmp_path):
    """A truncated/corrupt shard file is skipped with a warning; the good shards still merge."""
    good = _mk_shard(tmp_path, "index-vectors-shard-0.sqlite", [("a", "sa", 1.0, b"\x01")])
    bad = tmp_path / "index-vectors-shard-1.sqlite"
    bad.write_bytes(b"not a sqlite database at all")
    out = tmp_path / "index-vectors.sqlite"
    stats = mvs.merge_shards([good, bad], out)
    assert stats["index-vectors-shard-0.sqlite"] == 1
    assert "index-vectors-shard-1.sqlite" not in stats  # skipped
    assert stats["_total"] == 1
    con = sqlite3.connect(str(out))
    ids = {r[0] for r in con.execute("SELECT id FROM job_vectors").fetchall()}
    con.close()
    assert ids == {"a"}


def test_merge_no_shards_found_returns_error(tmp_path, capsys):
    rc = mvs.main(["--shards-dir", str(tmp_path), "--out", str(tmp_path / "out.sqlite")])
    assert rc == 1
    assert "no index-vectors-shard-*.sqlite files found" in capsys.readouterr().err


def test_main_end_to_end(tmp_path, capsys):
    _mk_shard(tmp_path, "index-vectors-shard-0.sqlite", [("a", "sa", 1.0, b"\x01")])
    _mk_shard(tmp_path, "index-vectors-shard-1.sqlite", [("b", "sb", 1.0, b"\x02")])
    out = tmp_path / "combined" / "index-vectors.sqlite"
    rc = mvs.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    out_text = capsys.readouterr().out
    assert "merged 2 shard(s), 2 rows" in out_text
    con = sqlite3.connect(str(out))
    ids = {r[0] for r in con.execute("SELECT id FROM job_vectors").fetchall()}
    con.close()
    assert ids == {"a", "b"}
