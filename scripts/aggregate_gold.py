"""Aggregate the 3-vote blind labels in data/judge2/out_*.jsonl into a consensus gold set.

Majority vote per field across the 3 judges of each posting; reports inter-annotator agreement
and any postings missing votes (failed agents). Writes tests/data/gold.jsonl.

Usage:
    .venv/bin/python scripts/aggregate_gold.py
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JUDGE = ROOT / "data" / "judge2"
GOLD = ROOT / "tests" / "data" / "gold.jsonl"
FIELDS = ["level", "sector", "country", "city", "remote", "salary", "yoe"]


def _key(v: object) -> str:
    return json.dumps(v, sort_keys=True)


def aggregate() -> tuple[list[dict], dict[str, Any]]:
    """Return (consensus_rows, stats). Does not write any files."""
    inputs: dict[str, dict] = {}
    for f in JUDGE.glob("assign_*.jsonl"):
        for line in f.read_text().split("\n"):
            if line.strip():
                r = json.loads(line)
                inputs[r["id"]] = r

    votes: dict[str, list[dict]] = defaultdict(list)
    out_files = sorted(JUDGE.glob("out_*.jsonl"))
    for f in out_files:
        for line in f.read_text().split("\n"):
            if line.strip():
                r = json.loads(line)
                if r.get("id") and isinstance(r.get("gold"), dict):
                    votes[r["id"]].append(r["gold"])

    agree_full: Counter[str] = Counter()
    agree_maj: Counter[str] = Counter()
    nomaj: Counter[str] = Counter()
    coverage: Counter[int] = Counter()
    final: list[dict] = []

    for jid, inp in inputs.items():
        gs = votes.get(jid, [])
        coverage[len(gs)] += 1
        if not gs:
            continue
        gold: dict = {}
        for field in FIELDS:
            vals = [g.get(field) for g in gs]
            cnt = Counter(_key(v) for v in vals)
            top, topn = cnt.most_common(1)[0]
            if topn == len(vals):
                agree_full[field] += 1
            if topn >= 2:
                agree_maj[field] += 1
                gold[field] = json.loads(top)
            else:
                nomaj[field] += 1
                chosen = vals[0]
                for v in vals:
                    if v not in (None, "unknown", ""):
                        chosen = v
                        break
                gold[field] = chosen
        final.append(
            {
                "id": jid,
                "source": inp.get("source"),
                "company_key": inp.get("company_key"),
                "title": inp.get("title"),
                "description_text": inp.get("description_windows"),
                "location_raw": inp.get("location_raw"),
                "structured_salary": inp.get("structured_salary"),
                "gold": gold,
            }
        )

    n = max(1, len(final))
    stats = {
        "out_files": len(out_files),
        "postings_total": len(inputs),
        "postings_labeled": len(final),
        "vote_coverage": dict(coverage),
        "agreement_unanimous": {f: round(agree_full[f] / n, 4) for f in FIELDS},
        "agreement_majority": {f: round(agree_maj[f] / n, 4) for f in FIELDS},
        "no_majority": {f: nomaj[f] for f in FIELDS},
        "positives": {
            f: sum(1 for r in final if r["gold"].get(f))
            for f in ("sector", "country", "city", "salary", "yoe")
        },
    }
    return final, stats


def main() -> None:
    final, stats = aggregate()
    GOLD.write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in final))
    print(json.dumps(stats, indent=2))
    print(f"wrote {GOLD.relative_to(ROOT)} ({len(final)} rows)")


if __name__ == "__main__":
    main()
