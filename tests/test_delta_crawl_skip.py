"""Sub-phase B parity: a delta-skipped board's rows == a full-crawled board's rows.

The mandated delta-vs-full parity gate for the idset_hash board-skip. We build the SAME board two
ways over the same inputs:

  (1) FULL   -- flag off: the board is re-crawled and re-inserted every run (today's path).
  (2) DELTA  -- flag on, with a prior index + a board_deltas sidecar whose idset_hash matches the
                board's stamped fingerprint: the board is SKIPPED and its prior rows carry forward.

and assert the resulting ``jobs`` DATA rows are identical. Bookkeeping columns that legitimately
differ between a fresh crawl (advanced to today) and a carry-forward (kept from the prior snapshot)
-- ``last_seen``/``fetched_at``/``build_id`` -- are excluded, exactly as they already differ on the
pre-existing 304 not-modified carry-forward path; every DATA + identity column (id, content_hash,
enrich_hash, enriched fields, first_seen, board_token, status, ...) must match to the byte.

Also asserts flag-OFF is unchanged (the board is crawled, never skipped) and that the skip actually
fired (provider.fetch not called; outcome marks not_modified).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.build import build_index_from_fresh_db  # noqa: E402
from ergon_tracker.index.db import connect  # noqa: E402
from ergon_tracker.index.freshness import idset_hash  # noqa: E402
from ergon_tracker.index.scheduler import BoardState  # noqa: E402
from ergon_tracker.models import JobPosting, RawJob  # noqa: E402

_POSTINGS = [("1", "Staff Engineer"), ("2", "Engineering Manager")]
_TOKEN = "acme"


class _Reg:
    def all(self):
        return {"acme": {"ats": "greenhouse", "token": _TOKEN, "domain": "acme.com"}}


class _Provider:
    name = "greenhouse"

    def __init__(self):
        self.fetch_calls = 0

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return None

    async def fetch(self, token, query, fetcher):
        self.fetch_calls += 1
        return [
            RawJob(
                source="greenhouse",
                source_job_id=i,
                company="Acme Corp",
                token=token,
                payload={"title": t},
            )
            for i, t in _POSTINGS
        ]

    def normalize(self, raw):
        return JobPosting.create(
            source="greenhouse",
            source_job_id=raw.source_job_id,
            company="Acme Corp",
            title=raw.payload["title"],
        )


def _write_sidecar(path: Path, source: str, token: str, h: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE board_deltas(source TEXT NOT NULL, board_token TEXT NOT NULL, "
            "added_ids TEXT NOT NULL, idset_hash TEXT NOT NULL, computed_at TEXT NOT NULL, "
            "PRIMARY KEY (source, board_token))"
        )
        con.execute(
            "INSERT INTO board_deltas VALUES (?,?,?,?,?)",
            (source, token, "[]", h, "2026-07-20T00:00:00+00:00"),
        )
        con.commit()
    finally:
        con.close()


def _crawl(states, fresh_path, prov, build_id):
    """Run one _crawl_due and assemble a final index next to fresh_path. Returns (index, outcome)."""
    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh_path, build_id)
    crawled_keys = set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    return outcome, crawled_keys


def _stable_rows(db: Path):
    con = connect(db, read_only=True)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        keep = [c for c in cols if c not in {"last_seen", "fetched_at", "build_id"}]
        sel = ",".join(keep)
        rows = con.execute(f"SELECT {sel} FROM jobs ORDER BY id").fetchall()  # noqa: S608
        return keep, [tuple(r) for r in rows]
    finally:
        con.close()


def test_delta_skip_matches_full_crawl(monkeypatch, tmp_path):
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    prov = _Provider()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    # Real AsyncFetcher: it never actually issues a request here (fetch is faked), but grab must be
    # able to call is_over_budget on it (list_host is None so it never does). Keep the real class.
    _ = http_mod  # (imported for parity with sibling tests; not monkeypatched)

    fingerprint = idset_hash({p[0] for p in _POSTINGS})

    # --- PRIOR snapshot: one full crawl+build (flag off), becomes the carry-forward source. ---
    monkeypatch.delenv("ERGON_DELTA_CRAWL", raising=False)
    prior_fresh = tmp_path / "prior" / "fresh.sqlite"
    prior_fresh.parent.mkdir(parents=True)
    s_prior = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    _, prior_keys = _crawl(s_prior, prior_fresh, prov, "prior")
    prior_db = tmp_path / "prior" / "index.sqlite"
    build_index_from_fresh_db(prior_fresh, prior_db, build_id="prior")

    # --- FULL path "today": re-crawl the board (flag off), carry from prior. ---
    full_fresh = tmp_path / "full" / "fresh.sqlite"
    full_fresh.parent.mkdir(parents=True)
    s_full = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    prov.fetch_calls = 0
    out_full, full_keys = _crawl(s_full, full_fresh, prov, "today")
    assert prov.fetch_calls == 1 and out_full["greenhouse|acme"]["not_modified"] is False
    full_db = tmp_path / "full" / "index.sqlite"
    build_index_from_fresh_db(full_fresh, full_db, build_id="today", prev_db=prior_db,
                              crawled_keys=full_keys)

    # --- DELTA path "today": flag on, matching sidecar + stamped fingerprint => board SKIPPED. ---
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    delta_fresh = tmp_path / "delta" / "fresh.sqlite"
    delta_fresh.parent.mkdir(parents=True)
    _write_sidecar(delta_fresh.parent / "index-freshness.sqlite", "greenhouse", _TOKEN, fingerprint)
    s_delta = {
        "greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN, idset_hash=fingerprint)
    }
    prov.fetch_calls = 0
    out_delta, delta_keys = _crawl(s_delta, delta_fresh, prov, "today")
    # The skip fired: no fetch, marked not_modified, nothing streamed to fresh, carries forward.
    assert prov.fetch_calls == 0
    assert out_delta["greenhouse|acme"]["not_modified"] is True
    assert delta_keys == set()
    delta_db = tmp_path / "delta" / "index.sqlite"
    build_index_from_fresh_db(delta_fresh, delta_db, build_id="today", prev_db=prior_db,
                              crawled_keys=delta_keys)

    # --- PARITY: every data/identity column is byte-identical between the two builds. ---
    cols_full, rows_full = _stable_rows(full_db)
    cols_delta, rows_delta = _stable_rows(delta_db)
    assert cols_full == cols_delta
    assert rows_full == rows_delta
    assert len(rows_full) == len(_POSTINGS)


def test_flag_off_never_skips(monkeypatch, tmp_path):
    """Flag OFF: even with a matching sidecar + stamped fingerprint the board is CRAWLED, not
    skipped -- proving the delta path ships dark (byte-for-byte today's behaviour when off)."""
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    prov = _Provider()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.delenv("ERGON_DELTA_CRAWL", raising=False)

    fingerprint = idset_hash({p[0] for p in _POSTINGS})
    fresh = tmp_path / "fresh.sqlite"
    _write_sidecar(tmp_path / "index-freshness.sqlite", "greenhouse", _TOKEN, fingerprint)
    states = {
        "greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN, idset_hash=fingerprint)
    }
    out, _ = anyio.run(bi._crawl_due, 10, states, fresh, "b1")
    assert prov.fetch_calls == 1  # crawled despite the matching hash -- flag is off
    assert out["greenhouse|acme"]["not_modified"] is False
