"""Tests for scripts/merge_detail_shards.py -- unions disjoint per-shard Tier-3 detail sidecars
(the drain matrix's `index-detail-shard-N.sqlite` outputs) into one combined `index-detail.sqlite`,
for the drain workflow's `merge` job. Reuses `open_detail` for schema, no duplicated DDL.
"""

from __future__ import annotations

import sqlite3

import pytest

from ergon.index.detail import open_detail

mds = pytest.importorskip("scripts.merge_detail_shards", reason="run from repo root")


# The pre-v3 sidecar DDL (no remote/employment_type/level/sector), for the old-shard-artifact
# tolerance test below. Frozen on purpose: it is what a shard uploaded by an older drain contains.
_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_detail (
  id TEXT PRIMARY KEY,
  sig TEXT,
  fetched_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  snippet TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
  years_min INTEGER, years_max INTEGER,
  degree_min TEXT, degree_required INTEGER,
  sponsorship_offered INTEGER,
  city TEXT, country TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _insert_rows(con, rows):
    """Insert `rows` (dicts keyed by column name) into whatever job_detail columns this db has."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(job_detail)")]
    for row in rows:
        unknown = set(row) - set(cols)
        assert not unknown, f"test row sets non-existent column(s): {sorted(unknown)}"
        values = dict.fromkeys(cols)
        values["fetched_at"] = "2026-07-01T00:00:00Z"
        values["attempts"] = 0
        values.update(row)
        placeholders = ", ".join(f":{c}" for c in cols)
        con.execute(f"INSERT INTO job_detail ({', '.join(cols)}) VALUES ({placeholders})", values)


def _mk_shard(tmp_path, name, rows, cursor=None):
    """rows: list of dicts with at least id, sig; other job_detail columns default to None."""
    p = tmp_path / name
    con = open_detail(str(p))
    _insert_rows(con, rows)
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


def test_merge_prefers_freshest_row_regardless_of_shard_order(tmp_path):
    """Regression for the sharded-drain merge-correctness bug: if a row's carry-forward seed
    (see ``.github/workflows/drain-detail.yml`` -- each shard's sidecar is `cp`'d from the prior
    FULL combined ``index-detail.sqlite``) somehow makes it into two shards' OUTPUT artifacts, the
    combine must keep the FRESH one and never let a STALE carry-forward clobber it -- in EITHER
    merge order. One shard has row 'x' freshly fetched (fetched_at set, real recovered fields);
    the other has the SAME id as a stale carry-forward (fetched_at NULL, no recovered fields).
    """
    fresh_row = {
        "id": "x",
        "sig": "sig-fresh",
        "fetched_at": "2026-07-10T12:00:00Z",
        "attempts": 0,
        "snippet": "Real recovered JD snippet.",
        "salary_min": 90000.0,
        "salary_max": 120000.0,
    }
    stale_row = {
        "id": "x",
        "sig": "sig-stale",
        "fetched_at": None,
        "attempts": 0,
        "snippet": None,
        "salary_min": None,
        "salary_max": None,
    }
    expected = (
        "sig-fresh",
        "2026-07-10T12:00:00Z",
        "Real recovered JD snippet.",
        90000.0,
        120000.0,
    )

    for label, first, second in [
        ("fresh-then-stale", fresh_row, stale_row),
        ("stale-then-fresh", stale_row, fresh_row),
    ]:
        a = _mk_shard(tmp_path, f"index-detail-shard-0-{label}.sqlite", [first])
        b = _mk_shard(tmp_path, f"index-detail-shard-1-{label}.sqlite", [second])
        out = tmp_path / f"combined-{label}.sqlite"
        mds.merge_shards([a, b], out)

        con = open_detail(str(out))
        row = con.execute(
            "SELECT sig, fetched_at, snippet, salary_min, salary_max FROM job_detail WHERE id='x'"
        ).fetchone()
        con.close()
        assert row == expected, f"order {label}: fresh row must survive the merge"


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


# --- non-destructive combine (the 2026-07-26 with_jd 85%->47% regression) -------------------


def test_partial_shard_set_without_base_drops_missing_shard_rows(tmp_path):
    """Reproduces the destructive-combine bug: a shard that timed out never uploaded, so the
    fresh-empty combine publishes ONLY the finished shard's rows and (via the drain's --clobber)
    permanently deletes the missing shard's previously-drained rows -> with_jd cliff."""
    s0 = _mk_shard(
        tmp_path, "index-detail-shard-0.sqlite", [{"id": "A", "sig": "sA", "snippet": "JD-A"}]
    )
    # shard 1 (row B) "timed out" and never uploaded -> absent from the merge input.
    out = tmp_path / "combined.sqlite"
    mds.merge_shards([s0], out)  # legacy fresh-empty combine, no base
    con = open_detail(str(out))
    ids = [r[0] for r in con.execute("SELECT id FROM job_detail ORDER BY id")]
    con.close()
    assert ids == ["A"]  # row B is GONE -- the destructive clobber this fix targets


def test_partial_shard_set_with_base_preserves_missing_shard_rows(tmp_path):
    """The fix: seeding the combine from the prior FULL sidecar preserves the un-finished shard's
    rows AND still refreshes the finished shard's rows (prefer-freshest, fetched_at-keyed)."""
    base = _mk_shard(
        tmp_path,
        "prior-full.sqlite",
        [
            {"id": "A", "sig": "sA", "snippet": "JD-A-old", "fetched_at": "2026-07-20T00:00:00Z"},
            {"id": "B", "sig": "sB", "snippet": "JD-B", "fetched_at": "2026-07-20T00:00:00Z"},
        ],
    )
    # This drain: only shard 0 (A, freshly re-fetched) finished; shard 1 (B) timed out.
    s0 = _mk_shard(
        tmp_path,
        "index-detail-shard-0.sqlite",
        [{"id": "A", "sig": "sA", "snippet": "JD-A-new", "fetched_at": "2026-07-26T00:00:00Z"}],
    )
    out = tmp_path / "combined.sqlite"
    mds.merge_shards([s0], out, base_path=base)
    con = open_detail(str(out))
    got = dict(con.execute("SELECT id, snippet FROM job_detail ORDER BY id").fetchall())
    con.close()
    assert got == {
        "A": "JD-A-new",
        "B": "JD-B",
    }  # A refreshed from the shard, B PRESERVED from base


# --- v3 recovered metadata (remote/employment_type/level/sector) ----------------------------


def _mk_v2_shard(tmp_path, name, rows):
    """A shard artifact written by a PRE-v3 drain: job_detail has no remote/employment_type/
    level/sector columns at all (and meta says schema_version 2)."""
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.executescript(_V2_SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '2')")
    _insert_rows(con, rows)
    con.commit()
    con.close()
    return p


def test_shard_select_list_nulls_out_columns_an_old_shard_lacks():
    columns = ("id", "snippet", "remote", "sector")
    assert (
        mds.shard_select_list(columns, {"id", "snippet"})
        == "id, snippet, NULL AS remote, NULL AS sector"
    )


def test_merge_carries_v3_columns_and_tolerates_a_v2_shard(tmp_path):
    """The four v3 columns must survive the combine (they were dropped by the old hardcoded
    column list), and a shard artifact from a pre-v3 drain must still merge -- its missing
    columns read as unknown rather than blowing up the SELECT."""
    v3 = _mk_shard(
        tmp_path,
        "index-detail-shard-0.sqlite",
        [
            {
                "id": "A",
                "sig": "sA",
                "snippet": "JD-A",
                "remote": "hybrid",
                "employment_type": "full_time",
                "level": "senior",
                "sector": "fintech",
            }
        ],
    )
    v2 = _mk_v2_shard(
        tmp_path, "index-detail-shard-1.sqlite", [{"id": "B", "sig": "sB", "snippet": "JD-B"}]
    )
    out = tmp_path / "combined.sqlite"
    stats = mds.merge_shards([v3, v2], out)
    assert stats["_total"] == 2

    con = open_detail(str(out))
    got = {
        r[0]: tuple(r[1:])
        for r in con.execute(
            "SELECT id, snippet, remote, employment_type, level, sector FROM job_detail"
        )
    }
    con.close()
    assert got["A"] == ("JD-A", "hybrid", "full_time", "senior", "fintech")
    assert got["B"] == ("JD-B", None, None, None, None)  # v2 shard: unknown, not an error


def test_known_metadata_is_not_clobbered_by_a_later_unknown(tmp_path):
    """A re-fetch whose provider did not expose these fields this time must not blank out what an
    earlier drain already recovered -- neither via NULL nor via the literal "unknown" sentinel."""
    base = _mk_shard(
        tmp_path,
        "prior-full.sqlite",
        [
            {
                "id": "X",
                "sig": "sX",
                "snippet": "JD-old",
                "fetched_at": "2026-07-20T00:00:00Z",
                "remote": "hybrid",
                "employment_type": "contract",
                "level": "senior",
                "sector": "fintech",
            }
        ],
    )
    s0 = _mk_shard(
        tmp_path,
        "index-detail-shard-0.sqlite",
        [
            {
                "id": "X",
                "sig": "sX",
                "snippet": "JD-new",
                "fetched_at": "2026-07-26T00:00:00Z",
                "remote": None,  # not recovered this time
                "employment_type": "unknown",  # recovered as the UNKNOWN enum member
                "level": None,
                "sector": None,
            }
        ],
    )
    out = tmp_path / "combined.sqlite"
    mds.merge_shards([s0], out, base_path=base)

    con = open_detail(str(out))
    row = con.execute(
        "SELECT snippet, remote, employment_type, level, sector FROM job_detail WHERE id='X'"
    ).fetchone()
    con.close()
    # snippet still refreshes (prefer-freshest row upsert); the metadata is preserved.
    assert row == ("JD-new", "hybrid", "contract", "senior", "fintech")


def test_unknown_metadata_is_filled_by_a_known_value(tmp_path):
    """The other direction: a row carrying unknown/NULL metadata takes the freshly recovered value."""
    base = _mk_shard(
        tmp_path,
        "prior-full.sqlite",
        [
            {
                "id": "X",
                "sig": "sX",
                "snippet": "JD-old",
                "fetched_at": "2026-07-20T00:00:00Z",
                "remote": "unknown",
                "employment_type": None,
                "level": "unknown",
                "sector": None,
            }
        ],
    )
    s0 = _mk_shard(
        tmp_path,
        "index-detail-shard-0.sqlite",
        [
            {
                "id": "X",
                "sig": "sX",
                "snippet": "JD-new",
                "fetched_at": "2026-07-26T00:00:00Z",
                "remote": "remote",
                "employment_type": "part_time",
                "level": "mid",
                "sector": "healthcare",
            }
        ],
    )
    out = tmp_path / "combined.sqlite"
    mds.merge_shards([s0], out, base_path=base)

    con = open_detail(str(out))
    row = con.execute(
        "SELECT remote, employment_type, level, sector FROM job_detail WHERE id='X'"
    ).fetchone()
    con.close()
    assert row == ("remote", "part_time", "mid", "healthcare")
