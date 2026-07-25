"""Item 3 parity: an ENRICH_VERSION bump re-enriches the carried-forward backlog FROM THE STORED JD.

Extraction only runs on freshly-crawled rows; the ~90% of rows a build carries forward reuse their
prior enrichment. So improving an extractor never reached them. Item 3 folds ``ENRICH_VERSION`` into
``enrich_hash`` and adds a version-gated re-enrich pass (``build.reenrich_carried_forward``) that
replays the full JD sidecar (Item 2) over the carried backlog — no re-crawl.

The PARITY GATE, both directions:
  1. Version UNCHANGED -> the re-enrich pass is a strict no-op; the carried ``jobs`` rows are
     byte-identical to a plain carry-forward (the reuse-skip still fires). Proven twice: the unit
     gate returns 0, and the built rows equal the no-pass build.
  2. Version BUMPED -> every carried row that HAS a stored JD is re-enriched from that JD, and the
     result equals a full COLD re-enrich of the same JD (stale prior enrichment is discarded). A
     carried row with NO stored JD retains its prior enrichment (documented, non-fatal).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ergon_tracker.index.build as build_mod  # noqa: E402
import ergon_tracker.index.mapping as mapping_mod  # noqa: E402
from ergon_tracker.enrich import enrich_in_place  # noqa: E402
from ergon_tracker.index import jd_store  # noqa: E402
from ergon_tracker.index.build import (  # noqa: E402
    append_jobs,
    build_index_from_fresh_db,
    reenrich_carried_forward,
)
from ergon_tracker.index.db import connect, fresh_db  # noqa: E402
from ergon_tracker.models import JobLevel, JobPosting, make_job_id  # noqa: E402

# A JD whose cold enrichment (years=9 -> SENIOR) DIFFERS from the deliberately-stale prior row
# (years=2), so a passing test proves the re-enrich actually re-extracted and did not merely reuse.
_JD = "We need a Staff Engineer. Requires 9+ years of experience building distributed systems."
_STALE_YEARS = 2

_TOKEN = "acme"
_ID_WITH_JD = make_job_id("greenhouse", "1")
_ID_NO_JD = make_job_id("greenhouse", "2")


def _stale_job(sid: str, title: str) -> JobPosting:
    """A prior-index posting carrying a STALE enrichment (as an old/buggy extractor would have left
    it): explicit SENIOR level + years=2, so nothing here is re-derived unless re-enrich fires."""
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company="Acme Corp",
        title=title,
        level=JobLevel.SENIOR,
        years_experience_min=_STALE_YEARS,
        years_experience_max=_STALE_YEARS,
        board_token=_TOKEN,
    )


def _build_prior(tmp_path: Path) -> Path:
    """Build a prior index (via the finalize path, so ``meta.enrich_version`` is stamped) holding two
    carried-forward-able postings, and seed the JD sidecar with a JD for ONLY the first."""
    fresh_prior = tmp_path / "fresh_prior.sqlite"
    fresh_db(fresh_prior)
    con = connect(fresh_prior)
    con.execute("PRAGMA foreign_keys = OFF")
    append_jobs(
        con,
        [_stale_job("1", "Staff Engineer"), _stale_job("2", "Data Analyst")],
        build_id="prior",
    )
    con.commit()
    con.close()

    prior_db = tmp_path / "prior.sqlite"
    build_index_from_fresh_db(fresh_prior, prior_db, build_id="prior")

    jcon = jd_store.open_jd_store(str(tmp_path / "index-jd.sqlite"))
    jd_store.put(jcon, _ID_WITH_JD, _JD)  # id "2" deliberately has NO stored JD
    jcon.commit()
    jcon.close()
    return prior_db


def _empty_fresh(tmp_path: Path) -> Path:
    """A fresh crawl DB with NO jobs -> the whole prior index carries forward (nothing re-crawled)."""
    p = tmp_path / "fresh_today.sqlite"
    fresh_db(p)
    return p


def _row(db: Path, jid: str) -> dict:
    con = connect(db, read_only=True)
    try:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        # sqlite3.Row: iterating yields VALUES, so .keys() is required to get column names.
        return {k: r[k] for k in r.keys()} if r else {}  # noqa: SIM118
    finally:
        con.close()


def _bump_version(monkeypatch, v: int) -> None:
    """Simulate an extractor/normalizer release: raise ENRICH_VERSION everywhere it is read."""
    monkeypatch.setattr(mapping_mod, "ENRICH_VERSION", v)
    monkeypatch.setattr(build_mod, "ENRICH_VERSION", v)


def test_version_bump_reenriches_carried_rows_from_jd(monkeypatch, tmp_path):
    prior_db = _build_prior(tmp_path)
    jd_db = tmp_path / "index-jd.sqlite"

    # The COLD baseline: what a full re-enrich of the stored JD produces (title from the prior row).
    cold = JobPosting.create(
        source="greenhouse", source_job_id="1", company="Acme Corp", title="Staff Engineer",
        description_text=_JD,
    )
    enrich_in_place(cold, infer_level_from_experience=True)
    assert cold.years_experience_min == 9  # the JD really does extract to 9 (differs from stale 2)

    # --- BUMP the version and rebuild carrying everything forward (crawled_keys empty => all carry).
    _bump_version(monkeypatch, 2)
    today_db = tmp_path / "today.sqlite"
    build_index_from_fresh_db(
        _empty_fresh(tmp_path), today_db, build_id="today",
        prev_db=prior_db, crawled_keys=set(), jd_db_path=jd_db,
    )

    # Direction 2a: the row WITH a stored JD was re-enriched from that JD == the cold re-enrich, and
    # the stale prior value (2) is gone.
    with_jd = _row(today_db, _ID_WITH_JD)
    assert with_jd["years_min"] == cold.years_experience_min == 9
    assert with_jd["level"] == cold.level.value
    assert with_jd["years_min"] != _STALE_YEARS

    # Direction 2b: the row with NO stored JD retains its prior enrichment (can't replay what wasn't
    # stored) — never blanked, never dropped.
    no_jd = _row(today_db, _ID_NO_JD)
    assert no_jd["years_min"] == _STALE_YEARS
    assert no_jd["level"] == JobLevel.SENIOR.value


def test_version_unchanged_is_byte_identical_noop(monkeypatch, tmp_path):
    prior_db = _build_prior(tmp_path)
    jd_db = tmp_path / "index-jd.sqlite"

    # Build WITH the JD sidecar available but the version UNCHANGED (still 1, matching the prior's
    # stamp) -> the re-enrich pass must not fire.
    same_db = tmp_path / "same.sqlite"
    build_index_from_fresh_db(
        _empty_fresh(tmp_path), same_db, build_id="same",
        prev_db=prior_db, crawled_keys=set(), jd_db_path=jd_db,
    )
    # Build again with NO sidecar at all (re-enrich impossible) -> the reference carry-forward.
    ref_db = tmp_path / "ref.sqlite"
    build_index_from_fresh_db(
        _empty_fresh(tmp_path), ref_db, build_id="same",
        prev_db=prior_db, crawled_keys=set(), jd_db_path=None,
    )

    # The carried rows are byte-identical whether or not the JD sidecar was passed: version unchanged
    # => the reuse-skip still fires and the stale prior enrichment carries forward untouched.
    for jid in (_ID_WITH_JD, _ID_NO_JD):
        assert _row(same_db, jid) == _row(ref_db, jid)
        assert _row(same_db, jid)["years_min"] == _STALE_YEARS


def test_reenrich_pass_is_strict_noop_when_version_matches(monkeypatch, tmp_path):
    """Unit gate: reenrich_carried_forward returns 0 (touches nothing) when the prior's stamped
    version equals the current ENRICH_VERSION, regardless of the sidecar."""
    prior_db = _build_prior(tmp_path)
    jd_db = tmp_path / "index-jd.sqlite"

    today_db = tmp_path / "today.sqlite"
    build_index_from_fresh_db(
        _empty_fresh(tmp_path), today_db, build_id="today", prev_db=prior_db, crawled_keys=set(),
    )
    con = connect(today_db)
    try:
        # version matches (both 1) -> no-op even with a valid sidecar present.
        assert reenrich_carried_forward(con, prior_db, jd_db, set()) == 0
        # a missing sidecar is also a no-op (never raises).
        assert reenrich_carried_forward(con, prior_db, None, set()) == 0
    finally:
        con.close()


def test_reenrich_fires_only_for_carried_not_crawled(monkeypatch, tmp_path):
    """A row whose company_key IS in crawled_keys (freshly crawled) is left as the crawl wrote it —
    only the carried backlog is replayed from the JD sidecar."""
    from ergon_tracker.dedup import normalize_company

    prior_db = _build_prior(tmp_path)
    jd_db = tmp_path / "index-jd.sqlite"
    _bump_version(monkeypatch, 2)

    # Build the carried index WITHOUT the sidecar so no re-enrich has run yet, then drive the pass
    # directly to isolate the crawled-key scoping.
    today_db = tmp_path / "today.sqlite"
    build_index_from_fresh_db(
        _empty_fresh(tmp_path), today_db, build_id="today", prev_db=prior_db, crawled_keys=set(),
    )
    acme = normalize_company("Acme Corp")

    con = connect(today_db)
    try:
        # Company marked crawled -> both rows excluded -> 0 re-enriched (crawl output is authoritative).
        assert reenrich_carried_forward(con, prior_db, jd_db, {acme}) == 0
        assert _row(today_db, _ID_WITH_JD)["years_min"] == _STALE_YEARS
        # Not crawled -> the JD-bearing row is re-enriched (1); the no-JD row is skipped.
        assert reenrich_carried_forward(con, prior_db, jd_db, set()) == 1
    finally:
        con.close()
