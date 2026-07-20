"""Recover boards for the census's supported-ATS sponsors -> candidates.json (strict, zero-junk).

The 3-layer census (runs/gap_ats_census.json) labelled each gap sponsor's ATS. The ones on
ATSes we already support are recoverable now — no new provider, just find the exact board. This
re-uses the conservative adjudicator from harvest_sponsors_tavily (display-name match for
non-Workday, strict tenant match for Workday) + the live verify gate, so only boards that
genuinely belong to the sponsor and return jobs are emitted.

Usage::

    .venv/bin/python scripts/recover_supported_sponsors.py [--out scripts/candidates_sponsors.json]
    .venv/bin/python scripts/build_registry.py scripts/candidates_sponsors.json
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
from harvest_sponsors_tavily import fetch_and_judge, search_candidates  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

CENSUS = ROOT / "runs" / "gap_ats_census.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_sponsors.json"
SUPPORTED = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable",
             "recruitee", "personio", "bamboohr", "breezy", "teamtailor", "join", "rippling",
             "pinpoint", "eightfold"}


async def main() -> None:
    out_path = DEFAULT_OUT
    gap_file: Path | None = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--gap-file":
            gap_file = Path(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set.")
        return
    if gap_file:
        # mid-tier mode: all sponsors in the gap list (search+adjudicate filters by ATS).
        sponsors = json.loads(gap_file.read_text())["uncovered_top"]
        print(f"{len(sponsors)} gap sponsors from {gap_file.name}; recovering (strict) ...")
    else:
        census = json.loads(CENSUS.read_text())
        sponsors = [s for s in census["per_sponsor"] if s.get("ats") in SUPPORTED]
        print(f"{len(sponsors)} census sponsors on supported ATSes; recovering (strict) ...")

    triples = await search_candidates(sponsors, key)
    print(f"{len(triples)} candidate boards found; adjudicating + would-verify ...")
    accepted, log = await fetch_and_judge(triples, load_seed_keys())

    n_acc = sum(1 for d in log if d["accept"])
    by_ats: dict[str, int] = {}
    for c in accepted:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"adjudicated {len(log)}: {n_acc} accepted; {len(accepted)} NEW (rest already in seed)")
    print(f"by_ats: {by_ats}")
    out_path.write_text(json.dumps(accepted, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"wrote {shown}\nnext: .venv/bin/python scripts/build_registry.py {shown}")


if __name__ == "__main__":
    anyio.run(main)
