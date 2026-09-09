"""Audit: how many H-1B sponsors does our registry actually track?

Critical for international students — a sponsor we don't track is a job we can't surface. This
joins the bundled H-1B sponsor index against the company registry and reports coverage two ways:

* by *employer* — what fraction of unique sponsors are in our registry, and
* by *filing volume* — what fraction of all certified H-1B filings come from sponsors we track
  (the volume-weighted number is what matters: covering the 500 biggest sponsors beats covering
  5,000 one-filing shops).

It then prints the largest *uncovered* sponsors (the highest-leverage registry gaps) and writes
them to ``runs/h1b_coverage_gap.json`` so they can feed registry growth.

Matching mirrors the live signal: exact normalized name, space-collapsed slug, or a registry
name that is the leading prefix of the sponsor's legal name followed by a corporate descriptor.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon.dedup import normalize_company  # noqa: E402
from ergon.extract.visa import _DESCRIPTORS  # noqa: E402
from ergon.registry.store import SeedRegistry  # noqa: E402

INDEX = ROOT / "src" / "ergon" / "registry" / "data" / "h1b_sponsors.json"
GAP_OUT = ROOT / "runs" / "h1b_coverage_gap.json"


def main() -> None:
    sponsors: dict[str, dict] = json.loads(INDEX.read_text())["sponsors"]

    # Registry name forms a company can be matched by.
    seed = SeedRegistry().all()
    reg_norm: set[str] = set()
    for key, e in seed.items():
        reg_norm.add(normalize_company(e.get("name") or key))
    reg_collapsed = {n.replace(" ", "") for n in reg_norm}

    def covered(sponsor_key: str) -> bool:
        if sponsor_key in reg_norm:
            return True
        if sponsor_key.replace(" ", "") in reg_collapsed:
            return True
        toks = sponsor_key.split()
        for k in range(1, len(toks)):  # registry name is a leading prefix + corporate descriptor
            if " ".join(toks[:k]) in reg_norm and toks[k] in _DESCRIPTORS:
                return True
        return False

    total_emp = len(sponsors)
    total_fil = sum(int(r.get("n") or 0) for r in sponsors.values())
    cov_emp = cov_fil = 0
    uncovered: list[tuple[str, int, str]] = []
    for name, rec in sponsors.items():
        n = int(rec.get("n") or 0)
        if covered(name):
            cov_emp += 1
            cov_fil += n
        else:
            uncovered.append((name, n, str(rec.get("last") or "")))

    uncovered.sort(key=lambda t: -t[1])
    print(f"sponsors (unique employers): {total_emp:,}")
    print(f"  tracked in registry:       {cov_emp:,} ({100 * cov_emp / total_emp:.1f}%)")
    print(f"certified filings (volume):  {total_fil:,}")
    print(
        f"  from tracked sponsors:     {cov_fil:,} ({100 * cov_fil / total_fil:.1f}%)  <- the number that matters"
    )
    print("\nTop 40 UNCOVERED sponsors by filing volume (registry gaps):")
    for name, n, last in uncovered[:40]:
        print(f"  {n:>6}  {name[:48]:48}  last {last}")

    GAP_OUT.parent.mkdir(exist_ok=True)
    GAP_OUT.write_text(
        json.dumps(
            {
                "total_sponsors": total_emp,
                "covered_sponsors": cov_emp,
                "total_filings": total_fil,
                "covered_filings": cov_fil,
                "uncovered_top": [
                    {"name": n, "filings": c, "last": d} for n, c, d in uncovered[:1000]
                ],
            },
            indent=2,
        )
    )
    print(f"\nwrote {GAP_OUT.relative_to(ROOT)} ({len(uncovered):,} uncovered total)")


if __name__ == "__main__":
    main()
