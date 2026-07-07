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


import os  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402


def _workers() -> int:
    env = os.environ.get("ERGON_PROBE_WORKERS")
    if env:
        return int(env)
    if os.environ.get("CI"):
        return max(2, (os.cpu_count() or 4) - 2)
    return 1


def record_industry(
    rec: dict, name_field: str = "name", industry_field: str = "industry"
) -> tuple[str, str, int] | None:
    n = norm(rec.get(name_field))
    if not n:
        return None
    completeness = sum(1 for v in rec.values() if v not in (None, "", [], {}))
    return n, (rec.get(industry_field) or ""), completeness


def join_chunk(lines: list[str], targets: frozenset[str]) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        got = record_industry(rec)
        if got is None:
            continue
        n, industry, comp = got
        if n not in targets:
            continue
        prev = out.get(n)
        if prev is None or comp > prev[1] or (comp == prev[1] and industry < prev[0]):
            out[n] = (industry, comp)
    return out


# module-level target set for worker processes (set via initializer; avoids re-pickling per chunk)
_WORKER_TARGETS: frozenset[str] = frozenset()


def _init_worker(targets: frozenset[str]) -> None:
    global _WORKER_TARGETS
    _WORKER_TARGETS = targets


def _join_chunk_worker(lines: list[str]) -> dict[str, tuple[str, int]]:
    return join_chunk(lines, _WORKER_TARGETS)


def _merge(
    dst: dict[str, tuple[str, int]], collisions: set[str], src: dict[str, tuple[str, int]]
) -> None:
    for n, (industry, comp) in src.items():
        prev = dst.get(n)
        if prev is None:
            dst[n] = (industry, comp)
        else:
            if prev[0] != industry:
                collisions.add(n)
            if comp > prev[1] or (comp == prev[1] and industry < prev[0]):
                dst[n] = (industry, comp)


def _chunks(line_iter, size: int):
    buf: list[str] = []
    for ln in line_iter:
        buf.append(ln)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def run_join(
    line_iter, targets: frozenset[str], *, workers: int, chunk_size: int = 20000
) -> tuple[dict[str, str], int]:
    acc: dict[str, tuple[str, int]] = {}
    collisions: set[str] = set()
    if workers <= 1:
        for chunk in _chunks(line_iter, chunk_size):
            _merge(acc, collisions, join_chunk(chunk, targets))
    else:
        # bounded in-flight submission so we never hold the whole file in pending futures
        from concurrent.futures import FIRST_COMPLETED, wait

        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(targets,)
        ) as pool:
            gen = _chunks(line_iter, chunk_size)
            pending: set = set()
            cap = workers * 2
            for chunk in gen:
                pending.add(pool.submit(_join_chunk_worker, chunk))
                if len(pending) >= cap:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for f in done:
                        _merge(acc, collisions, f.result())
            for f in pending:
                _merge(acc, collisions, f.result())
    return {n: industry for n, (industry, _) in acc.items()}, len(collisions)


def measure(
    matches: dict[str, str], idx: TargetIndex, crosswalk: dict[str, str], *, total_registry: int
) -> dict:
    # crosswalk each matched industry → sector (or None = abstain)
    sectored = {n: crosswalk.get(ind) for n, ind in matches.items()}
    sectored = {n: s for n, s in sectored.items() if s}  # keep only those with a real label

    # gold accuracy + coverage (measured on the gold display-name overlap)
    gold_hits = gold_total = 0
    for n, gold_sector in idx.gold_norm_to_sector.items():
        s = sectored.get(n)
        if s is None:
            continue
        gold_total += 1
        if s == gold_sector:
            gold_hits += 1
    gold_accuracy = gold_hits / gold_total if gold_total else 0.0
    gold_coverage = gold_total / len(idx.gold_norm_to_sector) if idx.gold_norm_to_sector else 0.0

    # registry net-new: keys whose norm got a sector AND that key isn't already covered
    newly = set()
    for n in sectored:
        for key in idx.norm_to_keys.get(n, []):
            if key not in idx.covered_keys:
                newly.add(key)
    projected = len(idx.covered_keys | newly) / total_registry if total_registry else 0.0

    return {
        "gold_accuracy": gold_accuracy,
        "gold_coverage": gold_coverage,
        "matched_with_sector": len(sectored),
        "net_new_keys": len(newly),
        "current_coverage": len(idx.covered_keys) / total_registry if total_registry else 0.0,
        "projected_coverage": projected,
    }


def verdict(metrics: dict, *, min_coverage: float = 0.35, min_accuracy: float = 0.724) -> bool:
    return (
        metrics["projected_coverage"] >= min_coverage and metrics["gold_accuracy"] >= min_accuracy
    )
