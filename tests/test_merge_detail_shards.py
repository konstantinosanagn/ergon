"""Tests for scripts/merge_detail_shards.py -- unions disjoint per-shard Tier-3 detail sidecars
(the drain matrix's `index-detail-shard-N.sqlite` outputs) into one combined `index-detail.sqlite`,
for the drain workflow's `merge` job. Reuses `open_detail` for schema, no duplicated DDL.
"""

from __future__ import annotations

import pytest

from ergon_tracker.index.detail import open_detail

mds = pytest.importorskip("scripts.merge_detail_shards", reason="run from repo root")


def _mk_shard(tmp_path, name, rows, cursor=None):
    """rows: list of dicts with at least id, sig; other job_detail columns default to None."""
    p = tmp_path / name
    con = open_detail(str(p))
    for row in rows:
        defaults = {
            "id": None,
            "sig": None,
            "fetched_at": "2026-07-01T00:00:00Z",
            "attempts": 0,
            "snippet": None,
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "salary_interval": None,
            "years_min": None,
            "years_max": None,
            "degree_min": None,
            "degree_required": None,
            "sponsorship_offered": None,
        }
        defaults.update(row)
        con.execute(
            "INSERT INTO job_detail (id, sig, fetched_at, attempts, snippet, salary_min, "
            "salary_max, salary_currency, salary_interval, years_min, years_max, degree_min, "
            "degree_required, sponsorship_offered) "
            "VALUES (:id, :sig, :fetched_at, :attempts, :snippet, :salary_min, :salary_max, "
            ":salary_currency, :salary_interval, :years_min, :years_max, :degree_min, "
            ":degree_required, :sponsorship_offered)",
            defaults,
        )
    if cursor is not None:
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('detail_cursor', ?)", (str(cursor),)
        )
    con.commit()
    con.close()
    return p


def test_find_shard_dbs_globs_and_sorts(tmp_path):
    _mk_shard(tmp_path, "index-detail-shard-1.sqlite", [])
    _mk_shard(tmp_path, "index-detail-shard-0.sqlite", [])
    _mk_shard(tmp_path, "not-a-shard.sqlite", [])
    found = mds.find_shard_dbs(tmp_path)
    assert [p.name for p in found] == ["index-detail-shard-0.sqlite", "index-detail-shard-1.sqlite"]


def test_merge_unions_disjoint_shard_rows(tmp_path):
    s0 = _mk_shard(
        tmp_path,
        "index-detail-shard-0.sqlite",
        [{"id": "a", "sig": "sa", "salary_min": 1.0}, {"id": "b", "sig": "sb", "salary_min": 2.0}],
        cursor=5,
    )
    s1 = _mk_shard(
        tmp_path,
        "index-detail-shard-1.sqlite",
        [{"id": "c", "sig": "sc", "salary_min": 3.0}],
        cursor=9,
    )
    out = tmp_path / "index-detail.sqlite"
    stats = mds.merge_shards([s0, s1], out)
    assert stats == {
        "index-detail-shard-0.sqlite": 2,
        "index-detail-shard-1.sqlite": 1,
        "_total": 3,
    }

    con = open_detail(str(out))
    rows = {
        r[0]: r[1]
        for r in con.execute("SELECT id, salary_min FROM job_detail ORDER BY id").fetchall()
    }
    assert rows == {"a": 1.0, "b": 2.0, "c": 3.0}  # no loss, no duplication

    # last-shard-wins cursor policy (documented, non-load-bearing for correctness)
    cursor = con.execute("SELECT value FROM meta WHERE key='detail_cursor'").fetchone()[0]
    assert cursor == "9"


def test_merge_is_idempotent_on_rerun(tmp_path):
    s0 = _mk_shard(tmp_path, "index-detail-shard-0.sqlite", [{"id": "a", "sig": "sa"}])
    out = tmp_path / "index-detail.sqlite"
    mds.merge_shards([s0], out)
    mds.merge_shards([s0], out)  # re-run (e.g. after a retry) must not error or double the row
    con = open_detail(str(out))
    n = con.execute("SELECT COUNT(*) FROM job_detail").fetchone()[0]
    assert n == 1


def test_merge_no_shards_found_returns_error(tmp_path, capsys):
    rc = mds.main(["--shards-dir", str(tmp_path), "--out", str(tmp_path / "out.sqlite")])
    assert rc == 1
    assert "no index-detail-shard-*.sqlite files found" in capsys.readouterr().err


def test_main_end_to_end(tmp_path, capsys):
    _mk_shard(tmp_path, "index-detail-shard-0.sqlite", [{"id": "a", "sig": "sa"}])
    _mk_shard(tmp_path, "index-detail-shard-1.sqlite", [{"id": "b", "sig": "sb"}])
    out = tmp_path / "combined" / "index-detail.sqlite"
    rc = mds.main(["--shards-dir", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    out_text = capsys.readouterr().out
    assert "merged 2 shard(s), 2 rows" in out_text
    con = open_detail(str(out))
    ids = {r[0] for r in con.execute("SELECT id FROM job_detail").fetchall()}
    assert ids == {"a", "b"}
