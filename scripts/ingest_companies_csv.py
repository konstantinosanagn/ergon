"""Ingest curated company-registry CSVs -> a candidates.json for build_registry.

This is the "one CSV per ATS, PR adds tenants, CI verifies" contribution path: humans
hand-curate ``data/companies/{ats}.csv`` (one ATS per file, one company per row); this script
flattens those CSVs into the candidate schema that :mod:`build_registry` understands, then
``build_registry.py`` **verifies every candidate live** through ergon_tracker's own providers before
merging into ``seed.json``.

Propose, don't dispose
----------------------
Like :mod:`harvest_crtsh`, this script only *proposes*: it never writes ``seed.json``. It also
skips any company whose key already exists in the seed registry, so re-adding a known company in
a CSV is harmless. Output is a ``candidates.json`` compatible with :mod:`build_registry`.

CSV schema (header row required)
--------------------------------
``name,ats,token,domain``

* ``name``   -> human company name; the company *key* is derived from it via :func:`company_key`.
* ``ats``    -> one of greenhouse, lever, ashby, workday, smartrecruiters, workable, recruitee,
  personio. May be left blank, in which case the file stem is used (so ``greenhouse.csv`` rows
  default to ``ats=greenhouse``).
* ``token``  -> the board token / slug / tenant. For **workday** rows this column holds the
  pipe-composite ``tenant|wd|site`` (e.g. ``nvidia|wd5|NVIDIAExternalCareerSite``); it is split
  into the ``tenant`` / ``wd`` / ``site`` fields the candidate schema expects.
* ``domain`` -> optional company domain; an empty cell becomes ``null``.

Blank lines and lines whose first cell starts with ``#`` are skipped as comments.

Usage::

    # read every data/companies/*.csv and propose candidates
    .venv/bin/python scripts/ingest_companies_csv.py --out scripts/candidates_csv.json

    # or pass explicit files
    .venv/bin/python scripts/ingest_companies_csv.py data/companies/greenhouse.csv --out out.json

    # then verify + merge through the real provider stack
    .venv/bin/python scripts/build_registry.py scripts/candidates_csv.json --dry-run
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
COMPANIES_DIR = ROOT / "data" / "companies"
DEFAULT_OUT = ROOT / "scripts" / "candidates_csv.json"

# ATS names build_registry / the provider stack understand. Mirrors ATS_PRIORITY there.
SUPPORTED_ATSES = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "workable",
        "recruitee",
        "personio",
    }
)

# Non-alphanumeric runs collapse to a single hyphen for the company key slug.
_SLUG_RE = re.compile(r"[^a-z0-9]+")

__all__ = [
    "SUPPORTED_ATSES",
    "company_key",
    "parse_csv_rows",
    "merge_candidates",
    "load_existing_keys",
]


# --- pure helpers (no network, no filesystem; unit-tested) ------------------------------------


def company_key(name: str) -> str:
    """Slugify a human company name into a registry key.

    Lowercase, collapse any run of non-alphanumerics to a single hyphen, and trim leading/
    trailing hyphens. ``"Acme, Inc."`` -> ``"acme-inc"``, ``"1Password"`` -> ``"1password"``.
    """
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def parse_csv_rows(text: str, default_ats: str) -> tuple[list[dict], list[str]]:
    """Parse one CSV file's text into ``(candidates, errors)``.

    Pure: no network and no filesystem. Expects a ``name,ats,token,domain`` header row. Blank
    lines and ``#`` comment lines are skipped. Each row's ``ats`` defaults to ``default_ats``
    (the file stem) when blank. Unsupported ATS names, missing names/tokens, and malformed
    workday composites are collected into ``errors`` (with 1-based row numbers) rather than
    raising. Candidates are deduped by company key *within this file* (first row wins).

    Returns candidate dicts in the exact schema :mod:`build_registry` expects:

    * simple ATS: ``{"company", "ats", "token", "domain"}``
    * workday:    ``{"company", "ats": "workday", "tenant", "wd", "site", "domain"}``
    """
    candidates: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return [], []

    # Drop a header row if present (first cell is literally "name").
    start = 1 if rows and rows[0] and rows[0][0].strip().lower() == "name" else 0

    for lineno, raw in enumerate(rows[start:], start=start + 1):
        if not raw or not any(cell.strip() for cell in raw):
            continue  # blank line
        if raw[0].lstrip().startswith("#"):
            continue  # comment line

        cells = [c.strip() for c in raw] + ["", "", "", ""]
        name, ats, token, domain = cells[0], cells[1], cells[2], cells[3]

        ats = (ats or default_ats).lower()
        if not name:
            errors.append(f"row {lineno}: missing name")
            continue
        if ats not in SUPPORTED_ATSES:
            errors.append(f"row {lineno}: unsupported ats {ats!r} ({name})")
            continue
        if not token:
            errors.append(f"row {lineno}: missing token ({name})")
            continue

        key = company_key(name)
        if not key:
            errors.append(f"row {lineno}: name {name!r} slugs to empty key")
            continue
        if key in seen:
            continue  # within-file dedupe; first row wins
        seen.add(key)

        dom = domain or None

        if ats == "workday":
            parts = [p.strip() for p in token.split("|")]
            if len(parts) != 3 or not all(parts):
                errors.append(
                    f"row {lineno}: workday token must be 'tenant|wd|site', got {token!r} ({name})"
                )
                seen.discard(key)
                continue
            tenant, wd, site = parts
            candidates.append(
                {
                    "company": key,
                    "ats": "workday",
                    "tenant": tenant,
                    "wd": wd,
                    "site": site,
                    "domain": dom,
                }
            )
        else:
            candidates.append({"company": key, "ats": ats, "token": token, "domain": dom})

    return candidates, errors


def merge_candidates(per_file: list[list[dict]]) -> list[dict]:
    """Merge candidate lists from several files, deduping by company key (first file wins).

    Pure: order-preserving across files in the order given.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for candidates in per_file:
        for cand in candidates:
            key = str(cand["company"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(cand)
    return merged


# --- existing-registry awareness (filesystem) -------------------------------------------------


def load_existing_keys(seed_path: Path = SEED) -> set[str]:
    """Return the set of company keys already present in the seed registry (``set()`` if absent).

    Reimplements the small loader from :mod:`harvest_crtsh` locally so this script has no
    cross-script import dependency.
    """
    if not seed_path.exists():
        return set()
    seed = json.loads(seed_path.read_text())
    companies: dict[str, dict] = seed.get("companies", {})
    return set(companies)


# --- CLI --------------------------------------------------------------------------------------


def _csv_paths(explicit: list[str]) -> list[Path]:
    """Resolve which CSVs to read: explicit paths, else every ``data/companies/*.csv`` sorted."""
    if explicit:
        return [Path(p) for p in explicit]
    return sorted(COMPANIES_DIR.glob("*.csv"))


def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    explicit: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif arg.startswith("--"):
            print(f"unknown flag: {arg}")
            return
        else:
            explicit.append(arg)
            i += 1

    paths = _csv_paths(explicit)
    if not paths:
        print(f"no CSVs found (looked in {COMPANIES_DIR.relative_to(ROOT)}/*.csv)")
        return

    existing = load_existing_keys()

    per_file: list[list[dict]] = []
    rows_read = 0
    by_ats: dict[str, int] = {}
    all_errors: list[str] = []

    for path in paths:
        if not path.exists():
            all_errors.append(f"{path}: file not found")
            continue
        default_ats = path.stem.lower()
        candidates, errors = parse_csv_rows(path.read_text(), default_ats)
        rows_read += len(candidates) + len(errors)
        for cand in candidates:
            by_ats[str(cand["ats"])] = by_ats.get(str(cand["ats"]), 0) + 1
        all_errors.extend(f"{path.name}: {e}" for e in errors)
        per_file.append(candidates)

    merged = merge_candidates(per_file)

    new = [c for c in merged if str(c["company"]) not in existing]
    skipped = len(merged) - len(new)

    out_path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path

    print(f"files={len(paths)}  rows_read={rows_read}  by_ats={by_ats}")
    print(f"new_candidates={len(new)}  skipped_existing={skipped}  malformed={len(all_errors)}")
    if all_errors:
        print("\nMALFORMED (first 20):")
        for e in all_errors[:20]:
            print(f"  {e}")
    print(f"\nwrote {shown}")
    print(f"\nnext: .venv/bin/python scripts/build_registry.py {shown} --dry-run")


if __name__ == "__main__":
    main()
