"""Resolve company NAMES -> ``Name,domain`` lines for ats_exhaustion_sweep.py.

The ladder's strongest rung (rung 1: careers-page tenant discovery) needs a domain, and the rigor
gate only marks a company browser-eligible if rung 1 actually inspected a careers page. So before
sweeping a bare name list at scale, resolve each name to its best domain via
``resolve_careers.company_domains`` (keyless Clearbit autocomplete + offline name-guess fallback).

Also drops obvious non-operating shells (SPACs / acquisition corps / trusts / funds) that have no
careers board, so the sweep doesn't waste probes on them.

Usage::
    .venv/bin/python scripts/prepare_ladder_input.py names.txt --out ladder_input.txt [--limit N]
    .venv/bin/python scripts/ats_exhaustion_sweep.py ladder_input.txt --resume
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from resolve_careers import company_domains  # noqa: E402

from ergon.http import AsyncFetcher  # noqa: E402

# Names matching these are non-operating entities (no employees, no careers board) — skip them.
_NONOP_RE = re.compile(
    r"(\bacquisition\b\s+(?:\S+\s+)?corp|\bspac\b|blank\s+check|\btrust\b|\bfund\b|\betf\b|"
    r"\bmunicipal\b)",
    re.IGNORECASE,
)


def is_nonoperating(name: str) -> bool:
    """True for SPAC / acquisition-corp / fund / trust shells that never host a careers board."""
    return bool(_NONOP_RE.search(name))


def filter_operating(names: list[str]) -> list[str]:
    return [n for n in names if n.strip() and not is_nonoperating(n)]


async def _resolve_all(names: list[str], concurrency: int) -> list[tuple[str, str | None]]:
    out: dict[int, tuple[str, str | None]] = {}
    sem = anyio.Semaphore(concurrency)
    async with AsyncFetcher(concurrency=concurrency, per_host_rate=4, timeout=15) as fetcher:

        async def one(i: int, name: str) -> None:
            async with sem:
                try:
                    domains = await company_domains(name, fetcher)
                except Exception:  # noqa: BLE001 - never let one lookup sink the batch
                    domains = []
                out[i] = (name, domains[0] if domains else None)

        async with anyio.create_task_group() as tg:
            for i, name in enumerate(names):
                tg.start_soon(one, i, name)
    return [out[i] for i in range(len(names))]


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print("usage: prepare_ladder_input.py names.txt [--out PATH] [--limit N] [--concurrency N]")
        return
    in_path = Path(args[0])
    out_path = ROOT / "scripts" / "ladder_input.txt"
    limit = 0
    concurrency = 8
    i = 1
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--limit":
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--concurrency":
            concurrency = int(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    names = [ln.strip() for ln in in_path.read_text().splitlines() if ln.strip()]
    operating = filter_operating(names)
    dropped = len(names) - len(operating)
    if limit:
        operating = operating[:limit]
    print(f"{len(names)} names; dropped {dropped} non-operating; resolving {len(operating)} ...")

    resolved = anyio.run(_resolve_all, operating, concurrency)
    with_domain = [(n, d) for n, d in resolved if d]
    lines = [f"{n},{d}" for n, d in with_domain]
    out_path.write_text("\n".join(lines) + "\n")
    print(f"resolved {len(with_domain)}/{len(operating)} to a domain -> {out_path}")
    print(f"\nnext: .venv/bin/python scripts/ats_exhaustion_sweep.py {out_path} --resume")


if __name__ == "__main__":
    main()
