"""Host-sharding partition for the daily freshness sweep (Phase 3, standalone CLI companion to
``freshness.py``; see ``docs/superpowers/specs/2026-07-18-daily-freshness-sweep-design.md``).

WHY: the design's approved workflow shape is a 20-way GitHub Actions matrix, one process per
shard, each running ``scripts/freshness_sweep.py`` against a slice of the ~58k-board registry.
``AsyncFetcher``'s per-host token bucket (``http.py``'s ``_rate_key``-keyed limiter) is a
PER-PROCESS structure -- it has no idea another shard's process exists. If two boards that hit
the SAME real backend (the same rate-limit bucket) landed on two different shards, each shard's
own token bucket would believe it owns the full request budget against that backend, and the
backend would see up to ``num_shards``x the intended real request rate: a silent self-inflicted
ban. So the partition invariant is: **every board whose fetch contends on the same politeness
bucket must land on exactly one shard.**

This mirrors ``index/detail.py``'s Tier-3 drain sharding (``rate_key_for_host`` / ``MEGAHOST_SHARDS``
/ ``_shard_of``) byte-for-byte in spirit -- reused here, not re-derived, via ``rate_key_for_host``
(the same public wrapper around ``http._rate_key`` the drain uses) -- but freshness boards are
``(source, board_token)`` pairs with no ``apply_url``/``listing_url`` to read a host off of (unlike
a Tier-3 ``DetailRef``), so this module supplies the missing piece: ``board_host``, a per-source
mapping from a board's token to the literal network host its provider's ``fetch()`` call will hit
(read off each provider's own URL template/token-parsing convention -- see the dispatch tables
below), which is then run through the SAME ``rate_key_for_host`` collapse the drain uses so the two
sharding schemes can never disagree about what counts as "the same backend".

SPECIAL CASE -- join.com: per the design doc, join is ~19,937 boards (~1/3 of the deterministic
board count) on one shared host at a measured ~5-7 req/s ceiling -- a ~50 minute wall-clock floor
that no amount of concurrency can shorten (the ceiling is per-request, not per-connection). The
design calls for join to get its OWN dedicated shard so its floor overlaps the rest of the sweep
instead of extending it. Unlike ``detail.py``'s ``MEGAHOST_SHARDS`` (which pins a handful of large
buckets to specific shard numbers but tolerates an unrelated hashed bucket landing on the same
shard by coincidence -- fine there, since the drain only needs "no bucket split", not "no bucket
sharing"), ``ISOLATED_HOSTS`` here is carved out of the hash range entirely (shard 0 is RESERVED,
never a hash target for anything else) so join's shard is guaranteed to contain nothing but join,
by construction, not by hash luck.

LOAD-BALANCE (crawl map/reduce): the remaining HEAVY single buckets (smartrecruiters, greenhouse,
workable, ... -- each one unsplittable politeness bucket carrying a large job volume) are PINNED to
distinct non-join shards via ``CRAWL_MEGAHOST_SHARDS``, exactly like ``detail.py``'s
``MEGAHOST_SHARDS`` -- because left to the hash several of them collided onto ONE shard (crawl run
30236311320: shard 5 = 640k jobs, no map/reduce speedup). Pinning is a pure load optimization layered
UNDER the same partition invariant: a pinned bucket is still ONE bucket on ONE shard, so no host is
split and the K-shard union is still the whole board set. Note ``board_host`` also reconstructs
workday's per-tenant ``*.myworkdayjobs.com`` host (the single biggest source, ~580k jobs) so it
spreads across its ~2,249 tenant buckets by the hash instead of collapsing to one -- see there.
"""

from __future__ import annotations

import hashlib

from .detail import rate_key_for_host

__all__ = [
    "CRAWL_MEGAHOST_SHARDS",
    "ISOLATED_HOSTS",
    "board_host",
    "board_rate_bucket",
    "board_shard",
    "shard_boards",
]

# Sources whose provider ``fetch()`` hits a SINGLE fixed host for every board of that source --
# the board token is a path/query parameter, never a subdomain (read off each provider's own
# ``_API``/``_FEED``/``_CAREERS`` URL template, see providers/{name}.py).
_FIXED_HOST_SOURCES: dict[str, str] = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
    "workable": "apply.workable.com",
    "jazzhr": "app.jazz.co",
    "rippling": "api.rippling.com",
    "join": "join.com",
    "dejobs": "prod-search-api.jobsyn.org",
    "smartrecruiters": "api.smartrecruiters.com",
}

# Sources whose token IS the per-company subdomain slug -- the board's real host is a simple
# string-format of the token (providers/breezy.py's ``_API = "https://{token}.breezy.hr/json"``,
# providers/eightfold.py's ``_API = "https://{tenant}.eightfold.ai/..."`` where ``tenant`` IS the
# token). NOTE: this is the raw fetch host, not yet the rate-limit bucket -- ``board_rate_bucket``
# runs it through ``rate_key_for_host`` next, which collapses breezy's (not per-tenant-listed)
# subdomains to the single shared ``breezy.hr`` bucket, while leaving eightfold's (per-tenant-listed
# in ``http._PER_TENANT_HOSTS``) subdomains each their own bucket. Getting that collapse right is
# exactly why this module reuses ``rate_key_for_host`` instead of hand-rolling a "looks the same
# source -> looks the same bucket" assumption.
_SUBDOMAIN_TOKEN_SOURCES: dict[str, str] = {
    "breezy": "{token}.breezy.hr",
    "eightfold": "{token}.eightfold.ai",
}

# Sources whose token is a composite ``"{host}|..."`` string (see each provider's own ``matches()``/
# ``_split``/``_resolve``): oracle.py's ``"{host}|{siteNumber}"``, successfactors.py's ``"{host}"``
# / ``"{host}|{siteid}"`` / ``"{host}|{siteid}|{company}"``, icims.py's ``"{host}"`` /
# ``"{host}|new"`` / ``"{host}|classic"``. The board's real host is always the first "|"-delimited
# component.
_HOST_PREFIXED_TOKEN_SOURCES: frozenset[str] = frozenset({"oracle", "successfactors", "icims"})

# Workday: the token is a composite ``"{tenant}|{wd}|{site}"`` (providers/workday.py's ``matches()``)
# and the real fetch host is RECONSTRUCTED from its first two components as
# ``"{tenant}.{wd}.myworkdayjobs.com"`` (see workday.py's ``_HOST_RE`` / URL template
# ``https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs``). Unlike the
# host-prefixed sources above, the host is not a literal token prefix -- it has to be rebuilt.
# CRITICAL: without this branch workday falls through to the per-board fallback, whose ``"{src}:{tok}"``
# string is then PORT-STRIPPED by ``rate_key_for_host`` (``_rate_key`` splits on ``:``) down to the
# single literal bucket ``"workday"`` -- collapsing ALL ~580k workday jobs (the single biggest source,
# ~40% of the index) onto ONE shard. Reconstructing the real host instead lets ``rate_key_for_host``
# keep each tenant its own bucket (``myworkdayjobs.com`` is in ``http._PER_TENANT_HOSTS``), spreading
# workday across ~2,249 buckets exactly as AsyncFetcher already rate-limits it per tenant.
_WORKDAY_SOURCE = "workday"
_WORKDAY_HOST = "{tenant}.{wd}.myworkdayjobs.com"


def board_host(source: str, token: str) -> str:
    """The literal network host a ``(source, token)`` board's provider ``fetch()`` call hits --
    i.e. ``urlsplit(url).netloc`` for the URL that call makes, WITHOUT yet collapsing to the
    ``_rate_key`` politeness bucket (see ``board_rate_bucket`` for that).

    A source outside the three dispatch tables above (i.e. not one of the 14 sources
    ``freshness.py``'s ``DETERMINISTIC_SOURCES | SEARCH_INDEX_SOURCES`` actually sweeps) has no
    verified host-derivation rule here, so it falls back to a per-board bucket
    (``"{source}:{token}"``) rather than guessing a shared host -- safe by construction (an
    unrecognized source is never wrongly assumed to share a backend with another board), and inert
    in practice since the sweep engine itself only ever fetches the 14 known sources.
    """
    src = source.strip().lower()
    tok = token.strip().lower()
    if src in _FIXED_HOST_SOURCES:
        return _FIXED_HOST_SOURCES[src]
    if src in _SUBDOMAIN_TOKEN_SOURCES:
        return _SUBDOMAIN_TOKEN_SOURCES[src].format(token=tok)
    if src in _HOST_PREFIXED_TOKEN_SOURCES:
        return tok.split("|", 1)[0].strip()
    if src == _WORKDAY_SOURCE:
        parts = tok.split("|")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return _WORKDAY_HOST.format(tenant=parts[0], wd=parts[1])
        return f"{src}:{tok}"  # malformed token -- isolated per-board bucket, never guess a host
    return f"{src}:{tok}"


def board_rate_bucket(source: str, token: str) -> str:
    """The politeness bucket a ``(source, token)`` board's fetch will actually contend on --
    ``rate_key_for_host(board_host(source, token))``. THIS, not the raw host, is the correct
    sharding key: it is the exact string ``AsyncFetcher`` keys its per-process token bucket on
    (mirrors ``index/detail.py``'s Tier-3 drain sharding, same reused ``rate_key_for_host``), so
    two boards that collapse to the same bucket here are guaranteed to collapse to the same
    real-world backend limiter too.
    """
    return rate_key_for_host(board_host(source, token))


# Rate buckets that get their own RESERVED shard (shard 0), carved out of the hash range entirely
# -- see the module docstring's join.com special case. A bucket in this set is the only bucket
# on shard 0, and shard 0 never receives any other bucket via the hash fallback.
ISOLATED_HOSTS: frozenset[str] = frozenset({"join.com"})

# HEAVY COLLAPSED buckets PINNED to a fixed NON-JOIN shard each, mirroring ``detail.py``'s
# ``MEGAHOST_SHARDS`` (same "pin the big single buckets so hash luck can't pile them onto one shard"
# intent). Unlike per-tenant sources (workday/oracle/icims/eightfold/successfactors -- many buckets,
# already spread by the hash), each of these is a SINGLE unsplittable politeness bucket carrying a
# large job volume, so left to the hash several collided onto ONE shard (measured on crawl run
# 30236311320: shard 5 = 640k jobs / 40% / 193 min, no map/reduce speedup at all). Pinning each to a
# DISTINCT shard spreads them so the map wall-clock floor drops from that 640k monolith toward the
# single heaviest UNSPLITTABLE bucket (smartrecruiters ~152k) plus the shard's unavoidable hashed
# baseline (~workday-per-tenant + long-tail), i.e. ~220k / ~15% -- a ~2.9x cut.
#
# The assignment is a longest-processing-time bin-pack (heaviest bucket onto the shard with the
# lowest projected hashed baseline) over the measured ``coverage.json`` volumes (build-2026-07-27):
#   smartrecruiters.com 152791 | greenhouse.io 134856 | workable.com 65874 | jazz.co 58461 (jazzhr)
#   lever.co 58273 | ashbyhq.com 49132 | apicapture 30484 | breezy.hr 28091 | jobsyn.org 27305 (dejobs)
#   rippling.com 14046
# Shard numbers are the DISTINCT non-join targets (1..NUM_SHARDS-1); the exact number matters only
# for balance -- correctness (no split, complete+disjoint partition) holds for ANY assignment.
CRAWL_MEGAHOST_SHARDS: dict[str, int] = {
    "smartrecruiters.com": 10,
    "greenhouse.io": 6,
    "workable.com": 5,
    "jazz.co": 8,
    "lever.co": 7,
    "ashbyhq.com": 3,
    "apicapture": 4,
    "breezy.hr": 9,
    "jobsyn.org": 1,
    "rippling.com": 2,
}


def _pinned_shard(pin: int, num_shards: int) -> int:
    """Map a ``CRAWL_MEGAHOST_SHARDS`` pin onto a concrete NON-JOIN shard for this ``num_shards``.

    Folded into ``1 .. num_shards - 1`` (never shard 0, which is join's reserved slot) so a pin is
    always a valid non-join shard regardless of how ``num_shards`` compares to the pin table -- the
    same defensive ``% num_shards`` spirit as ``detail._shard_of``, but shifted off shard 0."""
    return 1 + ((pin - 1) % (num_shards - 1))


def _shard_for_bucket(bucket: str, num_shards: int) -> int:
    """Deterministic shard assignment for one politeness bucket.

    An ``ISOLATED_HOSTS`` bucket always maps to shard 0. A ``CRAWL_MEGAHOST_SHARDS`` bucket maps to
    its PINNED non-join shard. Every other bucket hashes (SHA-1 -- NOT Python's built-in ``hash()``,
    which is salted per-process via ``PYTHONHASHSEED`` and would make shard assignment disagree
    across the matrix's independent processes) into shards ``1 .. num_shards - 1`` when
    ``num_shards > 1`` (so shard 0 is exclusively join's), or into the single shard 0 when
    ``num_shards == 1`` (nothing to isolate FROM, nothing to spread ACROSS).
    """
    if bucket in ISOLATED_HOSTS:
        return 0
    if num_shards <= 1:
        return 0
    pin = CRAWL_MEGAHOST_SHARDS.get(bucket)
    if pin is not None:
        return _pinned_shard(pin, num_shards)
    bucket_count = num_shards - 1
    digest = hashlib.sha1(bucket.encode("utf-8")).hexdigest()
    return 1 + (int(digest, 16) % bucket_count)


def board_shard(source: str, token: str, num_shards: int) -> int:
    """The shard a ``(source, token)`` board is assigned to -- the same host-bucketed partition
    ``shard_boards`` applies, exposed per-board so a caller can filter an arbitrary iterable of
    boards (e.g. the crawl's rotating window) without materializing the whole ``(source, token)``
    list. ``board_shard(s, t, K) == i`` iff ``(s, t)`` is in ``shard_boards([...], i, K)``.
    """
    return _shard_for_bucket(board_rate_bucket(source, token), num_shards)


def shard_boards(
    boards: list[tuple[str, str]], shard: int, num_shards: int
) -> list[tuple[str, str]]:
    """Partition ``boards`` (``(source, board_token)`` pairs) so every board whose fetch contends
    on the same politeness bucket (``board_rate_bucket``) lands on exactly ONE shard -- the
    drain-detail invariant (see module docstring). Returns only this ``shard``'s slice, in the
    same relative order as ``boards``.

    Pure and deterministic: the same ``boards``/``shard``/``num_shards`` always produces the same
    result, in-process or across independent processes (no reliance on Python's salted ``hash()``),
    which is what lets 20 independent GitHub Actions matrix jobs each compute their own slice
    without any shared coordination.
    """
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    if not (0 <= shard < num_shards):
        raise ValueError(f"shard must be in [0, {num_shards}), got {shard}")
    return [
        (s, t) for s, t in boards if _shard_for_bucket(board_rate_bucket(s, t), num_shards) == shard
    ]
