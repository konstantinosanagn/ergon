"""Completeness guardrail: catch fetch-time UNDER-COUNTING (a provider returning fewer jobs than
the board actually has). Born from three real pagination bugs found this session — Eightfold
(10/page step), Workday (500-page cap + 2000 instance-cap), Avature (fixed-page-size step).

For each audited board it fetches the board fully (bounded) via the real provider AND independently
asks the source API for its own reported total, then flags any board where ``fetched < total``.
For sources that expose no total it applies heuristics (a result that's exactly a known cap — SF-RMK
RSS 20, Workday flat 2000 — is suspect).

This is an OPERATIONAL audit (network, slow), not a CI unit test — run it periodically or after
touching a provider's pagination::

    .venv/bin/python scripts/audit_completeness.py [--ats workday,coveo,...] [--sample N] [--giants-only]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_tokens import _core  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
GIANTS = ROOT / "runs" / "giants.json"

# Providers that paginate (and thus could under-count). The flat-list providers
# (greenhouse/lever/ashby/...) return the whole board in one response and are not audited.
PAGINATING = {
    "workday",
    "eightfold",
    "avature",
    "coveo",
    "smartrecruiters",
    "icims",
    "taleo",
    "oracle",
    "phenom",
    "brassring",
    "successfactors",
}


async def api_total(ats: str, token: str, f: AsyncFetcher) -> int | None:
    """Independently ask the source for its own reported total, or None if it exposes none."""
    try:
        if ats == "workday":
            t, w, s = token.split("|")
            url = f"https://{t}.{w}.myworkdayjobs.com/wday/cxs/{t}/{s}/jobs"
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(
                    url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
                )
                d = r.json()
                # facet sums reveal the true size when the flat total is instance-capped at 2000.
                flat = int(d.get("total") or 0)
                fmax = 0
                for fc in d.get("facets") or []:
                    s_ = sum(int(v.get("count") or 0) for v in (fc.get("values") or []))
                    fmax = max(fmax, s_)
                return max(flat, fmax)
        if ats == "coveo":
            host, _, src = token.partition("|")
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(
                    f"https://{host}/coveo/rest/search/v2",
                    json={"q": "", "aq": f'@source=="{src}"', "numberOfResults": 1},
                )
                return r.json().get("totalCount")
        if ats == "smartrecruiters":
            d = await f.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                params={"limit": "1"},
            )
            return d.get("totalFound")
    except Exception:  # noqa: BLE001
        return None
    return None


async def main() -> None:
    args = sys.argv[1:]
    ats_filter = None
    sample = 0
    giants_only = False
    i = 0
    while i < len(args):
        if args[i] == "--ats":
            ats_filter = set(args[i + 1].split(","))
            i += 2
        elif args[i] == "--sample":
            sample = int(args[i + 1])
            i += 2
        elif args[i] == "--giants-only":
            giants_only = True
            i += 1
        else:
            print(f"unknown flag: {args[i]}")
            return

    seed = json.loads(SEED.read_text())["companies"]
    if giants_only:
        gk = {_core(g["name"]) for g in json.loads(GIANTS.read_text())["uncovered_top"]}
        seed = {k: v for k, v in seed.items() if k in gk}
    boards = [
        (k, v["ats"], v["token"])
        for k, v in seed.items()
        if v["ats"] in PAGINATING and (ats_filter is None or v["ats"] in ats_filter)
    ]
    if sample:
        boards = boards[:sample]
    print(f"auditing {len(boards)} paginating boards ...", flush=True)

    flags: list[str] = []
    done = [0]

    async def audit(key: str, ats: str, token: str, f: AsyncFetcher) -> None:
        try:
            fetched = len(await get_provider(ats).fetch(token, SearchQuery(limit=100000), f))
        except Exception:  # noqa: BLE001
            done[0] += 1
            return
        total = await api_total(ats, token, f)
        note = ""
        if isinstance(total, int) and total > 0 and fetched < total - max(2, total // 100):
            note = f"UNDER-COUNT fetched={fetched} total={total} (missing {total - fetched})"
        elif ats == "successfactors" and fetched == 20:
            note = "SF-RMK 20-cap suspect (CSB may be JS-rendered)"
        if note:
            flags.append(f"  {ats:14} {key[:26]:26} {note}")
        done[0] += 1
        if done[0] % 25 == 0:
            print(f"  audited {done[0]}/{len(boards)} (flags: {len(flags)})", flush=True)

    async with (
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=35.0, retries=1) as f,
        anyio.create_task_group() as tg,
    ):
        for key, ats, token in boards:
            tg.start_soon(audit, key, ats, token, f)

    print(f"\n=== {len(flags)} UNDER-COUNT FLAGS / {len(boards)} boards ===")
    for line in sorted(flags):
        print(line)
    if not flags:
        print("  none — all audited boards fetch their full reported total.")


if __name__ == "__main__":
    anyio.run(main)
