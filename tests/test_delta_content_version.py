"""A-2: per-posting content-version fold into the delta-crawl fingerprint (edit-safety).

The id-set-hash delta skip is MEMBERSHIP-only: it carries a board forward whenever its id-set is
unchanged, so it is BLIND to an in-place EDIT (a live posting's JD/title/salary rewritten, same id).
A-2 folds a stable per-posting content-version into the fingerprint (behind the DARK sub-flag
``ERGON_DELTA_CONTENT_VERSION``) so an edit flips the hash -> the board re-crawls -> fresh row.

This file proves, on the REAL build/sweep code paths:

  * FLAG-OFF PARITY   -- the composite fingerprint is byte-identical to the historical id-only hash,
                         so a version bump is (correctly) invisible when the flag is off (today).
  * FLAG-ON EDIT-SAFETY -- an edit (updated_at bumped) with an UNCHANGED id-set flips BOTH the sweep
                         fingerprint AND the crawl stamp, so the board is NOT skipped and re-crawls
                         to the edited content -- while an UNCHANGED posting still skips (savings).
  * CROSS-SIDE AGREEMENT -- the sweep's composite and the crawl stamp's composite for the SAME raws
                         hash identically (guarding the two sides against drifting).
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
from ergon_tracker.index.freshness import (  # noqa: E402
    content_fingerprint_ids,
    content_version_enabled,
    idset_hash,
    sweep_boards,
)
from ergon_tracker.index.scheduler import BoardState  # noqa: E402
from ergon_tracker.models import JobPosting, RawJob  # noqa: E402
from ergon_tracker.providers.base import BaseProvider  # noqa: E402
from ergon_tracker.providers.greenhouse import GreenhouseProvider  # noqa: E402
from ergon_tracker.providers.recruitee import RecruiteeProvider  # noqa: E402

_TOKEN = "acme"


# ======================================================================================
# Unit: the flag reader
# ======================================================================================


def test_content_version_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("ERGON_DELTA_CONTENT_VERSION", raising=False)
    assert content_version_enabled() is False
    monkeypatch.setenv("ERGON_DELTA_CONTENT_VERSION", "1")
    assert content_version_enabled() is True
    monkeypatch.setenv("ERGON_DELTA_CONTENT_VERSION", "")  # blank => OFF (ships dark)
    assert content_version_enabled() is False
    monkeypatch.setenv("ERGON_DELTA_CONTENT_VERSION", "0")  # only strict "1" enables
    assert content_version_enabled() is False


# ======================================================================================
# Unit: the SAFE provider content_version overrides (greenhouse / recruitee)
# ======================================================================================


def _gh_raw(sid: str, updated_at):
    return RawJob(
        source="greenhouse",
        source_job_id=sid,
        company="Acme",
        payload={"updated_at": updated_at, "title": "Eng"},
    )


def _rc_raw(sid: str, updated_at):
    return RawJob(
        source="recruitee",
        source_job_id=sid,
        company="Acme",
        payload={"updated_at": updated_at, "title": "Eng"},
    )


def test_greenhouse_content_version_is_raw_updated_at():
    p = GreenhouseProvider()
    assert p.content_version(_gh_raw("1", "2026-07-20T10:00:00Z")) == "2026-07-20T10:00:00Z"
    # A bumped updated_at yields a DIFFERENT token (an edit is visible).
    assert p.content_version(_gh_raw("1", "2026-07-21T09:00:00Z")) != p.content_version(
        _gh_raw("1", "2026-07-20T10:00:00Z")
    )


def test_greenhouse_content_version_none_when_absent():
    p = GreenhouseProvider()
    assert p.content_version(_gh_raw("1", None)) is None
    assert p.content_version(RawJob(source="greenhouse", source_job_id="1", company="Acme")) is None


def test_recruitee_content_version_is_raw_updated_at():
    p = RecruiteeProvider()
    assert p.content_version(_rc_raw("1", "2026-07-20T10:00:00Z")) == "2026-07-20T10:00:00Z"
    assert p.content_version(_rc_raw("1", None)) is None


def _br_raw(sid: str, lastupdated):
    return RawJob(
        source="brassring",
        source_job_id=sid,
        company="Acme",
        payload={"lastupdated": lastupdated, "reqid": sid},
    )


def test_brassring_content_version_is_raw_lastupdated():
    from ergon_tracker.providers.brassring import BrassRingProvider

    p = BrassRingProvider()
    # Raw served day-granular date; a cross-day edit yields a DIFFERENT token.
    assert p.content_version(_br_raw("1", "20-Jul-2026")) == "20-Jul-2026"
    assert p.content_version(_br_raw("1", "21-Jul-2026")) != p.content_version(
        _br_raw("1", "20-Jul-2026")
    )
    # Absent field -> None (falls back to id-only, no regression).
    assert p.content_version(_br_raw("1", None)) is None
    assert p.content_version(RawJob(source="brassring", source_job_id="1", company="Acme")) is None


def test_base_provider_content_version_is_none():
    # The default: a provider that does NOT override stays id-only (edit-blind, no regression).
    assert BaseProvider().content_version(_gh_raw("1", "x")) is None


# ======================================================================================
# Unit: content_fingerprint_ids -- the ONE shared composite basis
# ======================================================================================


class _VProvider(BaseProvider):
    """A greenhouse-shaped fake whose postings carry an ``updated_at`` and a content_version
    override -- exercises the fold. ``postings`` = list of (id, title, version)."""

    name = "greenhouse"
    validator_covers_body = False

    def __init__(self, postings):
        self._postings = postings
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
                payload={"title": t, "updated_at": v},
            )
            for i, t, v in self._postings
        ]

    def normalize(self, raw):
        return JobPosting.create(
            source="greenhouse",
            source_job_id=raw.source_job_id,
            company="Acme Corp",
            title=raw.payload["title"],
        )

    def content_version(self, raw):
        v = raw.payload.get("updated_at")
        return str(v) if v else None


def _raws(postings):
    return [
        RawJob(
            source="greenhouse",
            source_job_id=i,
            company="Acme",
            payload={"title": t, "updated_at": v},
        )
        for i, t, v in postings
    ]


def test_fold_off_is_byte_identical_to_id_only():
    prov = _VProvider([])
    raws = _raws([("1", "A", "v1"), ("2", "B", "v1")])
    # Flag OFF: composite == the bare id-set == the historical fingerprint basis, to the byte.
    assert content_fingerprint_ids(raws, prov, fold_version=False) == {"1", "2"}
    assert idset_hash(content_fingerprint_ids(raws, prov, fold_version=False)) == idset_hash(
        {"1", "2"}
    )


def test_fold_on_with_override_folds_version_and_flips_on_edit():
    prov = _VProvider([])
    base = _raws([("1", "A", "v1"), ("2", "B", "v1")])
    edited = _raws([("1", "A2", "v2"), ("2", "B", "v1")])  # SAME ids {1,2}, posting 1 edited (v2)
    h_base = idset_hash(content_fingerprint_ids(base, prov, fold_version=True))
    h_same = idset_hash(
        content_fingerprint_ids(
            _raws([("1", "A", "v1"), ("2", "B", "v1")]), prov, fold_version=True
        )
    )
    h_edit = idset_hash(content_fingerprint_ids(edited, prov, fold_version=True))
    assert h_base == h_same  # unchanged content -> unchanged hash (skip preserved)
    assert h_edit != h_base  # an in-place edit (same id-set) FLIPS the hash -> re-crawl
    # And folding-on differs from id-only whenever a version is present (that's the whole point).
    assert h_base != idset_hash({"1", "2"})


def test_fold_on_without_override_is_still_id_only():
    # Flag ON but the provider does NOT override content_version => all-bare-ids => id-only hash.
    raws = _raws([("1", "A", "v1"), ("2", "B", "v1")])
    assert content_fingerprint_ids(raws, BaseProvider(), fold_version=True) == {"1", "2"}


def test_fold_on_none_provider_is_id_only():
    raws = _raws([("1", "A", "v1")])
    assert content_fingerprint_ids(raws, None, fold_version=True) == {"1"}


def test_fold_mixed_presence_is_safe():
    # A board where only SOME postings carry a version: versioned ones fold, the rest stay bare id.
    prov = _VProvider([])
    raws = _raws([("1", "A", "v1"), ("2", "B", None)])
    composite = content_fingerprint_ids(raws, prov, fold_version=True)
    assert "2" in composite  # no version -> bare id
    assert "2\x1fNone" not in composite
    assert any(tok.startswith("1\x1f") for tok in composite)  # versioned -> folded


# ======================================================================================
# Cross-side agreement: the sweep's fingerprint == the crawl stamp's, for the SAME raws
# ======================================================================================


def _build_prior_index(tmp_path, prov, *, build_id="prior"):
    """One crawl+build of a greenhouse board with ``prov`` -> (stamp_hash, prior_db). ``stamp_hash``
    is the composite fingerprint the crawl STAMPED for the board (state.idset_hash); prior_db is a
    real index.sqlite. A fresh 'today' BoardState is later seeded with ``stamp_hash`` so the board
    is still 'due' (reusing this crawl's own state would mark it crawled-today, not due)."""
    fresh = tmp_path / build_id / "fresh.sqlite"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    states = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh, build_id)
    keys = set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    db = tmp_path / build_id / "index.sqlite"
    build_index_from_fresh_db(fresh, db, build_id=build_id, crawled_keys=keys)
    return states["greenhouse|acme"].idset_hash, db


def _today_states(stamp_hash):
    """A fresh (due) 'today' BoardState seeded with the prior crawl's stamped fingerprint."""
    return {
        "greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN, idset_hash=stamp_hash)
    }


def _sweep_hash(prior_db, prov, monkeypatch):
    """Run the REAL sweep against prior_db with ``prov`` and return the board's published
    idset_hash (what freshness-sweep.yml would write to the sidecar for the build to compare)."""
    import ergon_tracker.index.freshness as freshness

    monkeypatch.setattr(freshness, "get_provider", lambda name: prov)
    deltas: dict = {}
    con = sqlite3.connect(prior_db)
    anyio.run(
        lambda: sweep_boards(
            [("greenhouse", _TOKEN)],
            con,
            fetcher=object(),
            board_deltas=deltas,
            now=lambda: "2026-07-20T00:00:00+00:00",
        )
    )
    con.close()
    return deltas[("greenhouse", _TOKEN)].idset_hash


def _reg_for(prov):
    class _Reg:
        def all(self):
            return {"acme": {"ats": "greenhouse", "token": _TOKEN, "domain": "acme.com"}}

    return _Reg


def _patch(monkeypatch, prov):
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    monkeypatch.setattr(store_mod, "SeedRegistry", _reg_for(prov))
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)


def test_cross_side_sweep_and_crawl_stamp_agree(monkeypatch, tmp_path):
    """The sweep's published fingerprint for a board == the crawl stamp that board records, for the
    identical raws -- if the two sides built the composite differently, skips would never fire."""
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    monkeypatch.setenv("ERGON_DELTA_CONTENT_VERSION", "1")
    prov = _VProvider([("1", "A", "v1"), ("2", "B", "v1")])
    _patch(monkeypatch, prov)

    crawl_stamp, prior_db = _build_prior_index(tmp_path, prov)  # what the build stamped
    sweep_hash = _sweep_hash(prior_db, prov, monkeypatch)  # what the sweep publishes
    assert crawl_stamp is not None
    assert crawl_stamp == sweep_hash


# ======================================================================================
# End-to-end EDIT-SAFETY (flag on) + skip-preserved mirror + flag-off parity
# ======================================================================================


def _job_titles(db: Path) -> set[str]:
    con = connect(db, read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT title FROM jobs")}
    finally:
        con.close()


def _write_sidecar(path: Path, h: str) -> None:
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
            ("greenhouse", _TOKEN, "[]", h, "2026-07-20T00:00:00+00:00"),
        )
        con.commit()
    finally:
        con.close()


def _run_today(tmp_path, states, prior_db, prov, *, name):
    fresh = tmp_path / name / "fresh.sqlite"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    prov.fetch_calls = 0
    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh, "today", 0, False, prior_db)
    keys = set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    db = tmp_path / name / "index.sqlite"
    build_index_from_fresh_db(fresh, db, build_id="today", prev_db=prior_db, crawled_keys=keys)
    return outcome, db


def test_flag_on_edit_forces_recrawl_and_lands_fresh_content(monkeypatch, tmp_path):
    """FLAG ON: a posting edited (updated_at bumped) with an UNCHANGED id-set flips the fingerprint
    -> the board is NOT skipped -> it re-crawls -> the EDITED title lands (not a stale carry)."""
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    monkeypatch.setenv("ERGON_DELTA_CONTENT_VERSION", "1")

    prov = _VProvider([("1", "Staff Engineer", "v1"), ("2", "EM", "v1")])
    _patch(monkeypatch, prov)
    stamp, prior_db = _build_prior_index(tmp_path, prov)

    # An edit "today": SAME id-set {1,2}, posting 1's title + updated_at changed.
    edited = _VProvider([("1", "PRINCIPAL ENGINEER", "v2"), ("2", "EM", "v1")])
    _patch(monkeypatch, edited)
    # The sweep (fresh view) publishes the EDITED composite; the build's state still holds the v1
    # stamp from the prior crawl -> they DIFFER -> no skip.
    sweep_hash = _sweep_hash(prior_db, edited, monkeypatch)
    _patch(monkeypatch, edited)  # _sweep_hash repatched get_provider on the freshness module
    _write_sidecar(tmp_path / "today" / "index-freshness.sqlite", sweep_hash)

    out, db = _run_today(tmp_path, _today_states(stamp), prior_db, edited, name="today")
    assert edited.fetch_calls == 1  # NOT skipped -- the edit forced a re-crawl
    assert out["greenhouse|acme"]["not_modified"] is False
    assert "PRINCIPAL ENGINEER" in _job_titles(db)  # the EDITED content landed, not the stale one


def test_flag_on_unchanged_still_skips(monkeypatch, tmp_path):
    """FLAG ON mirror: an UNCHANGED board (same ids AND same versions) still skips -> savings
    preserved (the fold only re-crawls on a real edit, never on every run)."""
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    monkeypatch.setenv("ERGON_DELTA_CONTENT_VERSION", "1")

    prov = _VProvider([("1", "Staff Engineer", "v1"), ("2", "EM", "v1")])
    _patch(monkeypatch, prov)
    stamp, prior_db = _build_prior_index(tmp_path, prov)

    same = _VProvider([("1", "Staff Engineer", "v1"), ("2", "EM", "v1")])
    sweep_hash = _sweep_hash(prior_db, same, monkeypatch)
    _patch(monkeypatch, same)
    _write_sidecar(tmp_path / "today" / "index-freshness.sqlite", sweep_hash)

    out, _db = _run_today(tmp_path, _today_states(stamp), prior_db, same, name="today")
    assert same.fetch_calls == 0  # SKIPPED -- membership AND content unchanged
    assert out["greenhouse|acme"]["not_modified"] is True


def test_flag_off_edit_is_blind_and_skips(monkeypatch, tmp_path):
    """FLAG OFF (today's prod, byte-identical): the SAME edit as the edit-safety test produces the
    id-only fingerprint on both sides, so an updated_at bump is invisible -> the board still SKIPS.
    Proves flag-off ships dark: version folding changes nothing when the sub-flag is off."""
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    monkeypatch.delenv("ERGON_DELTA_CONTENT_VERSION", raising=False)  # OFF

    prov = _VProvider([("1", "Staff Engineer", "v1"), ("2", "EM", "v1")])
    _patch(monkeypatch, prov)
    stamp, prior_db = _build_prior_index(tmp_path, prov)

    edited = _VProvider([("1", "PRINCIPAL ENGINEER", "v2"), ("2", "EM", "v1")])
    sweep_hash = _sweep_hash(prior_db, edited, monkeypatch)  # flag off -> id-only hash
    _patch(monkeypatch, edited)
    # Cross-check: flag-off sweep hash equals the pure id-set hash (edit-blind by construction).
    assert sweep_hash == idset_hash({"1", "2"})
    _write_sidecar(tmp_path / "today" / "index-freshness.sqlite", sweep_hash)

    out, _db = _run_today(tmp_path, _today_states(stamp), prior_db, edited, name="today")
    assert edited.fetch_calls == 0  # SKIPPED despite the edit -- exactly today's behavior
    assert out["greenhouse|acme"]["not_modified"] is True
