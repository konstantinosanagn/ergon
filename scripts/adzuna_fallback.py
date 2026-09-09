"""Aggregator fallback for the truly-unreachable giants: capture them via Adzuna.

After the direct approaches (ATS host, sitemap/JSON-LD, own-domain API) are exhausted, the
remaining proxied giants (JPMorgan, Deloitte, Cognizant, Wells Fargo, ...) have NO fetchable
own site — but their postings are aggregated in Adzuna's keyed search API. The adzuna provider
now accepts a company token (searches the name + keeps only matching-employer results), so a
giant becomes a fallback board ``{ats: adzuna, token: "<name>"}``.

Per residual giant we make ONE Adzuna call and keep it only if >= MIN_MATCHES of the first page
are that employer (guards companies Adzuna barely indexes, e.g. India-posted TCS). One call per
giant keeps us well under Adzuna's free-tier rate limit. The dedup priority makes adzuna the
floor — any real ATS board always wins.

Usage::

    .venv/bin/python scripts/adzuna_fallback.py [--cap N] [--out scripts/candidates_adzuna.json]
    .venv/bin/python scripts/build_registry.py scripts/candidates_adzuna.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tokens import _core  # noqa: E402

from ergon.config import get_env  # noqa: E402
from ergon.http import AsyncFetcher  # noqa: E402
from ergon.models import SearchQuery  # noqa: E402
from ergon.providers.adzuna import _company_match  # noqa: E402
from ergon.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
GIANTS = ROOT / "runs" / "giants.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_adzuna.json"
_API = "https://api.adzuna.com/v1/api/jobs/us/search/1"
MIN_MATCHES = 12  # min same-employer hits on page 1 to accept (filters barely-indexed firms)


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    cap = 400
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--cap":
            cap = int(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    app_id, app_key = get_env("ADZUNA_APP_ID"), get_env("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("ADZUNA keys not set.")
        return
    seed_keys = load_seed_keys()
    giants = [
        g
        for g in json.loads(GIANTS.read_text())["uncovered_top"]
        if _core(g["name"]) not in seed_keys
    ][:cap]
    print(f"Adzuna fallback over {len(giants)} residual giants ...", flush=True)

    candidates: list[dict] = []
    taken: set[str] = set()
    done = [0]

    async def probe(g: dict, fetcher: AsyncFetcher) -> None:
        name = g["name"]
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what_phrase": name,
            "results_per_page": 50,
            "content-type": "application/json",
        }
        try:
            data = await fetcher.get_json(_API, params=params)
        except Exception:  # noqa: BLE001
            data = None
        results = data.get("results", []) if isinstance(data, dict) else []
        matches = sum(
            1
            for j in results
            if isinstance(j, dict)
            and _company_match(name, (j.get("company") or {}).get("display_name") or "")
        )
        ck = _core(name)
        if matches >= MIN_MATCHES and ck and ck not in seed_keys and ck not in taken:
            taken.add(ck)
            candidates.append(
                {
                    "company": ck,
                    "ats": "adzuna",
                    "token": name,
                    "domain": None,
                    "_filings": g.get("filings"),
                    "_matches": matches,
                }
            )
        done[0] += 1
        if done[0] % 50 == 0:
            print(f"  probed {done[0]}/{len(giants)} (kept: {len(candidates)})", flush=True)

    # Low concurrency / rate to respect Adzuna's free-tier limit.
    async with (
        AsyncFetcher(concurrency=3, per_host_rate=2, timeout=20.0, retries=2) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for g in giants:
            tg.start_soon(probe, g, fetcher)

    candidates.sort(key=lambda c: -(c.get("_filings") or 0))
    for c in candidates[:40]:
        print(
            f"  + {c['company'][:26]:26} ({c.get('_filings') or 0:>5} filings, "
            f"{c['_matches']}/50 adzuna) -> {c['token']}"
        )
    # quick provider re-check on the top few to make sure the company board actually fetches
    p = get_provider("adzuna")
    async with AsyncFetcher(per_host_rate=2, retries=1, timeout=20.0) as f:
        for c in candidates[:3]:
            raws = await p.fetch(c["token"], SearchQuery(limit=30), f)
            print(f"    verify {c['company']}: {len(raws)} jobs")

    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(out)} adzuna fallback candidates -> {out_path.name}")


if __name__ == "__main__":
    anyio.run(main)
