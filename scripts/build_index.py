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


def main(argv: list[str]) -> None:
    import anyio

    limit = 300
    out = ROOT / "dist"
    i = 0
    while i < len(argv):
        if argv[i] == "--limit-companies":
            limit = int(argv[i + 1])
            i += 2
        elif argv[i] == "--out":
            out = Path(argv[i + 1])
            i += 2
        else:
            print(f"unknown flag: {argv[i]}")
            return
    build_id = "m1-local"  # M2 replaces with a CI-supplied timestamp
    jobs = anyio.run(_crawl, limit)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "index.sqlite"
    n = build_index(jobs, db, build_id=build_id)
    publish_artifacts(db, out, build_id=build_id)
    print(f"built index: {n} jobs -> {out}/index.sqlite.gz (+manifest.json)")


if __name__ == "__main__":
    main(sys.argv[1:])
