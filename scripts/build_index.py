"""M1 build entry: crawl a bounded slice of the registry -> build index -> publish artifacts.

Usage:
  .venv/bin/python scripts/build_index.py --limit-companies 300 --out dist/
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.index.build import build_index  # noqa: E402


def publish_artifacts(db_path: Path, out_dir: Path, *, build_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = db_path.read_bytes()
    (out_dir / "index.sqlite.gz").write_bytes(gzip.compress(raw))
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema_version": 1,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
    )


async def _crawl(limit_companies: int) -> list:
    """Bounded registry crawl: fetch N boards directly by their stored (ats, token).

    Bypasses resolve() (which is for arbitrary user domains/URLs) and reuses the providers +
    enrich + dedup, crash-isolated per board so one dead board never sinks the run.
    """
    import anyio

    from ergon_tracker.dedup import deduplicate
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.base import get_provider, load_builtins
    from ergon_tracker.registry.store import SeedRegistry

    load_builtins()
    items = [
        (k, e)
        for k, e in list(SeedRegistry().all().items())[:limit_companies]
        if e.get("ats") and e.get("token")
    ]
    jobs: list = []

    async def grab(key: str, entry: dict, fetcher: AsyncFetcher) -> None:
        provider = get_provider(entry["ats"])
        if provider is None:
            return
        try:
            raws = await provider.fetch(entry["token"], SearchQuery(), fetcher)
        except Exception:  # noqa: BLE001 - dead/blocked board, skip
            return
        for raw in raws:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            if entry.get("domain") and not job.company_domain:
                job.company_domain = entry["domain"]
            enrich_in_place(job, company_key=key)
            jobs.append(job)

    async with AsyncFetcher() as fetcher, anyio.create_task_group() as tg:
        for key, entry in items:
            tg.start_soon(grab, key, entry, fetcher)
    return deduplicate(jobs)


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def build_and_publish_shards(jobs: list, out: Path, *, build_id: str) -> int:
    """Build per-sector shards from jobs and gzip each for release upload. Returns shard count."""
    from ergon_tracker.index.build import build_sharded_index

    manifest = build_sharded_index(jobs, out, build_id=build_id)
    for info in manifest["shards"].values():
        f = out / info["file"]
        (out / (info["file"] + ".gz")).write_bytes(gzip.compress(f.read_bytes()))
    return len(manifest["shards"])


def build_and_publish_shards_from_db(db_path: Path, out: Path, *, build_id: str) -> int:
    """Memory-bounded shard publish: partition the built index by sector via SQL, gzip each."""
    from ergon_tracker.index.build import build_sharded_index_from_db

    manifest = build_sharded_index_from_db(db_path, out, build_id=build_id)
    for info in manifest["shards"].values():
        f = out / info["file"]
        (out / (info["file"] + ".gz")).write_bytes(gzip.compress(f.read_bytes()))
    return len(manifest["shards"])


def _count_jobs(db_path: Path) -> int:
    """Row count of an index DB (cheap; avoids loading jobs into memory)."""
    from ergon_tracker.index.db import connect

    con = connect(db_path, read_only=True)
    try:
        return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        con.close()


def publish_coverage(db_path: Path, out_dir: Path, *, build_id: str) -> dict:
    """Write coverage.json + INDEX_STATUS.md so users/forkers can see index coverage."""
    from ergon_tracker.index.coverage import compute_coverage, render_status_md
    from ergon_tracker.index.db import connect

    con = connect(db_path, read_only=True)
    try:
        cov = compute_coverage(con)
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_status_md(cov, build_id=build_id)
    (out_dir / "coverage.json").write_text(json.dumps(cov, indent=2))
    (out_dir / "INDEX_STATUS.md").write_text(md)  # release asset
    (ROOT / "INDEX_STATUS.md").write_text(md)  # browsable in repo root
    return cov


def append_history(history_path: Path, row: dict) -> None:
    """Append one build-summary row to the history JSONL time series (for drift detection)."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _gated_publish(
    tmp_db: Path, final_db: Path, out: Path, *, build_id: str, prev_row_count: int | None = None
) -> bool:
    """Good-or-nothing publish: gate the temp build, promote+publish only if it passes.

    Writes gates.json always. On failure the previous snapshot (final_db) is left untouched.
    """
    from ergon_tracker.index.gates import evaluate_gates

    rep = evaluate_gates(tmp_db, prev_row_count=prev_row_count)
    out.mkdir(parents=True, exist_ok=True)
    (out / "gates.json").write_text(json.dumps(rep.to_dict(), indent=2))
    if not rep.passed:
        print(f"GATES FAILED — keeping previous snapshot. {rep.summary()}")
        tmp_db.unlink(missing_ok=True)
        return False
    tmp_db.replace(final_db)  # atomic promote
    publish_artifacts(final_db, out, build_id=build_id)
    cov = publish_coverage(final_db, out, build_id=build_id)
    print(f"gates passed: {rep.summary()} | coverage: {cov['total_jobs']} jobs, "
          f"{len(cov['by_source'])} providers, {len(cov['by_sector'])} sectors")
    return True


async def _crawl_due(limit_companies: int, states: dict, fresh_db_path, build_id: str) -> dict:
    """Crawl ONLY the boards due today, streaming each board's jobs into ``fresh_db_path``.

    Returns the per-board outcome (error/http_429/companies/not_modified). Jobs are written to the
    fresh DB as boards complete (memory O(in-flight boards), not O(all jobs)) — the streaming path
    that lets a full ~46k-board build run without holding ~1M jobs in RAM. Crash-isolated per board.
    """
    import anyio

    from ergon_tracker.dedup import deduplicate, normalize_company
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.exceptions import RateLimitError
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.index.build import append_jobs
    from ergon_tracker.index.db import connect, fresh_db
    from ergon_tracker.index.scheduler import BoardState, due_boards
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.providers.base import get_provider, load_builtins
    from ergon_tracker.registry.store import SeedRegistry

    load_builtins()
    boards = {}
    for key, e in list(SeedRegistry().all().items())[:limit_companies]:
        if e.get("ats") and e.get("token"):
            bs = BoardState(provider=e["ats"], token=e["token"])
            boards[bs.key] = (key, e)
            states.setdefault(bs.key, bs)
    due = set(due_boards(list(states.values()), _today())) & set(boards)

    outcome: dict[str, dict] = {
        b: {"error": False, "http_429": 0, "companies": set(), "not_modified": False} for b in due
    }
    fresh_db(fresh_db_path)
    con = connect(fresh_db_path)
    con.execute("PRAGMA foreign_keys = OFF")  # companies aggregated later (build_index_from_fresh_db)
    write_lock = anyio.Lock()

    async def grab(bkey: str, fetcher: AsyncFetcher) -> None:
        regkey, e = boards[bkey]
        provider = get_provider(e["ats"])
        state = states[bkey]
        # Cross-build conditional request: if this provider exposes a whole-board validator URL,
        # present the stored ETag/Last-Modified. A 304 means unchanged -> carry forward without
        # re-downloading (the big throttle/bandwidth win). A 200 refreshes the validator and we
        # parse that same body (no refetch) via raws_from_body.
        curl = provider.conditional_url(e["token"])
        try:
            if curl:
                res = await fetcher.conditional_get(
                    curl, etag=state.etag, last_modified=state.last_modified
                )
                if res.not_modified:
                    outcome[bkey]["not_modified"] = True
                    return  # unchanged -> prev jobs carry forward (company set stays empty)
                state.etag, state.last_modified = res.etag, res.last_modified
                # Reuse the body we just downloaded (200) instead of refetching the same board.
                raws = provider.raws_from_body(e["token"], res.body) if res.body else None
                if raws is None:
                    raws = await provider.fetch(e["token"], SearchQuery(), fetcher)
            else:
                raws = await provider.fetch(e["token"], SearchQuery(), fetcher)
        except RateLimitError:
            outcome[bkey].update(error=True, http_429=1)
            return
        except Exception:  # noqa: BLE001
            outcome[bkey]["error"] = True
            return
        board_jobs: list = []
        for raw in raws:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            if e.get("domain") and not job.company_domain:
                job.company_domain = e["domain"]
            enrich_in_place(job, company_key=regkey)
            board_jobs.append(job)
            outcome[bkey]["companies"].add(normalize_company(job.company))
        if board_jobs:
            # Per-board fuzzy dedup (cheap, memory-safe) recovers most of the old in-memory
            # deduplicate() quality; cross-board exact-id dedup is handled by append_jobs' UNIQUE.
            board_jobs = deduplicate(board_jobs)
            # one shared connection; the lock serializes the (sync, fast) batch insert
            async with write_lock:
                append_jobs(con, board_jobs, build_id=build_id)

    try:
        async with AsyncFetcher() as fetcher, anyio.create_task_group() as tg:
            for bkey in due:
                tg.start_soon(grab, bkey, fetcher)
        con.commit()
    finally:
        con.close()
    return outcome


def main(argv: list[str]) -> None:
    import anyio

    limit = 300
    out = ROOT / "dist"
    incremental = False
    sharded = False
    i = 0
    while i < len(argv):
        if argv[i] == "--limit-companies":
            limit = int(argv[i + 1])
            i += 2
        elif argv[i] == "--out":
            out = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--incremental":
            incremental = True
            i += 1
        elif argv[i] == "--sharded":
            sharded = True
            i += 1
        else:
            print(f"unknown flag: {argv[i]}")
            return
    out.mkdir(parents=True, exist_ok=True)
    db = out / "index.sqlite"
    build_id = f"build-{_today()}"

    if incremental:
        from ergon_tracker.index.build import (
            build_index_from_fresh_db,
            changed_companies_sql,
        )
        from ergon_tracker.index.scheduler import apply_outcome, load_state, save_state

        state_path = out / "board_state.json"
        states = load_state(state_path)
        prev_db = db if db.exists() else None
        prev_row_count = _count_jobs(db) if prev_db else None
        # Streaming crawl: jobs are written to fresh.sqlite as boards complete (memory-bounded).
        fresh_path = out / "fresh.sqlite"
        outcome = anyio.run(_crawl_due, limit, states, fresh_path, build_id)
        changed = changed_companies_sql(fresh_path, prev_db)  # SQL diff, no jobs in memory
        crawled_keys: set = (
            set().union(*(o["companies"] for o in outcome.values())) if outcome else set()
        )
        # fold each board's outcome back into its state (tiering + throttle back-pressure)
        for bkey, o in outcome.items():
            board_changed = bool(o["companies"] & changed)
            apply_outcome(
                states[bkey],
                today=_today(),
                changed=board_changed and not o["error"],
                error=o["error"],
                http_429=o["http_429"],
                requests=1,
            )
        fresh_jobs_count = _count_jobs(fresh_path)
        db_tmp = out / "index.tmp.sqlite"
        n = build_index_from_fresh_db(
            fresh_path, db_tmp, build_id=build_id, prev_db=prev_db, crawled_keys=crawled_keys
        )
        save_state(states, state_path)  # record the crawl regardless of publish outcome
        ok = _gated_publish(db_tmp, db, out, build_id=build_id, prev_row_count=prev_row_count)
        append_history(
            out / "history.jsonl",
            {
                "build_id": build_id, "date": _today(), "due_boards": len(outcome),
                "fresh_jobs": fresh_jobs_count, "total_jobs": n, "changed_companies": len(changed),
                "throttled_boards": sum(1 for o in outcome.values() if o["http_429"]),
                "errored_boards": sum(1 for o in outcome.values() if o["error"]),
                "not_modified_boards": sum(1 for o in outcome.values() if o.get("not_modified")),
                "published": ok,
            },
        )
        fresh_path.unlink(missing_ok=True)
        if ok and sharded:
            ns = build_and_publish_shards_from_db(db, out, build_id=build_id)
            print(f"  + published {ns} sector shards")
        print(
            f"incremental build: crawled {len(outcome)} due boards, {fresh_jobs_count} fresh jobs, "
            f"{n} total{' -> published' if ok else ' (gates FAILED, kept previous)'}"
        )
        if not ok:
            raise SystemExit(1)
        return

    jobs = anyio.run(_crawl, limit)
    db_tmp = out / "index.tmp.sqlite"
    n = build_index(jobs, db_tmp, build_id=build_id)
    if not _gated_publish(db_tmp, db, out, build_id=build_id):
        raise SystemExit(1)
    if sharded:
        ns = build_and_publish_shards(jobs, out, build_id=build_id)
        print(f"  + published {ns} sector shards")
    print(f"built index: {n} jobs -> {out}/index.sqlite.gz (+manifest.json)")


if __name__ == "__main__":
    main(sys.argv[1:])
