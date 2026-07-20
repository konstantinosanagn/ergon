"""End-to-end stress test for the freshness-sweep CLI (scripts/freshness_sweep.py, Phase 3).

OFFLINE: ``ergon_tracker.index.freshness.get_provider`` is monkeypatched to fake, in-process
providers -- never real network -- mirroring tests/test_freshness_sweep.py's pattern for the
underlying engine. Runs the CLI's ``main()`` directly (no subprocess) against a synthetic,
real-schema index built via ``fresh_db``, and asserts:

  * the sidecar (``--out``) ``expired_ids`` table contains exactly the confirmed-departed id, with
    a non-empty ``expired_at`` and the expected ``reason``;
  * a still-live id is absent from the sidecar;
  * a board whose fetch errors contributes nothing;
  * the MAIN index passed via ``--index`` is never mutated (every row's ``status`` is unchanged,
    proving the CLI truly detects without touching the real index).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import scripts.freshness_sweep as sweep_cli

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.freshness import idset_hash
from ergon_tracker.models import RawJob

_NOW = "2026-07-18T00:00:00+00:00"


def _build_index(tmp_path, jobs: list[dict]) -> str:
    """A real-schema synthetic index (``fresh_db``) seeded with the given job rows plus matching
    ``job_sources`` provenance rows -- mirrors tests/test_freshness_sweep.py's ``_build_index``
    helper exactly (freshness diffs against the RAW ``source_job_id``, which only ``job_sources``
    carries)."""
    p = tmp_path / "index.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    job_rows = []
    source_rows = []
    for j in jobs:
        row = {
            "content_hash": f"ch-{j['id']}",
            "company": "Acme",
            "title": "Engineer",
            "remote": "unknown",
            "level": "mid",
            "employment_type": "full_time",
            "status": "active",
            "ts": _NOW,
            "build_id": "b0",
            "company_key": None,
            "board_token": "acme",
            "apply_url": f"http://x/{j['id']}",
            "listing_url": None,
            "source_job_id": None,
        }
        row.update(j)
        if row["source_job_id"] is None:
            row["source_job_id"] = row["id"]
        job_rows.append(row)
        source_rows.append(
            {
                "job_id": row["id"],
                "source": row["source"],
                "source_job_id": row["source_job_id"],
                "apply_url": row["apply_url"],
                "fetched_at": row["ts"],
            }
        )
    con.executemany(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, company_key, "
        "board_token, apply_url, listing_url) "
        "VALUES (:id, :content_hash, :source, :company, :title, :remote, :level, "
        ":employment_type, :status, :ts, :ts, :ts, :build_id, :company_key, :board_token, "
        ":apply_url, :listing_url)",
        job_rows,
    )
    con.executemany(
        "INSERT INTO job_sources (job_id, source, source_job_id, apply_url, fetched_at) "
        "VALUES (:job_id, :source, :source_job_id, :apply_url, :fetched_at)",
        source_rows,
    )
    con.commit()
    con.close()
    return str(p)


def _job_row(job_id: str, *, source: str = "greenhouse", board_token: str = "acme") -> dict:
    return {"id": job_id, "source": source, "board_token": board_token}


def _all_statuses(idx_path: str) -> dict[str, str]:
    con = sqlite3.connect(idx_path)
    rows = con.execute("SELECT id, status FROM jobs").fetchall()
    con.close()
    return dict(rows)


def _raw(source_job_id: str, source: str = "greenhouse") -> RawJob:
    return RawJob(source=source, source_job_id=source_job_id, company="Acme")


class _FakeProvider:
    """Fetches a fixed id-set for one board's ``fetch()``, or raises for a board that must error."""

    def __init__(self, live_ids: list[str] | None = None, *, raises: bool = False):
        self._live_ids = live_ids or []
        self._raises = raises

    async def fetch(self, token, query, fetcher):
        if self._raises:
            raise RuntimeError("simulated fetch failure")
        return [_raw(i) for i in self._live_ids]


def _providers(by_source: dict[str, _FakeProvider]):
    def _get(name: str):
        return by_source.get(name)

    return _get


@pytest.fixture
def patched_providers(monkeypatch):
    """Returns a setter the test uses to install the fake provider registry; auto-applies to
    ``ergon_tracker.index.freshness.get_provider`` -- the exact name the engine looks up through
    (see freshness.py's module docstring: it calls ``get_provider(source).fetch(...)`` directly)."""
    import ergon_tracker.index.freshness as freshness

    def _set(by_source: dict[str, _FakeProvider]) -> None:
        monkeypatch.setattr(freshness, "get_provider", _providers(by_source))

    return _set


def test_cli_e2e_departed_confirmed_alive_kept_errored_board_contributes_nothing(
    tmp_path, patched_providers
):
    idx_path = _build_index(
        tmp_path,
        [
            # greenhouse/acme board: "1" still live on the board, "2" has departed.
            _job_row("job-alive", source="greenhouse"),
            _job_row("job-departed", source="greenhouse"),
            # lever/errboard: this board's fetch will raise -- must contribute nothing.
            _job_row("job-on-erroring-board", source="lever", board_token="errboard"),
        ],
    )
    # job-alive's source_job_id defaults to its own id ("job-alive"); the greenhouse board's live
    # fetch returns only that id, so "job-departed" is the confirmed departure.
    patched_providers(
        {
            "greenhouse": _FakeProvider(live_ids=["job-alive"]),
            "lever": _FakeProvider(raises=True),
        }
    )

    out_path = tmp_path / "index-freshness.sqlite"
    rc = sweep_cli.main(["--index", idx_path, "--out", str(out_path)])
    assert rc == 0

    # --- sidecar contract -------------------------------------------------------------------
    assert out_path.exists()
    con = sqlite3.connect(out_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "expired_ids" in tables
        rows = con.execute("SELECT id, expired_at, reason FROM expired_ids").fetchall()
    finally:
        con.close()

    ids = {r[0] for r in rows}
    assert ids == {"job-departed"}  # exactly the departed id, nothing else
    (row,) = rows
    _id, expired_at, reason = row
    assert expired_at  # non-empty, valid-looking timestamp
    assert reason == "departed_board"

    # --- the main index is NEVER mutated ------------------------------------------------------
    statuses = _all_statuses(idx_path)
    assert statuses == {
        "job-alive": "active",
        "job-departed": "active",  # still 'active' in the REAL index -- only the sidecar knows
        "job-on-erroring-board": "active",
    }


def test_cli_e2e_no_departures_writes_empty_but_well_formed_sidecar(tmp_path, patched_providers):
    idx_path = _build_index(tmp_path, [_job_row("job-alive", source="greenhouse")])
    patched_providers({"greenhouse": _FakeProvider(live_ids=["job-alive"])})

    out_path = tmp_path / "index-freshness.sqlite"
    rc = sweep_cli.main(["--index", idx_path, "--out", str(out_path)])
    assert rc == 0

    con = sqlite3.connect(out_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "expired_ids" in tables
        rows = con.execute("SELECT * FROM expired_ids").fetchall()
    finally:
        con.close()
    assert rows == []
    assert _all_statuses(idx_path) == {"job-alive": "active"}


def test_cli_e2e_shard_with_no_matching_boards_writes_empty_sidecar(tmp_path, patched_providers):
    # A single greenhouse board hashes to exactly one shard out of a large --num-shards; pick a
    # shard number guaranteed to miss it and confirm the CLI degrades to an empty, well-formed
    # sidecar rather than erroring.
    idx_path = _build_index(tmp_path, [_job_row("job-alive", source="greenhouse")])
    patched_providers({"greenhouse": _FakeProvider(live_ids=["job-alive"])})

    from ergon_tracker.index.freshness_shard import shard_boards

    num_shards = 8
    winning_shard = next(
        s for s in range(num_shards) if shard_boards([("greenhouse", "acme")], s, num_shards)
    )
    empty_shard = next(s for s in range(num_shards) if s != winning_shard)

    out_path = tmp_path / "shard-empty.sqlite"
    rc = sweep_cli.main(
        [
            "--index",
            idx_path,
            "--out",
            str(out_path),
            "--shard",
            str(empty_shard),
            "--num-shards",
            str(num_shards),
        ]
    )
    assert rc == 0
    con = sqlite3.connect(out_path)
    try:
        rows = con.execute("SELECT * FROM expired_ids").fetchall()
    finally:
        con.close()
    assert rows == []
    # Still unmutated (the CLI never even had to touch this board's data).
    assert _all_statuses(idx_path) == {"job-alive": "active"}


def test_cli_e2e_board_deltas_sidecar_carries_added_and_idset_hash(tmp_path, patched_providers):
    # A greenhouse board holds "job-alive" (source_job_id defaults to its own id). Its live fetch
    # now lists {job-alive, new-req}: nothing departed, but "new-req" is a genuine add. The sidecar
    # board_deltas table must carry that add + the fingerprint of the FULL live set. A separate
    # board whose fetch ERRORS must contribute NO delta row (added-side guard).
    idx_path = _build_index(
        tmp_path,
        [
            _job_row("job-alive", source="greenhouse"),
            _job_row("job-on-erroring-board", source="lever", board_token="errboard"),
        ],
    )
    patched_providers(
        {
            "greenhouse": _FakeProvider(live_ids=["job-alive", "new-req"]),
            "lever": _FakeProvider(raises=True),
        }
    )

    out_path = tmp_path / "index-freshness.sqlite"
    rc = sweep_cli.main(["--index", idx_path, "--out", str(out_path)])
    assert rc == 0

    con = sqlite3.connect(out_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"expired_ids", "board_deltas"} <= tables
        cols = {r[1] for r in con.execute("PRAGMA table_info(board_deltas)")}
        assert cols == {"source", "board_token", "added_ids", "idset_hash", "computed_at"}
        rows = con.execute(
            "SELECT source, board_token, added_ids, idset_hash, computed_at FROM board_deltas"
        ).fetchall()
        # no departures on the greenhouse board
        assert con.execute("SELECT COUNT(*) FROM expired_ids").fetchone()[0] == 0
    finally:
        con.close()

    # Exactly one delta: the greenhouse board. The erroring lever board emitted nothing.
    assert len(rows) == 1
    source, board_token, added_json, hash_val, computed_at = rows[0]
    assert (source, board_token) == ("greenhouse", "acme")
    assert json.loads(added_json) == ["new-req"]  # live - stored, sorted
    assert hash_val == idset_hash({"job-alive", "new-req"})  # fingerprint of the FULL live set
    assert computed_at  # non-empty timestamp


def test_cli_e2e_no_departures_writes_well_formed_empty_board_deltas(tmp_path, patched_providers):
    # An unchanged board still records a delta (empty added set + current fingerprint); this asserts
    # the board_deltas table is always well-formed even when there is nothing new.
    idx_path = _build_index(tmp_path, [_job_row("job-alive", source="greenhouse")])
    patched_providers({"greenhouse": _FakeProvider(live_ids=["job-alive"])})

    out_path = tmp_path / "index-freshness.sqlite"
    assert sweep_cli.main(["--index", idx_path, "--out", str(out_path)]) == 0

    con = sqlite3.connect(out_path)
    try:
        rows = con.execute("SELECT added_ids, idset_hash FROM board_deltas").fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    added_json, hash_val = rows[0]
    assert json.loads(added_json) == []
    assert hash_val == idset_hash({"job-alive"})


def test_cli_missing_index_returns_nonzero(tmp_path):
    rc = sweep_cli.main(
        ["--index", str(tmp_path / "does-not-exist.sqlite"), "--out", str(tmp_path / "out.sqlite")]
    )
    assert rc != 0


def test_cli_invalid_shard_args_returns_nonzero(tmp_path):
    idx_path = _build_index(tmp_path, [_job_row("job-alive")])
    rc = sweep_cli.main(
        [
            "--index",
            idx_path,
            "--out",
            str(tmp_path / "out.sqlite"),
            "--shard",
            "5",
            "--num-shards",
            "3",
        ]
    )
    assert rc != 0
