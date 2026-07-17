"""Stratified JD-bearing crawl CLI (Filter Benchmark v2, Task 3).

Builds ``bench/corpus_jd.jsonl`` by live-crawling a stratified sample of companies across the
JD-in-bulk provider set (greenhouse, ashby, lever, recruitee, teamtailor, pinpoint, jazzhr,
dejobs, join, personio, workable) -- sources whose board/list fetch either carries the JD
directly, or (verified against the real provider code, not assumed from the name) carries it in
``description_html`` rather than ``description_text``, or (join/workable) carries NO description
in the bulk response at all and needs a per-posting/per-board Tier-3 detail call
(``BaseProvider.fetch_detail``) to recover it. All three cases are handled uniformly by
``_crawl_one``'s fallback ladder: ``description_text`` -> ``html_to_text(description_html)`` ->
``provider.fetch_detail(...)`` (a no-op, no-network ``None`` for providers that don't implement
it, e.g. greenhouse/ashby/lever, so the ladder costs nothing extra for sources that never need
it). Enterprise ATSes (icims/oracle/successfactors/smartrecruiters/eightfold/...) are excluded
from the crawl TARGET selection (``JD_IN_BULK_PROVIDERS`` / ``select_targets``) entirely -- they
were never claimed to be bulk-JD sources, so there's no board-list budget to spend on them here.

    python -m scripts.bench.crawl_corpus --out bench/corpus_jd.jsonl --total 15000 --floor 150

``--total``/``--floor`` are a ROW budget: ``--total`` bounds the number of corpus rows written in
total, ``--floor`` bounds the minimum rows guaranteed per provider (capped at that provider's
realized availability). A board-picker (``select_targets``, unchanged/still company-count based)
first chooses which boards to crawl -- sized generously off ``ROWS_PER_BOARD_EST`` so there's
enough board-level supply to fill the row budget -- and then a per-board keep cap (derived from a
per-provider row budget via :func:`scripts.bench.strata.allocate`) bounds how many JD-bearing rows
each board contributes while crawling. Because boards run concurrently and each may independently
hit its cap, the per-provider total can overshoot slightly; a final deterministic trim (stable sort
by id) brings every provider down to its row budget and the corpus down to ``--total`` overall.
Realized per-provider counts (kept/dropped/duplicate/errored/trimmed) are always printed — nothing
is silently truncated.
"""

from __future__ import annotations

import argparse
import functools
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

import anyio

from ergon_tracker.extract.base import html_to_text
from ergon_tracker.http import AsyncFetcher
from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import DetailFetch, JobPosting, RawJob, SearchQuery
from ergon_tracker.providers.base import get_provider, load_builtins

from .schema import corpus_row, write_jsonl
from .strata import allocate

__all__ = [
    "JD_IN_BULK_PROVIDERS",
    "ROWS_PER_BOARD_EST",
    "MIN_BOARDS_PER_PROVIDER",
    "select_targets",
    "row_from_job",
    "per_board_caps",
    "trim_to_row_budget",
    "enforce_total_cap",
    "crawl_corpus",
    "main",
]

# Providers whose list/bulk fetch already carries full JD text -- see module docstring.
JD_IN_BULK_PROVIDERS: tuple[str, ...] = (
    "greenhouse",
    "ashby",
    "lever",
    "recruitee",
    "teamtailor",
    "pinpoint",
    "jazzhr",
    "dejobs",
    "join",
    "personio",
    "workable",
)

# Rough JD-bearing-postings-per-board estimate used only to size the BOARD budget generously
# enough that the row budget below can actually be filled -- not a promise, just supply padding.
ROWS_PER_BOARD_EST = 40

# Every JD-in-bulk provider gets at least this many boards selected, even if the row-target-derived
# board budget would otherwise round down to fewer -- keeps small `--total` smoke runs stratified
# across all providers instead of collapsing onto one or two.
MIN_BOARDS_PER_PROVIDER = 3


class _CompanyRegistry(Protocol):
    """Structural contract for the registry ``select_targets`` reads company/ATS data from.

    Matches ``ergon_tracker.registry.store.SeedRegistry`` (``.all() -> {company_key: {"ats",
    "token", "domain"}}``) without importing it, so the pure helper is unit-testable against a
    tiny synthetic stub instead of the real 58k-company packaged registry.
    """

    def all(self) -> dict[str, dict[str, Any]]: ...


def select_targets(registry: _CompanyRegistry, total: int, floor: int) -> dict[str, list[str]]:
    """Provider -> chosen ATS board tokens, stratified across every JD-in-bulk provider.

    Restricted to :data:`JD_IN_BULK_PROVIDERS` (the only sources whose list fetch carries JD
    text without a per-posting detail call). Counts available companies per provider in
    ``registry``, runs :func:`scripts.bench.strata.allocate` over those counts, then returns
    that many board tokens per provider — sorted by company key for determinism, so reruns pick
    the same sample. A provider that has candidates in ``registry`` is never silently missing
    from the output: ``allocate`` guarantees every stratum with available>0 gets at least
    ``min(available, floor)`` (bounded by ``total`` in the degenerate over-budget case).
    """
    tokens_by_ats: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for company_key, entry in registry.all().items():
        ats = entry.get("ats")
        token = entry.get("token")
        if ats in JD_IN_BULK_PROVIDERS and token:
            tokens_by_ats[ats].append((company_key, token))

    available = {ats: len(pairs) for ats, pairs in tokens_by_ats.items()}
    allocation = allocate(available, total, floor)

    out: dict[str, list[str]] = {}
    for ats, n in allocation.items():
        pairs = sorted(tokens_by_ats[ats], key=lambda kv: kv[0])
        out[ats] = [token for _, token in pairs[:n]]
    return out


def _salary_to_dict(job: JobPosting) -> dict[str, Any] | None:
    salary = job.salary
    if salary is None or (salary.min_amount is None and salary.max_amount is None):
        return None
    return {
        "min": salary.min_amount,
        "max": salary.max_amount,
        "currency": salary.currency,
        "interval": salary.interval.value if salary.interval else None,
    }


def row_from_job(job: JobPosting, source: str) -> dict[str, Any]:
    """A normalized ``JobPosting`` -> a ``corpus_row`` dict.

    Carries the provider-STATED ``employment_type``/``remote`` straight off the normalized
    posting: neither is ever inferred by the extractor pipeline (no registered extractor for
    either -- see ``predict.py``'s NOTE), so the corpus must record exactly what the ATS gave us
    rather than leave it for (nonexistent) enrichment to fill in later. ``id`` is
    ``"<source>:<source_job_id>"`` (NOT ``job.id``, which is an opaque hash) so downstream
    provider-matrix grouping can recover ``source`` from the id prefix even if it's dropped
    along the way.
    """
    loc = job.locations[0] if job.locations else None
    location_raw = ((loc.raw or loc.as_text()) if loc else "") or ""
    return corpus_row(
        id=f"{source}:{job.source_job_id}",
        source=source,
        company=job.company,
        title=job.title,
        description_text=job.description_text or "",
        location_raw=location_raw,
        structured_salary=_salary_to_dict(job),
        apply_url=job.apply_url or "",
        employment_type=job.employment_type.value,
        remote=job.remote.value,
    )


async def _recover_detail_text(
    provider: Any, ats: str, token: str, job: JobPosting, fetcher: AsyncFetcher
) -> str | None:
    """Last-resort Tier-3 per-posting JD recovery for a posting whose bulk fetch gave no text.

    ``BaseProvider.fetch_detail`` is opt-in (default: unconditionally ``None``, no network call),
    so calling it here is a free no-op for every provider that doesn't override it (greenhouse,
    ashby, lever, ...) and only does real work for the ones that do (join, workable, and any
    enterprise provider a future crawl might add). Mirrors ``fetch_detail``'s own contract
    (``index/detail.py``'s reconcile pass): non-raising, any exception is treated as "no detail
    available", never propagated.
    """
    ref = DetailRef(
        id=f"{ats}:{job.source_job_id}",
        source=ats,
        token=token,
        apply_url=job.apply_url,
        listing_url=None,
        content_sig="",
    )
    try:
        result = await provider.fetch_detail(ref, fetcher)
    except Exception:  # noqa: BLE001 - fetch_detail's own contract is non-raising; belt-and-suspenders
        return None
    if isinstance(result, DetailFetch):
        return result.text
    if isinstance(result, str):
        return result
    return None


async def _crawl_one(
    ats: str,
    token: str,
    fetcher: AsyncFetcher,
    rows: dict[str, dict[str, Any]],
    stats: dict[str, dict[str, int]],
    cap: int,
) -> None:
    """Fetch+normalize one board, keep at most ``cap`` JD-bearing postings FROM THIS BOARD,
    dedup into the shared ``rows`` by id.

    ``cap`` bounds only what this one board contributes -- it is a per-board row cap derived
    from the provider's overall row budget (see :func:`per_board_caps`), not a global cap.
    Once this board has newly kept ``cap`` rows, the remaining postings on the board are left
    unprocessed (no further normalize/detail-fetch work, since none of it could be kept anyway).
    Postings processed before the cap is hit are counted exactly as before (duplicates, empty
    descriptions, errors) -- the cap changes nothing about that accounting, it only stops early.

    Crash-isolated: any exception from a single dead/blocked board is caught and counted, never
    propagated -- mirrors ``scripts/build_index.py``'s ``_crawl.grab`` so one bad board can never
    sink the whole run.
    """
    st = stats[ats]
    provider = get_provider(ats)
    if provider is None:
        st["provider_missing"] += 1
        return
    try:
        raws: list[RawJob] = await provider.fetch(token, SearchQuery(), fetcher)
    except Exception:  # noqa: BLE001 - dead/blocked board, skip (never sinks the run)
        st["fetch_errors"] += 1
        return
    kept_this_board = 0
    for raw in raws:
        if kept_this_board >= cap:
            break
        try:
            job = provider.normalize(raw)
        except Exception:  # noqa: BLE001
            st["normalize_errors"] += 1
            continue
        text = job.description_text or html_to_text(job.description_html)
        if not (text and text.strip()):
            text = await _recover_detail_text(provider, ats, token, job, fetcher)
        if not (text and text.strip()):
            st["empty_description"] += 1
            continue
        job.description_text = text
        row = row_from_job(job, ats)
        row_id = row["id"]
        if row_id in rows:
            st["duplicates"] += 1
            continue
        rows[row_id] = row
        st["kept"] += 1
        kept_this_board += 1


def per_board_caps(targets: dict[str, list[str]], row_budget: dict[str, int]) -> dict[str, int]:
    """Provider -> per-board keep cap: ``row_budget[ats]`` spread evenly over the boards
    selected for that provider (``targets[ats]``), rounded up so a provider's boards can always
    reach (never undershoot) its row budget. Every provider in ``targets`` gets a cap of at
    least 1, even for providers ``row_budget`` has no entry for (treated as budget 0).
    """
    return {
        ats: max(1, math.ceil(row_budget.get(ats, 0) / max(1, len(tokens))))
        for ats, tokens in targets.items()
    }


def trim_to_row_budget(
    rows: dict[str, dict[str, Any]],
    row_budget: dict[str, int],
    stats: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Deterministically trim each provider's kept rows down to ``row_budget[ats]``.

    Boards run concurrently and each independently enforces its own per-board cap, so a
    provider's realized total can overshoot its row budget slightly (e.g. 4 boards each keeping
    up to their cap can together exceed the provider's budget by a few rows). This brings each
    provider back down to its exact budget: stable sort by row id, keep the first N. Every row
    dropped here is added to ``stats[source]["trimmed"]`` -- never a silent truncation.
    """
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows.values():
        by_source[row["source"]].append(row)
    kept: list[dict[str, Any]] = []
    for source, srows in by_source.items():
        srows.sort(key=lambda r: r["id"])
        budget = row_budget.get(source, len(srows))
        kept.extend(srows[:budget])
        overflow = len(srows) - budget
        if overflow > 0:
            stats[source]["trimmed"] += overflow
    return kept


def enforce_total_cap(
    rows: list[dict[str, Any]],
    total: int,
    stats: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Belt-and-suspenders overall cap: if the per-provider trim still leaves more than
    ``total`` rows combined (row budgets are individually <= total but their sum could exceed it
    in edge cases), deterministically sort ALL rows by id and keep only the first ``total``. Rows
    dropped here are counted into ``stats[source]["trimmed"]`` same as the per-provider trim.
    """
    if len(rows) <= total:
        return rows
    rows_sorted = sorted(rows, key=lambda r: r["id"])
    for row in rows_sorted[total:]:
        stats[row["source"]]["trimmed"] += 1
    return rows_sorted[:total]


async def crawl_corpus(
    targets: dict[str, list[str]],
    row_budget: dict[str, int],
    *,
    total: int | None = None,
    concurrency: int = 16,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Fetch every ``(provider, token)`` in ``targets`` concurrently via one shared
    ``AsyncFetcher`` (bounded global concurrency + per-host rate limiting), keep at most
    ``row_budget[ats]`` JD-bearing rows per provider (spread over its boards via
    :func:`per_board_caps`, then deterministically trimmed to the exact budget via
    :func:`trim_to_row_budget`), and optionally cap the combined corpus at ``total`` rows via
    :func:`enforce_total_cap`. Returns the deduped, budgeted corpus rows plus per-provider
    realized/dropped/trimmed counts. Never silently truncates: every kept/dropped/duplicate/
    errored/trimmed posting is counted in ``stats`` for the caller to log.
    """
    load_builtins()
    rows: dict[str, dict[str, Any]] = {}
    stats: dict[str, dict[str, int]] = {ats: defaultdict(int) for ats in targets}
    caps = per_board_caps(targets, row_budget)
    async with AsyncFetcher(concurrency=concurrency) as fetcher, anyio.create_task_group() as tg:
        for ats, tokens in targets.items():
            cap = caps.get(ats, 1)
            for token in tokens:
                tg.start_soon(_crawl_one, ats, token, fetcher, rows, stats, cap)
    trimmed = trim_to_row_budget(rows, row_budget, stats)
    if total is not None:
        trimmed = enforce_total_cap(trimmed, total, stats)
    return trimmed, stats


def _log_stats(
    targets: dict[str, list[str]],
    row_budget: dict[str, int],
    stats: dict[str, dict[str, int]],
) -> None:
    missing = [ats for ats in JD_IN_BULK_PROVIDERS if ats not in targets]
    if missing:
        print(f"[crawl_corpus] no companies available in registry for: {', '.join(missing)}")
    for ats in sorted(stats):
        s = stats[ats]
        print(
            f"[crawl_corpus] {ats}: boards={len(targets.get(ats, []))} "
            f"row_budget={row_budget.get(ats, 0)} kept={s.get('kept', 0)} "
            f"trimmed={s.get('trimmed', 0)} empty_description={s.get('empty_description', 0)} "
            f"duplicates={s.get('duplicates', 0)} fetch_errors={s.get('fetch_errors', 0)} "
            f"normalize_errors={s.get('normalize_errors', 0)} "
            f"provider_missing={s.get('provider_missing', 0)}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Stratified JD-bearing crawl across every JD-in-bulk ATS provider."
    )
    parser.add_argument(
        "--out", required=True, help="Output JSONL path, e.g. bench/corpus_jd.jsonl."
    )
    parser.add_argument(
        "--total",
        type=int,
        required=True,
        help="Target row count for the written corpus (NOT a company/board count).",
    )
    parser.add_argument(
        "--floor",
        type=int,
        required=True,
        help="Minimum rows guaranteed per provider (capped at that provider's realized availability).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=16, help="AsyncFetcher global concurrency cap."
    )
    args = parser.parse_args(argv)

    from ergon_tracker.registry.store import SeedRegistry

    registry = SeedRegistry()

    # Board budget: sized off the row target (via ROWS_PER_BOARD_EST) so there's enough board
    # supply to fill --total rows, floored at MIN_BOARDS_PER_PROVIDER boards for every provider
    # so a small --total smoke run still stays stratified across all JD-in-bulk providers.
    board_budget = max(
        len(JD_IN_BULK_PROVIDERS) * MIN_BOARDS_PER_PROVIDER,
        math.ceil(args.total / ROWS_PER_BOARD_EST),
    )
    targets = select_targets(registry, board_budget, MIN_BOARDS_PER_PROVIDER)

    # Row budget: --total/--floor are ROW units now -- allocate over the providers that actually
    # got boards, with each treated as having effectively-unlimited (uniform-large) availability
    # so the row budget is just an even split of --total, floor-guaranteed per provider.
    row_budget = allocate(dict.fromkeys(targets, 10**9), args.total, args.floor)

    for ats in sorted(targets):
        print(
            f"[crawl_corpus] targeting {len(targets[ats])} {ats} board(s), "
            f"row_budget={row_budget.get(ats, 0)}"
        )

    rows, stats = anyio.run(
        functools.partial(
            crawl_corpus, targets, row_budget, total=args.total, concurrency=args.concurrency
        )
    )
    _log_stats(targets, row_budget, stats)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_path, rows)
    sources = {row["source"] for row in rows}
    print(f"[crawl_corpus] wrote {len(rows)} rows spanning {len(sources)} sources -> {args.out}")


if __name__ == "__main__":
    main()
