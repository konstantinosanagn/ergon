"""Full-JD sidecar (pipeline-restructuring Item 2): the compressed replay store.

Covers the API Item 3 (re-enrich) consumes (``put``/``get`` round-trip), the carry-forward that
keeps a JD for a board NOT crawled this run, orphan-pruning to the live index, non-fatal absence,
and the MANDATED parity gate: turning JD capture on leaves the core ``jobs`` table byte-identical
(it only ADDS a sidecar — zero core-schema/to_row churn).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import anyio
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index import jd_store  # noqa: E402
from ergon_tracker.index.build import build_index_from_fresh_db  # noqa: E402
from ergon_tracker.index.db import connect  # noqa: E402
from ergon_tracker.index.mapping import (  # noqa: E402
    _SNIPPET,
    _snippet_source,
    full_jd_text,
    to_row,
)
from ergon_tracker.index.scheduler import BoardState  # noqa: E402
from ergon_tracker.models import JobPosting, RawJob  # noqa: E402


# --- unit: schema + round-trip ------------------------------------------------------------------
def test_schema_and_count():
    con = sqlite3.connect(":memory:")
    jd_store.ensure_jd_schema(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(job_jd)")}
    assert {"id", "jd"} <= cols
    assert jd_store.count(con) == 0
    ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert ver == str(jd_store.JD_SCHEMA_VERSION)


def test_put_get_roundtrip_identical():
    con = sqlite3.connect(":memory:")
    jd_store.ensure_jd_schema(con)
    # A realistic multi-KB JD with unicode + markup-ish text: get() must return it byte-for-byte.
    jd = ("Senior Platform Engineer\n\n" + "We build résumés & pipelines — 5+ yrs. " * 400).strip()
    jd_store.put(con, "greenhouse:acme:1", jd)
    assert jd_store.get(con, "greenhouse:acme:1") == jd
    assert jd_store.count(con) == 1
    # absent id -> None (never raises); empty/None put is a no-op (get stays None, not "")
    assert jd_store.get(con, "does-not-exist") is None
    jd_store.put(con, "empty", "")
    jd_store.put(con, "none", None)
    assert jd_store.get(con, "empty") is None and jd_store.get(con, "none") is None
    assert jd_store.count(con) == 1


def test_put_upserts_fresher_wins():
    con = sqlite3.connect(":memory:")
    jd_store.ensure_jd_schema(con)
    jd_store.put(con, "id1", "old JD text")
    jd_store.put(con, "id1", "new JD text")  # re-crawl with a changed JD
    assert jd_store.get(con, "id1") == "new JD text"
    assert jd_store.count(con) == 1


def test_put_many_batches_and_skips_empty():
    con = sqlite3.connect(":memory:")
    jd_store.ensure_jd_schema(con)
    n = jd_store.put_many(con, [("a", "JD a"), ("b", None), ("c", ""), ("d", "JD d")])
    assert n == 2
    assert jd_store.get(con, "a") == "JD a" and jd_store.get(con, "d") == "JD d"
    assert jd_store.get(con, "b") is None and jd_store.get(con, "c") is None


# --- unit: carry-forward + prune ----------------------------------------------------------------
def test_carry_forward_preserves_uncrawled_and_never_clobbers_fresh(tmp_path):
    prior_path = tmp_path / "prior-jd.sqlite"
    prior = jd_store.open_jd_store(str(prior_path))
    jd_store.put(prior, "uncrawled-board-job", "PRIOR jd — board not crawled this run")
    jd_store.put(prior, "shared", "PRIOR jd for a re-crawled posting")  # must NOT clobber fresh
    prior.commit()
    prior.close()

    fresh = jd_store.open_jd_store(str(tmp_path / "fresh-jd.sqlite"))
    jd_store.put(fresh, "shared", "FRESH jd (re-crawled)")  # fresher wins
    jd_store.put(fresh, "new-board-job", "FRESH jd for a newly-crawled posting")
    fresh.commit()

    moved = jd_store.carry_forward(fresh, str(prior_path))
    assert moved == 1  # only the uncrawled id was carried; 'shared' already present -> ignored
    assert jd_store.get(fresh, "uncrawled-board-job") == "PRIOR jd — board not crawled this run"
    assert jd_store.get(fresh, "shared") == "FRESH jd (re-crawled)"  # fresh preserved
    assert jd_store.get(fresh, "new-board-job") == "FRESH jd for a newly-crawled posting"
    fresh.close()


def test_carry_forward_absent_prior_is_non_fatal(tmp_path):
    fresh = jd_store.open_jd_store(str(tmp_path / "fresh-jd.sqlite"))
    assert jd_store.carry_forward(fresh, str(tmp_path / "nope.sqlite")) == 0
    assert jd_store.carry_forward(fresh, "") == 0
    fresh.close()


def test_prune_to_live_ids_drops_orphans():
    con = sqlite3.connect(":memory:")
    jd_store.ensure_jd_schema(con)
    jd_store.put_many(con, [("a", "JD a"), ("b", "JD b"), ("c", "JD c")])
    removed = jd_store.prune_to_live_ids(con, {"a", "c"})  # 'b' departed the index
    assert removed == 1
    assert jd_store.count(con) == 2
    assert jd_store.get(con, "b") is None
    assert jd_store.get(con, "a") == "JD a" and jd_store.get(con, "c") == "JD c"


# --- unit: full_jd_text is the untruncated snippet source ---------------------------------------
def test_full_jd_text_is_untruncated_snippet_source():
    body = "<p>" + ("Build distributed systems. " * 100) + "</p>"  # > 300 chars of text
    job = JobPosting.create(
        source="greenhouse", source_job_id="1", company="Acme", title="Eng", description_html=body
    )
    full = full_jd_text(job)
    assert full == _snippet_source(job)  # same source the snippet derives from
    assert len(full) > _SNIPPET  # the un-cap: full JD is longer than the stored snippet
    row = to_row(job, build_id="b")
    assert len(row["snippet"]) == _SNIPPET  # to_row still caps at 300 (unchanged)
    assert full.startswith(row["snippet"][:50].strip())


# --- integration: crawl-populate + PARITY GATE --------------------------------------------------
_TOKEN = "acme"
_POSTINGS = [("1", "Staff Engineer"), ("2", "Engineering Manager")]
_JD_BY_ID = {
    "1": "<h2>Staff Engineer</h2><p>" + ("Design large-scale platforms. " * 40) + "</p>",
    "2": "<h2>Engineering Manager</h2><p>" + ("Lead a team of engineers. " * 40) + "</p>",
}


class _Reg:
    def all(self):
        return {"acme": {"ats": "greenhouse", "token": _TOKEN, "domain": "acme.com"}}


class _Provider:
    name = "greenhouse"

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return None

    async def fetch(self, token, query, fetcher):
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
            description_html=_JD_BY_ID[str(raw.source_job_id)],
        )


def _all_rows(db: Path):
    """EVERY column of jobs (incl. bookkeeping): with the same build_id, JD capture must not perturb
    a single byte of the core table — it only writes a separate sidecar."""
    con = connect(db, read_only=True)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        sel = ",".join(cols)
        rows = con.execute(f"SELECT {sel} FROM jobs ORDER BY id").fetchall()  # noqa: S608
        return cols, [tuple(r) for r in rows]
    finally:
        con.close()


def _crawl_build(states, base: Path, capture_jd: bool):
    fresh = base / "fresh.sqlite"
    base.mkdir(parents=True, exist_ok=True)
    outcome, _ = anyio.run(
        bi._crawl_due, 10, states, fresh, "same-build", 0, False, None, capture_jd
    )
    keys = set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    db = base / "index.sqlite"
    build_index_from_fresh_db(fresh, db, build_id="same-build", crawled_keys=keys)
    return db


@pytest.fixture
def _patched(monkeypatch):
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    prov = _Provider()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.delenv("ERGON_DELTA_CRAWL", raising=False)
    return prov


def test_jobs_table_byte_identical_with_jd_capture(_patched, tmp_path):
    """PARITY GATE: crawl the SAME board twice — JD capture OFF vs ON — and assert the core jobs
    table is byte-identical across every column."""
    s_off = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    off_db = _crawl_build(s_off, tmp_path / "off", capture_jd=False)

    s_on = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    on_db = _crawl_build(s_on, tmp_path / "on", capture_jd=True)

    cols_off, rows_off = _all_rows(off_db)
    cols_on, rows_on = _all_rows(on_db)
    assert cols_off == cols_on
    assert rows_off == rows_on
    assert len(rows_on) == len(_POSTINGS)
    # OFF wrote no sidecar; ON did.
    assert not (tmp_path / "off" / "index-jd.sqlite").exists()
    assert (tmp_path / "on" / "index-jd.sqlite").exists()


def test_crawl_populates_jd_store_with_full_text(_patched, tmp_path):
    """The crawl persists the FULL JD (not the 300-char snippet) keyed by the real posting id, and it
    round-trips — the replay source Item 3 reads."""
    s_on = {"greenhouse|acme": BoardState(provider="greenhouse", token=_TOKEN)}
    _crawl_build(s_on, tmp_path / "on", capture_jd=True)

    jcon = jd_store.open_jd_store(str(tmp_path / "on" / "index-jd.sqlite"))
    try:
        assert jd_store.count(jcon) == len(_POSTINGS)
        # Reconstruct the crawled posting's id + expected stored text via the same mapping the crawl
        # used, and assert the stored JD matches full_jd_text exactly (longer than the snippet).
        prov = _patched
        for raw in anyio.run(prov.fetch, _TOKEN, None, None):
            job = prov.normalize(raw)
            expected = full_jd_text(job)
            got = jd_store.get(jcon, job.id)
            assert got == expected
            assert len(got) > _SNIPPET
    finally:
        jcon.close()
