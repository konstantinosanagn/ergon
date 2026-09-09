"""A transient fetch failure must never expire a live row — end to end through the liveness pass.

The audit reproduction: with join.com timing out on every request, one liveness run flipped
300 of 300 rows to ``expired`` and reported ``confirm_errored=0, boards_failed=0`` — a total
data loss that looked like a perfectly healthy run.

The consumer was never the problem. ``liveness.classify_row`` already distinguished a raised
``fetch_detail`` (indeterminate → keep the row) from a returned ``None`` (confirmed gone → expire).
The defect was that join/jobvite/workable swallowed every exception into ``None``, so the
distinction never reached it. ``CONFIRM_VIA_DETAIL_SOURCES`` uses
``_STREAK_THRESHOLD_CONFIRMED == 1``, so a single swallowed timeout was enough.

These tests drive the real ``reconcile_liveness_tier`` with the real providers, so they fail if
either half regresses — the provider swallowing again, or the pass collapsing raise into None.
"""

from __future__ import annotations

import sqlite3

import anyio
import httpx
import pytest

from ergon.index.db import fresh_db
from ergon.index.detail import DetailRef
from ergon.index.liveness import open_liveness, reconcile_liveness_tier
from ergon.providers.jobvite import JobviteProvider
from ergon.providers.join import JoinProvider
from ergon.providers.workable import WorkableProvider, _reset_workable_cache

_DAY0 = "2026-07-01T00:00:00+00:00"
_N = 25


def _index(tmp_path, source: str, apply_url_for) -> str:
    p = tmp_path / f"{source}.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    con.executemany(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, board_token, "
        "apply_url) VALUES (:id, :ch, :src, 'Acme', 'Engineer', 'unknown', 'mid', "
        "'full_time', 'active', :ts, :ts, :ts, 'b0', 'acme', :apply)",
        [
            {
                "id": f"{source}-{i}",
                "ch": f"ch-{i}",
                "src": source,
                "ts": _DAY0,
                "apply": apply_url_for(i),
            }
            for i in range(_N)
        ],
    )
    con.commit()
    con.close()
    return str(p)


def _counts(idx: str) -> tuple[int, int]:
    con = sqlite3.connect(idx)
    try:
        active = con.execute("SELECT COUNT(*) FROM jobs WHERE status='active'").fetchone()[0]
        expired = con.execute("SELECT COUNT(*) FROM jobs WHERE status='expired'").fetchone()[0]
        return active, expired
    finally:
        con.close()


class _AlwaysTimesOut:
    """Every transport call fails transiently. Nothing here is evidence a posting is gone."""

    async def get_text(self, url, **kw):  # noqa: ANN001, ANN003, ANN201
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    async def get_json(self, url, **kw):  # noqa: ANN001, ANN003, ANN201
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    async def request(self, method, url, **kw):  # noqa: ANN001, ANN003, ANN201
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))


_CASES = [
    ("join", JoinProvider(), lambda i: f"https://join.com/companies/acme/jobs/{100000 + i}"),
    ("jobvite", JobviteProvider(), lambda i: f"https://jobs.jobvite.com/acme/job/j{i}"),
    ("workable", WorkableProvider(), lambda i: f"https://apply.workable.com/acme/j/CODE{i:03d}"),
]


@pytest.mark.parametrize(("source", "provider", "url_for"), _CASES, ids=[c[0] for c in _CASES])
def test_total_transport_failure_expires_nothing(tmp_path, source, provider, url_for) -> None:
    """The audit's shape: the board lists nothing and every detail confirm times out.

    Pre-fix this expired 100% of the board while reporting confirm_errored=0. The row count must
    now be completely unchanged, and every row must be accounted for as an ERRORED confirm.
    """
    _reset_workable_cache()
    idx = _index(tmp_path, source, url_for)
    liv = str(tmp_path / f"{source}-liveness.sqlite")
    open_liveness(liv).close()
    fetcher = _AlwaysTimesOut()

    async def fetch_board(src: str, token: str) -> set[str]:
        # A board that responds but lists none of our ids — the list-miss that triggers the
        # per-posting confirm. This is the false-positive case the confirm exists to reject.
        return set()

    async def fetch_detail(ref: DetailRef):  # noqa: ANN202
        return await provider.fetch_detail(ref, fetcher)

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )

    active, expired = _counts(idx)
    assert expired == 0, f"{source}: a timeout expired {expired} live rows"
    assert active == _N
    assert stats["flipped_dead"] == 0
    # The failure must be VISIBLE, not silent — this counter read 0 during the incident.
    assert stats["confirm_errored"] == _N


@pytest.mark.parametrize(("source", "provider", "url_for"), _CASES, ids=[c[0] for c in _CASES])
def test_a_real_404_still_expires(tmp_path, source, provider, url_for) -> None:
    """The protection must not become a rubber stamp: a definitive gone-signal still expires.

    Skipped for workable, whose detail path fetches the BOARD, not the posting — a board 404 says
    nothing about one posting, so its gone-signal is 'board healthy, shortcode absent' instead.
    That case is covered in tests/test_workable_fetch_detail.py.
    """
    if source == "workable":
        pytest.skip("workable confirms via the board, not a per-posting 404 — covered elsewhere")

    idx = _index(tmp_path, source, url_for)
    liv = str(tmp_path / f"{source}-liveness.sqlite")
    open_liveness(liv).close()

    class _Gone:
        async def get_text(self, url, **kw):  # noqa: ANN001, ANN003, ANN201
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "404", request=req, response=httpx.Response(404, request=req)
            )

        async def get_json(self, url, **kw):  # noqa: ANN001, ANN003, ANN201
            return await self.get_text(url)

        async def request(self, method, url, **kw):  # noqa: ANN001, ANN003, ANN201
            return await self.get_text(url)

    async def fetch_board(src: str, token: str) -> set[str]:
        return set()

    async def fetch_detail(ref: DetailRef):  # noqa: ANN202
        return await provider.fetch_detail(ref, _Gone())

    anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )

    active, expired = _counts(idx)
    assert expired == _N, f"{source}: a real 404 must still expire (got {expired})"
    assert active == 0
