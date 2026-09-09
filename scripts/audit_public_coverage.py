"""Audit how many US-listed public companies (SEC) we cover — registry presence + live postings.

Uses entity resolution (``company_resolve``) so legal names ("Cisco Systems, Inc.") and brand
renames ("Alphabet"->google) match our short registry keys, giving a real coverage number instead
of the slug-mismatch undercount. Writes the *missing* public companies to a names file that
``seed_gov_names``/``harvest_tokens`` can target.

Usage::

    .venv/bin/python scripts/audit_public_coverage.py [--out-missing PATH] [--cache PATH]
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from company_resolve import build_key_index, match_keys  # noqa: E402

from ergon.dedup import normalize_company  # noqa: E402
from ergon.index.cache import _default_cache_dir  # noqa: E402
from ergon.registry.store import SeedRegistry  # noqa: E402

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
_UA = "ergon research konstantinos.a@tavily.com"  # SEC requires a UA with contact


def fetch_sec(cache: Path) -> dict[str, str]:
    """Return {normalized_name: title} for all SEC-listed companies (cached to ``cache``)."""
    if cache.exists():
        raw = json.loads(cache.read_text())
    else:
        req = urllib.request.Request(SEC_URL, headers={"User-Agent": _UA})
        raw = json.load(urllib.request.urlopen(req, timeout=30))  # noqa: S310 - fixed https URL
        cache.write_text(json.dumps(raw))
    out: dict[str, str] = {}
    for row in raw.values():
        out[normalize_company(row["title"])] = row["title"]
    return out


def index_job_keys(db: Path) -> set[str]:
    """Match-key index over index companies that currently have >=1 open role."""
    if not db.exists():
        return set()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [
            ck for ck, orl in con.execute("SELECT company_key, open_roles FROM companies") if orl
        ]
    finally:
        con.close()
    return build_key_index(names)


def audit(sec: dict[str, str], reg_keys: set[str], job_keys: set[str]) -> dict[str, object]:
    """Classify each SEC company as in-registry / has-live-jobs / missing (by entity match)."""
    in_reg = with_jobs = 0
    missing: list[str] = []
    for title in sec.values():
        keys = match_keys(title)
        covered = bool(keys & reg_keys)
        if covered:
            in_reg += 1
        else:
            missing.append(title)
        if keys & job_keys:
            with_jobs += 1
    total = len(sec)
    return {
        "public_companies": total,
        "in_registry": in_reg,
        "in_registry_pct": round(100 * in_reg / total, 1) if total else 0,
        "with_live_postings": with_jobs,
        "with_live_postings_pct": round(100 * with_jobs / total, 1) if total else 0,
        "missing": missing,
    }


def main() -> None:
    args = sys.argv[1:]
    out_missing = ROOT / "scripts" / "missing_public_companies.txt"
    cache = Path("/tmp/sec_company_tickers.json")
    i = 0
    while i < len(args):
        if args[i] == "--out-missing":
            out_missing = Path(args[i + 1])
            i += 2
        elif args[i] == "--cache":
            cache = Path(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    sec = fetch_sec(cache)
    reg_keys = build_key_index(SeedRegistry().all().keys())
    job_keys = index_job_keys(_default_cache_dir() / "index.sqlite")
    rep = audit(sec, reg_keys, job_keys)
    missing = rep.pop("missing")
    print(json.dumps(rep, indent=2))
    out_missing.write_text("\n".join(sorted(missing)) + "\n")  # type: ignore[arg-type]
    print(f"\nwrote {len(missing)} missing public companies -> {out_missing}")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
