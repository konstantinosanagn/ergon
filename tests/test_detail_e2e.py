"""End-to-end synthetic stress test for the Tier-3 detail-fetcher pipeline.

Drives the FULL pipeline -- `reconcile_detail_tier` (fetch + extract + sidecar write) then
`merge_detail_into_index` (sig-gated apply into the real index columns) -- against a real-schema
index (`ergon_tracker.index.db.fresh_db`) with many synthetic smartrecruiters postings, through a
FAKE `fetch_detail` that returns canned, deterministically-varied JDs for most refs, raises
`TimeoutError` for a designated failing subset, and returns `None` (no JD found) for another
subset. Everything is offline and deterministic: no real network, no wall-clock reads (`now` is
injected), no randomness (row variety is derived from the row index).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable

import anyio

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.detail import merge_detail_into_index, open_detail, reconcile_detail_tier

_NOW = "2026-07-12T00:00:00Z"


# --- synthetic index builder (real schema, smartrecruiters rows, empty snippet) -----------------


def _row_id(i: int) -> str:
    return f"sr-{i:03d}"


def _build_index(tmp_path, n: int, *, name: str = "index") -> str:
    """A real-schema index (via `fresh_db`) with `n` smartrecruiters rows, all with an empty
    `snippet` (the real Tier-3 candidate signal) and distinct `content_hash`/`id`."""
    p = tmp_path / f"{name}.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    rows = [
        {
            "id": _row_id(i),
            "content_hash": f"ch-{i:03d}",
            "source": "smartrecruiters",
            "company": "Acme",
            "title": f"Engineer {i}",
            "remote": "unknown",
            "level": "mid",
            "employment_type": "full_time",
            "ts": _NOW,
            "build_id": "b1",
            "board_token": "srtoken",
            "apply_url": f"http://x/{_row_id(i)}",
            "listing_url": None,
        }
        for i in range(n)
    ]
    con.executemany(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token, "
        "apply_url, listing_url) "
        "VALUES (:id, :content_hash, :source, :company, :title, :remote, :level, "
        ":employment_type, 'active', :ts, :ts, :ts, :build_id, :board_token, :apply_url, "
        ":listing_url)",
        rows,
    )
    con.commit()
    con.close()
    return str(p)


# --- canned synthetic JDs (parseable salary/years/degree, varied by row index, no RNG) ----------


def _expected(i: int) -> dict[str, object]:
    salary_min = 80_000 + (i % 10) * 5_000
    salary_max = salary_min + 30_000
    years = 2 + (i % 6)
    degree_min = "bachelor" if i % 2 == 0 else "master"
    return {
        "salary_min": float(salary_min),
        "salary_max": float(salary_max),
        "years_min": years,
        "degree_min": degree_min,
        "degree_required": 1,
    }


def _canned_good_jd(i: int) -> str:
    exp = _expected(i)
    degree_phrase = (
        "Bachelor's degree in CS required." if exp["degree_min"] == "bachelor"
        else "Master's degree required."
    )
    salary_min = int(exp["salary_min"])  # type: ignore[arg-type]
    salary_max = int(exp["salary_max"])  # type: ignore[arg-type]
    return (
        f"<p>Great opportunity, requisition {i}. {degree_phrase} "
        f"Minimum {exp['years_min']} years experience required. "
        f"Salary: ${salary_min:,} - ${salary_max:,} / year.</p>"
    )


def _make_fetcher(
    failing_ids: set[int], none_ids: set[int], calls: list[str]
) -> Callable[[object], Awaitable[str | None]]:
    async def fetch_detail(ref: object) -> str | None:
        calls.append(ref.id)  # type: ignore[attr-defined]
        i = int(ref.id.rsplit("-", 1)[1])  # type: ignore[attr-defined]
        if i in failing_ids:
            raise TimeoutError(f"dead page: {ref.id}")  # type: ignore[attr-defined]
        if i in none_ids:
            return None
        return _canned_good_jd(i)

    return fetch_detail


# --- tests ----------------------------------------------------------------------------------


def test_good_rows_recover_salary_years_degree_into_index(tmp_path):
    n = 10
    idx = _build_index(tmp_path, n)
    det = str(tmp_path / "detail.sqlite")
    calls: list[str] = []
    fetch = _make_fetcher(failing_ids=set(), none_ids=set(), calls=calls)

    stats = anyio.run(
        lambda: reconcile_detail_tier(det, idx, fetch_detail=fetch, now=lambda: _NOW)
    )
    assert stats == {"fetched": n, "failed": 0, "missing": 0}

    con = sqlite3.connect(idx)
    merged = merge_detail_into_index(con, det)
    assert merged == n

    for i in (0, 1, 7):
        exp = _expected(i)
        row = con.execute(
            "SELECT salary_min, salary_max, years_min, degree_min, degree_required, snippet "
            "FROM jobs WHERE id = ?",
            (_row_id(i),),
        ).fetchone()
        assert row[0] == exp["salary_min"]
        assert row[1] == exp["salary_max"]
        assert row[2] == exp["years_min"]
        assert row[3] == exp["degree_min"]
        assert row[4] == exp["degree_required"]
        assert row[5] and len(row[5]) <= 300
    con.close()


def test_failing_and_none_rows_marked_attempts_and_not_merged(tmp_path):
    # Scattered failing/none refs among good ones -- proves the pass is non-fatal (a dead ref
    # never aborts the others) and that only successfully-recovered rows reach the index.
    n = 6
    idx = _build_index(tmp_path, n)
    det = str(tmp_path / "detail.sqlite")
    calls: list[str] = []
    failing_ids = {1, 3}
    none_ids = {4}
    good_ids = {0, 2, 5}
    fetch = _make_fetcher(failing_ids=failing_ids, none_ids=none_ids, calls=calls)

    stats = anyio.run(
        lambda: reconcile_detail_tier(det, idx, fetch_detail=fetch, now=lambda: _NOW)
    )
    # non-fatal: all 6 refs were attempted despite 3 dead ones.
    assert stats["fetched"] == len(good_ids)
    assert stats["failed"] == len(failing_ids) + len(none_ids)
    # failing/none refs stay in the backlog (unspent retry budget); good ones drop out.
    assert stats["missing"] == len(failing_ids) + len(none_ids)
    assert set(calls) == {_row_id(i) for i in range(n)}

    det_con = open_detail(det)
    for i in failing_ids | none_ids:
        attempts, fetched_at = det_con.execute(
            "SELECT attempts, fetched_at FROM job_detail WHERE id = ?", (_row_id(i),)
        ).fetchone()
        assert attempts == 1
        assert fetched_at is None
    for i in good_ids:
        attempts, fetched_at = det_con.execute(
            "SELECT attempts, fetched_at FROM job_detail WHERE id = ?", (_row_id(i),)
        ).fetchone()
        assert attempts == 0
        assert fetched_at == _NOW
    det_con.close()

    con = sqlite3.connect(idx)
    merged = merge_detail_into_index(con, det)
    assert merged == len(good_ids)
    for i in failing_ids | none_ids:
        row = con.execute(
            "SELECT salary_min, snippet FROM jobs WHERE id = ?", (_row_id(i),)
        ).fetchone()
        assert row[0] is None and row[1] is None  # untouched -- never merged
    for i in good_ids:
        row = con.execute(
            "SELECT salary_min, snippet FROM jobs WHERE id = ?", (_row_id(i),)
        ).fetchone()
        assert row[0] is not None and row[1] is not None
    con.close()


def test_max_details_cap_holds_when_universe_exceeds_it(tmp_path):
    # 32 synthetic postings (within the plan's 30-50 range), all good, capped at 12 -- proves the
    # cap is a hard bound: exactly `cap` refs fetched, the rest counted as remaining backlog.
    n = 32
    cap = 12
    idx = _build_index(tmp_path, n)
    det = str(tmp_path / "detail.sqlite")
    calls: list[str] = []
    fetch = _make_fetcher(failing_ids=set(), none_ids=set(), calls=calls)

    stats = anyio.run(
        lambda: reconcile_detail_tier(
            det, idx, fetch_detail=fetch, max_details=cap, now=lambda: _NOW
        )
    )
    assert stats == {"fetched": cap, "failed": 0, "missing": n - cap}
    assert len(calls) == cap
    assert len(set(calls)) == cap  # no duplicate fetch in a single windowed run


def test_second_reconcile_run_skips_already_fetched_refs(tmp_path):
    # Idempotency: a second pass over an unchanged index must not re-fetch anything -- the
    # sig-gated eligibility check (Task 2) is what makes repeated bounded runs converge.
    n = 8
    idx = _build_index(tmp_path, n)
    det = str(tmp_path / "detail.sqlite")
    calls: list[str] = []
    fetch = _make_fetcher(failing_ids=set(), none_ids=set(), calls=calls)

    stats1 = anyio.run(
        lambda: reconcile_detail_tier(det, idx, fetch_detail=fetch, now=lambda: _NOW)
    )
    assert stats1 == {"fetched": n, "failed": 0, "missing": 0}
    assert len(calls) == n

    stats2 = anyio.run(
        lambda: reconcile_detail_tier(det, idx, fetch_detail=fetch, now=lambda: _NOW)
    )
    assert stats2 == {"fetched": 0, "failed": 0, "missing": 0}
    assert len(calls) == n  # no new calls on the second, unchanged-sig pass


def test_drain_converges_to_zero_across_capped_runs(tmp_path):
    # The primary synthetic stress scenario: 35 postings (within the plan's 30-50 range), a small
    # cap forcing several runs. `missing` must monotonically decrease and hit 0; every id is
    # fetched exactly once across the whole drain (idempotent, no re-fetch of a completed ref).
    n = 35
    cap = 10
    idx = _build_index(tmp_path, n)
    det = str(tmp_path / "detail.sqlite")
    calls: list[str] = []
    fetch = _make_fetcher(failing_ids=set(), none_ids=set(), calls=calls)

    missing_history: list[int] = []
    fetched_total = 0
    for _ in range(6):  # safety bound well above the ceil(35/10) = 4 runs actually needed
        stats = anyio.run(
            lambda: reconcile_detail_tier(
                det, idx, fetch_detail=fetch, max_details=cap, now=lambda: _NOW
            )
        )
        missing_history.append(stats["missing"])
        fetched_total += stats["fetched"]
        assert stats["failed"] == 0
        if stats["missing"] == 0:
            break
    else:
        raise AssertionError("drain did not converge within the safety bound")

    assert missing_history[-1] == 0
    assert all(
        missing_history[i] > missing_history[i + 1] for i in range(len(missing_history) - 1)
    )
    assert fetched_total == n
    assert len(calls) == n
    assert len(set(calls)) == n  # every id fetched exactly once across the whole drain

    con = sqlite3.connect(idx)
    merged = merge_detail_into_index(con, det)
    assert merged == n
    con.close()


def test_measure_detail_coverage_smoke(tmp_path):
    """Tiny smoke test for `scripts/measure_detail_coverage.py`: run it (as a subprocess -- it's a
    standalone, dependency-free script, not a package module) against a small real-schema index
    with a couple of rows in known populated/empty states, and check the printed coverage."""
    import subprocess
    import sys
    from pathlib import Path

    idx = _build_index(tmp_path, 2, name="cov_index")
    con = sqlite3.connect(idx)
    con.execute(
        "UPDATE jobs SET snippet = 'A real snippet.', salary_min = 90000.0, years_min = 5, "
        "degree_min = 'bachelor' WHERE id = ?",
        (_row_id(0),),
    )
    con.commit()
    con.close()

    script = Path(__file__).resolve().parents[1] / "scripts" / "measure_detail_coverage.py"
    result = subprocess.run(
        [sys.executable, str(script), idx, "--sources", "smartrecruiters,workday"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout
    assert "smartrecruiters" in out
    assert "1/2" in out  # one of two smartrecruiters rows populated for each recovered field
    assert "workday" in out
    assert "0/0" in out  # zero workday rows in this synthetic index
