"""ATS-Exhaustion Sweep — the standardized ladder runner (predecessor to ANY browser work).

For each company, walk the ATS-exhaustion ladder **in order**, stop at the first rung that yields an
**entity-correct** hit (adjudicated through our own provider stack), and record every rung's outcome.
A company becomes "browser-eligible" ONLY after every autonomous rung is logged as failed.

    THE GATE: never assume a board needs a browser. The browser subsystem consumes ONLY the
    `browser_queue.json` this script emits (`ats_exhausted == true`). See
    docs/superpowers/plans/2026-06-21-ats-exhaustion-ladder.md.

Ladder rungs this runner executes autonomously (no WebSearch / no manual / no browser):
  0  registry hit            — already in seed.json? (skip; already captured)
  1  correct-tenant discovery — curl_cffi the careers page; extract ATS URLs; provider.matches() -> fetch
  2  brute-force token disc.  — harvest_tokens.probe_company (path-based + icims/taleo host guesses)
  5  federation/aggregator    — dejobs (hyphenated slug) + themuse (brand name), entity-clean
  7  schema.org               — JSON-LD / job sitemap via the schemaorg provider

Rungs 3 (WebSearch the apply URL) and 6 (bespoke apicapture spec) are NOT autonomously scriptable;
they are logged as `deferred: needs agent/manual` so a human/agent pass can complete them before a
board is declared browser-eligible. Rung 4 (per-provider mode variants) is folded into rung 1 (every
ATS URL found is run through that provider, whose own token-form logic covers its modes).

Propose, don't dispose: emits `candidates.json` (compatible with build_registry.py, which live-verifies
again before merging) + `browser_queue.json` (the genuine residual) + one append-only log per company.

Usage::
    .venv/bin/python scripts/ats_exhaustion_sweep.py scripts/companies_to_probe.txt --limit 100
    .venv/bin/python scripts/ats_exhaustion_sweep.py --gaps        # sweep current S&P 500 gaps
    .venv/bin/python scripts/build_registry.py scripts/cand_exhaustion.json --dry-run
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
from pathlib import Path
from typing import Any

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Reuse the proven brute-force + entity-adjudication logic (do not reinvent).
from harvest_tokens import (  # noqa: E402
    company_key,
    load_existing,
    name_match,
    parse_companies,
    probe_company,
)

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import (  # noqa: E402
    get_provider,
    iter_providers,
    load_builtins,
)

LOG_DIR = ROOT / "runs" / "exhaustion"
OUT_CANDIDATES = ROOT / "scripts" / "cand_exhaustion.json"
OUT_BROWSER_QUEUE = ROOT / "scripts" / "browser_queue.json"
PROBE = SearchQuery(limit=8)  # small live probe; enough to adjudicate entity

# ATS host/URL signatures to pull out of a careers page (rung 1). Each captured URL is handed to
# every provider's matches() — the provider's own logic maps it to a token (and thus its modes).
_ATS_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*?(?:"
    r"myworkdayjobs\.com|oraclecloud\.com|\.icims\.com|successfactors\.(?:com|eu)|\.taleo\.net|"
    r"phenompeople\.com|\.recruiting\.com|avature\.net|dayforcehcm\.com|ultipro\.com|"
    r"pageuppeople\.com|jobvite\.com|brassring\.com|applytojob\.com|jazz\.co|jazzhr|"
    r"radancy|talentbrew|/search-jobs|smartrecruiters\.com|boards\.greenhouse\.io|"
    r"jobs\.lever\.co|jobs\.ashbyhq\.com|paylocity\.com|recruiting\.adp\.com|workforcenow\.adp\.com"
    r")[^\s\"'<>]*",
    re.IGNORECASE,
)
# Careers-page URL candidates derived from a bare domain (cheapest first).
_CAREERS_PATHS = (
    "https://{d}/careers",
    "https://careers.{d}",
    "https://jobs.{d}",
    "https://www.{d}/careers",
    "https://{d}/jobs",
)


def _make_candidate(ats: str, token: str, name: str, domain: str | None) -> dict[str, Any]:
    """Build a build_registry-compatible candidate.

    Workday is special: build_registry rebuilds its token from ``tenant|wd|site`` fields, so a
    Workday hit (whose ``provider.matches()`` returns the ``tenant|wd|site`` composite) must be
    split into those fields — otherwise the hardened gate rejects it as a malformed candidate and
    the board is silently lost. Non-Workday ATSes carry a plain ``token``.
    """
    cand: dict[str, Any] = {
        "company": company_key(name),
        "ats": ats,
        "token": token,
        "domain": domain,
    }
    if ats == "workday" and token.count("|") == 2:
        tenant, wd, site = token.split("|")
        cand.update(tenant=tenant, wd=wd, site=site)
    return cand


def classify_exhaustion(captured: bool, rung1_inspected: bool) -> str:
    """The rigor gate, as pure logic.

    A company is only browser-eligible (``ats-exhausted``) when every autonomous rung failed AND
    rung 1 actually INSPECTED a careers page. If we never even fetched a careers page, the ATS
    path is unproven — the board could be on any host — so the company is ``incomplete`` and must
    NOT reach the browser queue. This is the anti-"assume-it-needs-a-browser" guard, decoupled
    from any log-string wording.
    """
    if captured:
        return "captured"
    return "ats-exhausted" if rung1_inspected else "incomplete-needs-careers-url"


# Statuses after which a re-run learns nothing new -> skip on --resume. timeout / incomplete /
# error are NON-terminal: a later run (less load, a domain now supplied, a recovered host) may yet
# succeed, so they are always re-probed.
_TERMINAL_STATUSES = frozenset({"captured", "ats-exhausted", "already-in-registry"})


def _is_done(rec: dict[str, Any]) -> bool:
    """True if a prior per-company log is definitively terminal (skip it on --resume)."""
    return rec.get("status") in _TERMINAL_STATUSES


def _aggregate_from_logs(log_dir: Path = LOG_DIR) -> tuple[list[dict], list[dict]]:
    """Rebuild ``(candidates, browser_queue)`` cumulatively from ALL per-company logs.

    The per-company logs are the source of truth, so the aggregate outputs are deduped and
    idempotent across batches — a second sweep never clobbers the first's results, it unions them.
    """
    candidates: list[dict] = []
    browser_queue: list[dict] = []
    seen_c: set[str] = set()
    seen_b: set[str] = set()
    for f in sorted(log_dir.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        key = rec.get("company")
        if rec.get("status") == "captured" and rec.get("candidate") and key not in seen_c:
            seen_c.add(key)
            candidates.append(rec["candidate"])
        if rec.get("ats_exhausted") and key not in seen_b:
            seen_b.add(key)
            browser_queue.append(
                {
                    "company": key,
                    "name": rec.get("name"),
                    "domain": rec.get("domain"),
                    "exhaustion_log": rec.get("rungs", []),
                }
            )
    return candidates, browser_queue


def _adjudicate(company: str, raws: list, provider, trust: bool = False) -> bool:
    """True if the provider returned >=1 job that plausibly IS this company.

    `trust=True` for entity-clean federations (dejobs/themuse expose company_exact). Otherwise we
    guard slug collisions with name_match against the board's reported company + a title sample.
    """
    if not raws:
        return False
    if trust:
        return True
    boards = {str(getattr(r, "company", "") or "") for r in raws[:5]}
    return any(name_match(company, b) for b in boards if b)


async def _careers_html(domain: str | None, fetcher: AsyncFetcher) -> str:
    """Fetch the company's careers page (curl_cffi TLS — careers pages are often WAF'd)."""
    if not domain:
        return ""
    from curl_cffi.requests import AsyncSession

    # Tight per-URL timeout: some careers hosts (Akamai/WAF) tarpit non-browser clients, so a long
    # timeout × several URL candidates can hang a worker. Fail fast and let later rungs/queue handle it.
    async with AsyncSession(impersonate="chrome124", verify=False, timeout=8) as s:
        for tmpl in _CAREERS_PATHS:
            url = tmpl.format(d=domain)
            try:
                r = await s.get(url, allow_redirects=True)
            except Exception:
                continue
            if r.status_code == 200 and len(r.text) > 1500:
                return r.text
    return ""


async def _rung1_tenant_discovery(
    name: str, domain: str | None, fetcher: AsyncFetcher
) -> tuple[dict | None, str, bool]:
    """Grep the careers page for ATS URLs; map each via provider.matches(); fetch + adjudicate.

    Returns ``(candidate_or_None, note, inspected)``. ``inspected`` is the structured signal the
    rigor gate consumes: True iff a careers page was actually fetched and examined (so a failure
    genuinely proves the ATS path is dead, not merely unreached).
    """
    html = await _careers_html(domain, fetcher)
    if not html:
        return (
            None,
            ("no careers page resolved from domain" if domain else "no domain given"),
            False,
        )
    urls = list(dict.fromkeys(_ATS_URL_RE.findall(html)))[:25]
    if not urls:
        return None, "careers page has no recognizable ATS host", True
    providers = list(iter_providers())
    tried: list[str] = []
    for url in urls:
        for prov in providers:
            try:
                token = prov.matches(url)
            except Exception:
                token = None
            if not token:
                continue
            tag = f"{prov.name}|{token}"
            if tag in tried:
                continue
            tried.append(tag)
            try:
                raws = await prov.fetch(token, PROBE, fetcher)
            except Exception:
                continue
            if _adjudicate(name, raws, prov):
                return _make_candidate(prov.name, token, name, domain), f"HIT via {tag}", True
    return (
        None,
        f"{len(tried)} (provider,token) candidates from careers page, none entity-correct",
        True,
    )


async def _rung5_federation(
    name: str, domain: str | None, fetcher: AsyncFetcher
) -> tuple[dict | None, str]:
    """DirectEmployers (dejobs) + The Muse — entity-clean fallbacks for WAF-walled giants."""
    slug = company_key(name)  # hyphenated, e.g. "hca-healthcare"
    pd = get_provider("dejobs")
    for tok in (slug, slug.replace("-", "")):
        try:
            raws = await pd.fetch(tok, PROBE, fetcher)
        except Exception:
            raws = []
        if _adjudicate(name, raws, pd, trust=True) and raws:
            return {
                "company": slug,
                "ats": "dejobs",
                "token": tok,
                "domain": domain,
            }, f"HIT dejobs|{tok}"
    pm = get_provider("themuse")
    brand = re.sub(
        r"\b(inc|corp|corporation|co|company|plc|ltd|group|holdings)\b\.?", "", name, flags=re.I
    ).strip()
    try:
        raws = await pm.fetch(brand, PROBE, fetcher)
    except Exception:
        raws = []
    if raws and _adjudicate(name, raws, pm, trust=True):
        return {
            "company": slug,
            "ats": "themuse",
            "token": brand,
            "domain": domain,
        }, f"HIT themuse|{brand}"
    return None, "not in dejobs/themuse federation"


async def _rung7_schemaorg(
    name: str, domain: str | None, fetcher: AsyncFetcher
) -> tuple[dict | None, str]:
    """JSON-LD / job-sitemap via the schemaorg provider (lowest-priority generic surface)."""
    if not domain:
        return None, "no domain for schema.org"
    p = get_provider("schemaorg")
    for tok in (f"https://{domain}/careers", f"careers.{domain}", domain):
        try:
            raws = await p.fetch(tok, PROBE, fetcher)
        except Exception:
            raws = []
        if raws and _adjudicate(name, raws, p):
            return {
                "company": company_key(name),
                "ats": "schemaorg",
                "token": tok,
                "domain": domain,
            }, f"HIT schemaorg|{tok}"
    return None, "no JSON-LD/sitemap jobs"


async def exhaust_one(
    name: str, domain: str | None, fetcher: AsyncFetcher, existing_keys: set[str]
) -> dict[str, Any]:
    """Walk the ladder for one company; short-circuit on first entity-correct hit; log every rung."""
    key = company_key(name)
    log: list[dict[str, str]] = []
    record: dict[str, Any] = {"company": key, "name": name, "domain": domain, "rungs": log}

    # Rung 0 — already captured?
    if key in existing_keys:
        record.update(status="already-in-registry", ats_exhausted=False)
        log.append({"rung": "0", "method": "registry", "result": "already in seed.json"})
        return record

    # Rung 1 (special): careers-page tenant discovery. Captured separately because it yields the
    # `inspected` signal the rigor gate needs — decoupled from any log-string wording.
    try:
        cand, note, rung1_inspected = await _rung1_tenant_discovery(name, domain, fetcher)
    except Exception as exc:  # noqa: BLE001 — record, never crash the sweep
        cand, note, rung1_inspected = None, f"error: {type(exc).__name__}: {exc}"[:100], False
    log.append({"rung": "1", "method": "correct-tenant-discovery", "result": note})
    if cand:
        record.update(status="captured", candidate=cand, ats_exhausted=False)
        return record

    # Rungs 2, 5, 7 in order — stop at first hit.
    rungs = [
        ("2", "harvest_tokens", lambda: _probe_rung2(name, domain, fetcher)),
        ("5", "dejobs/themuse", lambda: _rung5_federation(name, domain, fetcher)),
        ("7", "schemaorg", lambda: _rung7_schemaorg(name, domain, fetcher)),
    ]
    for num, method, run in rungs:
        try:
            cand, note = await run()
        except Exception as exc:  # noqa: BLE001 — record, never crash the sweep
            cand, note = None, f"error: {type(exc).__name__}: {exc}"[:100]
        log.append({"rung": num, "method": method, "result": note})
        if cand:
            record.update(status="captured", candidate=cand, ats_exhausted=False)
            return record

    # Rungs 3 & 6 are not autonomously scriptable — flag for an agent/manual pass before browser.
    log.append(
        {"rung": "3", "method": "websearch-apply-url", "result": "deferred: needs agent/manual"}
    )
    log.append(
        {"rung": "6", "method": "apicapture-bespoke", "result": "deferred: needs agent/manual"}
    )

    status = classify_exhaustion(captured=False, rung1_inspected=rung1_inspected)
    record.update(status=status, ats_exhausted=(status == "ats-exhausted"))
    if status == "ats-exhausted":
        record["browser_tier_hint"] = None  # tier hint set by the agent/manual pass
    return record


async def _probe_rung2(
    name: str, domain: str | None, fetcher: AsyncFetcher
) -> tuple[dict | None, str]:
    """Rung 2 wrapper around harvest_tokens.probe_company (path-based + icims/taleo host guesses)."""
    cand = await probe_company(name, domain, fetcher)
    if cand:
        return cand, f"HIT {cand.get('ats')}|{cand.get('token')}"
    return None, "no path-based/icims/taleo token matched"


async def run_sweep(
    companies: list[tuple[str, str | None]], concurrency: int, *, resume: bool = False
) -> dict[str, Any]:
    load_builtins()
    existing_keys, _ = load_existing()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    skipped = 0
    sem = anyio.Semaphore(concurrency)

    # ONE shared fetcher across the whole sweep so per-host rate limits and circuit breakers are
    # GLOBAL, not per-company. (A per-worker fetcher let N workers each hit greenhouse/lever at the
    # full per-host rate simultaneously — an uncoordinated throttle storm at registry scale.)
    async with AsyncFetcher(retries=1, timeout=12, per_host_rate=4) as fetcher:

        async def worker(name: str, domain: str | None) -> None:
            nonlocal skipped
            key = company_key(name)
            log_path = LOG_DIR / f"{key}.json"
            if resume and log_path.exists():
                try:
                    prior = json.loads(log_path.read_text())
                except (ValueError, OSError):
                    prior = None
                if prior and _is_done(prior):
                    skipped += 1
                    return  # cumulative outputs are rebuilt from logs, so prior work is preserved
            async with sem:
                # Hard per-company deadline: a tarpitting host can never stall the whole sweep. A
                # timed-out company is NOT browser-eligible (unproven) — it's re-probed next run.
                rec: dict[str, Any] = {
                    "company": key,
                    "name": name,
                    "domain": domain,
                    "rungs": [],
                    "status": "timeout",
                    "ats_exhausted": False,
                }
                with anyio.move_on_after(90):
                    rec = await exhaust_one(name, domain, fetcher, existing_keys)
                log_path.write_text(json.dumps(rec, indent=1))
                results.append(rec)

        async with anyio.create_task_group() as tg:
            for nm, dom in companies:
                tg.start_soon(worker, nm, dom)

    # Rebuild aggregate outputs from ALL per-company logs (cumulative + idempotent across batches).
    candidates, browser_queue = _aggregate_from_logs()
    OUT_CANDIDATES.write_text(json.dumps(candidates, indent=1))
    OUT_BROWSER_QUEUE.write_text(json.dumps(browser_queue, indent=1))
    return {
        "total": len(results),
        "skipped_done": skipped,
        "already": sum(1 for r in results if r.get("status") == "already-in-registry"),
        "captured_this_run": sum(1 for r in results if r.get("status") == "captured"),
        "candidates_total": len(candidates),
        "browser_eligible_total": len(browser_queue),
    }


# Curated domains for known S&P-500 gaps so rung 1 (careers-page tenant discovery) actually runs.
# Without a domain the strongest ATS rung is skipped and the company is logged incomplete, not exhausted.
_GAP_DOMAINS = {
    "Darden Restaurants": "darden.com",
    "Fastenal": "fastenal.com",
    "Linde plc": "linde.com",
    "Roper Technologies": "ropertech.com",
    "TransDigm Group": "transdigm.com",
    "Sempra": "sempra.com",
    "Vici Properties": "viciproperties.com",
    "Texas Pacific Land Corporation": "texaspacificland.com",
    "Ralph Lauren Corporation": "ralphlauren.com",
    "Targa Resources": "targaresources.com",
}


def _load_gap_companies() -> list[tuple[str, str | None]]:
    """The current S&P 500 gaps (from the crosswalk) — a ready-made target set for --gaps.

    Attaches a curated domain per gap so rung 1/7 (domain-dependent) genuinely run; unmapped gaps get
    ``None`` and will be logged ``incomplete-needs-domain`` (NOT browser-eligible) by design.
    """
    import subprocess

    out = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts" / "sp500_crosswalk.py")]
    ).decode()
    names = [
        line.split("] ", 1)[1].strip()
        for line in out.splitlines()
        if re.match(r"\s*\[", line) and "] " in line
    ]
    return [(nm, _GAP_DOMAINS.get(nm)) for nm in names]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ATS-exhaustion ladder sweep (predecessor to browser work)"
    )
    ap.add_argument("input", nargs="?", help="companies file: 'Name[,domain]' per line")
    ap.add_argument("--gaps", action="store_true", help="sweep the current S&P 500 crosswalk gaps")
    ap.add_argument("--limit", type=int, default=0, help="cap number of companies (0 = all)")
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument(
        "--resume",
        action="store_true",
        help="skip companies with a terminal prior log (captured/exhausted/in-registry)",
    )
    args = ap.parse_args()

    if args.gaps:
        companies = _load_gap_companies()
    elif args.input:
        companies = parse_companies(Path(args.input).read_text())
    else:
        ap.error("provide a companies file or --gaps")
    if args.limit:
        companies = companies[: args.limit]

    summary = anyio.run(
        functools.partial(run_sweep, companies, args.concurrency, resume=args.resume)
    )
    print(
        f"swept {summary['total']} (skipped_done={summary['skipped_done']}) | "
        f"already={summary['already']} | captured_this_run={summary['captured_this_run']} | "
        f"candidates_total={summary['candidates_total']} -> {OUT_CANDIDATES.name} | "
        f"browser-eligible_total={summary['browser_eligible_total']} -> {OUT_BROWSER_QUEUE.name}"
    )
    print(f"per-company logs: {LOG_DIR.relative_to(ROOT)}/")
    if summary["candidates_total"]:
        print(
            f"NEXT: .venv/bin/python scripts/build_registry.py {OUT_CANDIDATES.relative_to(ROOT)} --dry-run"
        )


if __name__ == "__main__":
    main()
