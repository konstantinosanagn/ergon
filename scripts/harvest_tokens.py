"""Brute-force ATS board-token discovery for PATH-BASED ATSes -> candidates.json.

Path-based ATSes (greenhouse ``boards.greenhouse.io/{token}``, lever, ashby,
smartrecruiters, workable) put the company's board token in a URL **path**, not a subdomain.
crt.sh therefore cannot enumerate them (see :mod:`harvest_crtsh`). But the token is almost
always a *predictable slug* of the company name (``Acme Labs Inc`` -> ``acmelabs``,
``acme-labs``, ``acmelabsinc`` ...). So instead of enumerating, we **guess**: generate a small
ordered set of plausible slug variations per company and probe each path-based ATS's public
API directly through jobspine's own provider stack. A token that returns >=1 job is live.

This is the keyless analog of the crt.sh harvester. No API key, no scraping, no paid service —
just the same public ATS endpoints jobspine already speaks, driven by name-slug heuristics
(approach borrowed from Babak-hasani/company-career-scraper).

Which ATSes this works for
--------------------------
Only **path-based, single-token** ATSes, probed in this priority order::

    greenhouse > lever > ashby > smartrecruiters > workable

Subdomain / triple-token ATSes (recruitee, personio, workday) are intentionally excluded —
they are handled by :mod:`harvest_crtsh`.

How probing works
-----------------
For each company we walk the ATSes in priority order; for each ATS we try every generated
token variation until one returns jobs, then **short-circuit**: the first (ats, variant) hit
wins and we move to the next company. So one company yields *at most one* candidate. Companies
are probed concurrently via an ``anyio`` task group, bounded by the shared ``AsyncFetcher``.

Propose, don't dispose
----------------------
Output is a ``candidates.json`` compatible with :mod:`build_registry`, which then **verifies
every candidate live** through jobspine's own providers before merging into ``seed.json``.
This script only *proposes*; ``build_registry.py`` *disposes*. We never write ``seed.json``.

Usage::

    # probe a list of company names (one per line, optional ",domain")
    .venv/bin/python scripts/harvest_tokens.py scripts/companies_to_probe.txt --limit 50

    # then verify + merge through the real provider stack
    .venv/bin/python scripts/build_registry.py scripts/candidates_tokens.json --dry-run
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobspine.http import AsyncFetcher  # noqa: E402
from jobspine.models import SearchQuery  # noqa: E402
from jobspine.providers.base import get_provider, load_builtins  # noqa: E402

__all__ = [
    "TARGET_ATSES",
    "company_key",
    "generate_token_variations",
    "parse_companies",
    "load_existing",
    "probe_company",
    "harvest",
]

SEED = ROOT / "src" / "jobspine" / "registry" / "data" / "seed.json"
DEFAULT_INPUT = ROOT / "scripts" / "companies_to_probe.txt"
DEFAULT_OUT = ROOT / "scripts" / "candidates_tokens.json"

# Path-based, single-token ATSes, probed in this priority order. The first ATS+variant that
# returns jobs for a company wins (short-circuit). NOT recruitee/personio/workday.
TARGET_ATSES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable")

# Corporate-form suffixes stripped from a trailing position to recover the "core" name. Ordered
# longest-first so multi-word forms are tried before their substrings.
_SUFFIXES = (
    "technologies",
    "holding",
    "holdings",
    "group",
    "labs",
    "gmbh",
    "corp",
    "inc",
    "llc",
    "ltd",
    "ag",
    "co",
    "sa",
    "bv",
)

# Second-level labels of common multi-part public suffixes (co.uk, com.au, ...). When a domain
# ends in one of these we step one label further left to find the real registrable name.
_MULTI_PART_TLD_HEADS = frozenset({"co", "com", "org", "net", "gov", "edu", "ac"})

# Token slugs are lowercase alphanumerics + hyphens; ATS APIs reject anything else.
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_KEEP_RE = re.compile(r"[^A-Za-z0-9]+")


# --- pure slug generation (no network; unit-tested) -------------------------------------------


def _strip_leading_the(name: str) -> str:
    """Drop a leading ``the `` (e.g. ``The Foo Company`` -> ``Foo Company``)."""
    return re.sub(r"^the\s+", "", name, flags=re.IGNORECASE)


def _strip_suffixes(words: list[str]) -> list[str]:
    """Repeatedly drop trailing corporate-form words (inc, llc, gmbh, ...)."""
    out = list(words)
    changed = True
    while changed and out:
        changed = False
        last = re.sub(r"[^a-z0-9]", "", out[-1].lower())
        if last in _SUFFIXES:
            out.pop()
            changed = True
    return out


def company_key(name: str) -> str:
    """A stable lowercase registry key for a company name.

    Lowercases, strips a leading ``the``, and collapses every run of non-alphanumerics to a
    single hyphen (``Acme Labs, Inc.`` -> ``acme-labs-inc``). This is the dict key under
    ``seed.json["companies"]``; it is *not* the ATS board token.
    """
    base = _strip_leading_the(name).strip().lower()
    slug = _NONALNUM_RE.sub("-", base).strip("-")
    return slug


def generate_token_variations(name: str, domain: str | None = None) -> list[str]:
    """Return ~10-15 ordered, deduped candidate board-token slugs for a company name.

    Strategy (most-likely first): lowercase no-spaces, lowercase hyphenated, punctuation-
    stripped, CamelCase no-spaces, original-case no-spaces, plus the same family with trailing
    corporate suffixes (inc/llc/ltd/gmbh/...) removed and a leading ``the`` dropped. If a
    ``domain`` is given, its second-level label is added as a strong candidate.

    Pure and network-free so it can be unit-tested. Order matters: probing stops at the first
    live variant, so cheaper/more-likely guesses come first.
    """
    variants: list[str] = []

    def add(token: str) -> None:
        token = token.strip().strip("-")
        if token and token not in variants:
            variants.append(token)

    def family(raw: str) -> None:
        """Add the slug family for one source string."""
        words = [w for w in re.split(r"\s+", raw.strip()) if w]
        # lowercase no-spaces (collapse all punctuation away)
        add(_NONALNUM_RE.sub("", raw.lower()))
        # lowercase hyphenated (each punctuation run -> one hyphen)
        add(_NONALNUM_RE.sub("-", raw.lower()).strip("-"))
        # CamelCase no-spaces (preserve original capitalisation, drop separators)
        add(_CAMEL_KEEP_RE.sub("", raw))
        # original-case, punctuation-stripped, spaces -> hyphen
        add(_CAMEL_KEEP_RE.sub("-", raw).strip("-"))
        # suffix-stripped variants
        stripped = _strip_suffixes(words)
        if stripped and stripped != words:
            joined = " ".join(stripped)
            add(_NONALNUM_RE.sub("", joined.lower()))
            add(_NONALNUM_RE.sub("-", joined.lower()).strip("-"))
            add(_CAMEL_KEEP_RE.sub("", joined))

    family(name)
    # also the "the "-removed form (only differs when name actually starts with "the ")
    no_the = _strip_leading_the(name)
    if no_the != name:
        family(no_the)

    if domain:
        label = domain.strip().lower()
        label = re.sub(r"^https?://", "", label)
        label = label.split("/")[0].split(":")[0]
        # second-level label: foo.com -> foo, jobs.foo.com -> foo, foo.co.uk -> foo
        parts = [p for p in label.split(".") if p]
        # Skip a trailing multi-part public suffix (co.uk, com.au, ...) so we land on the
        # real registrable label rather than the "co"/"com" filler.
        if len(parts) >= 3 and parts[-2] in _MULTI_PART_TLD_HEADS:
            sld = parts[-3]
        elif len(parts) >= 2:
            sld = parts[-2]
        elif parts:
            sld = parts[0]
        else:
            sld = ""
        add(_NONALNUM_RE.sub("", sld))

    return variants


# --- input parsing (no network; unit-tested) --------------------------------------------------


def parse_companies(text: str) -> list[tuple[str, str | None]]:
    """Parse an input file into ``[(name, domain|None), ...]``.

    One company per line; an optional ``,domain`` after a comma. Blank lines and ``#`` comments
    are ignored. Never raises.
    """
    out: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(",")
        name = name.strip()
        domain = rest.strip() or None
        if name:
            out.append((name, domain))
    return out


# --- existing-registry awareness --------------------------------------------------------------


def load_existing(seed_path: Path = SEED) -> tuple[set[str], dict[str, set[str]]]:
    """Return ``(company_keys, {ats: {tokens}})`` already present in the seed registry.

    Mirrors :func:`harvest_crtsh.load_existing` so candidates already in the seed are skipped.
    """
    if not seed_path.exists():
        return set(), {}
    seed = json.loads(seed_path.read_text())
    companies: dict[str, dict] = seed.get("companies", {})
    keys = set(companies)
    tokens_by_ats: dict[str, set[str]] = {}
    for entry in companies.values():
        ats = entry.get("ats")
        token = entry.get("token")
        if isinstance(ats, str) and isinstance(token, str):
            tokens_by_ats.setdefault(ats, set()).add(token)
    return keys, tokens_by_ats


# --- network probing --------------------------------------------------------------------------


async def _is_live(ats: str, token: str, fetcher: AsyncFetcher) -> bool:
    """True if ``ats`` returns >=1 job for ``token``. Network errors -> False (never raise)."""
    provider = get_provider(ats)
    if provider is None:
        return False
    try:
        raws = await provider.fetch(token, SearchQuery(limit=1), fetcher)
    except Exception:  # noqa: BLE001 - a dead token / 404 / timeout just means "not this one"
        return False
    return bool(raws)


async def probe_company(
    name: str, domain: str | None, fetcher: AsyncFetcher
) -> dict[str, object] | None:
    """Probe one company across the target ATSes and return its first live candidate, or None.

    Walks ATSes in :data:`TARGET_ATSES` priority order; for each ATS tries the generated token
    variations in order and short-circuits on the first hit. Never raises — a fully-dead
    company simply yields ``None``.
    """
    variations = generate_token_variations(name, domain)
    key = company_key(name)
    for ats in TARGET_ATSES:
        for token in variations:
            if await _is_live(ats, token, fetcher):
                return {"company": key, "ats": ats, "token": token, "domain": domain}
    return None


async def harvest(
    companies: list[tuple[str, str | None]], fetcher: AsyncFetcher
) -> list[dict[str, object]]:
    """Probe many companies concurrently, skipping ones already in the seed registry.

    Per-company failures are isolated: one company crashing or timing out can never abort the
    sweep. Returns candidates in input order.
    """
    existing_keys, _existing_tokens = load_existing()
    results: dict[int, dict[str, object] | None] = {}

    async def _run(i: int, name: str, domain: str | None) -> None:
        try:
            results[i] = await probe_company(name, domain, fetcher)
        except Exception as exc:  # noqa: BLE001 - report, never crash the whole sweep
            print(f"  [{name}] probe failed: {type(exc).__name__}: {exc}")
            results[i] = None

    async with anyio.create_task_group() as tg:
        for i, (name, domain) in enumerate(companies):
            if company_key(name) in existing_keys:
                print(f"  [{name}] skip: already in seed ({company_key(name)})")
                continue
            tg.start_soon(_run, i, name, domain)

    candidates: list[dict[str, object]] = []
    for i in sorted(results):
        cand = results[i]
        if cand is not None:
            candidates.append(cand)
    return candidates


async def main() -> None:
    args = sys.argv[1:]
    in_path = DEFAULT_INPUT
    out_path = DEFAULT_OUT
    limit: int | None = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif arg == "--limit":
            limit = int(args[i + 1])
            i += 2
        elif arg.startswith("--"):
            print(f"unknown flag: {arg}")
            return
        else:
            positional.append(arg)
            i += 1

    if positional:
        in_path = Path(positional[0])
    if not in_path.exists():
        print(f"input file not found: {in_path}")
        return

    companies = parse_companies(in_path.read_text())
    if limit is not None:
        companies = companies[:limit]
    print(f"probing {len(companies)} companies across {list(TARGET_ATSES)}  (limit={limit})")

    load_builtins()
    async with AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher:
        candidates = await harvest(companies, fetcher)

    by_ats: dict[str, int] = {}
    for c in candidates:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"new candidates: {len(candidates)}  by_ats={by_ats}")

    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"wrote {shown}")
    print(f"\nnext: .venv/bin/python scripts/build_registry.py {shown} --dry-run")


if __name__ == "__main__":
    anyio.run(main)
