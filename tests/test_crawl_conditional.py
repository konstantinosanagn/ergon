"""Crawler conditional pre-check: a 304 carries forward without calling provider.fetch."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.http import ConditionalResult  # noqa: E402
from ergon_tracker.index.freshness import idset_hash  # noqa: E402
from ergon_tracker.index.scheduler import BoardState  # noqa: E402


class _FakeReg:
    def all(self):
        return {"co": {"ats": "greenhouse", "token": "stripe", "domain": "stripe.com"}}


class _Provider304:
    name = "greenhouse"

    def conditional_url(self, token):
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    async def fetch(self, *a):  # must NOT run when the board is unchanged
        raise AssertionError("provider.fetch called despite 304")

    def normalize(self, raw):  # pragma: no cover - not reached on 304
        raise AssertionError


class _Fetcher304:
    def __init__(self, *args, **kwargs):  # tolerate AsyncFetcher(timeout=, retries=) kwargs
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def conditional_get(self, url, *, etag=None, last_modified=None):
        return ConditionalResult(
            not_modified=True, status_code=304, etag=etag, last_modified=last_modified
        )


def test_crawl_due_304_carries_forward(monkeypatch, tmp_path):
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod
    from ergon_tracker.index.db import connect

    monkeypatch.setattr(store_mod, "SeedRegistry", _FakeReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _Provider304())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(http_mod, "AsyncFetcher", _Fetcher304)

    # Pre-seed state with a stored validator + a past due date so the board is crawled.
    bs = BoardState(provider="greenhouse", token="stripe", etag='W/"abc"', next_due="2000-01-01")
    states = {bs.key: bs}
    fresh_db_path = tmp_path / "fresh.sqlite"

    outcome, _cursor = anyio.run(bi._crawl_due, 10, states, fresh_db_path, "b1")

    assert (
        connect(fresh_db_path, read_only=True).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        == 0
    )  # nothing re-downloaded
    assert outcome[bs.key]["not_modified"] is True
    assert outcome[bs.key]["companies"] == set()  # empty -> prev jobs carry forward in merge


class _Provider200:
    """Returns a 200 with a body; the crawler must parse it WITHOUT calling fetch()."""

    name = "greenhouse"

    def conditional_url(self, token):
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    async def fetch(self, *a):
        raise AssertionError("fetch called despite a reusable 200 body")

    def raws_from_body(self, token, body):
        import json

        from ergon_tracker.models import RawJob

        data = json.loads(body)
        return [
            RawJob(
                source="greenhouse",
                source_job_id=str(j["id"]),
                company=token,
                token=token,
                url=None,
                payload=j,
            )
            for j in data["jobs"]
        ]

    def normalize(self, raw):
        from ergon_tracker.models import JobPosting

        return JobPosting.create(
            source="greenhouse",
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=raw.payload["title"],
        )


class _Fetcher200:
    _BODY = b'{"jobs": [{"id": 1, "title": "Engineer"}]}'

    def __init__(self, *args, **kwargs):  # tolerate AsyncFetcher(timeout=, retries=) kwargs
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def conditional_get(self, url, *, etag=None, last_modified=None):
        return ConditionalResult(
            not_modified=False, status_code=200, etag='W/"new"', last_modified=None, body=self._BODY
        )


def test_crawl_due_200_reuses_body_without_refetch(monkeypatch, tmp_path):
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod
    from ergon_tracker.index.db import connect

    monkeypatch.setattr(store_mod, "SeedRegistry", _FakeReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _Provider200())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(http_mod, "AsyncFetcher", _Fetcher200)

    bs = BoardState(provider="greenhouse", token="stripe", etag='W/"old"', next_due="2000-01-01")
    states = {bs.key: bs}
    fresh_db_path = tmp_path / "fresh.sqlite"
    outcome, _cursor = anyio.run(bi._crawl_due, 10, states, fresh_db_path, "b1")

    rows = connect(fresh_db_path, read_only=True).execute("SELECT title FROM jobs").fetchall()
    assert len(rows) == 1 and rows[0][0] == "Engineer"  # parsed from the 200 body, streamed to DB
    assert outcome[bs.key]["not_modified"] is False
    assert states[bs.key].etag == 'W/"new"'  # validator refreshed for next run


def test_registry_window_rotates_and_wraps(monkeypatch):
    import ergon_tracker.registry.store as store_mod

    class _Reg:
        def all(self):  # 5 crawlable boards: t0..t4
            return {f"c{i}": {"ats": "greenhouse", "token": f"t{i}"} for i in range(5)}

    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)

    # window smaller than total -> rotating slice + advancing cursor
    win, nxt = bi._registry_window(0, 2)
    assert [e["token"] for _, e in win] == ["t0", "t1"] and nxt == 2
    win, nxt = bi._registry_window(2, 2)
    assert [e["token"] for _, e in win] == ["t2", "t3"] and nxt == 4
    # wraparound: cursor 4, window 2 -> t4, t0 ; next cursor wraps to 1
    win, nxt = bi._registry_window(4, 2)
    assert [e["token"] for _, e in win] == ["t4", "t0"] and nxt == 1
    # window >= total -> everything, cursor resets to 0 (full pass)
    win, nxt = bi._registry_window(0, 99)
    assert len(win) == 5 and nxt == 0


def test_registry_window_skips_uncrawlable(monkeypatch):
    import ergon_tracker.registry.store as store_mod

    class _Reg:
        def all(self):
            return {
                "a": {"ats": "greenhouse", "token": "t1"},
                "b": {"ats": "greenhouse"},  # no token -> skipped
                "c": {"token": "t2"},  # no ats -> skipped
                "d": {"ats": "lever", "token": "t3"},
            }

    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    win, nxt = bi._registry_window(0, 10)
    assert {e["token"] for _, e in win} == {"t1", "t3"} and nxt == 0


def test_registry_window_caps_giant_limit_and_resumes(monkeypatch):
    # A giant --limit-companies must NOT crawl the whole registry as one window: the per-run window
    # is capped and the cursor advances, so a killed run resumes from the next slice instead of
    # re-doing one 58k-style window forever (the CI-timeout failure this fixes).
    import ergon_tracker.registry.store as store_mod

    class _Reg:
        def all(self):  # 10 crawlable boards: t0..t9
            return {f"c{i}": {"ats": "greenhouse", "token": f"t{i}"} for i in range(10)}

    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)

    win, nxt = bi._registry_window(0, 999999, max_window=4)
    assert (
        len(win) == 4 and nxt == 4
    )  # bounded to the cap, cursor advanced (NOT the whole registry)

    # Resume: feeding the returned cursor back continues to the next, disjoint slice.
    win2, nxt2 = bi._registry_window(nxt, 999999, max_window=4)
    assert len(win2) == 4 and nxt2 == 8
    assert {e["token"] for _, e in win}.isdisjoint({e["token"] for _, e in win2})

    # Over ceil(total/window) runs the whole registry is covered.
    seen, cursor = set(), 0
    for _ in range(3):  # ceil(10 / 4) == 3
        w, cursor = bi._registry_window(cursor, 999999, max_window=4)
        seen.update(e["token"] for _, e in w)
    assert seen == {f"t{i}" for i in range(10)}


def test_registry_window_cap_from_env(monkeypatch):
    import ergon_tracker.registry.store as store_mod

    class _Reg:
        def all(self):
            return {f"c{i}": {"ats": "greenhouse", "token": f"t{i}"} for i in range(10)}

    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setenv("ERGON_CRAWL_MAX_WINDOW", "3")  # cap sourced from env when not passed
    win, nxt = bi._registry_window(0, 999999)
    assert len(win) == 3 and nxt == 3


# --------------------------------------------------------------------------------------------------
# Delta-crawl EDIT-SAFETY: a ``validator_covers_body`` provider must NOT be id-set-skipped, so its
# edit-safe conditional-GET runs even when the id-set (and thus the freshness sidecar hash) is
# unchanged. Contrast: a non-body-validator deterministic provider still id-set-skips (edit-blind).
# --------------------------------------------------------------------------------------------------

_LEVER_URL = "https://api.lever.co/v0/postings/acme?mode=json"


def _write_delta_sidecar(fresh_db_path: Path, source: str, token: str, h: str) -> None:
    """Write the freshness ``board_deltas`` sidecar the crawl reads for the id-set-hash skip."""
    path = fresh_db_path.parent / "index-freshness.sqlite"
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


class _LeverBodyEdited:
    """A ``validator_covers_body`` provider (lever): conditional_url validates the JD-bearing body,
    so an in-place JD edit flips the ETag -> a 200 re-processes the edited body (id-set unchanged)."""

    name = "lever"
    validator_covers_body = True

    def conditional_url(self, token):
        return _LEVER_URL

    def list_host(self, token):
        return None

    async def fetch(self, *a):  # the 200 body is reusable -> fetch must never run
        raise AssertionError("fetch called despite a reusable 200 body")

    def raws_from_body(self, token, body):
        import json

        from ergon_tracker.models import RawJob

        return [
            RawJob(
                source="lever",
                source_job_id=str(j["id"]),
                company=token,
                token=token,
                url=None,
                payload=j,
            )
            for j in json.loads(body)
        ]

    def normalize(self, raw):
        from ergon_tracker.models import JobPosting

        return JobPosting.create(
            source="lever",
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=raw.payload["title"],
            description_text=raw.payload.get("descriptionPlain"),
        )


class _LeverReg:
    def all(self):
        return {"acme": {"ats": "lever", "token": "acme", "domain": "acme.com"}}


def _fetcher_returning(result: ConditionalResult, *, calls: list):
    class _F:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def conditional_get(self, url, *, etag=None, last_modified=None):
            calls.append(url)
            return result

    return _F


def test_delta_body_validator_not_idset_skipped_reprocesses_edit(monkeypatch, tmp_path):
    """Edit-safety RESTORED: with the flag on and a MATCHING sidecar+stamp (which would id-set-skip
    a normal deterministic board), a ``validator_covers_body`` board instead falls through to the
    conditional-GET; a 200 re-processes the EDITED body and the fresh, non-stale row is streamed."""
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod
    from ergon_tracker.index.db import connect

    edited_body = b'[{"id": 1, "title": "Staff Engineer", "descriptionPlain": "EDITED JD body"}]'
    cget_calls: list = []
    monkeypatch.setattr(store_mod, "SeedRegistry", _LeverReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _LeverBodyEdited())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(
        http_mod,
        "AsyncFetcher",
        _fetcher_returning(
            ConditionalResult(
                not_modified=False,
                status_code=200,
                etag='W/"new"',
                last_modified=None,
                body=edited_body,
            ),
            calls=cget_calls,
        ),
    )
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")

    # id-set is UNCHANGED ({"1"}), so the sidecar hash MATCHES the stamped fingerprint: a normal
    # deterministic board WOULD be skipped here. The body-validator exception must override that.
    fingerprint = idset_hash({"1"})
    fresh = tmp_path / "fresh.sqlite"
    _write_delta_sidecar(fresh, "lever", "acme", fingerprint)
    bs = BoardState(
        provider="lever",
        token="acme",
        etag='W/"old"',
        next_due="2000-01-01",
        idset_hash=fingerprint,
    )
    states = {bs.key: bs}

    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh, "b1")

    assert cget_calls == [_LEVER_URL]  # fell through to the edit-safe conditional-GET (NOT skipped)
    assert outcome[bs.key]["not_modified"] is False  # a 200 -> re-processed, not carried forward
    rows = connect(fresh, read_only=True).execute("SELECT title, snippet FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Staff Engineer"
    assert rows[0][1] == "EDITED JD body"  # the EDITED body landed (not a stale carry-forward)
    assert states[bs.key].etag == 'W/"new"'  # validator refreshed


class _GreenhouseDeterministic:
    """A deterministic provider WITHOUT a body validator (greenhouse's conditional_url is the light,
    content-less board response). It must still be id-set-skipped -- the conditional-GET never runs.
    """

    name = "greenhouse"

    def conditional_url(self, token):  # pragma: no cover - never reached (skip fires first)
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

    def list_host(self, token):
        return None

    async def fetch(self, *a):  # pragma: no cover
        raise AssertionError("fetch called despite the id-set skip")

    def normalize(self, raw):  # pragma: no cover
        raise AssertionError


class _GreenhouseReg:
    def all(self):
        return {"acme": {"ats": "greenhouse", "token": "acme", "domain": "acme.com"}}


def test_delta_non_body_validator_still_idset_skips(monkeypatch, tmp_path):
    """CONTRAST: the SAME matching sidecar+stamp on a non-body-validator deterministic provider
    still id-set-skips (edit-blind by design -- catching in-place edits there is A-2's job). The
    conditional-GET must NOT run (the skip returns first)."""
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod
    from ergon_tracker.index.db import connect

    cget_calls: list = []
    monkeypatch.setattr(store_mod, "SeedRegistry", _GreenhouseReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _GreenhouseDeterministic())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(
        http_mod,
        "AsyncFetcher",
        _fetcher_returning(
            ConditionalResult(not_modified=True, status_code=304, etag=None, last_modified=None),
            calls=cget_calls,
        ),
    )
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")

    fingerprint = idset_hash({"1"})
    fresh = tmp_path / "fresh.sqlite"
    _write_delta_sidecar(fresh, "greenhouse", "acme", fingerprint)
    bs = BoardState(
        provider="greenhouse",
        token="acme",
        etag='W/"old"',
        next_due="2000-01-01",
        idset_hash=fingerprint,
    )
    states = {bs.key: bs}

    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh, "b1")

    assert cget_calls == []  # id-set skip fired FIRST -- the conditional-GET never ran
    assert outcome[bs.key]["not_modified"] is True  # carried forward via the membership-only skip
    assert connect(fresh, read_only=True).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_delta_body_validator_304_still_carries_forward(monkeypatch, tmp_path):
    """A ``validator_covers_body`` board whose body did NOT change: it falls through to the
    conditional-GET (proven by the call), the 304 carries prior rows forward, and fetch never
    runs -- the edit-safe path is strictly better, never worse, than the id-set skip."""
    import ergon_tracker.http as http_mod
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod
    from ergon_tracker.index.db import connect

    cget_calls: list = []
    monkeypatch.setattr(store_mod, "SeedRegistry", _LeverReg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: _LeverBodyEdited())
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(
        http_mod,
        "AsyncFetcher",
        _fetcher_returning(
            ConditionalResult(
                not_modified=True, status_code=304, etag='W/"old"', last_modified=None
            ),
            calls=cget_calls,
        ),
    )
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")

    fingerprint = idset_hash({"1"})
    fresh = tmp_path / "fresh.sqlite"
    _write_delta_sidecar(fresh, "lever", "acme", fingerprint)
    bs = BoardState(
        provider="lever",
        token="acme",
        etag='W/"old"',
        next_due="2000-01-01",
        idset_hash=fingerprint,
    )
    states = {bs.key: bs}

    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh, "b1")

    assert cget_calls == [_LEVER_URL]  # fell through to the conditional-GET (not the id-set skip)
    assert outcome[bs.key]["not_modified"] is True  # 304 -> carry forward
    assert outcome[bs.key]["companies"] == set()  # nothing streamed; prior rows carry forward
    assert connect(fresh, read_only=True).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
