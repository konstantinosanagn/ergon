"""End-to-end wiring test for build_index.py main() incremental+streaming path (offline).

Exercises the exact code path the CI workflow runs — changed_companies_sql,
build_index_from_fresh_db, build_and_publish_shards_from_db, gates, coverage, publish — with the
network crawl faked out. Catches import/wiring breakage (e.g. an uncommitted helper) before CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.db import connect  # noqa: E402


async def _fake_crawl_due(
    limit_companies,
    states,
    fresh_db_path,
    build_id,
    cursor=0,
    capture_rich=False,
    prev_db=None,
    capture_jd=False,
    only_sources=None,
    exclude_sources=None,
):
    """Write a few jobs straight into the fresh DB (no network), return (outcome, next_cursor)."""
    from ergon_tracker.index.build import append_jobs
    from ergon_tracker.index.db import connect as _connect
    from ergon_tracker.index.db import fresh_db
    from ergon_tracker.models import JobPosting, Location, RemoteType

    fresh_db(fresh_db_path)
    con = _connect(fresh_db_path)
    con.execute("PRAGMA foreign_keys = OFF")
    jobs = [
        JobPosting.create(
            source="greenhouse",
            source_job_id=str(i),
            company=f"Co{i % 3}",
            title=f"Backend Engineer {i}",
            sector=["Fintech", "AI/ML", None][i % 3],
            locations=[Location(raw="Remote", is_remote=True)],
            remote=RemoteType.REMOTE,
        )
        for i in range(9)
    ]
    append_jobs(con, jobs, build_id=build_id)
    con.commit()
    con.close()
    return {}, 0  # (outcome, next_cursor): no due boards -> exercises build/publish wiring


def test_main_incremental_streaming_wiring(tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    out = tmp_path / "dist"
    bi.main(["--incremental", "--sharded", "--limit-companies", "5", "--out", str(out)])

    # the full publish set the streaming path must produce
    for name in (
        "index.sqlite",
        "index.sqlite.gz",
        "manifest.json",
        "manifest-set.json",  # Item 6: top-level set manifest, written last
        "gates.json",
        "coverage.json",
        "INDEX_STATUS.md",
        "shards.json",
        "board_state.json",
    ):
        assert (out / name).exists(), f"missing artifact: {name}"

    # index is real + queryable
    con = connect(out / "index.sqlite", read_only=True)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 9
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # sharded by sector via the DB path; at least one gzipped shard published
    assert list(out.glob("shard-*.sqlite.gz"))


def test_main_non_incremental_routes_to_streaming(tmp_path, monkeypatch):
    # A crawl invoked WITHOUT --incremental (e.g. a big manual `--limit-companies 58078 --sharded`)
    # must now take the bounded, resumable streaming path -- not the deleted in-memory `_crawl`
    # (which had no window and no cursor, so a CI-timeout kill lost the whole run). Proven by the
    # streaming-only artifacts it emits (board_state.json + crawl_cursor.json), which the old
    # in-memory path never wrote. If routing regressed, main() would NameError on the removed _crawl.
    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    out = tmp_path / "dist"
    bi.main(["--sharded", "--limit-companies", "58078", "--out", str(out)])

    for name in (
        "index.sqlite",
        "board_state.json",  # streaming-path only (in-memory path never wrote crawl state)
        "crawl_cursor.json",  # resumable cursor -> proves the good path ran
        "shards.json",
    ):
        assert (out / name).exists(), f"missing streaming artifact: {name}"
    con = connect(out / "index.sqlite", read_only=True)
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 9


def test_jd_requested_but_sidecar_absent_emits_tripwire(tmp_path, monkeypatch):
    """Reproduce the 2-day silent incident: --jd was requested but no sidecar was produced (the fake
    crawl never creates out/index-jd.sqlite, like a crawl killed before it persisted one) -> main()
    must emit jd_sidecar.json marking it NOT ok, which the notify job turns into an alert."""
    import json

    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    out = tmp_path / "dist"
    bi.main(["--incremental", "--sharded", "--limit-companies", "5", "--jd", "--out", str(out)])
    sig = json.loads((out / "jd_sidecar.json").read_text())
    assert sig["expected"] is True and sig["published"] is False and sig["jd_sidecar_ok"] is False
    assert sig["reason"]  # names why (sidecar absent)


def test_no_jd_flag_never_trips_sidecar_tripwire(tmp_path, monkeypatch):
    """A build WITHOUT --jd (e.g. the join shard, which correctly carries the sidecar forward) has
    expected=False -> jd_sidecar_ok=True -> never alerts. Guards against a carry-forward false-alarm."""
    import json

    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    out = tmp_path / "dist"
    bi.main(["--incremental", "--sharded", "--limit-companies", "5", "--out", str(out)])
    sig = json.loads((out / "jd_sidecar.json").read_text())
    assert sig["expected"] is False and sig["jd_sidecar_ok"] is True


def test_no_publish_still_builds_but_flags_skip(tmp_path, monkeypatch):
    """--no-publish (dry run): the build runs FULLY -- every dist/ artifact + gates are produced
    exactly as a real build -- but the run is flagged do-NOT-publish via the dist/PUBLISH_SKIPPED
    marker (the script-level assertion point; the workflow gates its `gh release upload` on the same
    flag). This is the 2026-07-27 safety fix: a validation dispatch can't overwrite the prod release."""
    import json

    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    out = tmp_path / "dist"
    bi.main(
        ["--incremental", "--sharded", "--limit-companies", "5", "--no-publish", "--out", str(out)]
    )

    # The build still produced the full local artifact set (the dry run is a real build).
    for name in ("index.sqlite", "index.sqlite.gz", "manifest.json", "gates.json", "coverage.json"):
        assert (out / name).exists(), f"--no-publish must still produce {name}"
    # ...but it dropped the do-not-publish marker naming this build.
    marker = out / "PUBLISH_SKIPPED"
    assert marker.exists(), "--no-publish must write the PUBLISH_SKIPPED marker"
    sig = json.loads(marker.read_text())
    assert sig["no_publish"] is True and sig["build_id"]


def test_default_publishes_no_skip_marker(tmp_path, monkeypatch):
    """WITHOUT --no-publish the default publish path is taken (byte-for-byte today's daily build):
    no PUBLISH_SKIPPED marker is written, so the workflow's release-upload steps proceed."""
    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    monkeypatch.delenv("ERGON_NO_PUBLISH", raising=False)  # ensure the env switch isn't set either
    out = tmp_path / "dist"
    bi.main(["--incremental", "--sharded", "--limit-companies", "5", "--out", str(out)])
    assert not (out / "PUBLISH_SKIPPED").exists(), "default build must NOT flag a publish skip"


def test_no_publish_env_switch_flags_skip(tmp_path, monkeypatch):
    """ERGON_NO_PUBLISH=1 (env) is equivalent to the --no-publish CLI flag -- it's the switch the
    workflow sets. Same marker, same do-not-publish semantics."""
    monkeypatch.setattr(bi, "_crawl_due", _fake_crawl_due)
    monkeypatch.setenv("ERGON_NO_PUBLISH", "1")
    out = tmp_path / "dist"
    bi.main(["--incremental", "--sharded", "--limit-companies", "5", "--out", str(out)])
    assert (out / "PUBLISH_SKIPPED").exists(), "ERGON_NO_PUBLISH=1 must flag a publish skip"
