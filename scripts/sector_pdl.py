"""Build a precision-gated company→sector map from the PDL Free dataset (offline).

Reuses the Stage-2 name-join (scripts/probe_pdl_sectors.py) and keeps ONLY a curated allow-list of
high-precision LinkedIn industries, so the output is safe to gap-fill into the authoritative
sectors.json (via scripts/merge_sectors.py). Ships nothing itself; writes scripts/sector_pdl.json.

Usage:
  .venv/bin/python scripts/sector_pdl.py --dump scripts/.probe_cache/pdl_free.ndjson.gz
  .venv/bin/python scripts/sector_pdl.py --dump <path> --sample 200000   # stress gate first
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import probe_pdl_sectors as probe  # noqa: E402

OUT = ROOT / "scripts" / "sector_pdl.json"

# Curated allow-list: each LinkedIn industry maps 1:1 to a label AND scored high in the Stage-2
# probe. Coarse/ambiguous buckets (internet, IT services, financial services, marketing,
# consumer goods, entertainment, real estate, telecommunications) are deliberately excluded —
# that is the gate.
PDL_ALLOWLIST: dict[str, str] = {
    "biotechnology": "Biotech/Pharma",
    "pharmaceuticals": "Biotech/Pharma",
    "banking": "Banking/Finance",
    "insurance": "Insurance",
    "medical devices": "Healthcare",
    "hospital & health care": "Healthcare",
    "oil & energy": "Energy/Climate",
    "utilities": "Energy/Climate",
    "chemicals": "Manufacturing/Industrial",
    "mining & metals": "Manufacturing/Industrial",
    "mechanical or industrial engineering": "Manufacturing/Industrial",
    "semiconductors": "Semiconductors/Hardware",
    "higher education": "Education",
}


def build_pdl_map(matches: dict[str, str], idx) -> dict[str, dict]:
    """Matched norm→industry → {registry_key: {sector, source, industry}} for allow-list industries."""
    out: dict[str, dict] = {}
    for n, industry in matches.items():
        sector = PDL_ALLOWLIST.get(industry)
        if not sector:
            continue
        for key in idx.norm_to_keys.get(n, []):
            out[key] = {"sector": sector, "source": "pdl", "industry": industry}
    return out


def accuracy_on_gold(matches: dict[str, str], idx) -> tuple[int, int]:
    """(correct, total) of allow-list predictions vs gold, on the gold∩matches∩allow-list overlap."""
    correct = total = 0
    for n, gold_sector in idx.gold_norm_to_sector.items():
        sector = PDL_ALLOWLIST.get(matches.get(n, ""))
        if not sector:
            continue
        total += 1
        correct += sector == gold_sector
    return correct, total


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--chunk-size", type=int, default=20000)
    args = ap.parse_args(argv)

    dump = probe.resolve_dump(args)
    seed, sectors, gold = probe.load_inputs()
    idx = probe.build_target_index(seed, sectors, gold)
    targets = frozenset(idx.registry_norms | set(idx.gold_norm_to_sector))
    workers = probe._workers()
    print(f"[pdl] registry={len(seed)} targets={len(targets)} workers={workers} dump={dump.name}")

    t0 = time.monotonic()
    with probe.open_dump(dump) as fh:
        it = itertools.islice(fh, args.sample) if args.sample else fh
        matches, collisions = probe.run_join(
            it, targets, workers=workers, chunk_size=args.chunk_size
        )
    wall = time.monotonic() - t0

    if args.sample:
        print(
            f"[stress] sample={args.sample} matches={len(matches)} "
            f"peakRSS={probe._peak_rss_mb():.0f}MB wall={wall:.1f}s — full run is safe."
        )
        return

    pdl_map = build_pdl_map(matches, idx)
    current = {k for k, v in sectors.items() if v.get("sector")}
    net_new = sum(1 for k in pdl_map if k not in current)
    correct, total = accuracy_on_gold(matches, idx)
    OUT.write_text(json.dumps(dict(sorted(pdl_map.items())), ensure_ascii=True, indent=1) + "\n")
    acc = f"{correct}/{total} = {correct / total:.1%}" if total else "n/a"
    print(
        f"[pdl] wrote {len(pdl_map)} entries → {OUT.name}; net-new vs current {net_new}; "
        f"gold-acc {acc}; collisions {collisions}; peakRSS={probe._peak_rss_mb():.0f}MB wall={wall:.1f}s"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
