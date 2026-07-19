"""Stress tests for the freshness-sweep host-sharding partition
(src/ergon_tracker/index/freshness_shard.py, Phase 3).

Everything here is a pure-function test -- no network, no sqlite, no async -- proving the
drain-detail invariant: every board whose fetch contends on the same politeness bucket
(``board_rate_bucket``) lands on exactly one shard, the partition is complete and disjoint, join
gets its own isolated shard, and assignment is deterministic (never Python's salted ``hash()``).
"""

from __future__ import annotations

import hashlib

import pytest

from ergon_tracker.index.freshness_shard import (
    ISOLATED_HOSTS,
    board_host,
    board_rate_bucket,
    shard_boards,
)

# --- board_host: per-source host derivation ----------------------------------------------------


@pytest.mark.parametrize(
    ("source", "token", "expected_host"),
    [
        ("greenhouse", "acme", "boards-api.greenhouse.io"),
        ("lever", "acme", "api.lever.co"),
        ("ashby", "acme", "api.ashbyhq.com"),
        ("workable", "acme", "apply.workable.com"),
        ("jazzhr", "acme", "app.jazz.co"),
        ("rippling", "acme", "api.rippling.com"),
        ("join", "acme", "join.com"),
        ("dejobs", "acme", "prod-search-api.jobsyn.org"),
        ("smartrecruiters", "acme", "api.smartrecruiters.com"),
        ("breezy", "foo", "foo.breezy.hr"),
        ("eightfold", "tenant1", "tenant1.eightfold.ai"),
        ("oracle", "eeho.fa.us2.oraclecloud.com|CX_1", "eeho.fa.us2.oraclecloud.com"),
        ("successfactors", "career25.sapsf.com|siteid|Label", "career25.sapsf.com"),
        ("successfactors", "career25.sapsf.com", "career25.sapsf.com"),
        ("icims", "company.icims.com|new", "company.icims.com"),
        ("icims", "company.icims.com", "company.icims.com"),
    ],
)
def test_board_host_matches_provider_url_convention(source, token, expected_host):
    assert board_host(source, token) == expected_host


def test_board_host_unknown_source_falls_back_to_per_board_bucket():
    # Never guess a shared host for an unrecognized source -- fall back to an isolated
    # per-(source, token) bucket instead.
    assert board_host("some-future-source", "tok") == "some-future-source:tok"


def test_board_host_is_case_and_whitespace_insensitive():
    assert board_host("GreenHouse", " Acme ") == board_host("greenhouse", "acme")


# --- board_rate_bucket: collapses to the SAME key AsyncFetcher's own limiter uses --------------


def test_board_rate_bucket_collapses_shared_backends():
    # breezy.hr is NOT in http._PER_TENANT_HOSTS -- every company's subdomain shares one bucket.
    assert board_rate_bucket("breezy", "foo") == board_rate_bucket("breezy", "bar")


def test_board_rate_bucket_keeps_per_tenant_hosts_separate():
    # eightfold.ai IS in http._PER_TENANT_HOSTS -- each tenant keeps its own bucket.
    assert board_rate_bucket("eightfold", "tenant1") != board_rate_bucket("eightfold", "tenant2")


def test_board_rate_bucket_per_tenant_oracle_hosts_separate():
    a = board_rate_bucket("oracle", "eeho.fa.us2.oraclecloud.com|CX_1")
    b = board_rate_bucket("oracle", "other.fa.us2.oraclecloud.com|CX_1")
    assert a != b


def test_board_rate_bucket_join_is_the_isolated_key():
    assert board_rate_bucket("join", "anything") in ISOLATED_HOSTS


# --- shard_boards: the partition invariants -----------------------------------------------------


def _partition(boards, num_shards):
    return {s: shard_boards(boards, s, num_shards) for s in range(num_shards)}


def _shard_containing(partition, board):
    hits = [s for s, lst in partition.items() if board in lst]
    assert len(hits) == 1, f"{board} found in shards {hits}, expected exactly one"
    return hits[0]


def test_every_board_of_a_shared_host_lands_on_one_shard_greenhouse():
    # greenhouse is a SINGLE shared host across every company -- all boards must be cohesive.
    gh_boards = [("greenhouse", f"company-{i}") for i in range(12)]
    other = [("lever", "acme"), ("ashby", "globex")]
    boards = gh_boards + other
    partition = _partition(boards, num_shards=6)
    target_shard = _shard_containing(partition, gh_boards[0])
    for b in gh_boards[1:]:
        assert _shard_containing(partition, b) == target_shard


def test_every_board_of_a_shared_host_lands_on_one_shard_breezy():
    # breezy.hr collapses across every company's subdomain (see board_rate_bucket test above) --
    # different TOKENS, same real bucket, must still be cohesive.
    breezy_boards = [("breezy", f"company-{i}") for i in range(10)]
    partition = _partition(breezy_boards + [("lever", "acme")], num_shards=5)
    target_shard = _shard_containing(partition, breezy_boards[0])
    for b in breezy_boards[1:]:
        assert _shard_containing(partition, b) == target_shard


def test_per_tenant_host_boards_can_split_across_shards():
    # eightfold tenants are independent buckets -- they are NOT required to land together (unlike
    # breezy above). This just proves the partition doesn't over-conservatively glue them.
    boards = [("eightfold", f"tenant-{i}") for i in range(40)]
    partition = _partition(boards, num_shards=8)
    shards_used = {s for s, lst in partition.items() if lst}
    assert len(shards_used) > 1, "40 independent tenants should spread across more than one shard"


def test_same_eightfold_tenant_stays_together_even_with_multiple_boards():
    # Sanity: two entries for the exact same tenant token are still one bucket -> one shard
    # (duplicate board tuples shouldn't happen in practice, but the invariant must still hold).
    boards = [("eightfold", "tenant-x"), ("eightfold", "tenant-x"), ("eightfold", "tenant-y")]
    partition = _partition(boards, num_shards=4)
    tx_shards = {s for s, lst in partition.items() for b in lst if b == ("eightfold", "tenant-x")}
    assert len(tx_shards) == 1


def test_join_gets_its_own_isolated_shard():
    join_boards = [("join", f"company-{i}") for i in range(25)]
    other_boards = [("greenhouse", "acme"), ("lever", "globex"), ("breezy", "initech")]
    boards = join_boards + other_boards
    partition = _partition(boards, num_shards=5)

    join_shard = _shard_containing(partition, join_boards[0])
    assert join_shard == 0  # reserved slot, per the design doc

    # Every join board lands on that shard...
    for b in join_boards:
        assert _shard_containing(partition, b) == join_shard
    # ...and NOTHING else does: shard 0 is exclusively join's.
    assert set(partition[join_shard]) == set(join_boards)
    # No other-source board was pushed onto shard 0 by hash coincidence.
    for other in other_boards:
        assert _shard_containing(partition, other) != join_shard


def test_join_isolation_holds_across_shard_counts():
    join_boards = [("join", f"co-{i}") for i in range(5)]
    other_boards = [("greenhouse", f"co-{i}") for i in range(5)]
    boards = join_boards + other_boards
    for num_shards in (2, 3, 7, 20):
        partition = _partition(boards, num_shards)
        assert set(partition[0]) == set(join_boards), f"failed at num_shards={num_shards}"


def test_single_shard_contains_everything_join_included():
    boards = [("join", "a"), ("greenhouse", "b"), ("breezy", "c")]
    only = shard_boards(boards, 0, 1)
    assert set(only) == set(boards)


def test_partition_is_complete_and_disjoint():
    boards = (
        [("greenhouse", f"gh-{i}") for i in range(6)]
        + [("lever", f"lv-{i}") for i in range(4)]
        + [("breezy", f"bz-{i}") for i in range(8)]
        + [("eightfold", f"ef-{i}") for i in range(15)]
        + [("join", f"jn-{i}") for i in range(20)]
        + [("oracle", f"host{i}.fa.us2.oraclecloud.com|CX_1") for i in range(5)]
    )
    num_shards = 9
    partition = _partition(boards, num_shards)

    all_seen: list[tuple[str, str]] = []
    for lst in partition.values():
        all_seen.extend(lst)

    # Disjoint: no board appears on more than one shard.
    assert len(all_seen) == len(boards)
    # Complete: the union covers every input board exactly once.
    assert sorted(all_seen) == sorted(boards)


def test_shard_boards_preserves_relative_order():
    boards = [("greenhouse", f"gh-{i}") for i in range(5)] + [("lever", "acme")]
    result = shard_boards(boards, 0, 1)  # single shard -> full list, order preserved
    assert result == boards


def test_shard_boards_is_deterministic_across_calls():
    boards = [("smartrecruiters", f"co-{i}") for i in range(30)] + [
        ("icims", f"host{i}.icims.com") for i in range(10)
    ]
    first = _partition(boards, 6)
    second = _partition(boards, 6)
    assert first == second


def test_shard_assignment_is_not_pythons_salted_hash():
    # Reproduce the assignment from first principles (sha1, not builtin hash()) to guard against a
    # regression to PYTHONHASHSEED-salted hash() -- which would make shard membership disagree
    # across independent CI matrix processes.
    boards = [("greenhouse", "acme"), ("lever", "globex"), ("ashby", "initech")]
    num_shards = 4
    partition = _partition(boards, num_shards)
    for source, token in boards:
        bucket = board_rate_bucket(source, token)
        expected = 1 + (
            int(hashlib.sha1(bucket.encode("utf-8")).hexdigest(), 16) % (num_shards - 1)
        )
        assert _shard_containing(partition, (source, token)) == expected


# --- shard_boards: argument validation -----------------------------------------------------------


def test_shard_boards_rejects_negative_shard():
    with pytest.raises(ValueError):
        shard_boards([("greenhouse", "acme")], -1, 4)


def test_shard_boards_rejects_shard_out_of_range():
    with pytest.raises(ValueError):
        shard_boards([("greenhouse", "acme")], 4, 4)


def test_shard_boards_rejects_zero_num_shards():
    with pytest.raises(ValueError):
        shard_boards([("greenhouse", "acme")], 0, 0)


def test_shard_boards_empty_boards_list_is_fine():
    assert shard_boards([], 0, 5) == []
