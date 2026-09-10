"""The drain must tell "gone" from "transient", and must never retry the un-fetchable.

For seven weeks drain-detail reported ~23,000 failures a day as ``success``. Reproduced live, they
were postings that no longer exist: HTTP 200 with no JD body, or a provider returning ``None`` —
its own contract for "confirmed gone". ``handle()`` swallowed every outcome in one bare
``except``, never logged the class, and ``_record_attempt`` bumped the retry budget on a
posting that could never succeed; ``reset_detail_attempts`` then zeroed that budget every run,
so the same dead rows were re-fetched daily, forever.

Pinned here: ``None`` retires a row (attempted once, immune to the reset, re-opened only by a
changed posting); an exception keeps the bounded retry budget; the failure classes are logged;
and rows that cannot be fetched at all — expired, or with nothing to address — are never
candidates.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

import anyio
import pytest

from ergon.index.db import fresh_db
from ergon.index.detail import (
    RETRY_CAP,
    _tier3_rows,
    open_detail,
    reconcile_detail_tier,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reset_detail_attempts import reset_stuck_attempts  # noqa: E402

_NOW = "2026-09-11T00:00:00+00:00"
_JD = "<p>We are hiring a senior engineer. Salary $150,000 - $180,000. 5+ years required.</p>"


def _index(path: Path, rows: list[tuple[str, str, str | None, str | None, str | None]]) -> str:
    """rows: (id, status, board_token, apply_url, listing_url); all smartrecruiters (tier-3)."""
    fresh_db(path)
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token, "
        "apply_url, listing_url) VALUES (?, ?, 'smartrecruiters', 'A', 'T', 'unknown', 'mid', "
        "'full_time', ?, ?, ?, ?, 'b0', ?, ?, ?)",
        [(i, f"h-{i}", st, _NOW, _NOW, _NOW, bt, au, lu) for i, st, bt, au, lu in rows],
    )
    con.commit()
    con.close()
    return str(path)


def _run(det: str, idx: str, fetch):  # noqa: ANN001, ANN202
    return anyio.run(lambda: reconcile_detail_tier(det, idx, fetch_detail=fetch, now=lambda: _NOW))


def _row(det: str, id_: str) -> tuple:
    con = open_detail(det)
    try:
        return con.execute(
            "SELECT attempts, fetched_at, snippet FROM job_detail WHERE id = ?", (id_,)
        ).fetchone()
    finally:
        con.close()


# --- gone ----------------------------------------------------------------------------------


def test_confirmed_gone_is_retired_not_retried(tmp_path: Path) -> None:
    idx = _index(tmp_path / "i.sqlite", [("gone", "active", "tok", "http://x/gone", None)])
    det = str(tmp_path / "d.sqlite")
    calls: list[str] = []

    async def fetch(ref):  # noqa: ANN001, ANN202
        calls.append(ref.id)
        return None

    s1 = _run(det, idx, fetch)
    assert s1 == {"fetched": 0, "failed": 0, "gone": 1, "missing": 0}
    attempts, fetched_at, _ = _row(det, "gone")
    assert attempts == RETRY_CAP and fetched_at == _NOW
    _run(det, idx, fetch)
    assert calls == ["gone"], "a confirmed-gone posting was fetched again"


def test_reset_script_cannot_revive_a_gone_row(tmp_path: Path) -> None:
    """The reset only touches ``attempts``; a retired row must survive it."""
    idx = _index(tmp_path / "i.sqlite", [("gone", "active", "tok", "http://x/gone", None)])
    det = str(tmp_path / "d.sqlite")
    calls: list[str] = []

    async def fetch(ref):  # noqa: ANN001, ANN202
        calls.append(ref.id)
        return None

    _run(det, idx, fetch)
    reset_stuck_attempts(det)
    _run(det, idx, fetch)
    assert calls == ["gone"]


def test_changed_posting_reopens_a_gone_row(tmp_path: Path) -> None:
    idx = _index(tmp_path / "i.sqlite", [("gone", "active", "tok", "http://x/gone", None)])
    det = str(tmp_path / "d.sqlite")
    calls: list[str] = []

    async def fetch(ref):  # noqa: ANN001, ANN202
        calls.append(ref.id)
        return None if len(calls) == 1 else _JD

    _run(det, idx, fetch)
    con = sqlite3.connect(idx)
    con.execute("UPDATE jobs SET content_hash = 'h-changed' WHERE id = 'gone'")
    con.commit()
    con.close()
    s2 = _run(det, idx, fetch)
    assert calls == ["gone", "gone"] and s2["fetched"] == 1


def test_gone_keeps_previously_recovered_fields(tmp_path: Path) -> None:
    idx = _index(tmp_path / "i.sqlite", [("j", "active", "tok", "http://x/j", None)])
    det = str(tmp_path / "d.sqlite")
    n = 0

    async def fetch(ref):  # noqa: ANN001, ANN202
        nonlocal n
        n += 1
        return _JD if n == 1 else None

    _run(det, idx, fetch)
    con = open_detail(det)  # re-queue it the way the location backfill does
    con.execute("UPDATE job_detail SET fetched_at = NULL, attempts = 0")
    con.commit()
    con.close()
    _run(det, idx, fetch)
    _, fetched_at, snippet = _row(det, "j")
    assert fetched_at == _NOW and snippet, "a gone verdict must not erase a recovered snippet"


# --- transient -----------------------------------------------------------------------------


def test_transient_failure_keeps_a_bounded_retry_budget(tmp_path: Path) -> None:
    idx = _index(tmp_path / "i.sqlite", [("flaky", "active", "tok", "http://x/f", None)])
    det = str(tmp_path / "d.sqlite")
    calls: list[str] = []

    async def fetch(ref):  # noqa: ANN001, ANN202
        calls.append(ref.id)
        raise TimeoutError("host down")

    for i in range(1, RETRY_CAP + 1):
        s = _run(det, idx, fetch)
        assert s["failed"] == 1 and s["gone"] == 0
        assert _row(det, "flaky")[0] == i
    _run(det, idx, fetch)
    assert len(calls) == RETRY_CAP


def test_failure_classes_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Seven weeks of 98% failure went unnoticed because nothing said what was failing."""
    idx = _index(
        tmp_path / "i.sqlite",
        [("a", "active", "tok", "http://x/a", None), ("b", "active", "tok", "http://x/b", None)],
    )
    det = str(tmp_path / "d.sqlite")

    async def fetch(ref):  # noqa: ANN001, ANN202
        raise TimeoutError(ref.id) if ref.id == "a" else ValueError(ref.id)

    with caplog.at_level(logging.WARNING, logger="ergon.index.detail"):
        _run(det, idx, fetch)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "2 transient failure" in msg
    assert "smartrecruiters:TimeoutError=1" in msg and "smartrecruiters:ValueError=1" in msg


def test_transport_error_is_not_mistaken_for_gone(tmp_path: Path) -> None:
    """The failure mode that matters: a dead host must not retire live postings."""
    idx = _index(tmp_path / "i.sqlite", [("live", "active", "tok", "http://x/live", None)])
    det = str(tmp_path / "d.sqlite")

    async def fetch(ref):  # noqa: ANN001, ANN202
        raise ConnectionError("refused")

    _run(det, idx, fetch)
    attempts, fetched_at, _ = _row(det, "live")
    assert attempts == 1 and fetched_at is None


# --- candidates ----------------------------------------------------------------------------


def test_expired_and_addressless_rows_are_not_candidates(tmp_path: Path) -> None:
    """4,540 of one shard's daily failures had no URL and no token: nothing could fetch them."""
    idx = _index(
        tmp_path / "i.sqlite",
        [
            ("ok-url", "active", None, "http://x/1", None),
            ("ok-listing", "active", None, None, "http://x/l"),
            ("ok-token", "active", "tok", None, None),
            ("expired", "expired", "tok", "http://x/2", None),
            ("nothing", "active", None, None, None),
        ],
    )
    con = sqlite3.connect(idx)
    ids = {r["id"] for r in _tier3_rows(con, ["smartrecruiters"])}
    assert ids == {"ok-url", "ok-listing", "ok-token"}
