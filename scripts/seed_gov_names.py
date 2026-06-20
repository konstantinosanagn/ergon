"""Build a prioritized, registry-filtered company-name seed for ``harvest_tokens`` from
government data we already ship: H-1B LCA sponsors + SEC EDGAR public companies.

Both are high-signal name sources for ATS token discovery — companies that sponsor visas or file
with the SEC are real, hiring employers that overwhelmingly run a modern ATS. The US has no
single "all companies" registry (registration is state-level), so these federal datasets are the
best free name sources; raw business lists are mostly tiny firms with no ATS.

Prioritization: H-1B sponsors are ordered by filing volume ``n`` (a size / hiring-velocity proxy)
then recency ``last``, so the biggest, most-active employers are probed first within a bounded
slice. Both seeds are pre-filtered against the CURRENT registry (and cross-seed-deduped) so we
never waste probes on boards we already have.

Output is a names file (one company per line) for ``scripts/harvest_tokens.py``, which generates
slug variations and probes path-based ATSes; ``build_registry`` then verifies + merges.

Usage::

    .venv/bin/python scripts/seed_gov_names.py [--h1b-limit N] [--edgar-limit N] [--out PATH]
    .venv/bin/python scripts/harvest_tokens.py scripts/gov_names.txt --limit 50
    .venv/bin/python scripts/build_registry.py scripts/candidates_tokens.json --gentle --onboard-empty
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_tokens import company_key, load_existing  # noqa: E402

H1B = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "h1b_sponsors.json"
EDGAR = ROOT / "scripts" / "_edgar_candidates.json"
DEFAULT_OUT = ROOT / "scripts" / "gov_names.txt"


def h1b_prioritized(sponsors: dict, existing_keys: set[str], limit: int) -> list[str]:
    """H-1B sponsor names ordered by filing volume (``n`` desc) then recency (``last`` desc),
    excluding companies already in the registry. Highest-``n`` = biggest / most-active first."""
    ranked = sorted(
        sponsors.items(),
        # `or` (not get-default) so an explicit null n/last in the data coerces to 0/"".
        key=lambda kv: (kv[1].get("n") or 0, kv[1].get("last") or ""),
        reverse=True,
    )
    out: list[str] = []
    for name, _info in ranked:
        if not name or company_key(name) in existing_keys:
            continue
        out.append(name)
        if len(out) >= limit:
            break
    return out


def edgar_names(edgar: dict, existing_keys: set[str], limit: int) -> list[str]:
    """SEC EDGAR public-company names not already in the registry (near-100% ATS users)."""
    out: list[str] = []
    for name in edgar:
        if not name or company_key(name) in existing_keys:
            continue
        out.append(name)
        if len(out) >= limit:
            break
    return out


def build_seed(
    sponsors: dict,
    edgar: dict,
    existing_keys: set[str],
    *,
    h1b_limit: int,
    edgar_limit: int,
) -> list[str]:
    """Combined name list: H-1B (priority-ordered) first, then EDGAR, deduped across both seeds
    and against the registry."""
    names = h1b_prioritized(sponsors, existing_keys, h1b_limit)
    seen = {company_key(n) for n in names}
    for name in edgar_names(edgar, existing_keys, edgar_limit):
        key = company_key(name)
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    h1b_limit = 400
    edgar_limit = 400
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--h1b-limit":
            h1b_limit = int(args[i + 1])
            i += 2
        elif args[i] == "--edgar-limit":
            edgar_limit = int(args[i + 1])
            i += 2
        elif args[i] == "--limit":
            h1b_limit = edgar_limit = int(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    sponsors = json.loads(H1B.read_text()).get("sponsors", {}) if H1B.exists() else {}
    edgar = json.loads(EDGAR.read_text()) if EDGAR.exists() else {}
    existing_keys, _ = load_existing()
    names = build_seed(sponsors, edgar, existing_keys, h1b_limit=h1b_limit, edgar_limit=edgar_limit)
    out_path.write_text("\n".join(names) + "\n")
    rel = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
    print(
        f"h1b_sponsors={len(sponsors)} edgar={len(edgar)} registry={len(existing_keys)} "
        f"-> {len(names)} prioritized new names"
    )
    print(f"wrote {rel}")
    print(f"next: .venv/bin/python scripts/harvest_tokens.py {rel} --limit 50")


if __name__ == "__main__":
    main()
