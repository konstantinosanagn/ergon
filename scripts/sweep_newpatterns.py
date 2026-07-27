"""Tavily-resolve each residual giant's real careers domain, then probe the NEW fetchable
patterns the prior agent sweep didn't cover: WP-REST job CPT, /feed/?post_type=jobs RSS,
HTML <table>, schema.org JobPosting JSON-LD, and /api/jobs JSON. Job-content-gated to avoid
blog-feed / nav-carousel false positives. Read-only; prints candidate specs to wire."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from census_residual import brand_query  # noqa: E402
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core, name_match  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402

GIANTS = ROOT / "runs" / "giants.json"
EXCLUDE = [
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "dice.com",
    "google.com",
    "facebook.com",
    "wikipedia.org",
    "h1bdata.info",
    "myvisajobs.com",
    "trackitt.com",
    "joblist.com",
    "simplyhired.com",
    "monster.com",
    "naukri.com",
]
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
JOBWORD = re.compile(
    r"engineer|developer|analyst|consultant|manager|administrator|architect|specialist|"
    r"programmer|designer|\blead\b|scientist|nurse|technician|director|associate|intern|"
    r"recruiter|accountant|coordinator|officer",
    re.I,
)


async def tavily(query: str, key: str, fetcher: AsyncFetcher) -> list[str]:
    body = {"query": f"{query} careers jobs", "exclude_domains": EXCLUDE, "max_results": 5}
    try:
        data = await fetcher.post_json(
            "https://api.tavily.com/search",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
        )
    except Exception:
        return []
    return [r.get("url", "") for r in (data.get("results", []) if isinstance(data, dict) else [])]


def pick_domain(brand: str, urls: list[str]) -> str | None:
    """The registrable domain of the first result whose host relates to the brand."""
    core = _core(brand)
    for u in urls:
        host = urlsplit(u if "//" in u else "//" + u).netloc.split(":")[0].lower()
        if not host:
            continue
        reg = ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host
        label = reg.split(".")[0]
        # relate: brand core contains the domain label or vice-versa, or name_match
        if label and (label in core or core[:6] in label or name_match(brand, label)):
            return host
    # fall back to the first result's host (best effort)
    if urls:
        return urlsplit(urls[0] if "//" in urls[0] else "//" + urls[0]).netloc.split(":")[0].lower()
    return None


def _titles_look_like_jobs(titles: list[str]) -> bool:
    joined = " ".join(t for t in titles if t)
    return bool(JOBWORD.search(joined))


# Payroll/HR-integrated recruiting platforms + Symphony Talent — fingerprint on the careers/home
# page, capture the tenant id. Each is no-auth fetchable once the id is known.
_ATS_EMBED = {
    "paycom": r"paycomonline\.net/v4/ats/web\.php/jobs\?clientkey=([A-Za-z0-9]+)",
    "paylocity": r"recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/[Aa]ll/(\d+)",
    "applicantpro": r"https?://([a-z0-9\-]+)\.applicantpro\.com",
    "isolvedhire": r"https?://([a-z0-9\-]+)\.isolvedhire\.com",
    "adp": r"workforcenow\.adp\.com/mascsr/default/mdf/recruitment/recruitment\.html\?cid=([a-f0-9\-]+)",
    "mcloud": r"jobsapi[a-z\-]*\.m-cloud\.io/api/job/search\?[^\"']*companyName=companies/([a-f0-9\-]+)",
    "jobvite": r"jobs\.jobvite\.com/([a-z0-9\-]+)",
}
# Standard no-auth ATS boards we already have providers for — extract the REAL embedded board
# token from the company's own careers page (entity-safe; catches token != company-name slug).
# Key = our provider name; value = (regex capturing the token, ...). First capture group is token.
_PROVIDER_EMBED = {
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9]+)",
    "lever": r"jobs\.lever\.co/([a-z0-9\-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([a-z0-9\-]+)",
    "workable": r"(?:apply\.workable\.com/|([a-z0-9\-]+)\.workable\.com)",
    "recruitee": r"([a-z0-9\-]+)\.recruitee\.com",
    "bamboohr": r"([a-z0-9\-]+)\.bamboohr\.com",
    "jazzhr": r"([a-z0-9\-]+)\.applytojob\.com",
    "smartrecruiters": r"careers\.smartrecruiters\.com/([A-Za-z0-9]+)",
    "icims": r"([a-z0-9\-]+)\.icims\.com",
}
_GENERIC_SLUG = {"careers", "jobs", "embed", "www", "apply", "job", "search", "static", "assets"}
# Enterprise ATS hosts — detect on the careers page (catches MIGRATIONS off Darwinbox/PeopleSoft).
# Workday/Oracle need multi-part tokens, built specially below; the rest report host for follow-up.
_WORKDAY_RE = re.compile(
    r"https://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)"
)
_ENT_HOST = {
    "successfactors": r"https://([a-z0-9.\-]+\.successfactors\.com)",
    "oracle": r"https://([a-z0-9.\-]+\.oraclecloud\.com)",
    "phenom": r"https://([a-z0-9.\-]+\.phenompeople\.com)",
    "taleo": r"https://([a-z0-9\-]+\.taleo\.net)",
    "avature": r"https://([a-z0-9\-]+\.avature\.net)",
}


def _token_from_match(m: re.Match) -> str | None:
    tok = next((g for g in m.groups() if g), None)
    if tok and tok.lower() not in _GENERIC_SLUG and len(tok) >= 3:
        return tok
    return None


_CEIPAL_AK = re.compile(r'data-ceipal-api-key=["\']([^"\']+)["\']')
_CEIPAL_CP = re.compile(r'data-ceipal-career-portal-id=["\']([^"\']+)["\']')


async def detect_ats_embed(host: str, client: httpx.AsyncClient) -> dict | None:
    base = f"https://{host}"
    for path in ("/careers", "/careers/", "/jobs", "/", "/about/careers", "/company/careers"):
        try:
            r = await client.get(base + path, timeout=10, follow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        # Ceipal: the dominant IT-staffing ATS. Keys are in data-* attrs (static when SSR'd).
        ak, cp = _CEIPAL_AK.search(r.text), _CEIPAL_CP.search(r.text)
        if ak and cp:
            return {
                "kind": "provider:ceipal",
                "ats": "ceipal",
                "id": f"{ak.group(1)}|{cp.group(1)}",
                "page": base + path,
                "candidate": True,
            }
        if "ceipal" in r.text.lower() and ("widget.js" in r.text or "ceipal-widget" in r.text):
            return {
                "kind": "ceipal-js",
                "ats": "ceipal",
                "id": "(keys JS-rendered)",
                "page": base + path,
            }
        # enterprise ATS (catches migrations): Workday with full tenant|wdN|site token
        wm = _WORKDAY_RE.search(r.text)
        if wm and wm.group(1) not in _GENERIC_SLUG:
            tok = f"{wm.group(1)}|{wm.group(2)}|{wm.group(3)}"
            return {
                "kind": "provider:workday",
                "ats": "workday",
                "id": tok,
                "page": base + path,
                "candidate": True,
            }
        for ats, pat in _ENT_HOST.items():
            m = re.search(pat, r.text)
            if m:
                return {
                    "kind": f"enterprise:{ats}",
                    "ats": ats,
                    "id": m.group(1),
                    "page": base + path,
                }
        # standard no-auth ATS boards we have providers for -> emit a verifiable candidate
        for ats, pat in _PROVIDER_EMBED.items():
            for m in re.finditer(pat, r.text):
                tok = _token_from_match(m)
                if tok:
                    return {
                        "kind": f"provider:{ats}",
                        "ats": ats,
                        "id": tok,
                        "page": base + path,
                        "candidate": True,
                    }
        for ats, pat in _ATS_EMBED.items():
            m = re.search(pat, r.text)
            if m:
                return {"kind": f"embed:{ats}", "ats": ats, "id": m.group(1), "page": base + path}
    return None


async def probe_domain(brand: str, host: str, client: httpx.AsyncClient) -> dict | None:
    base = f"https://{host}"
    # 0) payroll/HR-platform + Symphony Talent embeds (capture the tenant id)
    emb = await detect_ats_embed(host, client)
    if emb:
        emb.update(
            {
                "brand": brand,
                "host": host,
                "endpoint": f"{emb['ats']}:{emb['id']}",
                "n": "?",
                "titles": [],
            }
        )
        return emb
    # 1) WP-REST job CPT via types
    try:
        r = await client.get(f"{base}/wp-json/wp/v2/types", timeout=10)
        if r.status_code == 200 and isinstance(r.json(), dict):
            for k, v in r.json().items():
                if any(
                    w in k.lower()
                    for w in ("job", "career", "vacan", "position", "opening", "opportunit")
                ):
                    rb = (v or {}).get("rest_base") or k
                    rr = await client.get(f"{base}/wp-json/wp/v2/{rb}?per_page=20", timeout=10)
                    if rr.status_code == 200 and isinstance(rr.json(), list) and rr.json():
                        titles = [
                            (x.get("title", {}) or {}).get("rendered", "") for x in rr.json()[:5]
                        ]
                        if _titles_look_like_jobs(titles):
                            return {
                                "brand": brand,
                                "host": host,
                                "kind": "wp-rest",
                                "endpoint": f"{base}/wp-json/wp/v2/{rb}?per_page=100",
                                "rest_base": rb,
                                "n": len(rr.json()),
                                "titles": titles[:3],
                            }
    except Exception:
        pass
    # 2) RSS /feed/?post_type=jobs (gate on job-like titles)
    for path in ("/feed/?post_type=jobs", "/careers/feed/", "/jobs/feed/"):
        try:
            r = await client.get(base + path, timeout=10)
            if r.status_code == 200 and "<item" in r.text:
                titles = re.findall(
                    r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", r.text, re.S
                )[1:6]
                if titles and _titles_look_like_jobs(titles):
                    return {
                        "brand": brand,
                        "host": host,
                        "kind": "rss",
                        "endpoint": base + path,
                        "n": r.text.count("<item"),
                        "titles": [t.strip()[:40] for t in titles[:3]],
                    }
        except Exception:
            pass
    # 3) schema.org JobPosting JSON-LD on careers pages
    for path in ("/careers", "/jobs", "/careers/", "/job-openings"):
        try:
            r = await client.get(base + path, timeout=10, follow_redirects=True)
            if r.status_code == 200 and '"JobPosting"' in r.text:
                n = r.text.count('"JobPosting"')
                return {
                    "brand": brand,
                    "host": host,
                    "kind": "schemaorg-jsonld",
                    "endpoint": base + path,
                    "n": n,
                    "titles": [],
                }
        except Exception:
            pass
    # 4) HTML table with job content
    for path in ("/careers", "/jobs", "/current-openings", "/job-openings"):
        try:
            r = await client.get(base + path, timeout=10, follow_redirects=True)
            if r.status_code == 200 and r.text.count("<tr") >= 4 and JOBWORD.search(r.text):
                rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
                jobrows = [row for row in rows if JOBWORD.search(re.sub(r"<[^>]+>", "", row))]
                if len(jobrows) >= 2:
                    return {
                        "brand": brand,
                        "host": host,
                        "kind": "html-table",
                        "endpoint": base + path,
                        "n": len(jobrows),
                        "titles": [],
                    }
        except Exception:
            pass
    return None


async def main() -> None:
    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set")
        return
    seed_keys = load_seed_keys()
    giants = [
        g
        for g in json.loads(GIANTS.read_text())["uncovered_top"]
        if _core(g["name"]) not in seed_keys
    ]
    print(f"resolving + probing {len(giants)} residual giants ...", flush=True)
    hits: list[dict] = []
    sem = asyncio.Semaphore(10)
    async with (
        AsyncFetcher(per_host_rate=20) as fetcher,
        httpx.AsyncClient(
            headers={"User-Agent": UA}, verify=False, follow_redirects=False
        ) as client,
    ):

        async def work(g: dict) -> None:
            async with sem:
                brand = brand_query(g["name"])
                urls = await tavily(brand, key, fetcher)
                host = pick_domain(g["name"], urls)
                if not host:
                    return
                res = await probe_domain(g["name"], host, client)
                if res:
                    res["filings"] = g["filings"]
                    hits.append(res)
                    print(
                        f"  HIT [{res['kind']}] {g['name']!r} {res['endpoint']} "
                        f"(n={res['n']}) {res.get('titles', [])}",
                        flush=True,
                    )

        await asyncio.gather(*(work(g) for g in giants))
    hits.sort(key=lambda h: -h["filings"])
    (ROOT / "runs" / "newpattern_hits.json").write_text(json.dumps(hits, indent=1))
    print(f"\n{len(hits)} hits -> runs/newpattern_hits.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
