"""``--reconcile-only`` (merge-only republish): skip the crawl, carry the WHOLE prior index forward,
and refill list-only ``snippet`` from the detail sidecar — recovering JD coverage WITHOUT a crawl.

A normal build first runs a FULL crawl (re-crawling ~28k never-delta-skipped search-index boards ->
~4h) before its assemble/publish tail. But recovering JD coverage after a bad publish needs ONLY the
detail merge (refill list-only ``snippet`` from ``index-detail.sqlite``). ``--reconcile-only`` reuses
the build's existing "no crawl -> full carry-forward" machinery: it feeds an EMPTY fresh DB to the
same build+publish tail, so ``build_index_from_fresh_db(..., crawled_keys=set())`` carries every prior
company forward and ``build_and_publish_detail`` then merges the sidecar in — a ~10-20 min republish.

The gates run on the CARRIED index (pre-merge), then the detail merge lifts coverage; so the faithful
recovery is: the carried index sits at ~the last published coverage (no drop -> JD gate passes) and the
merge climbs it. The test mirrors that exactly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.build import append_jobs, build_index_from_fresh_db  # noqa: E402
from ergon_tracker.index.db import connect, fresh_db  # noqa: E402
from ergon_tracker.index.detail import detail_sig, open_detail  # noqa: E402
from ergon_tracker.models import JobPosting  # noqa: E402

_JD = "We are hiring a Backend Engineer to build distributed systems. Requires 5+ years experience."


def _job(i: int, *, with_jd: bool) -> JobPosting:
    """A prior-index posting. ``with_jd`` gives it a description (-> non-empty snippet); otherwise it's
    list-only exactly as a search-index board yields it (NULL snippet, the Tier-3 candidate signal)."""
    return JobPosting.create(
        source="greenhouse",
        source_job_id=str(i),
        company=f"Co{i % 3}",
        title=f"Backend Engineer {i}",
        board_token="acme",
        description_text=_JD if with_jd else None,
    )


def _seed_prior_index(out: Path) -> list[str]:
    """Build a prior published index at ``out/index.sqlite``: 5 rows, 2 already carrying a snippet
    (40% JD coverage) + 3 list-only (NULL snippet). Returns the built row ids."""
    out.mkdir(parents=True, exist_ok=True)
    fresh_prior = out / "fresh_prior.sqlite"
    fresh_db(fresh_prior)
    con = connect(fresh_prior)
    con.execute("PRAGMA foreign_keys = OFF")
    append_jobs(
        con,
        [_job(i, with_jd=i < 2) for i in range(5)],  # ids 0,1 have a JD; 2,3,4 are list-only
        build_id="prior",
    )
    con.commit()
    con.close()
    build_index_from_fresh_db(fresh_prior, out / "index.sqlite", build_id="prior")
    fresh_prior.unlink()
    con = connect(out / "index.sqlite", read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id").fetchall()]
    finally:
        con.close()


def _seed_detail_sidecar(out: Path) -> int:
    """Seed ``out/index-detail.sqlite`` with a JD snippet for EVERY prior row, using each row's CURRENT
    sig (recomputed from its content_hash/title/level) so the build-time merge accepts it. The merge
    only fills the empty snippets -> the 3 list-only rows. Returns rows seeded."""
    idx = connect(out / "index.sqlite", read_only=True)
    try:
        rows = idx.execute("SELECT id, content_hash, title, level FROM jobs").fetchall()
    finally:
        idx.close()
    det = open_detail(str(out / "index-detail.sqlite"))
    try:
        for r in rows:
            sig = detail_sig({"content_hash": r[1], "title": r[2], "level": r[3]})
            det.execute(
                "INSERT INTO job_detail (id, sig, fetched_at, attempts, snippet) "
                "VALUES (?, ?, '2026-07-27T00:00:00Z', 1, ?)",
                (r[0], sig, "Recovered JD: " + _JD),
            )
        det.commit()
    finally:
        det.close()
    return len(rows)


def _snippet_count(db: Path) -> int:
    con = connect(db, read_only=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM jobs WHERE snippet IS NOT NULL AND TRIM(snippet) != ''"
        ).fetchone()[0]
    finally:
        con.close()


def _boom_crawl(*args, **kwargs):
    raise AssertionError("_crawl_due must NOT be called under --reconcile-only")


def test_reconcile_only_recovers_jd_without_crawl(tmp_path, monkeypatch):
    out = tmp_path / "dist"
    ids = _seed_prior_index(out)
    assert _seed_detail_sidecar(out) == 5
    assert _snippet_count(out / "index.sqlite") == 2  # prior sits at 40% JD (2 of 5)

    # The last published build recorded that same 40% JD coverage -> the JD-coverage gate has a 40%
    # baseline. The carried index (pre-merge) is also ~40%, so the gate sees NO drop and passes; the
    # detail merge THEN climbs coverage. total_jobs=5 gives the row_floor its durable basis too.
    (out / "history.jsonl").write_text(
        json.dumps(
            {
                "build_id": "prior",
                "date": "2026-07-26",
                "published": True,
                "total_jobs": 5,
                "metrics": {"jd_pct": 40.0},
            }
        )
        + "\n"
    )

    monkeypatch.setattr(bi, "_crawl_due", _boom_crawl)  # PROVES no crawl runs
    bi.main(["--reconcile-only", "--detail", "--out", str(out)])

    # Carry-forward: every prior row is still present (no board dropped; crawled_keys empty).
    con = connect(out / "index.sqlite", read_only=True)
    try:
        assert [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id").fetchall()] == ids
    finally:
        con.close()

    # The detail merge refilled the 3 list-only snippets -> JD coverage 2 -> 5 (with_jd went UP), all
    # WITHOUT a crawl.
    assert _snippet_count(out / "index.sqlite") == 5

    # The JD-coverage gate passed (carried ~40% vs 40% baseline, no drop) -> the recovery published.
    gates = json.loads((out / "gates.json").read_text())
    assert gates["passed"] is True
    jd_gate = next(g for g in gates["gates"] if g["name"] == "jd_coverage")
    assert jd_gate["passed"] is True
    assert (out / "index.sqlite.gz").exists()  # the single core publish ran


def test_reconcile_only_without_prior_index_errs_cleanly(tmp_path, monkeypatch):
    """Reconcile-only over nothing is meaningless: with NO prior index it must error clearly (exit 2),
    never crawl-nothing into an empty publish."""
    out = tmp_path / "dist"  # no prior index.sqlite exists
    monkeypatch.setattr(bi, "_crawl_due", _boom_crawl)
    with pytest.raises(SystemExit) as ei:
        bi.main(["--reconcile-only", "--out", str(out)])
    assert ei.value.code == 2
    assert not (out / "index.sqlite").exists()  # never wrote an (empty) publish


def test_flag_off_still_crawls(tmp_path, monkeypatch):
    """Default (no --reconcile-only): the crawl path is unchanged -- ``_crawl_due`` IS invoked."""
    called = {"n": 0}

    async def _spy_crawl(limit, states, fresh_db_path, build_id, *rest):
        called["n"] += 1
        fresh_db(fresh_db_path)
        c = connect(fresh_db_path)
        c.execute("PRAGMA foreign_keys = OFF")
        append_jobs(c, [_job(i, with_jd=False) for i in range(3)], build_id=build_id)
        c.commit()
        c.close()
        return {}, 0  # (outcome, next_cursor)

    monkeypatch.setattr(bi, "_crawl_due", _spy_crawl)
    out = tmp_path / "dist"
    bi.main(["--incremental", "--limit-companies", "5", "--out", str(out)])
    assert called["n"] == 1  # the crawl ran (default behaviour untouched)
    assert (out / "index.sqlite").exists()
