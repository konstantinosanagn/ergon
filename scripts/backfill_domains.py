"""Backfill missing ``domain`` values on the seed registry, deterministically and add-only.

The seed registry maps a company -> ``(ats, token, domain)`` triple. ``domain`` is the offline
lookup key the resolver uses (``lookup_domain``), but most seed entries never got one — they were
minted from an ATS token alone. This script fills that gap from two *deterministic, offline*
sources, never overwriting a curated domain and never inventing one:

  Source D (token host)  — many tokens embed the employer's own careers host
                           (``careers.ey.com|ey`` -> ``ey.com``). We parse a host out of the
                           token and reduce it to the registrable apex. ATS *vendor* hosts
                           (``x.icims.com``, ``y.taleo.net``, ``*.oraclecloud.com`` ...) are the
                           board backend, NOT the company domain, so they are denied.

  Source A (index)       — the built index carries a ``jobs.company_domain`` observed from live
                           postings. Aggregated per ``normalize_company`` (the exact join
                           ``coverage.py`` uses), a company that maps to EXACTLY ONE distinct
                           domain contributes that domain. Multi-domain keys are ambiguous and
                           skipped.

The two sources are unioned, then collisions are resolved so the result can NEVER create an
ambiguous ``domain -> key`` mapping: a candidate whose domain is already owned by an existing
entry is dropped (add-only never displaces the curated set), and when a *new* domain is claimed
by several new keys only one wins (most ``open_roles``, lexicographic key tie-break) —
deterministic and reproducible. Every surviving candidate is run through the same
``store._normalize_domain`` + shape regex the runtime uses, so no dirty value is ever written.

Usage:
    python scripts/backfill_domains.py [--index PATH] [--dry-run]

``--dry-run`` prints the add count and by-source breakdown and writes NOTHING.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# scripts/ on the path too, so ``build_registry`` imports whether this module is run as a script
# or imported by name (e.g. from tests).
sys.path.insert(0, str(ROOT / "scripts"))

# Reuse the exact advisory-lock critical section from build_registry so concurrent seed writers
# compose instead of clobbering (imported, never reinvented).
from build_registry import SEED, seed_lock  # noqa: E402

from ergon_tracker.dedup import normalize_company  # noqa: E402
from ergon_tracker.registry.store import _normalize_domain  # noqa: E402

DEFAULT_INDEX = Path(
    "/private/tmp/claude-501/-Users-kanagn-Desktop-job-researcher/"
    "d20c6e7c-0b7f-4b04-a828-a75251378b9c/scratchpad/index.sqlite"
)

# The runtime shape gate (store._normalize_domain lower-cases + strips ``www.``; this is the shape
# a clean registrable host must have). Anything else is dirty and rejected pre-write.
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")

# Registrable apexes of ATS *vendor* backends. A host under one of these is the board's plumbing,
# never the employer's own domain, so Source D denies it. Over-inclusive is safe (skips a
# candidate); under-inclusive would write an ATS host as a company domain, so we err long.
_ATS_VENDOR_APEXES = frozenset(
    {
        "taleo.net",
        "icims.com",
        "oraclecloud.com",
        "myworkdayjobs.com",
        "myworkdaysite.com",
        "workday.com",
        "ultipro.com",
        "ukg.net",
        "adp.com",
        "brassring.com",
        "avature.net",
        "successfactors.com",
        "sapsf.com",
        "sapsf.eu",
        "paycomonline.net",
        "dayforcehcm.com",
        "zwayam.com",
        "ripplehire.com",
        "pageuppeople.com",
        "peopleadmin.com",
        "usajobs.gov",
        "jobdiva.com",
        "ceipal.com",
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "workable.com",
        "bamboohr.com",
        "smartrecruiters.com",
        "recruitee.com",
        "personio.com",
        "personio.de",
        "breezy.hr",
        "teamtailor.com",
        "join.com",
        "rippling.com",
        "pinpointhq.com",
        "jazzhr.com",
        "applytojob.com",
        "jobvite.com",
        "phenompeople.com",
        "eightfold.ai",
        "applicantpro.com",
        "coveo.com",
        "jibeapply.com",  # Jibe/iCIMS apply host (dollargeneral.jibeapply.com), not the employer
        "cloud.sap",  # SAP SuccessFactors platform host (*.jobs.hr.cloud.sap), not the employer
        "sapsf.cn",
        "jobs2web.com",  # legacy SuccessFactors/Jobs2Web career-site host
        "recruitmenttechnologies.com",
    }
)

# A registrable apex must never START with one of these labels: it means the token embedded only a
# careers *prefix* with no real registrable domain (``careers.abb`` -> apex ``careers.abb``), which
# is a truncated/brand-TLD host, not a company domain. Genuine apexes reduce past their careers
# subdomain (``careers.ey.com`` -> ``ey.com``), so their leading label is the real name.
_GENERIC_HOST_PREFIXES = frozenset(
    {"careers", "career", "jobs", "job", "apply", "recruiting", "recruit", "hiring", "talent"}
)

# Registrable apexes with a two-label public suffix, so the apex reduction lands on the real
# registrable name (``jobs.acme.co.uk`` -> ``acme.co.uk``, not ``co.uk``). Mirrors http.py.
_TWO_LEVEL_TLDS = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "co.jp",
        "co.in",
        "com.br",
        "com.mx",
        "com.sg",
        "com.hk",
        "co.za",
        "com.tr",
        "co.il",
        "com.cn",
    }
)


def registrable_apex(host: str) -> str | None:
    """Reduce a host to its registrable apex, ccTLD-aware. ``None`` if it isn't a real host.

    ``careers.acme.com`` -> ``acme.com``; ``jobs.acme.co.uk`` -> ``acme.co.uk``.
    """
    host = _normalize_domain(host).split("@")[-1].split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return None
    last2 = ".".join(parts[-2:])
    if last2 in _TWO_LEVEL_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def host_from_token(token: str) -> str | None:
    """Parse a candidate host out of a seed ``token``, or ``None`` if it embeds no host.

    Handles the ``token.split("|")[0]`` composite form, a ``schemaorg:`` prefix, and full
    URLs. Only the *first* field is considered: for a few ATSes (e.g. adp) later ``|`` fields
    carry the vendor host, which we never want anyway.
    """
    if not token:
        return None
    first = token.split("|")[0].strip()
    if first.startswith("schemaorg:"):
        first = first[len("schemaorg:") :]
    if "://" in first:
        first = urlparse(first).netloc or first
    first = first.split("/")[0].strip()
    if "." not in first:
        return None
    return first.lower()


def domain_from_token(token: str) -> str | None:
    """Source D: the registrable apex for a token, unless it's an ATS vendor host (-> ``None``)."""
    host = host_from_token(token)
    if host is None:
        return None
    apex = registrable_apex(host)
    if apex is None or apex in _ATS_VENDOR_APEXES:
        return None
    if apex.split(".")[0] in _GENERIC_HOST_PREFIXES:
        return None  # truncated careers-prefix host (``careers.abb``), not a company domain
    return apex


def index_domain_map(index_path: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Read the index once, returning:

    * Source A map: ``normalize_company(company_key) -> domain`` for keys that map to EXACTLY ONE
      distinct (normalized) ``jobs.company_domain``. Ambiguous multi-domain keys are omitted.
    * ``open_roles`` per ``normalize_company(company_key)`` (from the ``companies`` table),
      summed across raw keys that normalize together — the deterministic collision tiebreaker.
    """
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        by_norm: dict[str, set[str]] = {}
        for company_key, company_domain in con.execute(
            "SELECT company_key, company_domain FROM jobs "
            "WHERE company_domain IS NOT NULL AND company_domain != ''"
        ):
            dom = _normalize_domain(str(company_domain))
            if not dom:
                continue
            by_norm.setdefault(normalize_company(str(company_key)), set()).add(dom)
        source_a = {n: next(iter(doms)) for n, doms in by_norm.items() if len(doms) == 1}

        open_roles: dict[str, int] = {}
        for company_key, roles in con.execute("SELECT company_key, open_roles FROM companies"):
            n = normalize_company(str(company_key))
            open_roles[n] = open_roles.get(n, 0) + int(roles or 0)
    finally:
        con.close()
    return source_a, open_roles


def build_candidates(
    companies: dict[str, dict], source_a: dict[str, str]
) -> dict[str, list[tuple[str, str]]]:
    """Per domainless key, the candidate ``(domain, source)`` list in PREFERENCE order.

    Source A (a domain observed on live index postings) is preferred over Source D (token host);
    duplicates (both sources agreeing) collapse to a single A candidate. Keeping both lets a key
    whose A domain turns out to be circular (already owned) fall back to its D domain instead of
    being lost.
    """
    chosen: dict[str, list[tuple[str, str]]] = {}
    for key, entry in companies.items():
        if entry.get("domain"):  # add-only: never touch an entry that already has a domain
            continue
        cands: list[tuple[str, str]] = []
        dom_a = source_a.get(normalize_company(key))
        if dom_a:
            cands.append((dom_a, "A"))
        dom_d = domain_from_token(str(entry.get("token") or ""))
        if dom_d and dom_d != dom_a:
            cands.append((dom_d, "D"))
        if cands:
            chosen[key] = cands
    return chosen


def resolve_collisions(
    chosen: dict[str, list[tuple[str, str]]],
    owned_domains: set[str],
    open_roles: dict[str, int],
) -> tuple[dict[str, tuple[str, str]], int]:
    """Reduce the per-key candidate lists to one ``(domain, source)`` per surviving key.

    (i) Any candidate whose domain is already owned by an existing entry is dropped (add-only
        never displaces the curated set); a key keeps its first non-owned candidate, so an owned
        Source-A domain falls back to Source D. (ii) When a *new* domain is claimed by 2+ new
        keys, keep the key with the most ``open_roles`` (lexicographic key tie-break). Returns
        (kept, skipped_collision).
    """
    # (i) per-key: first candidate whose domain isn't already owned.
    picked: dict[str, tuple[str, str]] = {}
    skipped = 0
    for key, cands in chosen.items():
        survivor = next(
            ((d, s) for d, s in cands if _normalize_domain(d) not in owned_domains), None
        )
        if survivor is None:
            skipped += 1  # every candidate for this key was already-owned (circular)
            continue
        picked[key] = survivor

    # (ii) cross-key: one winner per new domain.
    by_domain: dict[str, list[str]] = {}
    for key, (domain, _src) in picked.items():
        by_domain.setdefault(_normalize_domain(domain), []).append(key)

    kept: dict[str, tuple[str, str]] = {}
    for _domain, keys in by_domain.items():
        if len(keys) == 1:
            winner = keys[0]
        else:
            # most open_roles wins; tie-break on the lexicographically smallest key (min over the
            # negated-roles/key pair — deterministic, no custom ordering class needed).
            winner = min(keys, key=lambda k: (-open_roles.get(normalize_company(k), 0), k))
            skipped += len(keys) - 1
        kept[winner] = picked[winner]
    return kept, skipped


def backfill(index_path: Path, *, dry_run: bool) -> dict[str, int]:
    """Run the whole backfill. Returns a stats dict (also printed)."""
    source_a, open_roles = index_domain_map(index_path)

    with seed_lock():
        seed = json.loads(SEED.read_text())
        companies: dict[str, dict] = seed["companies"]

        owned = {
            _normalize_domain(str(e["domain"]))
            for e in companies.values()
            if e.get("domain")
        }
        before_owned = len(owned)
        entries_before = sum(1 for e in companies.values() if e.get("domain"))

        chosen = build_candidates(companies, source_a)
        kept, skipped_collision = resolve_collisions(chosen, owned, open_roles)

        # Pre-write sanity: every survivor must pass the runtime shape gate. A dirty value
        # (``www.x.com``, ``INC.,y.com``) is rejected rather than poisoning the registry.
        by_source = {"A": 0, "D": 0}
        added = 0
        rejected_dirty = 0
        for key in sorted(kept):
            domain, source = kept[key]
            clean = _normalize_domain(domain)
            if not _DOMAIN_RE.match(clean):
                rejected_dirty += 1
                continue
            entry = companies[key]
            if entry.get("domain"):  # belt-and-braces add-only guard
                continue
            entry["domain"] = clean
            by_source[source] += 1
            added += 1

        stats = {
            "added": added,
            "by_source_A": by_source["A"],
            "by_source_D": by_source["D"],
            "skipped_collision": skipped_collision,
            "rejected_dirty": rejected_dirty,
            "entries_before": entries_before,
            "entries_after": entries_before + added,
            "distinct_domains_before": before_owned,
            "distinct_domains_after": before_owned + added,
        }
        print(
            f"added={added} by_source={{'A': {by_source['A']}, 'D': {by_source['D']}}} "
            f"skipped_collision={skipped_collision} rejected_dirty={rejected_dirty}"
        )
        total = len(companies)
        print(
            f"entries with domain: {entries_before} -> {entries_before + added} of {total} "
            f"({100 * entries_before / total:.2f}% -> {100 * (entries_before + added) / total:.2f}%)"
        )
        print(
            f"distinct domains: {before_owned} -> {before_owned + added}"
        )

        if dry_run:
            print("\n--dry-run: seed.json NOT written")
            return stats

        seed["_meta"]["updated"] = dt.date.today().isoformat()
        # indent=1 + trailing newline EXACTLY matches build_registry.py's write so the diff is
        # minimal (only the changed/added domain lines move).
        SEED.write_text(json.dumps(seed, indent=1, ensure_ascii=False) + "\n")
        try:
            shown: Path | str = SEED.relative_to(ROOT)
        except ValueError:  # SEED points outside the repo (e.g. a test temp dir)
            shown = SEED
        print(f"\nwrote {shown}")
        return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="path to index.sqlite")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the add count + breakdown, write nothing"
    )
    args = parser.parse_args()
    backfill(args.index, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
