"""Tier-1 browser pass — headless, automated, over the triage's api-spa targets.

Doing 82 boards by hand in a browser isn't feasible, so this automates the exact Tier-1 loop on the
triage shortlist (browser_tier1_targets.json, each with a resolved careers_url): headless Playwright
navigates the careers page, records every JSON XHR response, picks the one carrying the job-list array,
and runs it straight through browser_discovery.propose_spec -> verify_spec (replay via the apicapture
provider). Verified hits -> scripts/cand_tier1.json + scripts/tier1_specs.json (the apicapture specs to
review + add). Emits candidates only; never writes seed.json.

Bounded + concurrent like the rest of B: a semaphore caps concurrent pages, each board has a hard
move_on_after deadline so one slow SPA can't stall the batch.

LIMITATION (measured 2026-06-23): on the drained micro-cap queue this yields ~0 real boards — most
careers pages expose NO job-list JSON XHR (jobs are server-rendered HTML or load on a sub-page). Worse,
the verify-gate here only checks "spec replays >= 1 record", NOT "the records are actually JOBS for this
company": a careers page's marketing/analytics JSON (e.g. a Salesforce data-extension array) can be
mis-picked by find_records_path and falsely "verify". So candidates from this tool are UNTRUSTED until a
job-ness + entity check is added (titles look like roles, company name matches) — do not merge blindly.
Best used on known-good SPA targets, not blind micro-cap sweeps.

Usage::  python scripts/tier1_capture.py [--limit N] [--concurrency 4] [--offset 0]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from browser_discovery import find_records_path, propose_spec, verify_spec_async  # noqa: E402

TARGETS = ROOT / "scripts" / "browser_tier1_targets.json"
OUT_CAND = ROOT / "scripts" / "cand_tier1.json"
OUT_SPECS = ROOT / "scripts" / "tier1_specs.json"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _records_len(response: object, path: list) -> int:
    node = response
    for p in path:
        node = node[p] if isinstance(node, dict) else None
        if node is None:
            return 0
    return len(node) if isinstance(node, list) else 0


async def capture_one(browser: object, entry: dict, settle_ms: int) -> dict | None:
    """Navigate the careers page, find the job-list XHR, propose+verify an apicapture spec."""
    token = entry["company"].replace("-", "") + "_t1"
    ctx = await browser.new_context(user_agent=_UA, locale="en-US", timezone_id="America/New_York")
    page = await ctx.new_page()
    responses: list = []
    page.on("response", lambda r: responses.append(r))
    try:
        await page.goto(entry["careers_url"], wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(settle_ms)
    except Exception:
        await ctx.close()
        return None

    best: tuple[int, dict, object] | None = None  # (record_count, request, response_json)
    for resp in responses:
        if "json" not in (resp.headers.get("content-type") or "").lower():
            continue
        try:
            data = await resp.json()
        except Exception:
            continue
        n = _records_len(data, find_records_path(data))
        if n and (best is None or n > best[0]):
            req = resp.request
            body = None
            if (req.method or "GET").upper() == "POST":
                with __import__("contextlib").suppress(Exception):
                    body = json.loads(req.post_data or "{}")
            best = (n, {"url": req.url, "method": req.method, "body": body,
                        "headers": dict(req.headers)}, data)
    await ctx.close()
    if best is None:
        return None
    try:  # propose only; verification happens sequentially after the browser closes (no monkeypatch race)
        spec = propose_spec(best[1], best[2], company=entry["name"], token=token)
    except Exception:
        return None
    return {"company": entry["company"], "name": entry["name"], "ats": "apicapture",
            "token": token, "spec": spec, "careers_url": entry["careers_url"]}


async def main() -> None:
    ap = argparse.ArgumentParser(description="Headless Tier-1 capture over api-spa targets")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--settle-ms", type=int, default=3500)
    args = ap.parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("needs Playwright: uv pip install ergon-tracker[browser] && playwright install chromium")
        sys.exit(1)

    targets = json.loads(TARGETS.read_text())[args.offset : args.offset + args.limit]
    print(f"Tier-1 capture over {len(targets)} api-spa targets (concurrency {args.concurrency})...")
    results: list[dict | None] = [None] * len(targets)
    sem = anyio.Semaphore(args.concurrency)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def work(i: int, entry: dict) -> None:
            async with sem:
                with anyio.move_on_after(40):  # one slow SPA can't stall the batch
                    results[i] = await capture_one(browser, entry, args.settle_ms)
                status = "HIT " if results[i] else "miss"
                print(f"  {status} {entry['name'][:40]}", flush=True)

        async with anyio.create_task_group() as tg:
            for i, entry in enumerate(targets):
                tg.start_soon(work, i, entry)
        await browser.close()

    # Verify proposals SEQUENTIALLY (verify_spec_async monkeypatches a module global — no concurrency).
    proposed = [r for r in results if r]
    hits: list[dict] = []
    for r in proposed:
        n, msg = await verify_spec_async(r["spec"], r["token"])
        if n:
            r["verify"] = msg
            hits.append(r)
            print(f"  VERIFIED {r['name']}: {msg}", flush=True)
    OUT_CAND.write_text(json.dumps([{k: v for k, v in h.items() if k != "spec"} for h in hits], indent=1))
    OUT_SPECS.write_text(json.dumps({h["token"]: h["spec"] for h in hits}, indent=1))
    print(f"\nVERIFIED {len(hits)}/{len(targets)} (of {len(proposed)} proposed) -> {OUT_CAND.name} + {OUT_SPECS.name}")


if __name__ == "__main__":
    anyio.run(main)
