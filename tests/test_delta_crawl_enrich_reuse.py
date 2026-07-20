"""Sub-phase C parity: a reused (enrich_hash-matched) row == a freshly-enriched row.

The mandated enrich-reuse gate. Same board is built two ways over the SAME today-inputs:

  (1) FULL   -- flag off: every posting is re-enriched (today's path).
  (2) DELTA  -- flag on with a prior index: an UNCHANGED posting (matching enrich_hash) copies its
                prior enriched row; a posting whose JD BODY was rewritten (content_hash unchanged,
                enrich_hash changed) is RE-ENRICHED, never served stale.

Asserts: the two builds' data rows are byte-identical; the delta build ran enrich FEWER times
(reuse fired); and the body-rewritten posting picked up the NEW body's enrichment (no stale reuse).
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.build import build_index_from_fresh_db  # noqa: E402
from ergon_tracker.index.db import connect  # noqa: E402
from ergon_tracker.index.scheduler import BoardState  # noqa: E402
from ergon_tracker.models import JobLevel, JobPosting, RawJob, make_job_id  # noqa: E402

_TOKEN = "acme"
# Distinct titles so the two postings are NOT fuzzy-deduped into one. id "1" is stable (same body
# prior & today -> reuse fires). id "2" is body-rewritten today but keeps its title/level/no-loc/
# no-salary, so its content_hash is UNCHANGED while its enrich_hash changes -> must re-enrich.
_TITLES = {"1": "Staff Engineer", "2": "Engineering Manager"}
_BODIES = {
    "prior": {"1": "Requires 2+ years of experience.", "2": "Requires 2+ years of experience."},
    "today": {"1": "Requires 2+ years of experience.", "2": "Requires 9+ years of experience."},
}


class _Reg:
    def all(self):
        return {"acme": {"ats": "greenhouse", "token": _TOKEN}}


class _Provider:
    name = "greenhouse"

    def __init__(self):
        self.mode = "prior"

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return None

    async def fetch(self, token, query, fetcher):
        return [
            RawJob(source="greenhouse", source_job_id=i, company="Acme Corp", token=token,
                   payload={"body": b, "title": _TITLES[i]})
            for i, b in _BODIES[self.mode].items()
        ]

    def normalize(self, raw):
        # level set explicitly (not UNKNOWN) so enrich never changes content_hash -> an unchanged
        # posting's enrich_hash is stable and the reuse path can fire.
        return JobPosting.create(
            source="greenhouse", source_job_id=raw.source_job_id, company="Acme Corp",
            title=raw.payload["title"], level=JobLevel.SENIOR,
            description_text=raw.payload["body"],
        )


def _crawl_and_build(states, work_dir, prov, build_id, prev_db, monkeypatch):
    work_dir.mkdir(parents=True, exist_ok=True)
    fresh = work_dir / "fresh.sqlite"
    # Spy on enrich_in_place (local-imported inside _crawl_due at call time, so patching the module
    # attribute is picked up). Counts how many postings were actually enriched this run.
    import ergon_tracker.enrich as enrich_mod

    real = enrich_mod.enrich_in_place
    calls = {"n": 0}

    def _spy(job, **kw):
        calls["n"] += 1
        return real(job, **kw)

    monkeypatch.setattr(enrich_mod, "enrich_in_place", _spy)
    outcome, _ = anyio.run(bi._crawl_due, 10, states, fresh, build_id, 0, False, prev_db)
    monkeypatch.setattr(enrich_mod, "enrich_in_place", real)
    crawled = set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    db = work_dir / "index.sqlite"
    build_index_from_fresh_db(fresh, db, build_id=build_id, prev_db=prev_db, crawled_keys=crawled)
    return db, calls["n"]


def _stable_rows(db: Path):
    con = connect(db, read_only=True)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        keep = [c for c in cols if c not in {"last_seen", "fetched_at", "build_id"}]
        rows = con.execute(f"SELECT {','.join(keep)} FROM jobs ORDER BY id").fetchall()  # noqa: S608
        return keep, [tuple(r) for r in rows]
    finally:
        con.close()


def _years(db: Path, sid: str):
    con = connect(db, read_only=True)
    try:
        r = con.execute(
            "SELECT years_min FROM jobs WHERE id=?", (make_job_id("greenhouse", sid),)
        ).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def test_enrich_reuse_matches_full_and_reenriches_changed_body(monkeypatch, tmp_path):
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    prov = _Provider()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)

    # --- PRIOR snapshot (flag off): id2 body says "2 years". ---
    monkeypatch.delenv("ERGON_DELTA_CRAWL", raising=False)
    prov.mode = "prior"
    s_prior = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    prior_db, _ = _crawl_and_build(s_prior, tmp_path / "prior", prov, "prior", None, monkeypatch)
    assert _years(prior_db, "2") == 2  # prior enrichment captured the OLD body

    # --- FULL today (flag off): re-enrich BOTH postings with today's bodies. ---
    prov.mode = "today"
    s_full = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    full_db, full_calls = _crawl_and_build(
        s_full, tmp_path / "full", prov, "today", prior_db, monkeypatch
    )

    # --- DELTA today (flag on): id1 reused, id2 re-enriched (body changed). ---
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    prov.mode = "today"
    s_delta = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    delta_db, delta_calls = _crawl_and_build(
        s_delta, tmp_path / "delta", prov, "today", prior_db, monkeypatch
    )

    # PARITY: the delta build's rows are byte-identical to the full re-enrich.
    cols_f, rows_f = _stable_rows(full_db)
    cols_d, rows_d = _stable_rows(delta_db)
    assert cols_f == cols_d
    assert rows_f == rows_d

    # Reuse actually fired: delta enriched fewer postings than the full re-enrich.
    assert full_calls == 2  # both postings enriched
    assert delta_calls == 1  # only the body-rewritten id2 re-enriched; id1 reused

    # The body-rewritten posting was RE-ENRICHED from the NEW body (no stale reuse): its years moved
    # from the prior 2 to today's 9, identically in both builds.
    assert _years(full_db, "2") == 9
    assert _years(delta_db, "2") == 9
    # The unchanged posting kept its (identical) enrichment in both builds.
    assert _years(full_db, "1") == 2
    assert _years(delta_db, "1") == 2


class _InferLevelProvider:
    """A board whose postings arrive with level=UNKNOWN and get their level INFERRED by enrich
    (from the body's years-of-experience). This is the case the sibling test above had to dodge
    (it hard-set level=SENIOR) -- enrich mutates level, which feeds content_hash, so a PRE-fix
    build stored a POST-enrich enrich_hash and this posting could never hit reuse. Post-fix the
    stored fingerprint is pre-enrich, so an unchanged inferred-level posting reuses correctly."""

    name = "greenhouse"
    mode = "today"  # body identical prior & today -> genuinely unchanged

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return None

    async def fetch(self, token, query, fetcher):
        return [
            RawJob(
                source="greenhouse", source_job_id="7", company="Acme Corp", token=token,
                payload={"body": "Requires 9+ years of experience.", "title": "Engineer"},
            )
        ]

    def normalize(self, raw):
        # NOTE: level deliberately left UNKNOWN -> enrich_in_place infers it (level_from_years),
        # mutating content_hash. The stored enrich_hash MUST be the pre-enrich value for reuse to fire.
        return JobPosting.create(
            source="greenhouse", source_job_id=raw.source_job_id, company="Acme Corp",
            title=raw.payload["title"], description_text=raw.payload["body"],
        )


def test_enrich_reuse_fires_for_inferred_level_posting(monkeypatch, tmp_path):
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    prov = _InferLevelProvider()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)

    # PRIOR (flag off): enrich infers the level from "9+ years"; the stored enrich_hash is pre-enrich.
    monkeypatch.delenv("ERGON_DELTA_CRAWL", raising=False)
    s_prior = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    prior_db, _ = _crawl_and_build(s_prior, tmp_path / "p", prov, "prior", None, monkeypatch)

    # The posting's level really was inferred (not UNKNOWN) -> pre-fix this row's stored enrich_hash
    # would be post-enrich and reuse could never match it.
    con = connect(prior_db, read_only=True)
    try:
        lvl = con.execute(
            "SELECT level FROM jobs WHERE id=?", (make_job_id("greenhouse", "7"),)
        ).fetchone()[0]
    finally:
        con.close()
    assert lvl not in (None, "", "unknown")

    # FULL today (flag off): re-enriches. DELTA today (flag on): body unchanged -> reuse MUST fire.
    s_full = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    full_db, full_calls = _crawl_and_build(s_full, tmp_path / "f", prov, "t", prior_db, monkeypatch)

    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    s_delta = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    delta_db, delta_calls = _crawl_and_build(
        s_delta, tmp_path / "d", prov, "t", prior_db, monkeypatch
    )

    cols_f, rows_f = _stable_rows(full_db)
    cols_d, rows_d = _stable_rows(delta_db)
    assert cols_f == cols_d
    assert rows_f == rows_d  # byte-identical -> reuse produced the same row a full enrich would
    assert full_calls == 1  # full path enriches the posting
    assert delta_calls == 0  # THE FIX: inferred-level posting is reused, not re-enriched
