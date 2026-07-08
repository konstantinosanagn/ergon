"""Merge the deterministic sector sources into sectors.json with priority.

Priority: curated (existing sectors.json) > wikidata > edgar > naics > slug-heuristic.
Reports each source's accuracy vs the curated gold (on overlap) + cross-source agreement
(a precision proxy on companies the curated set doesn't cover), then fills unclassified.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from classify_sectors import classify as slug_classify  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
SECTORS = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "sectors.json"


def _load(name: str) -> dict[str, str]:
    p = ROOT / "scripts" / name
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: v["sector"] for k, v in data.items() if v.get("sector")}


def apply_priority(
    seed: dict, curated: dict, sources: dict, priority: list[str]
) -> dict[str, dict]:
    """Gap-fill non-curated keys: first source in ``priority`` order that has the key wins.
    ``pdl`` is last, so it only fills keys no higher source covered — it never overrides."""
    out: dict[str, dict] = {}
    for key in seed:
        if key in curated:
            continue
        for src in priority:
            val = sources[src].get(key)
            if val:
                out[key] = {
                    "sector": val,
                    "domain": seed[key].get("domain"),
                    "source": src,
                }
                break
    return out


def main() -> None:
    apply = "--apply" in sys.argv
    seed = json.loads(SEED.read_text())["companies"]
    sec = json.loads(SECTORS.read_text())
    curated = {k: v["sector"] for k, v in sec["companies"].items() if v.get("sector")}

    sources = {
        "wikidata": _load("sector_wikidata.json"),
        "edgar": _load("sector_edgar.json"),
        "naics": _load("sector_naics.json"),
        "pdl": _load("sector_pdl.json"),
    }
    sources["slug"] = {k: s for k in seed if (s := slug_classify(k, seed[k].get("domain")))}

    # accuracy vs curated gold (overlap)
    print("source     coverage(22k)  acc-vs-curated(overlap)")
    for name, m in sources.items():
        overlap = [k for k in m if k in curated]
        acc = sum(m[k] == curated[k] for k in overlap) / len(overlap) if overlap else 0
        print(f"  {name:9s} {len(m):>7d}        {acc:.0%} (n={len(overlap)})")

    # priority merge for companies not already curated. NAICS excluded (36% exact — its
    # taxonomy can't express tech sectors; would pollute). Data sources before slug heuristic;
    # pdl is LAST (gap-fill only — it fills a key only if no higher source did, never overrides).
    priority = ["edgar", "wikidata", "slug", "pdl"]
    filled = apply_priority(seed, curated, sources, priority)
    sec["companies"].update(filled)
    added = dict.fromkeys(priority, 0)
    for rec in filled.values():
        added[rec["source"]] += 1

    total = len(sec["companies"])
    print(f"\nadded by source: {added}")
    print(f"sectors coverage: {total}/{len(seed)} = {total / len(seed):.0%} (was {len(curated)})")

    if apply:
        SECTORS.write_text(json.dumps(sec, ensure_ascii=True, indent=1) + "\n")
        print("wrote sectors.json")


if __name__ == "__main__":
    main()
