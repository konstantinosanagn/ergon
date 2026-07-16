"""Regression: merge_detail_into_index must read O(sidecar rows), never a whole-table `SELECT *
FROM jobs`. This proves the merge is scoped to rows present in the (bounded) job_detail sidecar
by building an index with rows the sidecar never mentions and asserting they're untouched, and
that the returned count matches exactly the sidecar-matched rows."""

import sqlite3

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.detail import detail_sig, merge_detail_into_index, open_detail


def _mk_real_index(tmp_path, job_rows):
    """Build an index DB against the REAL production `jobs` schema (schema.sql via db.fresh_db)."""
    p = tmp_path / "real_index.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    for row in job_rows:
        defaults = {
            "source": "oracle",
            "company": "Acme",
            "remote": "unknown",
            "level": "mid",
            "employment_type": "full_time",
            "ts": "2026-07-01T00:00:00Z",
            "build_id": "b1",
            "salary_min": None,
            "salary_max": None,
            "salary_currency": None,
            "salary_interval": None,
            "years_min": None,
            "years_max": None,
            "degree_min": None,
            "degree_required": None,
            "sponsorship_offered": None,
            "snippet": None,
        }
        defaults.update(row)
        con.execute(
            "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
            "employment_type, status, first_seen, last_seen, fetched_at, build_id, "
            "salary_min, salary_max, salary_currency, salary_interval, years_min, years_max, "
            "degree_min, degree_required, sponsorship_offered, snippet) "
            "VALUES (:id, :content_hash, :source, :company, :title, :remote, :level, "
            ":employment_type, 'active', :ts, :ts, :ts, :build_id, "
            ":salary_min, :salary_max, :salary_currency, :salary_interval, :years_min, "
            ":years_max, :degree_min, :degree_required, :sponsorship_offered, :snippet)",
            defaults,
        )
    con.commit()
    return con


def _mk_detail_sidecar(tmp_path, rows):
    """rows: list of dicts with at least id, sig; other job_detail columns default to None."""
    p = tmp_path / "detail.sqlite"
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
    con.commit()
    con.close()
    return str(p)


def test_merge_is_scoped_to_sidecar_rows_not_whole_table(tmp_path):
    """5 index rows, but the sidecar only mentions 2 of them (with matching sigs). Only those 2
    should be merged/counted; the other 3 (never in the sidecar) must be completely untouched --
    proving the merge reads O(sidecar rows) via the ATTACH+JOIN, not a whole-table jobs scan."""
    good_sig = detail_sig({"content_hash": "h1", "title": "Engineer", "level": "mid"})
    idx = _mk_real_index(
        tmp_path,
        [
            {"id": "1", "content_hash": "h1", "title": "Engineer"},  # in sidecar -- should merge
            {"id": "2", "content_hash": "h1", "title": "Engineer"},  # in sidecar -- should merge
            {
                "id": "3",
                "content_hash": "h1",
                "title": "Engineer",
            },  # NOT in sidecar -- must be untouched
            {
                "id": "4",
                "content_hash": "h1",
                "title": "Engineer",
            },  # NOT in sidecar -- must be untouched
            {
                "id": "5",
                "content_hash": "h1",
                "title": "Engineer",
            },  # NOT in sidecar -- must be untouched
        ],
    )
    det = _mk_detail_sidecar(
        tmp_path,
        [
            {"id": "1", "sig": good_sig, "salary_min": 90000.0, "snippet": "Role one."},
            {"id": "2", "sig": good_sig, "salary_min": 95000.0, "snippet": "Role two."},
        ],
    )

    n = merge_detail_into_index(idx, det)
    assert n == 2  # exactly the two sidecar-matched rows were merged

    row1 = idx.execute("SELECT salary_min, snippet FROM jobs WHERE id='1'").fetchone()
    assert row1 == (90000.0, "Role one.")
    row2 = idx.execute("SELECT salary_min, snippet FROM jobs WHERE id='2'").fetchone()
    assert row2 == (95000.0, "Role two.")

    # The 3 rows absent from the sidecar must be completely untouched (still NULL).
    for id_ in ("3", "4", "5"):
        row = idx.execute("SELECT salary_min, snippet FROM jobs WHERE id=?", (id_,)).fetchone()
        assert row == (None, None), f"row {id_} should be untouched but was {row}"
