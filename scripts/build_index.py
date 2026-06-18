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
    print(f"gates passed: {rep.summary()}")
    return True


async def _crawl_due(limit_companies: int, states: dict) -> tuple[list, dict]:
    """Crawl ONLY the boards due today (per the scheduler) -> (fresh_jobs, per-board outcome).

    outcome[boardkey] = {error, http_429, companies}. This is the throttle-proofing: a daily
    build touches a fraction of the registry instead of all of it. Crash-isolated per board.
    """
    import anyio

    from ergon_tracker.dedup import normalize_company
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.exceptions import RateLimitError
    from ergon_tracker.http import AsyncFetcher
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

    fresh: list = []
    outcome: dict[str, dict] = {b: {"error": False, "http_429": 0, "companies": set()} for b in due}

    async def grab(bkey: str, fetcher: AsyncFetcher) -> None:
        regkey, e = boards[bkey]
        provider = get_provider(e["ats"])
        try:
            raws = await provider.fetch(e["token"], SearchQuery(), fetcher)
        except RateLimitError:
            outcome[bkey].update(error=True, http_429=1)
            return
        except Exception:  # noqa: BLE001
            outcome[bkey]["error"] = True
            return
        for raw in raws:
            try:
                job = provider.normalize(raw)
            except Exception:  # noqa: BLE001
                continue
            if e.get("domain") and not job.company_domain:
                job.company_domain = e["domain"]
            enrich_in_place(job, company_key=regkey)
            fresh.append(job)
            outcome[bkey]["companies"].add(normalize_company(job.company))

    async with AsyncFetcher() as fetcher, anyio.create_task_group() as tg:
        for bkey in due:
            tg.start_soon(grab, bkey, fetcher)
    return fresh, outcome


def main(argv: list[str]) -> None:
    import anyio

    limit = 300
    out = ROOT / "dist"
    incremental = False
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
        else:
            print(f"unknown flag: {argv[i]}")
            return
    out.mkdir(parents=True, exist_ok=True)
    db = out / "index.sqlite"
    build_id = f"build-{_today()}"

    if incremental:
        from ergon_tracker.index.build import (
            build_index_incremental,
            changed_companies,
            read_index_jobs,
        )
        from ergon_tracker.index.scheduler import apply_outcome, load_state, save_state

        state_path = out / "board_state.json"
        states = load_state(state_path)
        prev_jobs = read_index_jobs(db) if db.exists() else []
        fresh, outcome = anyio.run(_crawl_due, limit, states)
        changed = changed_companies(prev_jobs, fresh)
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
        db_tmp = out / "index.tmp.sqlite"
        n = build_index_incremental(
            db if db.exists() else None, fresh, crawled_keys, db_tmp, build_id=build_id
        )
        save_state(states, state_path)  # record the crawl regardless of publish outcome
        ok = _gated_publish(db_tmp, db, out, build_id=build_id, prev_row_count=len(prev_jobs) or None)
        print(
            f"incremental build: crawled {len(outcome)} due boards, {len(fresh)} fresh jobs, "
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
    print(f"built index: {n} jobs -> {out}/index.sqlite.gz (+manifest.json)")


if __name__ == "__main__":
    main(sys.argv[1:])
