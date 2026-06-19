"""Auto-infer own-domain job-API specs for residual "proxied" giants (generalises Goldman).

The residual giants expose no ATS host even in-browser, but many call a public no-auth JSON/
GraphQL job API on their OWN domain. This loads each giant's careers/search page in headless
Chromium, watches every JSON RESPONSE for one carrying job RECORDS, and auto-infers an
``apicapture`` spec from the matching request+response: url, method, verbatim body, the dot-path
to the records + total, a best-effort field map, and the pagination knob (a 0/1 numeric in the
body/query whose key looks like page/offset/from/start). Replays page 1 to confirm, then writes
the spec to ``registry/data/apicapture.json`` and emits a candidate.

Usage::

    .venv/bin/python scripts/capture_api.py [--cap N] [--out scripts/candidates_apicap.json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from census_residual import brand_query, residual_urls  # noqa: E402
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core  # noqa: E402

from census_successfactors import tavily  # noqa: E402  # isort: skip

GIANTS = ROOT / "runs" / "giants.json"
SPECS = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "apicapture.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_apicap.json"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_TITLE_KEYS = (
    "jobtitle",
    "title",
    "postingtitle",
    "positiontitle",
    "name",
    "position",
    "displayname",
    "role",
)
_ID_KEYS = (
    "jobid",
    "reqid",
    "roleid",
    "jobseqno",
    "displayjobid",
    "autoreqid",
    "id",
    "slug",
    "requisitionid",
    "jobreqid",
)
_LOC_KEYS = (
    "primarylocation",
    "locations",
    "location",
    "citystatecountry",
    "city",
    "joblocation",
    "locationname",
    "formattedlocation",
)
_DEPT_KEYS = (
    "department",
    "division",
    "category",
    "jobfunction",
    "jobfamily",
    "team",
    "businessunit",
    "function",
)
_TOTAL_KEYS = (
    "totalcount",
    "total",
    "totalresults",
    "totaljobscount",
    "count",
    "numfound",
    "totalhits",
    "recordcount",
)
_PAGE_HINT = ("pagenumber", "page", "offset", "from", "start", "startrow", "startindex", "pageno")
# Third-party job boards / aggregators / gov sites a careers page may EMBED — their records are
# not the sponsor's own jobs (Citi's page embeds Singapore's mycareersfuture). Reject these hosts.
_DENY_HOSTS = (
    "mycareersfuture",
    "indeed.",
    "linkedin.",
    "glassdoor",
    "ziprecruiter",
    "monster.",
    "naukri",
    "shine.com",
    "google.",
    "bing.",
    "facebook.",
    "ledinside",
    "dice.com",
    "simplyhired",
    "talent.com",
    "jobstreet",
    "seek.com",
    # Embedded chatbot/conversational-ATS widgets, H-1B/job aggregators, and SaaS infra that a
    # careers page calls but which never serve the sponsor's OWN postings (found poisoning a
    # brand-normalized residual pass: fedex->paradox.ai, slalom->h1bvisajobs, sphere->a16z, ...).
    "paradox.ai",
    "olivia.paradox",
    "jobdiva.com",
    "bebee.com",
    "theirstack",
    "chronicle.com",
    "consentmo",
    "onrender.com",
    "h1bvisajobs",
    "h1btrends",
    "h1bgrader",
    "myvisajobs",
    "supabase.co",
    "a16z.com",
    "amazon.jobs",
    "greenhouse.io/embed",
)


def _brand_tokens(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", name.lower()) if len(w) >= 4}


def _host_label(host: str) -> str:
    parts = host.lower().split(":")[0].split(".")
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _host_relates(host: str, name: str) -> bool:
    """A legit OWN-domain job API is hosted on the sponsor's own domain (Goldman ->
    api-higher.gs.com). A third-party widget/aggregator is on someone else's. Require the request
    host's registrable label to share a brand token (or vice-versa) — kills the embeds that a
    brand-normalized "X careers" search surfaces, which otherwise poison the registry."""
    label = _host_label(host)
    if not label:
        return False
    toks = _brand_tokens(name)
    return any(label in t or t in label for t in toks)


def _find_records(obj, path=()):
    """Deepest list of >=3 dicts that have a title-ish key. Returns (path, records) or None."""
    best = None
    if isinstance(obj, list) and len(obj) >= 3 and all(isinstance(x, dict) for x in obj[:3]):
        keys = {k.lower() for k in obj[0]}
        if any(t in keys for t in _TITLE_KEYS):
            best = (list(path), obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            r = _find_records(v, (*path, k))
            if r and (best is None or len(r[1]) > len(best[1])):
                best = r
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:8]):
            r = _find_records(v, (*path, i))
            if r and (best is None or len(r[1]) > len(best[1])):
                best = r
    return best


def _pick(rec: dict, keys: tuple[str, ...]) -> str | None:
    low = {k.lower(): k for k in rec}
    for want in keys:
        if want in low:
            return low[want]
    return None


def _find_total(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _TOTAL_KEYS and isinstance(v, int) and v > 0:
                return [*path, k]
            r = _find_total(v, (*path, k))
            if r:
                return r
    return None


def _find_page_path(body):
    """A numeric leaf (0 or 1) whose key path hints at pagination -> dot path into the body."""
    found: list[list] = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, int) and v in (0, 1) and any(h in k.lower() for h in _PAGE_HINT):
                    found.append([*path, k])
                walk(v, [*path, k])
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, [*path, i])

    walk(body, [])
    return found[0] if found else None


def infer_spec(req_url: str, method: str, post_data: str | None, resp, name: str):
    """Build an apicapture spec from a captured job-records request+response, or None."""
    parts = urlsplit(req_url)
    host, path = parts.netloc.lower(), parts.path.lower()
    if any(d in host for d in _DENY_HOSTS):
        return None  # embedded third-party job board / gov site, not the sponsor's own jobs
    if not _host_relates(host, name):
        return None  # API host doesn't belong to the sponsor -> an embedded widget/aggregator
    if host.split(".")[0] in ("static", "cdn", "assets") or any(
        seg in path for seg in ("/assets/", "/static/", "/dist/", "/_next/", ".js")
    ):
        return None  # a static-asset config blob (Wells Fargo /assets/js/...), not a live job API
    rec_hit = _find_records(resp)
    if not rec_hit:
        return None
    rec_path, records = rec_hit
    sample = records[0]
    title_k = _pick(sample, _TITLE_KEYS)
    id_k = _pick(sample, _ID_KEYS)
    if not title_k or not id_k:
        return None
    loc_k = _pick(sample, _LOC_KEYS)
    # Job-quality guard: real job records carry a location OR are field-rich. A title+id-only
    # record with <5 keys is usually navigation/config (Wells Fargo's menu), not a posting.
    if not loc_k and len(sample) < 5:
        return None
    spec = {
        "url": req_url,
        "method": method.upper(),
        "records_path": rec_path,
        "total_path": _find_total(resp) or [],
        "company": name,
        "fields": {
            "id": id_k,
            "title": title_k,
            "location": _pick(sample, _LOC_KEYS) or "",
            "department": _pick(sample, _DEPT_KEYS) or "",
        },
    }
    if method.upper() == "POST" and post_data:
        try:
            body = json.loads(post_data)
        except ValueError:
            return None
        spec["body"] = body
        pp = _find_page_path(body)
        if pp:
            spec["page_path"] = pp
    else:  # GET — pagination via a query param
        qs = parse_qs(urlsplit(req_url).query)
        for param, vals in qs.items():
            if any(h in param.lower() for h in _PAGE_HINT) and vals and vals[0] in ("0", "1"):
                spec["page_param"] = param
                spec["page_start"] = int(vals[0])
                break
    return spec


async def capture(name: str, urls: list[str], browser, limiter) -> dict | None:
    """Load careers pages; return the inferred spec from the richest job-records response."""
    best: dict | None = None
    best_n = 0
    async with limiter:
        try:
            ctx = await browser.new_context(user_agent=_UA)
        except Exception:  # noqa: BLE001
            return None
        try:
            for u in urls[:2]:
                try:
                    page = await ctx.new_page()

                    async def on_resp(r):
                        nonlocal best, best_n
                        if "json" not in r.headers.get("content-type", "").lower():
                            return
                        try:
                            d = await r.json()
                        except Exception:  # noqa: BLE001
                            return
                        spec = infer_spec(r.url, r.request.method, r.request.post_data, d, name)
                        if spec:
                            n = len(_find_records(d)[1])
                            if n > best_n:
                                best, best_n = spec, n

                    page.on("response", on_resp)
                    try:
                        await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(6000)
                    except Exception:  # noqa: BLE001
                        pass
                    await page.close()
                except Exception:  # noqa: BLE001
                    continue
        finally:
            await ctx.close()
    return best


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    cap = 400
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--cap":
            cap = int(args[i + 1])
            i += 2
        else:
            print(f"unknown flag: {args[i]}")
            return

    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set.")
        return
    seed_keys = load_seed_keys()
    giants = [
        g
        for g in json.loads(GIANTS.read_text())["uncovered_top"]
        if _core(g["name"]) not in seed_keys
    ][:cap]
    print(f"API-capture over {len(giants)} residual giants ...", flush=True)

    urls_by_idx: dict[int, list[str]] = {}

    async def find_urls(idx: int, g: dict, fetcher) -> None:
        # Brand-normalize the legal filing name first: Tavily can't find a careers page for
        # "trustees of university of pennsylvania" but trivially finds one for the brand. Same
        # blindspot the static residual census had — and the Playwright pass is exactly where the
        # JS-loaded boards (OSF iCIMS iframe, university SPAs) get captured.
        brand = brand_query(g["name"])
        urls = await tavily(f"{brand} careers", key, fetcher)
        urls_by_idx[idx] = residual_urls(brand, g["name"], urls)

    from ergon_tracker.http import AsyncFetcher  # noqa: E402

    async with (
        AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=3) as tav,
        anyio.create_task_group() as tg,
    ):
        for idx, g in enumerate(giants):
            tg.start_soon(find_urls, idx, g, tav)

    specs: dict[str, dict] = {}
    done = [0]
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable: {exc}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        limiter = anyio.CapacityLimiter(6)

        async def grab(idx: int, g: dict) -> None:
            spec = await capture(g["name"], urls_by_idx.get(idx, []), browser, limiter)
            if spec:
                specs[_core(g["name"]) or g["name"]] = spec
            done[0] += 1
            if done[0] % 25 == 0:
                print(f"  captured {done[0]}/{len(giants)} (API specs: {len(specs)})", flush=True)

        async with anyio.create_task_group() as tg:
            for idx, g in enumerate(giants):
                tg.start_soon(grab, idx, g)
        await browser.close()

    # Merge new specs into the data file; emit one candidate per spec.
    existing = json.loads(SPECS.read_text()) if SPECS.exists() else {}
    existing.update(specs)
    SPECS.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    candidates = [{"company": k, "ats": "apicapture", "token": k, "domain": None} for k in specs]
    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    print(
        f"\ninferred {len(specs)} API specs -> apicapture.json; wrote {len(candidates)} "
        f"candidates -> {out_path.name}"
    )
    for k, s in specs.items():
        print(f"  {k:22} {s['method']:4} {s['url'][:60]}")


if __name__ == "__main__":
    anyio.run(main)
