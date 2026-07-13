"""Sharded parallel drain: the shard-key primitives (`_rate_bucket_for_ref`, `_shard_of`,
`MEGAHOST_SHARDS`) and `reconcile_detail_tier`'s `shard`/`num_shards` filtering.

THE CORRECTNESS INVARIANT under test: shard assignment is a pure, stable function of a ref's
politeness bucket (`_rate_key`-derived), so every rate-bucket lives on exactly one shard and the
non-sharded path is completely unaffected.
"""

from __future__ import annotations

import sqlite3

import anyio

from ergon_tracker.index.detail import (
    MEGAHOST_SHARDS,
    DetailRef,
    _host_for_ref,
    _rate_bucket_for_ref,
    _ref_in_shard,
    _shard_of,
    open_detail,
    rate_key_for_host,
    reconcile_detail_tier,
)

NUM_SHARDS = 4


def _ref(id_: str, source: str, apply_url: str | None, listing_url: str | None = None) -> DetailRef:
    return DetailRef(
        id=id_,
        source=source,
        token=None,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="sig",
    )


# --- _host_for_ref / _rate_bucket_for_ref -------------------------------------------------------


def test_host_for_ref_prefers_apply_url_then_listing_url():
    r1 = _ref("1", "oracle", "https://foo.oraclecloud.com/job/1", "https://other.example/1")
    assert _host_for_ref(r1) == "foo.oraclecloud.com"
    r2 = _ref("2", "oracle", None, "https://bar.oraclecloud.com/job/2")
    assert _host_for_ref(r2) == "bar.oraclecloud.com"
    r3 = _ref("3", "oracle", None, None)
    assert _host_for_ref(r3) is None


def test_rate_bucket_reuses_http_rate_key_collapsing_subdomains():
    # apply.workable.com and jobs.workable.com must collapse to the SAME bucket as workable.com
    # (the whole point: two shards must never independently rate-limit the same collapsed host).
    a = _ref("a", "workable", "https://apply.workable.com/j/1")
    b = _ref("b", "workable", "https://jobs.workable.com/j/2")
    assert _rate_bucket_for_ref(a) == _rate_bucket_for_ref(b) == "workable.com"
    assert _rate_bucket_for_ref(a) == rate_key_for_host("apply.workable.com")


def test_rate_bucket_per_tenant_host_stays_distinct():
    # Workday tenants are per-tenant hosts (_PER_TENANT_HOSTS) -- must NOT collapse together.
    a = _ref("a", "workday", "https://acme.myworkdayjobs.com/en-US/External/job/1")
    b = _ref("b", "workday", "https://other.myworkdayjobs.com/en-US/External/job/2")
    assert _rate_bucket_for_ref(a) != _rate_bucket_for_ref(b)


def test_rate_bucket_falls_back_to_source_when_no_url():
    r = _ref("1", "phenom", None, None)
    assert _rate_bucket_for_ref(r) == "source:phenom"


# --- _shard_of -----------------------------------------------------------------------------------


def test_megahost_pinned_shards_are_used():
    for host, pinned in MEGAHOST_SHARDS.items():
        assert _shard_of(host, NUM_SHARDS) == pinned % NUM_SHARDS


def test_shard_of_is_stable_hash_not_salted_hash():
    # hashlib.sha1-based -- deterministic across repeated calls (unlike Python's salted hash()).
    key = "some-random-host.example.com"
    results = {_shard_of(key, NUM_SHARDS) for _ in range(20)}
    assert len(results) == 1
    assert 0 <= next(iter(results)) < NUM_SHARDS


def test_shard_of_stable_across_process_like_reimport():
    # sha1 of the same string always yields the same digest regardless of PYTHONHASHSEED --
    # simulate "another process" by hashing independently via hashlib directly and comparing.
    import hashlib

    key = "greenhouse.io"
    expected = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % NUM_SHARDS
    assert _shard_of(key, NUM_SHARDS) == expected


# --- exactly-one-shard / no-loss over many synthetic refs ----------------------------------------


def _synthetic_refs(n: int) -> list[DetailRef]:
    hosts = [
        "smartrecruiters.com",
        "boards.smartrecruiters.com",  # megahost, should collapse+pin
        "apply.workable.com",
        "workable.com",  # megahost, should collapse+pin
        "acme.oraclecloud.com",
        "other.oraclecloud.com",  # megahost (default pin)
        "join.com",
        "careers.join.com",
        "icims.com",
        "acme-careers.icims.com",
        "acme.myworkdayjobs.com",
        "other.myworkdayjobs.com",  # per-tenant, NOT a megahost
        "boards.greenhouse.io",
        "jobs.lever.co",
        "eightfold.ai",
        "radancy.com",
    ]
    refs = []
    for i in range(n):
        host = hosts[i % len(hosts)]
        refs.append(_ref(str(i), "src", f"https://{host}/job/{i}"))
    return refs


def test_each_ref_lands_in_exactly_one_shard():
    refs = _synthetic_refs(200)
    assignments = [_shard_of(_rate_bucket_for_ref(r), NUM_SHARDS) for r in refs]
    for a in assignments:
        assert 0 <= a < NUM_SHARDS


def test_megahost_refs_all_land_on_the_pinned_shard():
    refs = _synthetic_refs(200)
    for r in refs:
        bucket = _rate_bucket_for_ref(r)
        if bucket in MEGAHOST_SHARDS:
            assert _shard_of(bucket, NUM_SHARDS) == MEGAHOST_SHARDS[bucket] % NUM_SHARDS
    # spot-check the two smartrecruiters hosts collapse to the SAME bucket + pinned shard
    a = _rate_bucket_for_ref(_ref("x", "smartrecruiters", "https://smartrecruiters.com/j/1"))
    b = _rate_bucket_for_ref(_ref("y", "smartrecruiters", "https://boards.smartrecruiters.com/j/2"))
    assert a == b == "smartrecruiters.com"
    assert (
        _shard_of(a, NUM_SHARDS)
        == _shard_of(b, NUM_SHARDS)
        == MEGAHOST_SHARDS["smartrecruiters.com"] % NUM_SHARDS
    )


def test_union_of_all_shards_covers_every_ref_no_loss_no_dup():
    refs = _synthetic_refs(500)
    by_shard: dict[int, list[DetailRef]] = {s: [] for s in range(NUM_SHARDS)}
    for r in refs:
        by_shard[_shard_of(_rate_bucket_for_ref(r), NUM_SHARDS)].append(r)
    union_ids = sorted(int(r.id) for shard_refs in by_shard.values() for r in shard_refs)
    assert union_ids == list(range(500))  # every ref present exactly once across all shards


def test_shard_assignment_stable_across_repeated_calls():
    refs = _synthetic_refs(100)
    first = [_shard_of(_rate_bucket_for_ref(r), NUM_SHARDS) for r in refs]
    second = [_shard_of(_rate_bucket_for_ref(r), NUM_SHARDS) for r in refs]
    assert first == second


def test_ref_in_shard_matches_shard_of():
    refs = _synthetic_refs(50)
    for r in refs:
        expected = _shard_of(_rate_bucket_for_ref(r), NUM_SHARDS)
        for s in range(NUM_SHARDS):
            assert _ref_in_shard(r, s, NUM_SHARDS) == (expected == s)


# --- reconcile_detail_tier integration: shard filtering + non-sharded path unchanged -------------


def _mk_index(tmp_path, rows):
    p = tmp_path / "index.sqlite"
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE jobs (id TEXT, source TEXT, board_token TEXT, apply_url TEXT, "
        "listing_url TEXT, content_hash TEXT, snippet TEXT, "
        "salary_min REAL, salary_max REAL, years_min INTEGER)"
    )
    c.executemany(
        "INSERT INTO jobs (id,source,apply_url,content_hash,snippet) VALUES (?,?,?,?,?)", rows
    )
    c.commit()
    c.close()
    return str(p)


def test_reconcile_shard_only_fetches_its_own_refs(tmp_path):
    hosts = [
        "smartrecruiters.com",
        "workable.com",
        "acme.myworkdayjobs.com",
        "other.myworkdayjobs.com",
    ]
    rows = [
        (str(i), "src", f"https://{hosts[i % len(hosts)]}/job/{i}", f"h{i}", None)
        for i in range(40)
    ]
    idx = _mk_index(tmp_path, rows)

    fetched_per_shard: dict[int, list[str]] = {}
    for shard in range(NUM_SHARDS):
        det = str(tmp_path / f"detail-{shard}.sqlite")
        calls: list[str] = []

        async def fake(ref, calls=calls):
            calls.append(ref.id)
            return "<p>Salary: $100,000 / year</p>"

        anyio.run(
            lambda shard=shard, det=det, fake=fake: reconcile_detail_tier(
                det, idx, fetch_detail=fake, now=lambda: "t", shard=shard, num_shards=NUM_SHARDS
            )
        )
        fetched_per_shard[shard] = calls

    # No id appears in more than one shard's fetch set -- exactly one shard fetches each ref.
    all_fetched: list[str] = [i for ids in fetched_per_shard.values() for i in ids]
    assert len(all_fetched) == len(set(all_fetched))
    # Union across all 4 shards recovers every candidate row (no loss).
    assert sorted(all_fetched, key=int) == [str(i) for i in range(40)]


def test_reconcile_non_sharded_path_is_unaffected_by_shard_default(tmp_path):
    rows = [
        (str(i), "smartrecruiters", f"https://smartrecruiters.com/job/{i}", f"h{i}", None)
        for i in range(5)
    ]
    idx = _mk_index(tmp_path, rows)

    async def fake(ref):
        return "<p>Salary: $100,000 / year</p>"

    det_a = str(tmp_path / "a.sqlite")
    det_b = str(tmp_path / "b.sqlite")
    stats_default = anyio.run(
        lambda: reconcile_detail_tier(det_a, idx, fetch_detail=fake, now=lambda: "t")
    )
    stats_explicit_none = anyio.run(
        lambda: reconcile_detail_tier(
            det_b, idx, fetch_detail=fake, now=lambda: "t", shard=None, num_shards=None
        )
    )
    assert stats_default == stats_explicit_none == {"fetched": 5, "failed": 0, "missing": 0}
    con_a = open_detail(det_a)
    con_b = open_detail(det_b)
    got_a = con_a.execute("SELECT id FROM job_detail ORDER BY id").fetchall()
    got_b = con_b.execute("SELECT id FROM job_detail ORDER BY id").fetchall()
    assert got_a == got_b


def test_reconcile_shard_requires_both_shard_and_num_shards(tmp_path):
    idx = _mk_index(tmp_path, [("1", "oracle", "https://oraclecloud.com/1", "h1", None)])
    det = str(tmp_path / "detail.sqlite")

    async def fake(ref):
        return "<p>Salary: $1 / year</p>"

    import pytest

    with pytest.raises(ValueError):
        anyio.run(
            lambda: reconcile_detail_tier(det, idx, fetch_detail=fake, now=lambda: "t", shard=0)
        )
    with pytest.raises(ValueError):
        anyio.run(
            lambda: reconcile_detail_tier(
                det, idx, fetch_detail=fake, now=lambda: "t", num_shards=4
            )
        )
    with pytest.raises(ValueError):
        anyio.run(
            lambda: reconcile_detail_tier(
                det, idx, fetch_detail=fake, now=lambda: "t", shard=4, num_shards=4
            )
        )
