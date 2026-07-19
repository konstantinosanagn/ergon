"""Integration smoke test for scripts/build_index.py's liveness wiring: `build_and_publish_liveness`
(mirrors `build_and_publish_detail`) end-to-end against a real registered provider, monkeypatched
`.fetch` (OFFLINE -- no real network), and a pre-seeded sidecar so the flip threshold is reached in
one call without needing to fake wall-clock time (the CLI entry point uses the real clock, unlike
`reconcile_liveness_tier`'s injectable `now`, which tests/test_liveness.py exercises directly)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_index import build_and_publish_liveness  # noqa: E402

from ergon_tracker.index.db import fresh_db  # noqa: E402
from ergon_tracker.index.liveness import open_liveness  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402


def _build_index(path) -> None:
    fresh_db(path)
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, company_key, "
        "board_token, apply_url, listing_url) "
        "VALUES ('gh-1', 'ch-1', 'greenhouse', 'Acme', 'Engineer', 'unknown', 'mid', "
        "'full_time', 'active', '2026-01-01', '2026-01-01', '2026-01-01', 'b0', 'acme', "
        "'acme', 'http://x/1', NULL)"
    )
    con.commit()
    con.close()


def test_build_and_publish_liveness_flips_dead_row_and_republishes(tmp_path, monkeypatch):
    out = tmp_path
    db = out / "index.sqlite"
    _build_index(db)

    # Pre-seed the sidecar with a dead_streak=1 miss, checked long enough ago that this run's
    # (real-clock) recheck-days eligibility check unconditionally re-selects it -- so a SINGLE
    # call reaches the streak=2 flip threshold for this non-tier3 source.
    liveness_db = out / "index-liveness.sqlite"
    con = open_liveness(str(liveness_db))
    con.execute(
        "INSERT INTO job_liveness(id, checked_at, dead_streak, verdict) VALUES "
        "('gh-1', '2000-01-01T00:00:00+00:00', 1, 'candidate')"
    )
    con.commit()
    con.close()

    load_builtins()
    provider = get_provider("greenhouse")
    assert provider is not None

    async def fake_fetch(token, query, fetcher):
        return []  # the board now returns nothing -- gh-1 departed

    monkeypatch.setattr(provider, "fetch", fake_fetch)

    stats = build_and_publish_liveness(db, out, build_id="test-build-1")

    assert stats["flipped_dead"] == 1
    assert stats["checked"] == 1

    idx_con = sqlite3.connect(db)
    status, reason = idx_con.execute(
        "SELECT status, expiry_reason FROM jobs WHERE id = 'gh-1'"
    ).fetchone()
    idx_con.close()
    assert status == "expired" and reason == "dead_link"

    # Sidecar published (persists the recheck cadence across builds).
    assert (out / "index-liveness.sqlite.gz").exists()
    manifest = json.loads((out / "manifest-liveness.json").read_text())
    assert manifest["build_id"] == "test-build-1"
    assert manifest["schema_version"] == 1
    assert "sha256" in manifest and "bytes" in manifest

    # Core index re-published (ORDERING: the flip must reach the gz a downloading user fetches).
    assert (out / "index.sqlite.gz").exists()
    core_manifest = json.loads((out / "manifest.json").read_text())
    assert core_manifest["build_id"] == "test-build-1"
