"""Join-isolation parity: a source-partitioned crawl covers 100% of the registry and produces the
SAME ``jobs`` rows as a single un-partitioned full crawl.

join.com is 34% of the registry and crawls ~10x slower than everything else, forcing a rotating
window that starves the other ~66%. Item 1 ISOLATES join onto its own shard so the non-join ~66%
crawls FULLY every day. This is a *partition* of the same crawl -- excluding a source from a run is
exactly a window that happens to contain none of that source's boards, and ``carry_forward`` copies
those boards' prior rows -- so the union of the two partitions must be byte-identical to crawling
everything at once.

The gate proves three things:
  1. ``_registry_window`` with a partition is a clean split: exclude(join) + only(join) partition the
     registry with no overlap and no loss, and the default (both None) is byte-identical to today.
  2. END-TO-END PARITY: build the index (a) as one full crawl, and (b) as non-join-full THEN
     join-only (each carrying the other partition forward). Every DATA/identity column of the ``jobs``
     table matches to the byte (bookkeeping cols that already differ on the carry-forward/304 path --
     ``last_seen``/``fetched_at``/``build_id`` -- excluded, exactly as the delta-crawl parity test does).
  3. The default (unpartitioned) crawl still fetches every source -- flag-off = today.
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
from ergon_tracker.models import JobPosting, RawJob  # noqa: E402

# A multi-source registry: 4 greenhouse (non-join) boards + 3 join boards.
_REGISTRY = {
    "gh-a": {"ats": "greenhouse", "token": "gh-a", "domain": "a.com"},
    "gh-b": {"ats": "greenhouse", "token": "gh-b", "domain": "b.com"},
    "gh-c": {"ats": "greenhouse", "token": "gh-c", "domain": "c.com"},
    "gh-d": {"ats": "greenhouse", "token": "gh-d", "domain": "d.com"},
    "jn-x": {"ats": "join", "token": "jn-x", "domain": "x.com"},
    "jn-y": {"ats": "join", "token": "jn-y", "domain": "y.com"},
    "jn-z": {"ats": "join", "token": "jn-z", "domain": "z.com"},
}
_NON_JOIN_TOKENS = {"gh-a", "gh-b", "gh-c", "gh-d"}
_JOIN_TOKENS = {"jn-x", "jn-y", "jn-z"}


class _Reg:
    def all(self):
        return dict(_REGISTRY)


class _Prov:
    def __init__(self, name: str):
        self.name = name
        self.fetch_calls = 0

    def conditional_url(self, token):
        return None

    def list_host(self, token):
        return None  # no deadline-box in the test (host_budget=0 anyway)

    async def fetch(self, token, query, fetcher):
        self.fetch_calls += 1
        return [
            RawJob(
                source=self.name,
                source_job_id=f"{token}-{i}",  # board-unique (else same-source ids collide in dedup)
                company=f"{token} Inc",
                token=token,
                payload={"title": f"Role {i}"},
            )
            for i in (1, 2)
        ]

    def normalize(self, raw):
        return JobPosting.create(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=raw.company,
            title=raw.payload["title"],
        )


def _install(monkeypatch):
    import ergon_tracker.providers.base as base_mod
    import ergon_tracker.registry.store as store_mod

    provs = {"greenhouse": _Prov("greenhouse"), "join": _Prov("join")}
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: provs[n])
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.delenv("ERGON_DELTA_CRAWL", raising=False)
    monkeypatch.delenv("ERGON_CRAWL_HOST_BUDGET_S", raising=False)
    return provs


def _crawl(fresh_path: Path, build_id: str, *, only=None, exclude=None):
    fresh_path.parent.mkdir(parents=True, exist_ok=True)
    outcome, _cursor = anyio.run(
        lambda: bi._crawl_due(
            100, {}, fresh_path, build_id, 0, False, None, only_sources=only, exclude_sources=exclude
        )
    )
    keys = set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
    return outcome, keys


def _stable_rows(db: Path):
    con = connect(db, read_only=True)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()]
        # Exclude bookkeeping columns that legitimately differ between a fresh crawl and a
        # carry-forward: last_seen/fetched_at/build_id (as the delta-crawl parity test does), plus
        # `rowid` -- SQLite's insertion-order storage key, which depends on the order boards were
        # inserted (fresh-then-carried in the partitioned build vs interleaved in the full crawl).
        # The STABLE posting identity is `id` (UNIQUE), which is compared and must match.
        keep = [c for c in cols if c not in {"rowid", "last_seen", "fetched_at", "build_id"}]
        sel = ",".join(keep)
        rows = con.execute(f"SELECT {sel} FROM jobs ORDER BY id").fetchall()  # noqa: S608
        return keep, [tuple(r) for r in rows]
    finally:
        con.close()


def _tokens_in(db: Path) -> set[str]:
    con = connect(db, read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT board_token FROM jobs")}
    finally:
        con.close()


# --------------------------------------------------------------------------- unit: window partition


def test_registry_window_partition_is_a_clean_split(monkeypatch):
    """exclude(join) and only(join) partition the registry: no overlap, no loss, union == whole."""
    _install(monkeypatch)

    non_join, _ = bi._registry_window(0, 1000, exclude_sources={"join"})
    join_only, _ = bi._registry_window(0, 1000, only_sources={"join"})

    non_join_tokens = {e["token"] for _, e in non_join}
    join_tokens = {e["token"] for _, e in join_only}

    assert non_join_tokens == _NON_JOIN_TOKENS
    assert join_tokens == _JOIN_TOKENS
    assert non_join_tokens & join_tokens == set()  # disjoint
    assert non_join_tokens | join_tokens == _NON_JOIN_TOKENS | _JOIN_TOKENS  # 100% coverage


def test_registry_window_default_is_unchanged(monkeypatch):
    """Both partition args None => byte-identical window to the pre-partition signature (flag off)."""
    _install(monkeypatch)
    with_kwargs, cur_a = bi._registry_window(0, 1000, only_sources=None, exclude_sources=None)
    positional, cur_b = bi._registry_window(0, 1000)
    assert with_kwargs == positional
    assert cur_a == cur_b
    assert {e["token"] for _, e in with_kwargs} == _NON_JOIN_TOKENS | _JOIN_TOKENS


def test_split_sources_and_cursor_filename():
    assert bi._split_sources(None) is None
    assert bi._split_sources("") is None
    assert bi._split_sources("  ") is None
    assert bi._split_sources("Join") == {"join"}
    assert bi._split_sources("join, greenhouse ,,") == {"join", "greenhouse"}
    # default partition keeps the legacy cursor name (byte-identical published asset)
    assert bi._cursor_filename(None) == "crawl_cursor.json"
    assert bi._cursor_filename({"join"}) == "crawl_cursor-join.json"


def test_partition_filter_identity_and_carve():
    items = list(_REGISTRY.items())
    assert bi._partition_filter(items, None, None) == items  # identity
    only_join = bi._partition_filter(items, {"join"}, None)
    assert {e["token"] for _, e in only_join} == _JOIN_TOKENS
    excl_join = bi._partition_filter(items, None, {"join"})
    assert {e["token"] for _, e in excl_join} == _NON_JOIN_TOKENS


# ----------------------------------------------------------------------- end-to-end parity gate


def test_partitioned_crawl_matches_full_crawl(monkeypatch, tmp_path):
    """The two-pass partitioned crawl (non-join full, then join-only) yields the SAME jobs table as
    one un-partitioned full crawl -- proving no board is dropped and every row is byte-identical."""
    provs = _install(monkeypatch)

    # --- PRIOR: one full crawl+build, the shared carry-forward source for both paths below. ---
    prior_fresh = tmp_path / "prior" / "fresh.sqlite"
    _, prior_keys = _crawl(prior_fresh, "prior")
    prior_db = tmp_path / "prior" / "index.sqlite"
    build_index_from_fresh_db(prior_fresh, prior_db, build_id="prior")

    # --- (A) FULL: crawl the whole registry in one pass. ---
    provs["greenhouse"].fetch_calls = provs["join"].fetch_calls = 0
    full_fresh = tmp_path / "full" / "fresh.sqlite"
    _, full_keys = _crawl(full_fresh, "today")
    assert provs["greenhouse"].fetch_calls == 4 and provs["join"].fetch_calls == 3
    full_db = tmp_path / "full" / "index.sqlite"
    build_index_from_fresh_db(
        full_fresh, full_db, build_id="today", prev_db=prior_db, crawled_keys=full_keys
    )

    # --- (B) PARTITIONED pass 1: non-join FULL (join carries forward from prior). ---
    provs["greenhouse"].fetch_calls = provs["join"].fetch_calls = 0
    nj_fresh = tmp_path / "nonjoin" / "fresh.sqlite"
    _, nj_keys = _crawl(nj_fresh, "today-nonjoin", exclude={"join"})
    assert provs["greenhouse"].fetch_calls == 4  # every non-join board crawled
    assert provs["join"].fetch_calls == 0  # join NOT touched in the non-join pass
    nj_db = tmp_path / "nonjoin" / "index.sqlite"
    build_index_from_fresh_db(
        nj_fresh, nj_db, build_id="today-nonjoin", prev_db=prior_db, crawled_keys=nj_keys
    )
    # join boards are present in the non-join index -- carried forward, not dropped.
    assert _tokens_in(nj_db) == _NON_JOIN_TOKENS | _JOIN_TOKENS

    # --- (B) PARTITIONED pass 2: join ONLY (non-join carries forward from pass 1). ---
    provs["greenhouse"].fetch_calls = provs["join"].fetch_calls = 0
    jn_fresh = tmp_path / "join" / "fresh.sqlite"
    _, jn_keys = _crawl(jn_fresh, "today-join", only={"join"})
    assert provs["join"].fetch_calls == 3  # every join board crawled on its shard
    assert provs["greenhouse"].fetch_calls == 0  # non-join NOT re-touched on the join shard
    jn_db = tmp_path / "join" / "index.sqlite"
    build_index_from_fresh_db(
        jn_fresh, jn_db, build_id="today-join", prev_db=nj_db, crawled_keys=jn_keys
    )

    # Partitions are a clean split of the crawl work.
    assert nj_keys & jn_keys == set()
    assert nj_keys | jn_keys == full_keys

    # --- PARITY: the final partitioned index == the full-crawl index, byte-for-byte on data cols. ---
    cols_full, rows_full = _stable_rows(full_db)
    cols_part, rows_part = _stable_rows(jn_db)
    assert cols_full == cols_part
    assert rows_full == rows_part
    assert len(rows_full) == 2 * len(_REGISTRY)  # every board's 2 postings, nothing lost


def test_default_crawl_still_fetches_every_source(monkeypatch, tmp_path):
    """Flag off (no partition env / args): the crawl fetches join AND non-join -- today's behaviour."""
    provs = _install(monkeypatch)
    fresh = tmp_path / "fresh.sqlite"
    _crawl(fresh, "b1")  # only=None, exclude=None
    assert provs["greenhouse"].fetch_calls == 4
    assert provs["join"].fetch_calls == 3
