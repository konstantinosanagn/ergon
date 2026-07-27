"""Resolve the triage's 'ats-iframe' boards into ready-to-merge candidates for Stream A.

The triage found 37 proven-"exhausted" boards whose careers page actually embeds a known ATS the
ladder's rung-1 grep missed (the ATS URL lives in an iframe src / script / JS-rendered link, not a
plain anchor). All 37 use ATSes we already have providers for — so they're not new-provider gaps,
they're recoverable NOW. This re-runs a *broader* rung-1: fetch the resolved careers_url, extract every
embedded ATS URL (anchors + iframe src + script text), map each via provider.matches(), and entity-
verify a live fetch. Verified hits -> scripts/cand_missed_ats.json (Stream A feeds it to build_registry,
which owns seed.json). Unresolved -> a gaps note so the ladder learns to read iframe/JS-embedded hosts.

Respects the A/B boundary: emits CANDIDATES only; never writes seed.json.

Usage::  python scripts/missed_ats_handoff.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
from curl_cffi import requests as creq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_tokens import company_key, name_match  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import iter_providers, load_builtins  # noqa: E402

REPORT = ROOT / "runs" / "browser_queue_triage.json"
OUT = ROOT / "scripts" / "cand_missed_ats.json"
GAPS = ROOT / "runs" / "missed_ats_ladder_gaps.json"

# Pull any full ATS URL out of the careers HTML — anchors, iframe src, or inline script. Broader than
# the ladder's rung-1 (which missed these): we scan the whole document, not just <a href>.
_ATS_URL = re.compile(
    r"https?://[^\s\"'<>]*?(?:"
    r"myworkdayjobs\.com|\.icims\.com|successfactors\.(?:com|eu)|\.taleo\.net|phenompeople|"
    r"\.recruiting\.com|avature\.net|dayforcehcm\.com|\.ultipro\.com|paylocity\.com|jobvite\.com|"
    r"brassring\.com|boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|smartrecruiters\.com|"
    r"\.workable\.com|bamboohr\.com|teamtailor\.com|breezy\.hr|recruitee\.com"
    r")[^\s\"'<>]*",
    re.IGNORECASE,
)


async def _fetch_html(url: str) -> str:
    # Async (not sync creq.get): a blocking fetch would stall the event loop and serialize every
    # board. AsyncSession lets the boards' careers fetches overlap under the task group.
    if not url:
        return ""
    try:
        async with creq.AsyncSession(impersonate="chrome124", verify=False, timeout=12) as s:
            r = await s.get(url, allow_redirects=True)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


# The triage already detected which ATS each board embeds — so probe ONLY that provider, not all 53.
# (Providers already early-exit on limit, so the win here isn't pagination; it's avoiding 50+ spurious
# matches()/fetches per board.) Maps the detected ATS marker -> provider name(s); unmapped -> try all.
_ATS_TO_PROVIDER = {
    "successfactors": ["successfactors"],
    "lever.co": ["lever"],
    "myworkdayjobs": ["workday"],
    "icims.com": ["icims"],
    "greenhouse.io": ["greenhouse"],
    "taleo.net": ["taleo", "taleobe"],
    "phenom": ["phenom"],
    "teamtailor": ["teamtailor"],
    "paylocity": ["paylocity"],
    "dayforce": ["dayforce"],
    "breezy.hr": ["breezy"],
    "bamboohr": ["bamboohr"],
    "workable.com": ["workable"],
    "ultipro": ["ukg"],
    "avature.net": ["avature"],
    "smartrecruiters": ["smartrecruiters"],
    "recruitee.com": ["recruitee"],
    "jobvite.com": ["jobvite"],
    "brassring.com": ["brassring"],
    "ashbyhq": ["ashby"],
}


async def resolve(entry: dict, providers: list, fetcher: AsyncFetcher) -> dict | None:
    """Extract the embedded ATS URL from the careers page, map -> token, entity-verify a live fetch."""
    name = entry["name"]
    html = await _fetch_html(entry.get("careers_url") or "")
    if not html:
        return None
    detected = (entry.get("ats") or "").lower()
    want = next((names for key, names in _ATS_TO_PROVIDER.items() if key in detected), None)
    probe = [p for p in providers if p.name in want] if want else providers
    urls = list(dict.fromkeys(_ATS_URL.findall(html)))[:20]
    for url in urls:
        for prov in probe:
            try:
                token = prov.matches(url)
            except Exception:
                token = None
            if not token:
                continue
            try:
                raws = await prov.fetch(token, SearchQuery(limit=8), fetcher)
            except Exception:
                continue
            if raws and any(
                name_match(name, str(getattr(r, "company", "") or "")) for r in raws[:5]
            ):
                return {
                    "company": company_key(name),
                    "ats": prov.name,
                    "token": token,
                    "domain": entry.get("domain"),
                    "careers_url": entry.get("careers_url"),
                    "via": "missed-ats-handoff",
                }
    return None


async def main() -> None:
    load_builtins()
    providers = list(iter_providers())
    rows = json.loads(REPORT.read_text())
    targets = [r for r in rows if r.get("verdict") == "ats-iframe"]
    print(f"resolving {len(targets)} missed-ATS boards into candidates...")

    results: list[dict | None] = [None] * len(targets)
    sem = anyio.Semaphore(
        10
    )  # cap concurrent boards (the AsyncFetcher caps provider fetches within)
    async with AsyncFetcher(timeout=20, retries=1) as f:

        async def work(i: int, entry: dict) -> None:
            async with sem:
                # Hard per-board deadline: one board stuck in a per-host circuit-breaker backoff must
                # never stall the whole task group (the exhaustion-sweep lesson). Timed-out -> None.
                with anyio.move_on_after(40):
                    results[i] = await resolve(entry, providers, f)

        async with anyio.create_task_group() as tg:
            for i, entry in enumerate(targets):
                tg.start_soon(work, i, entry)

    candidates = [c for c in results if c]
    unresolved = [
        {
            "name": e["name"],
            "domain": e.get("domain"),
            "detected_ats": e.get("ats"),
            "careers_url": e.get("careers_url"),
        }
        for e, c in zip(targets, results, strict=True)
        if not c
    ]
    for c in candidates:
        print(f"  HIT  {c['company'][:34]:36s} {c['ats']}|{c['token']}")

    OUT.write_text(json.dumps(candidates, indent=1))
    GAPS.write_text(json.dumps(unresolved, indent=1))
    print(
        f"\nresolved {len(candidates)}/{len(targets)} -> {OUT.name} (Stream A: build_registry --dry-run)"
    )
    print(
        f"unresolved (ladder needs iframe/JS-host parsing): {len(unresolved)} -> {GAPS.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    anyio.run(main)
