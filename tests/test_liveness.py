"""Stress tests for the build-time job-posting LIVENESS pass (src/ergon_tracker/index/liveness.py).

Everything here is OFFLINE: `fetch_board`/`fetch_detail` are injected fakes (never real network),
`now` is injected (no wall-clock reads), matching the pattern already used by
tests/test_detail_e2e.py for the Tier-3 detail pass this module mirrors.
"""

from __future__ import annotations

import sqlite3

import anyio

from ergon_tracker.index.db import fresh_db
from ergon_tracker.index.liveness import CONFIRM_VIA_DETAIL_SOURCES, reconcile_liveness_tier
from ergon_tracker.index.query import search_rows, whats_new_rows
from ergon_tracker.models import DetailFetch, SearchQuery

_DAY0 = "2026-07-01T00:00:00+00:00"
_DAY7 = "2026-07-08T00:00:00+00:00"
_DAY14 = "2026-07-15T00:00:00+00:00"


# --- synthetic index builder (real schema) -------------------------------------------------


def _build_index(tmp_path, jobs: list[dict], *, name: str = "index") -> str:
    """A real-schema index (via `fresh_db`) seeded with the given job row dicts. Each dict may
    override any of the defaults below; unset columns fall back to sane, always-active-row
    defaults so callers only specify what a given test actually varies."""
    p = tmp_path / f"{name}.sqlite"
    fresh_db(p)
    con = sqlite3.connect(p)
    rows = []
    for j in jobs:
        row = {
            "content_hash": f"ch-{j['id']}",
            "company": "Acme",
            "title": "Engineer",
            "remote": "unknown",
            "level": "mid",
            "employment_type": "full_time",
            "status": "active",
            "ts": _DAY0,
            "build_id": "b0",
            "company_key": None,
            "board_token": None,
            "apply_url": f"http://x/{j['id']}",
            "listing_url": None,
        }
        row.update(j)
        rows.append(row)
    con.executemany(
        "INSERT INTO jobs (id, content_hash, source, company, title, remote, level, "
        "employment_type, status, first_seen, last_seen, fetched_at, build_id, company_key, "
        "board_token, apply_url, listing_url) "
        "VALUES (:id, :content_hash, :source, :company, :title, :remote, :level, "
        ":employment_type, :status, :ts, :ts, :ts, :build_id, :company_key, :board_token, "
        ":apply_url, :listing_url)",
        rows,
    )
    con.commit()
    con.close()
    return str(p)


def _job_row(job_id: str, *, source: str = "greenhouse", board_token: str = "acme") -> dict:
    return {"id": job_id, "source": source, "board_token": board_token}


def _liveness_status(idx_path: str, job_id: str) -> tuple[str, str | None]:
    con = sqlite3.connect(idx_path)
    row = con.execute("SELECT status, expiry_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()
    con.close()
    return row


def _sidecar_row(det_path: str, job_id: str) -> tuple[int, str] | None:
    con = sqlite3.connect(det_path)
    ensure = con.execute(
        "SELECT dead_streak, verdict FROM job_liveness WHERE id = ?", (job_id,)
    ).fetchone()
    con.close()
    return ensure


# --- fake fetch_board / fetch_detail dispatchers ------------------------------------------


def _make_fetch_board(present: dict[tuple[str, str], set[str] | None]):
    """`present[(source, token)]` -> the set of ids this board's fresh list currently has, or
    `None` to simulate a failed/errored board fetch. Missing keys default to an empty board."""

    async def fetch_board(source: str, token: str) -> set[str] | None:
        return present.get((source, token), set())

    return fetch_board


def _make_fetch_detail(alive_ids: set[str], calls: list[str]):
    """Stage-2 confirm fake: returns a non-empty DetailFetch for ids in `alive_ids` (confirmed
    alive -- the list miss was a false positive), else `None` (explicit confirmed-dead, per
    fetch_detail's non-raising contract)."""

    async def fetch_detail(ref) -> DetailFetch | None:
        calls.append(ref.id)
        if ref.id in alive_ids:
            return DetailFetch(text="<p>Still here.</p>")
        return None

    return fetch_detail


# --- (a) non-fetch_detail source: streak-gated (threshold 2) ------------------------------


def test_unconfirmed_source_streak_one_does_not_flip(tmp_path):
    idx = _build_index(tmp_path, [_job_row("gh-1")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_board = _make_fetch_board({("greenhouse", "acme"): set()})  # departed -> miss
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    assert stats["flipped_dead"] == 0
    assert stats["checked"] == 1
    status, reason = _liveness_status(idx, "gh-1")
    assert status == "active" and reason is None
    dead_streak, verdict = _sidecar_row(liv, "gh-1")
    assert dead_streak == 1 and verdict == "candidate"


def test_unconfirmed_source_streak_two_flips_dead(tmp_path):
    idx = _build_index(tmp_path, [_job_row("gh-1")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_board = _make_fetch_board({("greenhouse", "acme"): set()})
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    # Two runs, RECHECK_DAYS apart (recheck_days=7 default; DAY0 -> DAY7 satisfies eligibility).
    for now in (_DAY0, _DAY7):
        stats = anyio.run(
            lambda now=now: reconcile_liveness_tier(
                liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: now
            )
        )
    assert stats["flipped_dead"] == 1
    status, reason = _liveness_status(idx, "gh-1")
    assert status == "expired" and reason == "dead_link"
    dead_streak, verdict = _sidecar_row(liv, "gh-1")
    assert dead_streak == 2 and verdict == "dead"


def test_unconfirmed_source_hit_after_miss_resets_streak(tmp_path):
    idx = _build_index(tmp_path, [_job_row("gh-1")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    # DAY0: miss (streak -> 1). DAY7: hit (reappears -> streak resets to 0, never flips).
    miss_board = _make_fetch_board({("greenhouse", "acme"): set()})
    anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=miss_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    hit_board = _make_fetch_board({("greenhouse", "acme"): {"gh-1"}})
    anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=hit_board, fetch_detail=fetch_detail, now=lambda: _DAY7
        )
    )
    status, reason = _liveness_status(idx, "gh-1")
    assert status == "active" and reason is None
    dead_streak, verdict = _sidecar_row(liv, "gh-1")
    assert dead_streak == 0 and verdict == "live"

    # A THIRD miss (DAY14) must need its own fresh 2-streak -- proves the reset was real, not
    # just a display artifact of the second run's bookkeeping.
    anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=miss_board, fetch_detail=fetch_detail, now=lambda: _DAY14
        )
    )
    status, _ = _liveness_status(idx, "gh-1")
    assert status == "active"  # single miss after a reset -- not yet flipped
    dead_streak, verdict = _sidecar_row(liv, "gh-1")
    assert dead_streak == 1 and verdict == "candidate"


# --- (b) fetch_detail-capable source: single-fetch confirm ---------------------------------


def test_confirm_source_detail_fetch_succeeds_avoids_false_positive(tmp_path):
    assert "workday" in CONFIRM_VIA_DETAIL_SOURCES
    idx = _build_index(tmp_path, [_job_row("wd-1", source="workday", board_token="wd-board")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_board = _make_fetch_board({("workday", "wd-board"): set()})  # list-miss (reshuffle)
    calls: list[str] = []
    fetch_detail = _make_fetch_detail(alive_ids={"wd-1"}, calls=calls)  # detail confirms alive

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    assert calls == ["wd-1"]  # stage 2 was actually dispatched
    assert stats["flipped_dead"] == 0
    assert stats["confirmed_alive"] == 1
    status, reason = _liveness_status(idx, "wd-1")
    assert status == "active" and reason is None
    dead_streak, verdict = _sidecar_row(liv, "wd-1")
    assert dead_streak == 0 and verdict == "live"


def test_confirm_source_detail_fetch_fails_flips_immediately(tmp_path):
    idx = _build_index(tmp_path, [_job_row("wd-1", source="workday", board_token="wd-board")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_board = _make_fetch_board({("workday", "wd-board"): set()})
    calls: list[str] = []
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=calls)  # explicit confirm-dead

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    assert calls == ["wd-1"]
    assert stats["flipped_dead"] == 1  # single miss + single confirmed-dead fetch -- no streak wait
    status, reason = _liveness_status(idx, "wd-1")
    assert status == "expired" and reason == "dead_link"
    dead_streak, verdict = _sidecar_row(liv, "wd-1")
    assert dead_streak == 1 and verdict == "dead"


def test_confirm_source_detail_fetch_raises_keeps_alive(tmp_path):
    # A RAISED fetch_detail (transient 5xx/timeout under the hardened contract) is NOT evidence of
    # death: the row must be KEPT active and its streak left untouched, never expired on a single
    # transient blip (regression guard for the confirm_errored path).
    idx = _build_index(tmp_path, [_job_row("wd-1", source="workday", board_token="wd-board")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_board = _make_fetch_board({("workday", "wd-board"): set()})  # list-miss
    calls: list[str] = []

    async def fetch_detail(ref):  # transient confirm error -- raises, never returns None
        calls.append(ref.id)
        raise RuntimeError("503 from detail endpoint")

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    assert calls == ["wd-1"]  # stage 2 was dispatched
    assert stats["flipped_dead"] == 0  # NOT expired on a transient error
    assert stats["confirm_errored"] == 1
    status, reason = _liveness_status(idx, "wd-1")
    assert status == "active" and reason is None
    # streak left untouched (no record written on error) -- a later run retries cleanly
    assert _sidecar_row(liv, "wd-1") is None


# --- (c) present in the fresh board set -> live, streak reset ------------------------------


def test_row_present_in_fresh_set_is_live_and_resets_any_prior_streak(tmp_path):
    idx = _build_index(tmp_path, [_job_row("gh-1")])
    liv = str(tmp_path / "liveness.sqlite")
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    miss_board = _make_fetch_board({("greenhouse", "acme"): set()})
    anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=miss_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    dead_streak, verdict = _sidecar_row(liv, "gh-1")
    assert dead_streak == 1  # pre-condition: a real prior miss on the books

    hit_board = _make_fetch_board({("greenhouse", "acme"): {"gh-1"}})
    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=hit_board, fetch_detail=fetch_detail, now=lambda: _DAY7
        )
    )
    assert stats["flipped_dead"] == 0
    status, reason = _liveness_status(idx, "gh-1")
    assert status == "active" and reason is None
    dead_streak, verdict = _sidecar_row(liv, "gh-1")
    assert dead_streak == 0 and verdict == "live"


# --- end-to-end synthetic: full pipeline + query-layer + row-count invariants ---------------


def test_e2e_departed_job_excluded_from_queries_live_job_stays_row_count_unchanged(tmp_path):
    # Two jobs on one greenhouse board; the board's fresh list will only ever contain job A --
    # job B "left the board". Non-tier3 source -> needs 2 consecutive weekly misses to flip.
    idx = _build_index(
        tmp_path,
        [
            _job_row("gh-a", source="greenhouse", board_token="acme"),
            _job_row("gh-b", source="greenhouse", board_token="acme"),
        ],
    )
    liv = str(tmp_path / "liveness.sqlite")
    fetch_board = _make_fetch_board({("greenhouse", "acme"): {"gh-a"}})  # only A still listed
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    def count_jobs() -> int:
        con = sqlite3.connect(idx)
        n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        con.close()
        return n

    before = count_jobs()

    for now in (_DAY0, _DAY7):  # two weekly runs -> B's streak reaches the flip threshold
        anyio.run(
            lambda now=now: reconcile_liveness_tier(
                liv, idx, fetch_board=fetch_board, fetch_detail=fetch_detail, now=lambda: now
            )
        )

    # B flipped, A stayed active; COUNT(*) is untouched (never a hard delete -> row_floor-safe).
    assert _liveness_status(idx, "gh-b") == ("expired", "dead_link")
    assert _liveness_status(idx, "gh-a") == ("active", None)
    assert count_jobs() == before

    con = sqlite3.connect(idx)
    con.row_factory = sqlite3.Row
    ids = {r["id"] for r in search_rows(con, SearchQuery())}
    assert ids == {"gh-a"}  # status='active' filter (query.py) excludes the dead row

    new_ids = {r["id"] for r in whats_new_rows(con, SearchQuery(), since_iso="2020-01-01")}
    assert new_ids == {"gh-a"}
    con.close()


# --- concurrency: bounded board pool, no deadlock -------------------------------------------


def test_reconcile_honors_concurrency_cap_and_completes(tmp_path):
    n_boards = 40
    jobs = [
        _job_row(f"b{i}-job", source="greenhouse", board_token=f"board{i}") for i in range(n_boards)
    ]
    idx = _build_index(tmp_path, jobs)
    liv = str(tmp_path / "liveness.sqlite")
    cap = 5

    state = {"inflight": 0, "max_inflight": 0}
    lock = anyio.Lock()

    async def fetch_board(source: str, token: str) -> set[str]:
        async with lock:
            state["inflight"] += 1
            state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        await anyio.sleep(0.01)  # hold the slot long enough for overlap to actually occur
        async with lock:
            state["inflight"] -= 1
        idx_num = token.replace("board", "")
        return {f"b{idx_num}-job"}  # every board's job stays present -- no flips expected here

    async def fetch_detail(ref):
        return None

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv,
            idx,
            fetch_board=fetch_board,
            fetch_detail=fetch_detail,
            now=lambda: _DAY0,
            concurrency=cap,
        )
    )
    assert stats["boards_fetched"] == n_boards
    assert stats["flipped_dead"] == 0
    assert 1 <= state["max_inflight"] <= cap  # bounded by the cap, and real overlap happened
    assert stats["checked"] == n_boards


def test_failed_board_fetch_leaves_its_rows_untouched(tmp_path):
    # A transient board-fetch failure (fetch_board -> None) must NOT be mistaken for "every
    # posting on this board disappeared" -- the row stays exactly as it was, still eligible.
    idx = _build_index(tmp_path, [_job_row("gh-1")])
    liv = str(tmp_path / "liveness.sqlite")
    failing_board = _make_fetch_board({("greenhouse", "acme"): None})
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv, idx, fetch_board=failing_board, fetch_detail=fetch_detail, now=lambda: _DAY0
        )
    )
    assert stats["boards_failed"] == 1
    assert stats["checked"] == 0
    status, reason = _liveness_status(idx, "gh-1")
    assert status == "active" and reason is None
    assert _sidecar_row(liv, "gh-1") is None  # never recorded -- stays eligible next run


# --- source-list sync: CONFIRM_VIA_DETAIL_SOURCES == build_index._TIER3_DETAIL_SOURCES ---------


# Sources DELIBERATELY drained (Tier-3 JD recovery) but NOT wired into the liveness confirm path:
# their per-posting fetch_detail gone-signal is only SOFT (not a hard 404/410), so a returned None
# must never be allowed to expire a live row -- safe in the drain (a None just fails to recover a
# JD, retried up to RETRY_CAP) but WRONG in the confirm path. Their liveness/freshness is handled
# elsewhere (breezy: the deterministic bulk id-set relist, freshness.DETERMINISTIC_SOURCES; taleo:
# the freshness search-index bulk-relist confirm + its two-factor soft-404, freshness._BULK_RELIST_
# CONFIRM_SOURCES -- so taleo is drain-wired for JD recovery but its liveness stays on that path).
_DRAIN_ONLY_SOURCES = {"breezy", "apicapture", "taleo"}


def test_confirm_and_tier3_source_lists_are_in_sync():
    # The two lists are manually kept in sync by design (see liveness.py's comment near the
    # CONFIRM_VIA_DETAIL_SOURCES definition): both enumerate "sources with a working fetch_detail",
    # the same underlying fact from two call sites. They must stay identical as sets -- EXCEPT for
    # the deliberately drain-only sources (soft gone-signal, confirmed via a different path) -- so a
    # source can never be drained by one pass but silently un-covered for liveness altogether.
    from scripts.build_index import _TIER3_DETAIL_SOURCES

    # Everything in the confirm set must be drainable...
    assert set(CONFIRM_VIA_DETAIL_SOURCES) <= set(_TIER3_DETAIL_SOURCES)
    # ...and the only Tier-3 sources absent from the confirm set are the known drain-only ones.
    assert set(_TIER3_DETAIL_SOURCES) - set(CONFIRM_VIA_DETAIL_SOURCES) == _DRAIN_ONLY_SOURCES


def test_newly_wired_detail_providers_present_in_both_lists():
    # R3: the four already-built detail providers whose fetch_detail is verified (by their own
    # passing "alive returns text" tests: test_themuse/test_adp/test_avature/test_taleobe) are now
    # wired into BOTH the Tier-3 drain source list and the liveness confirm source set. taleo is
    # DRAIN-wired (its jobdetail.ftl fetch_detail works -- the old "JS-blocked" note was stale) but
    # stays OUT of the liveness confirm set: its two-factor soft-404 confirms via the freshness
    # bulk-relist path, so it's drain-only here (see _DRAIN_ONLY_SOURCES above).
    from scripts.build_index import _TIER3_DETAIL_SOURCES

    newly_wired = {"themuse", "adp", "avature", "taleobe"}
    assert newly_wired <= set(CONFIRM_VIA_DETAIL_SOURCES)
    assert newly_wired <= set(_TIER3_DETAIL_SOURCES)
    assert "taleo" in set(_TIER3_DETAIL_SOURCES)  # drain-wired for JD recovery
    assert "taleo" not in set(CONFIRM_VIA_DETAIL_SOURCES)  # liveness via freshness bulk-relist


def test_unresolvable_token_rows_are_skipped_not_flipped(tmp_path):
    # A row with no board_token AND no resolvable company_key must be left alone (never
    # misclassified as dead purely because we can't figure out which board to check).
    idx = _build_index(
        tmp_path, [{"id": "gh-1", "source": "greenhouse", "board_token": None, "company_key": None}]
    )
    liv = str(tmp_path / "liveness.sqlite")
    fetch_detail = _make_fetch_detail(alive_ids=set(), calls=[])

    stats = anyio.run(
        lambda: reconcile_liveness_tier(
            liv,
            idx,
            fetch_board=_make_fetch_board({}),
            fetch_detail=fetch_detail,
            now=lambda: _DAY0,
        )
    )
    assert stats["unresolved"] == 1
    assert stats["checked"] == 0
    status, _ = _liveness_status(idx, "gh-1")
    assert status == "active"
