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
    from ergon_tracker.engine import run_search  # reuse the live engine
    from ergon_tracker.http import AsyncFetcher
    from ergon_tracker.models import SearchQuery
    from ergon_tracker.registry.store import SeedRegistry

    keys = list(SeedRegistry().all())[:limit_companies]
    # companies= => run_search takes the live path (try_index returns None), so no recursion.
    q = SearchQuery(companies=keys)
    async with AsyncFetcher() as fetcher:
        result = await run_search(q, fetcher)
    return result.jobs


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
