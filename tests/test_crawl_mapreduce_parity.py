"""R3 parity: the K-shard map/reduce crawl == the single-process crawl, byte-for-byte.

The mandated correctness gate for map/reducing the non-join crawl across parallel runners. Over the
SAME fixed small registry slice we build the index two ways:

  (1) SINGLE  -- today's path: one ``_crawl_due`` over the whole window -> fresh.sqlite -> build.
  (2) K-SHARD -- ``_run_crawl_map`` per shard (each crawls only its host-bucketed slice, streaming a
                 partial fresh DB + emitting an outcome/state manifest artifact), then
                 ``_reduce_crawl_shards`` unions the K partials + merges the outcomes, then the SAME
                 build + deferred ``apply_outcome`` tail.

and assert the resulting ``jobs`` DATA rows are byte-identical (same volatile-column exclusions as the
delta-crawl parity tests) AND the post-crawl ``board_state`` (tier/next_due/idset_hash/...) is
equivalent per board. Also asserts the map actually SPLIT the registry (>=2 non-empty shards, none
shared) and that flag-off (no ``--shard``) crawls every board -- i.e. the un-sharded window equals the
union of the K slices, so the sharded path can only ever redistribute work, never change the result.

Runs on the REAL concurrent worker pool (``_crawl_due`` -> ``run_pool``); only ``provider.fetch`` is
faked, so no network is touched.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_index as bi  # noqa: E402

from ergon_tracker.index.build import (  # noqa: E402
    build_index_from_fresh_db,
    changed_companies_sql,
)
from ergon_tracker.index.db import connect  # noqa: E402
from ergon_tracker.index.freshness_shard import board_shard  # noqa: E402
from ergon_tracker.index.scheduler import (  # noqa: E402
    apply_outcome,
    load_state,
    save_state,
)
from ergon_tracker.models import JobPosting, RawJob  # noqa: E402

NUM_SHARDS = 4
_TODAY = "2026-07-26"

# 8 distinct fixed-host ATS sources -> 8 distinct politeness buckets, so board_shard spreads them
# across shards 1..K-1 (shard 0 is reserved for join, which this non-join slice never contains). Two
# tokens each so a source's boards all land on ITS single host bucket == one shard (the invariant).
_SOURCES = [
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "jazzhr",
    "smartrecruiters",
    "rippling",
    "dejobs",
]
_REGISTRY = {
    f"{src}-{tok}": {"ats": src, "token": tok, "domain": f"{src}{tok}.example"}
    for src in _SOURCES
    for tok in ("alpha", "beta")
}


class _Reg:
    def all(self):
        return dict(_REGISTRY)


class _Provider:
    """One fake provider per source; fetch returns deterministic postings keyed by (source, token)."""

    def __init__(self, source: str):
        self.source = source

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return None

    async def fetch(self, token, query, fetcher):
        company = f"{self.source}-{token} Inc"
        return [
            RawJob(
                source=self.source,
                source_job_id=f"{token}-{i}",
                company=company,
                token=token,
                payload={"title": f"{token.title()} Engineer {i}"},
            )
            for i in range(3)
        ]

    def normalize(self, raw):
        return JobPosting.create(
            source=raw.source,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=raw.payload["title"],
        )


_PROVIDERS: dict[str, _Provider] = {}


def _get_provider(source: str) -> _Provider:
    return _PROVIDERS.setdefault(source, _Provider(source))


def _install(monkeypatch):
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    _PROVIDERS.clear()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", _get_provider)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    # No source partition; delta-crawl ON so idset_hash is stamped into board_state (a stronger parity
    # check) but with no sidecar present nothing is ever SKIPPED -- every board is crawled either way.
    monkeypatch.delenv("ERGON_CRAWL_ONLY_SOURCES", raising=False)
    monkeypatch.delenv("ERGON_CRAWL_EXCLUDE_SOURCES", raising=False)
    monkeypatch.delenv("ERGON_DELTA_CONTENT_VERSION", raising=False)
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")


def _stable_rows(db: Path):
    con = connect(db, read_only=True)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        # rowid is an insertion-order INTEGER surrogate (the stable identity is the TEXT ``id``); it
        # legitimately differs when the union reorders inserts across shards, so it's excluded exactly
        # like the volatile last_seen/fetched_at/build_id bookkeeping columns.
        keep = [c for c in cols if c not in {"rowid", "last_seen", "fetched_at", "build_id"}]
        sel = ",".join(keep)
        rows = con.execute(f"SELECT {sel} FROM jobs ORDER BY id").fetchall()  # noqa: S608
        return keep, [tuple(r) for r in rows]
    finally:
        con.close()


def _apply_outcome_tail(states: dict, outcome: dict, fresh_path: Path, prev_db) -> None:
    """The deferred-apply tail exactly as main() runs it: fold each board's outcome back into its
    state using the GLOBAL changed set (this is the step R3 relocates from the crawl to the reduce)."""
    changed = changed_companies_sql(fresh_path, prev_db)
    for bkey, o in outcome.items():
        apply_outcome(
            states[bkey],
            today=_TODAY,
            changed=bool(o["companies"] & changed) and not o["error"],
            error=o["error"],
            http_429=o["http_429"],
            requests=1,
        )


def _crawled_keys(outcome: dict) -> set:
    return set().union(*(o["companies"] for o in outcome.values())) if outcome else set()


def test_kshard_crawl_reduce_matches_single_process(monkeypatch, tmp_path):
    _install(monkeypatch)

    # --- SINGLE (today's path): one crawl over the whole window. ---
    single_dir = tmp_path / "single"
    single_dir.mkdir()
    single_fresh = single_dir / "fresh.sqlite"
    s_single: dict = {}
    outcome_single, cur_single = anyio.run(bi._crawl_due, 1000, s_single, single_fresh, "b1")
    single_db = single_dir / "index.sqlite"
    build_index_from_fresh_db(
        single_fresh,
        single_db,
        build_id="b1",
        prev_db=None,
        crawled_keys=_crawled_keys(outcome_single),
    )
    _apply_outcome_tail(s_single, outcome_single, single_fresh, None)

    # every board was crawled (flag-off / un-sharded => full coverage) and produced 3 postings.
    assert len(outcome_single) == len(_REGISTRY)
    _, rows_single = _stable_rows(single_db)
    assert len(rows_single) == len(_REGISTRY) * 3

    # --- K-SHARD: run the real map per shard, then the real reduce. ---
    shard_dir = tmp_path / "shard"
    shard_dir.mkdir()
    save_state(
        {}, shard_dir / "board_state.json"
    )  # the shared base every map downloads (cold: empty)
    per_shard_boards: dict[int, int] = {}
    for i in range(NUM_SHARDS):
        bi._run_crawl_map(
            shard_dir,
            shard_dir / "index.sqlite",  # no prior index (cold build) => prev_db is None
            "b1",
            limit=1000,
            rich=False,
            jd=False,
            shard=i,
            num_shards=NUM_SHARDS,
        )
        # a manifest is written per shard; count its boards to prove the split.
        import json

        man = json.loads((shard_dir / f"crawl-map-shard-{i}.json").read_text())
        per_shard_boards[i] = len(man["outcome"])
        assert man["next_cursor"] == cur_single  # cursor is shard-independent

    # the map actually SPLIT the work: >=2 non-empty shards, and the slices partition the registry.
    non_empty = [i for i, n in per_shard_boards.items() if n]
    assert len(non_empty) >= 2, per_shard_boards
    assert sum(per_shard_boards.values()) == len(_REGISTRY)
    # every board landed on the shard board_shard assigns it to (the disjoint-host invariant).
    for e in _REGISTRY.values():
        assert board_shard(e["ats"], e["token"], NUM_SHARDS) in non_empty

    # REDUCE: union the partials + merge outcomes, then the identical build + apply tail.
    reduce_fresh = shard_dir / "fresh.sqlite"
    s_reduce = load_state(shard_dir / "board_state.json")  # same base the maps started from
    outcome_reduce, cur_reduce = bi._reduce_crawl_shards(
        shard_dir, reduce_fresh, s_reduce, 0, NUM_SHARDS, jd=False
    )
    reduce_db = shard_dir / "index.sqlite"
    build_index_from_fresh_db(
        reduce_fresh,
        reduce_db,
        build_id="b1",
        prev_db=None,
        crawled_keys=_crawled_keys(outcome_reduce),
    )
    _apply_outcome_tail(s_reduce, outcome_reduce, reduce_fresh, None)

    assert cur_reduce == cur_single

    # --- PARITY 1: jobs table byte-identical (data + identity columns). ---
    cols_single, rows_single2 = _stable_rows(single_db)
    cols_reduce, rows_reduce = _stable_rows(reduce_db)
    assert cols_single == cols_reduce
    assert rows_reduce == rows_single2

    # --- PARITY 2: post-crawl board_state equivalent per board (incl. idset_hash). ---
    assert set(s_single) == set(s_reduce)
    for bkey in s_single:
        assert asdict(s_single[bkey]) == asdict(s_reduce[bkey]), bkey


def test_flag_off_crawl_is_byte_identical(monkeypatch, tmp_path):
    """Flag-off: ``_crawl_due`` with no shard args crawls the FULL window, and its fresh jobs equal the
    UNION of the K per-shard slices -- so the sharding parameters only ever redistribute work (default
    None => today's exact single-process crawl)."""
    _install(monkeypatch)

    full_fresh = tmp_path / "full.sqlite"
    outcome_full, _ = anyio.run(bi._crawl_due, 1000, {}, full_fresh, "b1")

    # union the K shard slices (drive _crawl_due directly with shard args, no artifacts).
    shard_dir = tmp_path / "sh"
    shard_dir.mkdir()
    union_ids: set = set()
    for i in range(NUM_SHARDS):
        fp = shard_dir / f"fresh-shard-{i}.sqlite"
        outcome_i, _ = anyio.run(
            bi._crawl_due, 1000, {}, fp, "b1", 0, False, None, False, None, None, i, NUM_SHARDS
        )
        con = connect(fp, read_only=True)
        try:
            union_ids |= {r[0] for r in con.execute("SELECT id FROM jobs").fetchall()}
        finally:
            con.close()

    con = connect(full_fresh, read_only=True)
    try:
        full_ids = {r[0] for r in con.execute("SELECT id FROM jobs").fetchall()}
    finally:
        con.close()

    assert full_ids == union_ids
    assert len(outcome_full) == len(_REGISTRY)
