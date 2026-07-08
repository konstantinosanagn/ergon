"""Purge low-confidence label-pass entries from scripts/sector_wikidata.json (offline).

The Wikidata harvest's domain pass (P856) is clean; its label pass matches short/generic company
slugs to unrelated entities (e.g. `harper`→"pornography industry", `hud`→"shipbuilding"). This drops
the obvious junk — blacklisted industries + very short slugs — and rewrites the committed json (an
auditable diff). It does NOT re-query Wikidata; the committed json is the input.

Usage:
  .venv/bin/python scripts/clean_sector_wikidata.py            # apply (rewrites the json)
  .venv/bin/python scripts/clean_sector_wikidata.py --dry-run  # preview counts only
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WD = ROOT / "scripts" / "sector_wikidata.json"

# Industries that are near-always spurious entity collisions for employers in our registry (no real
# employer here legitimately carries them). Conservative — extend only with clearly-junk industries.
WD_JUNK_INDUSTRIES: frozenset[str] = frozenset({"pornography industry"})

# Label-pass acronym collisions: slugs this short (<=3 chars) almost never match the right entity.
SHORT_SLUG_MAX: int = 3


def clean(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Return (cleaned_raw, drop_counts). Keeps full records for survivors."""
    cleaned: dict[str, Any] = {}
    drops: dict[str, int] = {"junk_industry": 0, "short_slug": 0}
    for key, rec in raw.items():
        if rec.get("wd_industry") in WD_JUNK_INDUSTRIES:
            drops["junk_industry"] += 1
            continue
        if len(key) <= SHORT_SLUG_MAX:
            drops["short_slug"] += 1
            continue
        cleaned[key] = rec
    return cleaned, drops


def main(argv: list[str]) -> None:
    dry = "--dry-run" in argv
    raw: dict[str, Any] = json.loads(WD.read_text())
    cleaned, drops = clean(raw)
    print(
        f"[clean-wd] {len(raw)} -> {len(cleaned)} "
        f"(dropped junk_industry={drops['junk_industry']}, short_slug={drops['short_slug']})"
    )
    if dry:
        print("[clean-wd] dry-run — not written.")
        return
    WD.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n")
    print(f"[clean-wd] wrote {WD.name}")


if __name__ == "__main__":
    main(sys.argv[1:])
