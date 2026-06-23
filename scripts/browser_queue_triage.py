"""Triage the browser_queue before the (expensive) browser pass — make Tier-1 capture surgical.

Working 200+ proven-exhausted boards by hand-capturing each in a browser is the wrong use of effort:
most drained micro-caps are dead/parked/redirected domains, server-rendered WordPress HTML, or an ATS
iframe the ladder's regex missed — NOT clean own-API SPAs. This cheap curl pass classifies each board so
the browser pass only runs on genuine Tier-1 targets, and so the rest are routed correctly:

  dead-parked        final host is a domain-parking service (hugedomains/sedo/dan…) -> DROP
  off-domain         final host's registrable domain != the company's (acquired/hijacked) -> DROP/flag
  no-careers         no reachable on-domain careers page (200, on-domain, job content)
  ats-iframe         careers HTML embeds a known ATS -> route to the ladder (missed-ATS), NOT the browser
  api-spa            careers HTML hints at a job API (/api/, /wp-json/, graphql, application/json) -> TIER-1
  static-html        on-domain careers page, job content, no API hint -> apicapture html_table candidate

Emits a breakdown + runs/browser_queue_triage.json (per-board verdict + resolved careers_url) +
scripts/browser_tier1_targets.json (the api-spa shortlist — the real, surgical browser-pass input).
This also fixes the A->B contract gap: the queue lacked the resolved careers_url, forcing the browser to
guess (and a guess hit a tech-support SCAM redirect). Here we resolve it once, on-domain-verified.

Usage::
    python scripts/browser_queue_triage.py [--limit N] [--concurrency 12]
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests as creq

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "scripts" / "browser_queue.json"
OUT_REPORT = ROOT / "runs" / "browser_queue_triage.json"
OUT_TARGETS = ROOT / "scripts" / "browser_tier1_targets.json"

_PATHS = [
    "https://{d}/careers",
    "https://careers.{d}",
    "https://{d}/careers/jobs",
    "https://{d}/jobs",
    "https://www.{d}/careers",
    "https://{d}/company/careers",
    "https://{d}/about/careers",
]
_PARKING = re.compile(
    r"\b(hugedomains|sedo|dan\.com|afternic|domain.{0,3}for.{0,3}sale|buy this domain)", re.I
)
_JOB_SIGNAL = re.compile(r"\b(job|career|position|opening|vacanc|apply|hiring)", re.I)
# Known ATS hosts/markers — incl. Comeet (heavily used by the Israeli micro-caps in this queue) which the
# ladder's host regex misses. An iframe/link to any of these = a missed-ATS, route to the ladder.
_ATS = re.compile(
    r"comeet\.co|greenhouse\.io|lever\.co|myworkdayjobs|smartrecruiters|workable\.com|bamboohr|"
    r"recruitee|jobvite|teamtailor|ashbyhq|breezy\.hr|icims\.com|taleo\.net|successfactors|"
    r"phenom|avature\.net|pinpointhq|join\.com|rippling|paylocity|dayforce|ultipro",
    re.I,
)
_API_HINT = re.compile(
    r"/api/|/wp-json/|graphql|application/json|fetch\(|__NEXT_DATA__|axios", re.I
)


def _reg(host: str) -> str:
    return ".".join(host.split(".")[-2:])


def classify(status: int, body: str, final_host: str, domain: str) -> dict[str, str] | None:
    """Pure verdict for one fetched candidate URL (None = inconclusive, try the next path).

    Order matters: parking + off-domain are decided from the FINAL host (rejects ad-hijacks/acquirers/
    ?d= parking pages a substring check would miss), then job-content gating, then ATS > API > static."""
    if _PARKING.search(body) or _PARKING.search(final_host):
        return {"verdict": "dead-parked"}
    if _reg(final_host) != _reg(
        domain
    ):  # HOST check (not substring) — rejects parking/?d= + acquirers
        return {"verdict": "off-domain"}
    if status != 200 or len(body) < 2000 or not _JOB_SIGNAL.search(body):
        return None
    if m := _ATS.search(body):
        return {"verdict": "ats-iframe", "ats": m.group(0)}  # missed ATS -> route to the ladder
    if _API_HINT.search(body):
        return {"verdict": "api-spa"}  # own-domain job API -> the real Tier-1 browser target
    return {"verdict": "static-html"}


def triage(name: str, domain: str) -> dict[str, str]:
    s = creq.Session(impersonate="chrome124", verify=False, timeout=6)
    for tmpl in _PATHS:
        url = tmpl.format(d=domain)
        try:
            r = s.get(url, allow_redirects=True)
        except Exception:
            continue
        final_host = urlsplit(str(r.url)).netloc.split("@")[-1].split(":")[0].lower()
        verdict = classify(
            r.status_code, r.text if r.status_code == 200 else "", final_host, domain
        )
        if verdict is not None:
            return {**verdict, "careers_url": str(r.url)}
    return {"verdict": "no-careers", "careers_url": ""}


def main() -> None:
    ap = argparse.ArgumentParser(description="Triage browser_queue.json before the browser pass")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=12)
    args = ap.parse_args()

    queue = json.loads(QUEUE.read_text())
    if args.limit:
        queue = queue[: args.limit]
    results: list[dict[str, str]] = []

    def work(entry: dict) -> dict[str, str]:
        out = {
            "company": entry["company"],
            "name": entry["name"],
            "domain": entry.get("domain", ""),
        }
        out.update(
            triage(entry["name"], entry["domain"])
            if entry.get("domain")
            else {"verdict": "no-domain", "careers_url": ""}
        )
        return out

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(work, queue))

    from collections import Counter

    breakdown = Counter(r["verdict"] for r in results)
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(json.dumps(results, indent=1))
    # the surgical browser-pass shortlist: genuine own-API SPAs, WITH the resolved careers_url
    targets = [r for r in results if r["verdict"] == "api-spa"]
    OUT_TARGETS.write_text(json.dumps(targets, indent=1))

    print(f"triaged {len(results)} boards:")
    for v, n in breakdown.most_common():
        print(f"  {v:14s} {n}")
    print(
        f"\nTIER-1 browser targets (api-spa, careers_url resolved): {len(targets)} -> {OUT_TARGETS.name}"
    )
    print(f"ats-iframe (route to ladder, missed-ATS): {breakdown.get('ats-iframe', 0)}")
    print(f"full report: {OUT_REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
