"""Stress tests for the daily freshness sweep engine (src/ergon_tracker/index/freshness.py).

Everything here is OFFLINE: ``get_provider`` is monkeypatched to a fake in-process provider
(never real network), ``now`` is injected (no wall-clock reads) -- matching the pattern already
used by tests/test_liveness.py and tests/test_detail_e2e.py for the sibling passes this mirrors.
"""

from __future__ import annotations

import sqlite3

import anyio

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.freshness import (
    DETERMINISTIC_SOURCES,
    board_live_ids,
    departed_ids,
    sweep_boards,
)
from ergon_tracker.index.query import search_rows
from ergon_tracker.models import RawJob, SearchQuery

_NOW = "2026-07-18T00:00:00+00:00"


# --- synthetic index builder (real schema, incl. job_sources for the raw source_job_id) -------


def _build_index(tmp_path, jobs: list[dict], *, name: str = "index") -> str:
    """A real-schema index (via ``fresh_db``) seeded with the given job row dicts AND matching
    ``job_sources`` provenance rows (freshness diffs against the RAW ``source_job_id``, which only
    ``job_sources`` carries -- ``jobs`` itself only stores the derived, hashed ``id``). Each dict
    may override any default below; unset columns fall back to sane, always-active-row defaults.
    ``source_job_id`` defaults to the row's own ``id`` when not given (fine whenever a test
    doesn't care that the two id-spaces differ; some tests below deliberately set it differently
    to prove the join direction is correct)."""
    p = tmp_path / f"{name}.sqlite"
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


def _job_row(job_id: str, *, source: str = "greenhouse", source_job_id: str | None = None) -> dict:
    return {"id": job_id, "source": source, "source_job_id": source_job_id}


def _job_status(idx_path: str, job_id: str) -> tuple[str, str | None]:
    con = sqlite3.connect(idx_path)
    row = con.execute("SELECT status, expiry_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()
    con.close()
    return row


def _count_jobs(idx_path: str) -> int:
    con = sqlite3.connect(idx_path)
    n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    con.close()
    return n


# --- departed_ids: pure function edge cases ------------------------------------------------


def test_departed_ids_finds_the_missing_id():
    assert departed_ids({"a", "b", "c"}, {"a", "b"}) == {"c"}


def test_departed_ids_none_live_set_returns_empty():
    # An errored/undetermined board fetch must NEVER be mistaken for "everything departed".
    assert departed_ids({"a", "b", "c"}, None) == set()


def test_departed_ids_full_overlap_returns_empty():
    assert departed_ids({"a", "b"}, {"a", "b"}) == set()


def test_departed_ids_live_superset_returns_empty():
    # The board can have MORE ids than we've stored (new postings we haven't crawled yet) --
    # that's not a departure signal at all.
    assert departed_ids({"a"}, {"a", "b", "c"}) == set()


def test_departed_ids_empty_live_set_departs_everything_stored():
    # A genuinely empty (but non-None) live set means the board really has zero postings right
    # now -- distinct from None (couldn't determine).
    assert departed_ids({"a", "b"}, set()) == {"a", "b"}


def test_departed_ids_empty_stored_returns_empty():
    assert departed_ids(set(), {"a", "b"}) == set()


# --- board_live_ids: id-only fetch wrapper, non-raising --------------------------------------


class _FakeProvider:
    def __init__(self, raws=None, *, raises=False):
        self._raws = raws or []
        self._raises = raises

    async def fetch(self, token, query, fetcher):
        if self._raises:
            raise RuntimeError("boom")
        return self._raws


def _raw(source_job_id: str, source: str = "greenhouse") -> RawJob:
    return RawJob(source=source, source_job_id=source_job_id, company="Acme")


def test_board_live_ids_extracts_source_job_ids(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    monkeypatch.setattr(
        freshness, "get_provider", lambda name: _FakeProvider([_raw("1"), _raw("2")])
    )
    ids = anyio.run(lambda: board_live_ids("greenhouse", "acme", fetcher=object()))
    assert ids == {"1", "2"}


def test_board_live_ids_none_when_provider_unknown(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    monkeypatch.setattr(freshness, "get_provider", lambda name: None)
    ids = anyio.run(lambda: board_live_ids("nope", "acme", fetcher=object()))
    assert ids is None


def test_board_live_ids_none_on_fetch_exception(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    monkeypatch.setattr(freshness, "get_provider", lambda name: _FakeProvider(raises=True))
    ids = anyio.run(lambda: board_live_ids("greenhouse", "acme", fetcher=object()))
    assert ids is None


def test_board_live_ids_empty_board_is_empty_set_not_none(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    monkeypatch.setattr(freshness, "get_provider", lambda name: _FakeProvider([]))
    ids = anyio.run(lambda: board_live_ids("greenhouse", "acme", fetcher=object()))
    assert ids == set()  # a genuinely empty board is NOT the same as "couldn't determine"


# --- sweep_boards: end-to-end on a synthetic index --------------------------------------------


def _make_get_provider(present: dict[tuple[str, str], set[str] | None]):
    """`present[(source, token)]` -> the raw source_job_ids this board's fresh fetch currently
    returns, or `None` to simulate a failed/errored board fetch. Missing keys default to an empty
    board (no jobs returned, not an error)."""

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                ids = present.get((name, token), set())
                if ids is None:
                    raise RuntimeError("simulated board fetch failure")
                return [_raw(i, source=name) for i in ids]

        return _P()

    return get_provider


def test_sweep_expires_departed_keeps_live_row_count_unchanged_and_excludes_from_search(
    tmp_path, monkeypatch
):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("gh-a", source_job_id="1"),
            _job_row("gh-b", source_job_id="2"),
            _job_row("gh-c", source_job_id="3"),  # this one "left the board"
        ],
    )
    # The board's fresh fetch only returns ids 1 and 2 -- id 3 (gh-c) departed.
    monkeypatch.setattr(
        freshness,
        "get_provider",
        _make_get_provider({("greenhouse", "acme"): {"1", "2"}}),
    )

    before = _count_jobs(idx)
    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_boards(
            [("greenhouse", "acme")],
            con,
            fetcher=object(),
            deterministic_sources=DETERMINISTIC_SOURCES,
            now=lambda: _NOW,
        )
    )
    con.close()

    assert stats["greenhouse"] == {"checked": 1, "departed": 1, "expired": 1, "errored": 0}
    assert _job_status(idx, "gh-c") == ("expired", "departed_board")
    assert _job_status(idx, "gh-a") == ("active", None)
    assert _job_status(idx, "gh-b") == ("active", None)
    assert _count_jobs(idx) == before  # never a hard delete -> row_floor-safe

    con = sqlite3.connect(idx)
    con.row_factory = sqlite3.Row
    ids = {r["id"] for r in search_rows(con, SearchQuery())}
    con.close()
    assert ids == {"gh-a", "gh-b"}  # status='active' filter excludes the departed row


def test_sweep_errored_board_expires_nothing(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("gh-a", source_job_id="1")])
    monkeypatch.setattr(
        freshness, "get_provider", _make_get_provider({("greenhouse", "acme"): None})
    )

    before = _count_jobs(idx)
    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_boards([("greenhouse", "acme")], con, fetcher=object(), now=lambda: _NOW)
    )
    con.close()

    assert stats["greenhouse"] == {"checked": 1, "departed": 0, "expired": 0, "errored": 1}
    assert _job_status(idx, "gh-a") == ("active", None)
    assert _count_jobs(idx) == before


def test_sweep_excludes_search_index_sources(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("sr-a", source="smartrecruiters", source_job_id="1")])

    def get_provider(name):
        raise AssertionError(
            f"get_provider must never be called for a non-deterministic source: {name}"
        )

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_boards([("smartrecruiters", "acme")], con, fetcher=object(), now=lambda: _NOW)
    )
    con.close()

    assert stats == {}  # excluded up front -- never fetched, never touched
    assert _job_status(idx, "sr-a") == ("active", None)


def test_sweep_mixed_boards_only_sweeps_the_deterministic_one(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("gh-a", source="greenhouse", source_job_id="1"),
            _job_row("sr-a", source="smartrecruiters", source_job_id="1"),
        ],
    )

    def get_provider(name):
        assert name == "greenhouse"  # smartrecruiters must never reach get_provider

        class _P:
            async def fetch(self, token, query, fetcher):
                return []  # gh-a departs

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_boards(
            [("greenhouse", "acme"), ("smartrecruiters", "acme")],
            con,
            fetcher=object(),
            now=lambda: _NOW,
        )
    )
    con.close()

    assert set(stats.keys()) == {"greenhouse"}
    assert _job_status(idx, "gh-a") == ("expired", "departed_board")
    assert _job_status(idx, "sr-a") == ("active", None)  # untouched


def test_sweep_no_active_rows_on_board_still_checks_but_expires_nothing(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("gh-a", source_job_id="1")])
    monkeypatch.setattr(
        freshness,
        "get_provider",
        _make_get_provider({("greenhouse", "other-token"): {"9"}}),
    )

    con = sqlite3.connect(idx)
    # sweep a DIFFERENT board than the one gh-a lives on -- no stored active ids for it.
    stats = anyio.run(
        lambda: sweep_boards(
            [("greenhouse", "other-token")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["greenhouse"] == {"checked": 1, "departed": 0, "expired": 0, "errored": 0}
    assert _job_status(idx, "gh-a") == ("active", None)


def test_sweep_job_id_and_source_job_id_spaces_differ_correctly(tmp_path, monkeypatch):
    # jobs.id is the derived/hashed id; job_sources.source_job_id is the raw provider id. The
    # UPDATE must key off jobs.id even though the diff itself happens in source_job_id space.
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("hashed-job-a", source_job_id="raw-1001"),
            _job_row("hashed-job-b", source_job_id="raw-1002"),
        ],
    )
    # Only raw-1001 is still on the board -- raw-1002 (-> hashed-job-b) departed.
    monkeypatch.setattr(
        freshness,
        "get_provider",
        _make_get_provider({("greenhouse", "acme"): {"raw-1001"}}),
    )

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_boards([("greenhouse", "acme")], con, fetcher=object(), now=lambda: _NOW)
    )
    con.close()

    assert stats["greenhouse"]["expired"] == 1
    assert _job_status(idx, "hashed-job-b") == ("expired", "departed_board")
    assert _job_status(idx, "hashed-job-a") == ("active", None)


# --- concurrency: bounded board pool, no deadlock, honors the cap -----------------------------


def test_sweep_honors_concurrency_cap_and_completes(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    n_boards = 40
    jobs = [_job_row(f"b{i}-job", source_job_id=f"j{i}") for i in range(n_boards)]
    idx = _build_index(tmp_path, jobs)
    cap = 5

    state = {"inflight": 0, "max_inflight": 0}
    lock = anyio.Lock()

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                async with lock:
                    state["inflight"] += 1
                    state["max_inflight"] = max(state["max_inflight"], state["inflight"])
                await anyio.sleep(0.01)  # hold the slot long enough for overlap to occur
                async with lock:
                    state["inflight"] -= 1
                idx_num = token.replace("board", "")
                return [_raw(f"j{idx_num}", source=name)]  # every board's job stays present

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    # Each job lives on its own distinctly-tokened board (the synthetic index builder defaults
    # every row to board_token="acme" / matching source_job_id -- re-seed per-board here so each
    # of the n_boards fetches maps to a distinct row).
    con = sqlite3.connect(idx)
    for i in range(n_boards):
        con.execute("UPDATE jobs SET board_token = ? WHERE id = ?", (f"board{i}", f"b{i}-job"))
    con.commit()

    boards = [("greenhouse", f"board{i}") for i in range(n_boards)]
    stats = anyio.run(
        lambda: sweep_boards(boards, con, fetcher=object(), concurrency=cap, now=lambda: _NOW)
    )
    con.close()

    assert stats["greenhouse"]["checked"] == n_boards
    assert stats["greenhouse"]["expired"] == 0
    assert 1 <= state["max_inflight"] <= cap  # bounded by the cap, and real overlap happened


def test_sweep_empty_boards_iterable_returns_empty_dict(tmp_path):
    idx = _build_index(tmp_path, [_job_row("gh-a", source_job_id="1")])
    con = sqlite3.connect(idx)
    stats = anyio.run(lambda: sweep_boards([], con, fetcher=object(), now=lambda: _NOW))
    con.close()
    assert stats == {}
