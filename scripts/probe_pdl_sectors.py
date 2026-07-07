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


import argparse  # noqa: E402
import gzip  # noqa: E402
import time  # noqa: E402
from contextlib import contextmanager  # noqa: E402

CACHE_DIR = ROOT / "scripts" / ".probe_cache"
PDL_INFO = (
    "PDL Free Company Dataset (CC-BY-4.0): download the newline-delimited JSON dump "
    "(name+industry),\n"
    "place it at scripts/.probe_cache/pdl_free.ndjson.gz, and re-run with --dump <that path>.\n"
    "Fallback: BigPicture free company dataset (ODC-BY), same LinkedIn industry enum."
)


def _peak_rss_mb() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


@contextmanager
def open_dump(path: Path):
    p = Path(path)
    fh = (
        gzip.open(p, "rt", encoding="utf-8")  # noqa: SIM115
        if p.suffix == ".gz"
        else p.open(encoding="utf-8")
    )
    try:
        yield fh
    finally:
        fh.close()


def resolve_dump(args) -> Path:
    if args.dump:
        p = Path(args.dump)
        if p.exists():
            return p
        print(f"--dump path not found: {p}\n\n{PDL_INFO}")
        raise SystemExit(2)
    for name in ("pdl_free.ndjson.gz", "pdl_free.ndjson"):
        cand = CACHE_DIR / name
        if cand.exists():
            return cand
    print(f"no dump found in {CACHE_DIR}\n\n{PDL_INFO}")
    raise SystemExit(2)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--chunk-size", type=int, default=20000)
    args = ap.parse_args(argv)

    dump = resolve_dump(args)
    seed, sectors, gold = load_inputs()
    idx = build_target_index(seed, sectors, gold)
    crosswalk = load_crosswalk()
    targets = frozenset(idx.registry_norms | set(idx.gold_norm_to_sector))
    workers = _workers()
    print(f"[probe] registry={len(seed)} targets={len(targets)} workers={workers} dump={dump.name}")

    t0 = time.monotonic()
    with open_dump(dump) as fh:
        it = fh
        if args.sample:
            import itertools

            it = itertools.islice(fh, args.sample)
        matches, collisions = run_join(it, targets, workers=workers, chunk_size=args.chunk_size)
    wall = time.monotonic() - t0

    if args.sample:
        print(
            f"[stress] sample={args.sample} matches={len(matches)} collisions={collisions} "
            f"peakRSS={_peak_rss_mb():.0f}MB wall={wall:.1f}s — full run is safe."
        )
        return

    m = measure(matches, idx, crosswalk, total_registry=len(seed))
    print(
        f"[join] matches={len(matches)} w/sector={m['matched_with_sector']} "
        f"collisions={collisions} peakRSS={_peak_rss_mb():.0f}MB wall={wall:.1f}s"
    )
    print("\n=== Stage-2 PDL name-join probe ===")
    print(f"  gold accuracy-when-covered : {m['gold_accuracy']:.1%}  (bar 72.4%)")
    print(f"  gold coverage              : {m['gold_coverage']:.1%}")
    print(f"  registry current coverage  : {m['current_coverage']:.1%}")
    print(f"  registry net-new companies : {m['net_new_keys']}")
    print(f"  registry projected coverage: {m['projected_coverage']:.1%}  (bar 35%)")
    go = verdict(m)
    print(
        f"  VERDICT: "
        f"{'GO — build full Stage-2 pipeline' if go else 'NO-GO — pivot to squeeze-existing'}"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
