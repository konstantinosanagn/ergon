"""Stress tests for the daily freshness sweep engine (src/ergon_tracker/index/freshness.py).

Everything here is OFFLINE: ``get_provider`` is monkeypatched to a fake in-process provider
(never real network), ``now`` is injected (no wall-clock reads) -- matching the pattern already
used by tests/test_liveness.py and tests/test_detail_e2e.py for the sibling passes this mirrors.
"""

from __future__ import annotations

import sqlite3

import anyio

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.index.freshness import (
    DETERMINISTIC_SOURCES,
    SEARCH_INDEX_SOURCES,
    board_live_ids,
    confirm_departed,
    departed_ids,
    sweep_all_boards,
    sweep_boards,
    sweep_search_index_boards,
)
from ergon_tracker.index.query import search_rows
from ergon_tracker.models import DetailFetch, RawJob, SearchQuery

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
                # A non-empty live board that no longer lists gh-a (source_job_id "1") -- so gh-a
                # genuinely departs. (An empty live set would trip the safety valve, not expire.)
                return [_raw("999", source="greenhouse")]

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


def test_sweep_empty_live_set_never_expires_a_whole_board(tmp_path, monkeypatch):
    # A provider that silently returns [] on a transient failure (e.g. jazzhr/dejobs on a 429/5xx)
    # must NOT cause every stored active posting on that board to be expired. board_live_ids yields
    # set() (not None) for it, so the safety valve must treat "empty live set + non-empty stored"
    # as undetermined -- counted as errored, expiring nothing.
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("gh-a", source="greenhouse", source_job_id="1"),
            _job_row("gh-b", source="greenhouse", source_job_id="2"),
        ],
    )

    class _P:
        async def fetch(self, token, query, fetcher):
            return []  # a silently-swallowed transient failure looks exactly like this

    monkeypatch.setattr(freshness, "get_provider", lambda name: _P())

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_boards([("greenhouse", "acme")], con, fetcher=object(), now=lambda: _NOW)
    )
    con.close()

    assert stats["greenhouse"] == {"checked": 1, "departed": 0, "expired": 0, "errored": 1}
    assert _job_status(idx, "gh-a") == ("active", None)  # NOT expired
    assert _job_status(idx, "gh-b") == ("active", None)  # NOT expired


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


# --- PHASE 1: search-index sources (candidate + per-posting confirm) --------------------------
#
# oracle/smartrecruiters/successfactors reshuffle/paginate their lists (measured 50-100% list-miss
# false-positive rate), so a stored id missing from a fresh board list is only a CANDIDATE, never
# a confirmed departure -- it must be confirmed via the provider's per-posting fetch_detail before
# anything is expired. icims/eightfold skip the bulk relist entirely (their bulk lists are
# pathologically bloated) and per-posting-confirm directly against the board's stored active ids.


def _ref(job_id: str, *, source: str = "oracle", apply_url: str | None = None) -> DetailRef:
    return DetailRef(
        id=job_id,
        source=source,
        token="acme",
        apply_url=apply_url or f"http://x/{job_id}",
        listing_url=None,
        content_sig="sig",
    )


# --- confirm_departed: pure per-posting confirm wrapper, non-raising --------------------------


def test_confirm_departed_true_when_detail_is_none(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    class _P:
        async def fetch_detail(self, ref, fetcher):
            return None

    monkeypatch.setattr(freshness, "get_provider", lambda name: _P())
    verdict = anyio.run(lambda: confirm_departed(_ref("x"), fetcher=object()))
    assert verdict is True  # confirmed dead


def test_confirm_departed_false_when_str_detail_has_text(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    class _P:
        async def fetch_detail(self, ref, fetcher):
            return "Real JD text"

    monkeypatch.setattr(freshness, "get_provider", lambda name: _P())
    verdict = anyio.run(lambda: confirm_departed(_ref("x"), fetcher=object()))
    assert verdict is False  # confirmed alive -- list miss was a false positive


def test_confirm_departed_false_when_detailfetch_has_text(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    class _P:
        async def fetch_detail(self, ref, fetcher):
            return DetailFetch(text="Real JD text")

    monkeypatch.setattr(freshness, "get_provider", lambda name: _P())
    verdict = anyio.run(lambda: confirm_departed(_ref("x"), fetcher=object()))
    assert verdict is False


def test_confirm_departed_true_when_detailfetch_has_empty_text(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    class _P:
        async def fetch_detail(self, ref, fetcher):
            return DetailFetch(text="")

    monkeypatch.setattr(freshness, "get_provider", lambda name: _P())
    verdict = anyio.run(lambda: confirm_departed(_ref("x"), fetcher=object()))
    assert verdict is True


def test_confirm_departed_none_on_exception(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    class _P:
        async def fetch_detail(self, ref, fetcher):
            raise RuntimeError("timeout")

    monkeypatch.setattr(freshness, "get_provider", lambda name: _P())
    verdict = anyio.run(lambda: confirm_departed(_ref("x"), fetcher=object()))
    assert verdict is None  # could not determine -- caller must NOT expire on this


def test_confirm_departed_none_when_provider_unknown(monkeypatch):
    import ergon_tracker.index.freshness as freshness

    monkeypatch.setattr(freshness, "get_provider", lambda name: None)
    verdict = anyio.run(lambda: confirm_departed(_ref("x", source="nope"), fetcher=object()))
    assert verdict is None


# --- sweep_search_index_boards: bulk-relist-confirm sources (oracle/smartrecruiters/successfactors)


def _make_search_index_provider(*, fetch_present: set[str] | None, detail_verdicts: dict[str, str]):
    """``fetch_present``: raw source_job_ids the bulk list returns (``None`` simulates a failed
    board fetch). ``detail_verdicts``: ``job_id`` (the ``jobs.id``, recoverable from the ref's
    ``apply_url`` suffix, which the synthetic index always sets to ``http://x/{job_id}``) ->
    ``"alive" | "dead" | "error"``, defaulting to ``"dead"`` for any id not listed."""

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                if fetch_present is None:
                    raise RuntimeError("simulated board fetch failure")
                return [_raw(i, source=name) for i in fetch_present]

            async def fetch_detail(self, ref, fetcher):
                job_id = (ref.apply_url or "").rsplit("/", 1)[-1]
                verdict = detail_verdicts.get(job_id, "dead")
                if verdict == "error":
                    raise RuntimeError("boom")
                return "Real JD text" if verdict == "alive" else None

        return _P()

    return get_provider


def test_search_index_bulk_relist_confirms_candidates_oracle_style(tmp_path, monkeypatch):
    # 3 active rows; bulk-list returns only 1 -> 2 candidates (reshuffled-list false positives).
    # fetch_detail says candidate A is LIVE, candidate B is DEAD -> only B expires, A stays active,
    # row count unchanged.
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("or-a", source="oracle", source_job_id="1"),
            _job_row("or-b", source="oracle", source_job_id="2"),
            _job_row("or-c", source="oracle", source_job_id="3"),
        ],
    )
    monkeypatch.setattr(
        freshness,
        "get_provider",
        _make_search_index_provider(
            fetch_present={"1"}, detail_verdicts={"or-b": "alive", "or-c": "dead"}
        ),
    )

    before = _count_jobs(idx)
    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("oracle", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["oracle"]["checked"] == 1
    assert stats["oracle"]["candidates"] == 2
    assert stats["oracle"]["expired"] == 1
    assert stats["oracle"]["confirmed_alive"] == 1
    assert stats["oracle"]["unconfirmed"] == 0
    assert stats["oracle"]["errored"] == 0
    assert _job_status(idx, "or-a") == ("active", None)  # never missing from the list at all
    assert _job_status(idx, "or-b") == ("active", None)  # candidate, but confirmed alive
    assert _job_status(idx, "or-c") == ("expired", "departed_board")  # candidate, confirmed dead
    assert _count_jobs(idx) == before  # never a hard delete -> row_floor-safe


def test_search_index_bulk_relist_no_candidates_skips_confirm(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("sr-a", source="smartrecruiters", source_job_id="1")])

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                return [_raw("1", source=name)]  # full overlap -> no candidates

            async def fetch_detail(self, ref, fetcher):
                raise AssertionError("must not confirm when there are no candidates")

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("smartrecruiters", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["smartrecruiters"] == {
        "checked": 1,
        "candidates": 0,
        "expired": 0,
        "confirmed_alive": 0,
        "unconfirmed": 0,
        "errored": 0,
    }
    assert _job_status(idx, "sr-a") == ("active", None)


def test_search_index_bulk_relist_board_error_derives_no_candidates(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("sr-a", source="smartrecruiters", source_job_id="1")])
    monkeypatch.setattr(
        freshness,
        "get_provider",
        _make_search_index_provider(fetch_present=None, detail_verdicts={}),
    )

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("smartrecruiters", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["smartrecruiters"]["errored"] == 1
    assert stats["smartrecruiters"]["candidates"] == 0
    assert stats["smartrecruiters"]["expired"] == 0
    assert _job_status(idx, "sr-a") == ("active", None)


# --- sweep_search_index_boards: per-posting-confirm sources (icims/eightfold) ------------------


def test_search_index_per_posting_confirm_icims_style(tmp_path, monkeypatch):
    # No bulk relist for icims/eightfold -- every stored active id is confirmed directly.
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("ic-a", source="icims", source_job_id="1"),
            _job_row("ic-b", source="icims", source_job_id="2"),
        ],
    )

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                raise AssertionError("bulk relist must never be called for icims/eightfold")

            async def fetch_detail(self, ref, fetcher):
                job_id = (ref.apply_url or "").rsplit("/", 1)[-1]
                return None if job_id == "ic-b" else "Real JD text"

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    before = _count_jobs(idx)
    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("icims", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["icims"]["checked"] == 1
    assert stats["icims"]["candidates"] == 2
    assert stats["icims"]["expired"] == 1
    assert stats["icims"]["confirmed_alive"] == 1
    assert _job_status(idx, "ic-a") == ("active", None)  # confirmed alive
    assert _job_status(idx, "ic-b") == ("expired", "departed_board")  # confirmed dead
    assert _count_jobs(idx) == before


def test_search_index_per_posting_confirm_eightfold_style(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("ef-a", source="eightfold", source_job_id="1"),
            _job_row("ef-b", source="eightfold", source_job_id="2"),
        ],
    )

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                raise AssertionError("bulk relist must never be called for icims/eightfold")

            async def fetch_detail(self, ref, fetcher):
                job_id = (ref.apply_url or "").rsplit("/", 1)[-1]
                return DetailFetch(text="Real JD") if job_id == "ef-a" else DetailFetch(text="")

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("eightfold", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["eightfold"]["expired"] == 1
    assert stats["eightfold"]["confirmed_alive"] == 1
    assert _job_status(idx, "ef-a") == ("active", None)
    assert _job_status(idx, "ef-b") == ("expired", "departed_board")


def test_search_index_per_posting_board_limit_bounds_candidates(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    n = 10
    jobs = [_job_row(f"ic-{i}", source="icims", source_job_id=str(i)) for i in range(n)]
    idx = _build_index(tmp_path, jobs)

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                raise AssertionError("bulk relist must never be called for icims/eightfold")

            async def fetch_detail(self, ref, fetcher):
                return "Real JD text"

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("icims", "acme")],
            con,
            fetcher=object(),
            board_active_id_limit=3,
            now=lambda: _NOW,
        )
    )
    con.close()

    assert stats["icims"]["candidates"] == 3  # bounded, not all 10


# --- error path: a candidate whose fetch_detail raises/times out is NOT expired ----------------


def test_search_index_confirm_error_keeps_row_active(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("sr-a", source="smartrecruiters", source_job_id="1")])

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                return []  # "1" missing from list -> candidate

            async def fetch_detail(self, ref, fetcher):
                raise RuntimeError("timeout")

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    before = _count_jobs(idx)
    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("smartrecruiters", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats["smartrecruiters"]["candidates"] == 1
    assert stats["smartrecruiters"]["unconfirmed"] == 1
    assert stats["smartrecruiters"]["expired"] == 0
    assert _job_status(idx, "sr-a") == ("active", None)  # errored confirm -> kept, retry next run
    assert _count_jobs(idx) == before


# --- deterministic source still routes through Phase-0 logic (no fetch_detail confirm) ---------


def test_search_index_sweep_excludes_deterministic_sources(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(tmp_path, [_job_row("gh-a", source="greenhouse", source_job_id="1")])

    def get_provider(name):
        raise AssertionError(
            f"get_provider must never be called for a deterministic source: {name}"
        )

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("greenhouse", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert stats == {}  # excluded up front -- never fetched, never touched
    assert _job_status(idx, "gh-a") == ("active", None)


def test_sweep_all_boards_composes_phase0_and_phase1_without_cross_calling(tmp_path, monkeypatch):
    # greenhouse (deterministic) departs on a single list-miss, with NO fetch_detail confirm.
    # oracle (search-index) only reaches fetch_detail for its own candidate.
    import ergon_tracker.index.freshness as freshness

    idx = _build_index(
        tmp_path,
        [
            _job_row("gh-a", source="greenhouse", source_job_id="1"),
            _job_row("or-a", source="oracle", source_job_id="1"),
        ],
    )

    detail_calls: list[str] = []

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                # A non-empty live board that lists neither stored posting (source_job_id "1"):
                # greenhouse's "1" genuinely departs; oracle's "1" becomes a confirm candidate.
                # (An empty live set would trip the deterministic safety valve instead.)
                return [_raw("999", source=name)]

            async def fetch_detail(self, ref, fetcher):
                detail_calls.append(ref.id)
                return None  # confirmed dead

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_all_boards(
            [("greenhouse", "acme"), ("oracle", "acme")], con, fetcher=object(), now=lambda: _NOW
        )
    )
    con.close()

    assert set(stats.keys()) == {"greenhouse", "oracle"}
    assert stats["greenhouse"] == {"checked": 1, "departed": 1, "expired": 1, "errored": 0}
    assert stats["oracle"]["candidates"] == 1
    assert stats["oracle"]["expired"] == 1
    assert detail_calls == ["or-a"]  # fetch_detail only ever called for the search-index candidate
    assert _job_status(idx, "gh-a") == ("expired", "departed_board")
    assert _job_status(idx, "or-a") == ("expired", "departed_board")


def test_search_index_sources_constant_matches_spec():
    assert {
        "oracle",
        "smartrecruiters",
        "successfactors",
        "icims",
        "eightfold",
    } == SEARCH_INDEX_SOURCES
    assert SEARCH_INDEX_SOURCES.isdisjoint(DETERMINISTIC_SOURCES)


# --- concurrency: bounded confirm-fetch pool, no deadlock, honors the cap ----------------------


def test_search_index_confirm_honors_concurrency_cap(tmp_path, monkeypatch):
    import ergon_tracker.index.freshness as freshness

    n = 20
    jobs = [_job_row(f"ef-{i}", source="eightfold", source_job_id=str(i)) for i in range(n)]
    idx = _build_index(tmp_path, jobs)
    cap = 4

    state = {"inflight": 0, "max_inflight": 0}
    lock = anyio.Lock()

    def get_provider(name):
        class _P:
            async def fetch(self, token, query, fetcher):
                raise AssertionError("bulk relist must never be called for icims/eightfold")

            async def fetch_detail(self, ref, fetcher):
                async with lock:
                    state["inflight"] += 1
                    state["max_inflight"] = max(state["max_inflight"], state["inflight"])
                await anyio.sleep(0.01)  # hold the slot long enough for overlap to occur
                async with lock:
                    state["inflight"] -= 1
                return "Real JD text"  # everyone confirmed alive

        return _P()

    monkeypatch.setattr(freshness, "get_provider", get_provider)

    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards(
            [("eightfold", "acme")], con, fetcher=object(), concurrency=cap, now=lambda: _NOW
        )
    )
    con.close()

    assert stats["eightfold"]["candidates"] == n
    assert stats["eightfold"]["confirmed_alive"] == n
    assert 1 <= state["max_inflight"] <= cap  # bounded by the shared cap, real overlap happened


def test_search_index_sweep_empty_boards_iterable_returns_empty_dict(tmp_path):
    idx = _build_index(tmp_path, [_job_row("or-a", source="oracle", source_job_id="1")])
    con = sqlite3.connect(idx)
    stats = anyio.run(
        lambda: sweep_search_index_boards([], con, fetcher=object(), now=lambda: _NOW)
    )
    con.close()
    assert stats == {}
