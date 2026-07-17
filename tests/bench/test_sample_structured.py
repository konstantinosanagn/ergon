from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.bench.sample_structured import (
    sample_source,
    sample_structured,
    source_counts,
    stratified_sql_ids,
)

# --- pure helper: stratified_sql_ids (uses strata.allocate; no sqlite involved) -----------------


def test_stratified_sql_ids_floor_honored_no_provider_dropped():
    counts = {"workday": 5000, "greenhouse": 300, "ashby": 40, "lever": 120}
    out = stratified_sql_ids(counts, total=1000, floor=100)
    assert set(out) == set(counts)  # no provider silently dropped
    assert out["ashby"] == 40  # floor capped at real availability
    assert out["lever"] >= 100  # small-but-sufficient provider gets its floor
    assert sum(out.values()) == 1000  # total respected
    assert all(out[k] <= counts[k] for k in counts)  # never over-draws a provider


def test_stratified_sql_ids_total_capped_at_available():
    out = stratified_sql_ids({"a": 10, "b": 5}, total=1000, floor=100)
    assert out == {"a": 10, "b": 5}


def test_stratified_sql_ids_degenerate_floor_still_respects_total():
    out = stratified_sql_ids({"a": 100, "b": 100, "c": 100}, total=50, floor=40)
    assert sum(out.values()) == 50
    assert all(v >= 0 for v in out.values())


# --- sqlite sampling: synthetic index fixture ----------------------------------------------------

_COLUMNS = (
    "id",
    "source",
    "company",
    "title",
    "location",
    "city",
    "country",
    "remote",
    "level",
    "employment_type",
    "sector",
    "posted_at",
    "snippet",
)


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "index.sqlite"
    con = sqlite3.connect(str(db_path))
    con.execute(f"CREATE TABLE jobs ({', '.join(f'{c} TEXT' for c in _COLUMNS)}, PRIMARY KEY (id))")
    rows = []
    for i in range(30):
        rows.append(
            (
                f"gh{i:03d}",
                "greenhouse",
                "Acme",
                "Engineer",
                "San Francisco, CA",
                "San Francisco",
                "United States",
                "remote",
                "senior",
                "full_time",
                "Software/SaaS",
                "2026-01-01T00:00:00+00:00",
                "some snippet text",
            )
        )
    for i in range(5):
        rows.append(
            (
                f"lv{i:03d}",
                "lever",
                "Beta",
                "Analyst",
                "New York, NY",
                "New York",
                "United States",
                "onsite",
                "mid",
                "contract",
                "Finance",
                "2026-02-01T00:00:00+00:00",
                "",
            )
        )
    con.executemany(f"INSERT INTO jobs VALUES ({', '.join('?' for _ in _COLUMNS)})", rows)
    con.commit()
    con.close()
    return db_path


def test_source_counts(tmp_path: Path):
    db_path = _make_db(tmp_path)
    con = sqlite3.connect(str(db_path))
    try:
        assert source_counts(con) == {"greenhouse": 30, "lever": 5}
    finally:
        con.close()


def test_sample_source_deterministic_and_never_overdraws(tmp_path: Path):
    db_path = _make_db(tmp_path)
    con = sqlite3.connect(str(db_path))
    try:
        rows1 = sample_source(con, "greenhouse", 10)
        rows2 = sample_source(con, "greenhouse", 10)
        assert [r["id"] for r in rows1] == [r["id"] for r in rows2]  # deterministic reruns
        assert len(rows1) == 10

        # lever only has 5 rows -- asking for 100 must not pad/duplicate to reach 100.
        rows_lever = sample_source(con, "lever", 100)
        assert len(rows_lever) == 5
    finally:
        con.close()


def test_sample_source_rows_have_no_jd_text_but_carry_structured_fields(tmp_path: Path):
    db_path = _make_db(tmp_path)
    con = sqlite3.connect(str(db_path))
    try:
        rows = sample_source(con, "greenhouse", 3)
        assert len(rows) == 3
        for row in rows:
            assert row["description_text"] == ""  # no-JD rows: never relies on JD body
            assert row["source"] == "greenhouse"
            assert row["level"] == "senior"
            assert row["employment_type"] == "full_time"
            assert row["remote"] == "remote"
            assert row["id"].startswith("greenhouse:")
    finally:
        con.close()


def test_sample_structured_end_to_end_stratified_across_sources(tmp_path: Path):
    db_path = _make_db(tmp_path)
    rows, available, realized = sample_structured(db_path, total=20, floor=2)

    assert available == {"greenhouse": 30, "lever": 5}
    assert set(realized) <= set(available)
    assert sum(realized.values()) == len(rows)
    assert realized.get("lever", 0) <= 5  # never over-draws a thin source
    assert realized.get("lever", 0) >= 2  # floor honored

    for row in rows:
        assert row["description_text"] == ""
        assert row["source"] in ("greenhouse", "lever")
