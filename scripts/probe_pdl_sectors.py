"""Stage-2 de-risk probe: name-join the registry against the PDL Free Company Dataset and measure
achievable sector coverage + accuracy vs the go/no-go bar. Offline, stdlib-only, ships nothing.

Usage:
  .venv/bin/python scripts/probe_pdl_sectors.py --dump scripts/.probe_cache/pdl_free.ndjson.gz
  .venv/bin/python scripts/probe_pdl_sectors.py --dump <path> --sample 100000   # stress gate first
Env: ERGON_PROBE_WORKERS (explicit worker count; else CI=cpu-2, else 1).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.dedup import normalize_company  # noqa: E402
from ergon_tracker.registry.store import SeedRegistry  # noqa: E402

CROSSWALK_PATH = ROOT / "scripts" / "linkedin_industry_to_sector.json"
SECTORS_PATH = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "sectors.json"
GOLD_PATH = ROOT / "tests" / "fixtures" / "sector_corpus.jsonl"


def norm(name: str | None) -> str:
    return normalize_company(name) if name else ""


def load_crosswalk(path: Path = CROSSWALK_PATH) -> dict[str, str]:
    return json.loads(Path(path).read_text())


@dataclass
class TargetIndex:
    registry_norms: set[str] = field(default_factory=set)
    norm_to_keys: dict[str, list[str]] = field(default_factory=dict)
    covered_keys: set[str] = field(default_factory=set)
    gold_norm_to_sector: dict[str, str] = field(default_factory=dict)


def build_target_index(seed: dict, sectors: dict, gold: list[dict]) -> TargetIndex:
    idx = TargetIndex()
    for key in seed:
        n = norm(key)
        if not n:
            continue
        idx.registry_norms.add(n)
        idx.norm_to_keys.setdefault(n, []).append(key)
    for key, entry in sectors.items():
        if entry.get("sector"):
            idx.covered_keys.add(key)
    for row in gold:
        if row.get("sector"):
            idx.gold_norm_to_sector[norm(row.get("company"))] = row["sector"]
    return idx


def load_inputs() -> tuple[dict, dict, list[dict]]:
    seed = SeedRegistry().all()
    sectors = json.loads(SECTORS_PATH.read_text()).get("companies", {})
    gold = [json.loads(ln) for ln in GOLD_PATH.read_text().splitlines() if ln.strip()]
    return seed, sectors, gold
