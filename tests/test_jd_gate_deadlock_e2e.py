"""End-to-end proof that the 2026-07-28 JD-gate deadlock is broken, driven through ``main()``.

The unit tests in test_gate_jd_post_reconcile.py pin ``gates.py``. This pins the WIRING: that
``main()`` really defers the JD verdict until after ``build_and_publish_detail`` has run, and that
the resulting build publishes. Under the pre-fix ordering this scenario could not publish, no matter
how good the detail sidecar was, because the merge was gated on the verdict it was supposed to feed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon.index.build import append_jobs, build_index_from_fresh_db  # noqa: E402
from ergon.index.db import connect, fresh_db  # noqa: E402
from ergon.index.detail import detail_sig, open_detail  # noqa: E402
from ergon.models import JobPosting  # noqa: E402

_JD = "We are hiring a Backend Engineer to build distributed systems. Requires 5+ years experience."


def _job(i: int, *, with_jd: bool) -> JobPosting:
    return JobPosting.create(
        source="greenhouse",
        source_job_id=str(i),
        company=f"Co{i % 3}",
        title=f"Backend Engineer {i}",
        board_token="acme",
        description_text=_JD if with_jd else None,
    )


def _seed_prior(out: Path, n: int, with_jd: int) -> None:
    """A prior published index: ``with_jd`` of ``n`` rows carry a snippet."""
    out.mkdir(parents=True, exist_ok=True)
    fresh = out / "fresh_prior.sqlite"
    fresh_db(fresh)
    con = connect(fresh)
    con.execute("PRAGMA foreign_keys = OFF")
    append_jobs(con, [_job(i, with_jd=i < with_jd) for i in range(n)], build_id="prior")
    con.commit()
    con.close()
    build_index_from_fresh_db(fresh, out / "index.sqlite", build_id="prior")
    fresh.unlink()


def _seed_sidecar(out: Path, cover: int) -> None:
    """Seed the detail sidecar for the first ``cover`` rows, at each row's current sig."""
    idx = connect(out / "index.sqlite", read_only=True)
    try:
        rows = idx.execute("SELECT id, content_hash, title, level FROM jobs ORDER BY id").fetchall()
    finally:
        idx.close()
    det = open_detail(str(out / "index-detail.sqlite"))
    try:
        for r in rows[:cover]:
            det.execute(
                "INSERT INTO job_detail (id, sig, fetched_at, attempts, snippet) "
                "VALUES (?, ?, '2026-09-09T00:00:00Z', 1, ?)",
                (r[0], detail_sig({"content_hash": r[1], "title": r[2], "level": r[3]}), _JD),
            )
        det.commit()
    finally:
        det.close()


def _snippets(db: Path) -> int:
    con = connect(db, read_only=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM jobs WHERE snippet IS NOT NULL AND TRIM(snippet) != ''"
        ).fetchone()[0]
    finally:
        con.close()


def _history(out: Path, jd_pct: float, rows: int) -> None:
    """A last-published row carrying the high JD baseline that pinned the deadlock."""
    (out / "history.jsonl").write_text(
        json.dumps(
            {
                "build_id": "prior",
                "date": "2026-09-08",
                "total_jobs": rows,
                "published": True,
                "metrics": {"jd_pct": jd_pct, "active_jobs": rows},
            }
        )
        + "\n"
    )


def test_high_baseline_plus_list_only_carry_still_publishes(tmp_path, monkeypatch):
    """The deadlock shape: baseline 100%, the carried index is only 20% until the merge runs.

    Pre-fix, the JD gate read that 20% against the 100% baseline (an 80pt drop, limit 15) and
    refused to publish — and because the merge was gated on that verdict, it never got the chance
    to lift coverage. The gate is now read after the merge, so this publishes.
    """
    out = tmp_path / "dist"
    _seed_prior(out, n=5, with_jd=1)  # 1 of 5 = 20% pre-merge
    _seed_sidecar(out, cover=5)  # the sidecar can restore all 5
    _history(out, jd_pct=100.0, rows=5)
    assert _snippets(out / "index.sqlite") == 1

    monkeypatch.setattr(
        bi, "_crawl_due", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no crawl"))
    )
    bi.main(["--reconcile-only", "--detail", "--out", str(out)])

    # The merge ran and lifted coverage to 100%...
    assert _snippets(out / "index.sqlite") == 5
    # ...and the build PUBLISHED.
    assert (out / "index.sqlite.gz").exists()

    gates = json.loads((out / "gates.json").read_text())
    jd = next(g for g in gates["gates"] if g["name"] == "jd_coverage")
    assert jd["passed"] is True, f"post-merge JD gate should pass: {jd['detail']}"
    assert "100.0%" in jd["detail"], "the verdict must be read AFTER the merge, not before"
    assert gates["passed"] is True


def test_gutted_sidecar_still_blocks_the_publish(tmp_path, monkeypatch):
    """The protection must survive: if the merge cannot restore coverage, nothing publishes.

    This is the 2026-07-26 incident the gate was written for. Moving the gate later must not have
    turned it into a rubber stamp.
    """
    out = tmp_path / "dist"
    _seed_prior(out, n=5, with_jd=1)  # 20%
    _seed_sidecar(out, cover=0)  # sidecar contributes NOTHING
    _history(out, jd_pct=100.0, rows=5)

    monkeypatch.setattr(
        bi, "_crawl_due", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no crawl"))
    )
    # A blocked publish must also exit non-zero, so CI reports the failure loudly.
    with pytest.raises(SystemExit) as ei:
        bi.main(["--reconcile-only", "--detail", "--out", str(out)])
    assert ei.value.code == 1

    assert not (out / "index.sqlite.gz").exists(), "a real collapse must not publish"
    gates = json.loads((out / "gates.json").read_text())
    jd = next(g for g in gates["gates"] if g["name"] == "jd_coverage")
    assert jd["passed"] is False
    assert gates["passed"] is False


def test_coverage_json_describes_the_shipped_artifact(tmp_path, monkeypatch):
    """coverage.json must count the MERGED index, not the pre-merge one it used to report."""
    out = tmp_path / "dist"
    _seed_prior(out, n=5, with_jd=1)
    _seed_sidecar(out, cover=5)
    _history(out, jd_pct=20.0, rows=5)

    monkeypatch.setattr(
        bi, "_crawl_due", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no crawl"))
    )
    bi.main(["--reconcile-only", "--detail", "--out", str(out)])

    cov = json.loads((out / "coverage.json").read_text())
    assert cov["with_jd"] == 5, "coverage.json under-reported JD by reading the pre-merge DB"
    assert _snippets(out / "index.sqlite") == 5


def test_blocked_publish_restores_the_prior_index_and_ships_nothing(tmp_path, monkeypatch):
    """The new failure path promotes first, then gates. It must leave no partial release behind.

    Pre-fix the JD gate ran before promotion, so a failure simply never touched `index.sqlite`.
    Now the structural gate promotes and the JD verdict comes later, so the restore has to be real:
    the prior index back on disk, and no core/shard/slim/delta artifact written.
    """
    out = tmp_path / "dist"
    _seed_prior(out, n=5, with_jd=1)
    _seed_sidecar(out, cover=0)
    _history(out, jd_pct=100.0, rows=5)
    prior_ids = None
    con = connect(out / "index.sqlite", read_only=True)
    try:
        prior_ids = [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id").fetchall()]
    finally:
        con.close()

    monkeypatch.setattr(
        bi, "_crawl_due", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no crawl"))
    )
    with pytest.raises(SystemExit):
        bi.main(["--reconcile-only", "--detail", "--sharded", "--out", str(out)])

    # The prior index is back, intact.
    con = connect(out / "index.sqlite", read_only=True)
    try:
        assert [
            r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id").fetchall()
        ] == prior_ids
    finally:
        con.close()

    # Nothing downstream of the gate ran.
    for artifact in ("index.sqlite.gz", "index-slim.sqlite.gz", "index-delta.sqlite.gz"):
        assert not (out / artifact).exists(), f"{artifact} must not ship on a blocked publish"
    assert not list(out.glob("shard-*.sqlite.gz")), "no sector shard may ship on a blocked publish"
