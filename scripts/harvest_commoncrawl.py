"""Harvest ATS board tokens from the Common Crawl URL index -> candidates.json.

Common Crawl publishes a queryable index of billions of crawled URLs. ATS board URLs appear
all over the crawled web — ``boards.greenhouse.io/{token}``, ``jobs.lever.co/{token}``,
``jobs.ashbyhq.com/{token}``, ``{token}.bamboohr.com``, … — so querying the index for those
patterns yields tens of thousands of real, in-the-wild tokens with no name-guessing and no
paid API. This is the long-tail discovery method that reaches beyond any curated list.

Propose, don't dispose: we extract tokens, dedupe, skip ones already seeded, and write a
``candidates.json`` that ``scripts/build_registry.py`` verifies live before merging. Common
Crawl is just the *discovery* source; our provider stack is the truth.

Usage::

    .venv/bin/python scripts/harvest_commoncrawl.py [greenhouse lever ...] [--limit N] [--crawl CC-MAIN-YYYY-WW]
    .venv/bin/python scripts/build_registry.py scripts/candidates_commoncrawl.json --dry-run
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_commoncrawl.json"

_COLLINFO = "https://index.commoncrawl.org/collinfo.json"

# Path segments / subdomains that are never a real board token.
_JUNK = frozenset(
    {
        "embed", "job_app", "job_board", "jobs", "job", "api", "www", "v1", "v2", "boards",
        "secure", "app", "apps", "static", "assets", "content", "_next", "favicon.ico",
        "robots.txt", "sitemap.xml", "search", "share", "widget", "o", "embed.js", "css", "js",
        "images", "img", "auth", "login", "status", "support", "help", "blog", "cdn",
    }
)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


def _clean(token: str | None, *, lower: bool = True) -> str | None:
    """Validate a candidate token; return it (optionally lowercased) or ``None``."""
    if not token:
        return None
    tok = token.strip().strip("/")
    if lower:
        tok = tok.lower()
    if tok.lower() in _JUNK or not _TOKEN_RE.match(tok) or tok.isdigit():
        return None
    return tok


# --- per-ATS token extractors (pure; unit-tested) ---------------------------------------------


def _host_path(url: str) -> tuple[str, list[str], dict[str, list[str]]]:
    s = urlsplit(url if "://" in url else "https://" + url)
    host = s.netloc.split("@")[-1].split(":")[0].lower()
    segs = [seg for seg in s.path.split("/") if seg]
    return host, segs, parse_qs(s.query)


def _extract_greenhouse(url: str) -> str | None:
    host, segs, q = _host_path(url)
    if "greenhouse.io" not in host:
        return None
    if q.get("for"):  # embed/job_board?for={token}
        return _clean(q["for"][0])
    return _clean(segs[0]) if segs else None


def _path_extractor(host_needle: str, *, lower: bool = True):
    def extract(url: str) -> str | None:
        host, segs, _ = _host_path(url)
        if host_needle not in host or not segs:
            return None
        return _clean(segs[0], lower=lower)

    return extract


def _subdomain_extractor(domain: str):
    suffix = "." + domain
    label_re = re.compile(r"^([a-z0-9][a-z0-9-]*)" + re.escape(suffix) + r"$", re.IGNORECASE)

    def extract(url: str) -> str | None:
        host, _, _ = _host_path(url)
        m = label_re.match(host)
        return _clean(m.group(1)) if m else None

    return extract


@dataclass(frozen=True)
class CCSource:
    ats: str
    query: str  # CC index ``url`` value
    match_type: str  # "host" or "domain"
    extract: object  # Callable[[str], str | None]


CONFIGS: dict[str, CCSource] = {
    "greenhouse": CCSource("greenhouse", "boards.greenhouse.io", "host", _extract_greenhouse),
    "lever": CCSource("lever", "jobs.lever.co", "host", _path_extractor("jobs.lever.co")),
    "ashby": CCSource("ashby", "jobs.ashbyhq.com", "host", _path_extractor("jobs.ashbyhq.com")),
    "workable": CCSource(
        "workable", "apply.workable.com", "host", _path_extractor("apply.workable.com")
    ),
    "smartrecruiters": CCSource(
        "smartrecruiters", "careers.smartrecruiters.com", "host",
        _path_extractor("careers.smartrecruiters.com", lower=False),  # SR slugs are case-sensitive
    ),
    "bamboohr": CCSource("bamboohr", "bamboohr.com", "domain", _subdomain_extractor("bamboohr.com")),
    "breezy": CCSource("breezy", "breezy.hr", "domain", _subdomain_extractor("breezy.hr")),
    "teamtailor": CCSource(
        "teamtailor", "teamtailor.com", "domain", _subdomain_extractor("teamtailor.com")
    ),
    "recruitee": CCSource(
        "recruitee", "recruitee.com", "domain", _subdomain_extractor("recruitee.com")
    ),
    "rippling": CCSource(
        "rippling", "ats.rippling.com", "host", _path_extractor("ats.rippling.com")
    ),
    "pinpoint": CCSource(
        "pinpoint", "pinpointhq.com", "domain", _subdomain_extractor("pinpointhq.com")
    ),
}

# Path-based ATSes whose board paths Common Crawl actually captured. greenhouse/ashby/workable
# are richly crawled; lever's robots.txt blocks board paths (CC has only its robots.txt), so it
# is excluded from defaults — pass it explicitly if a future crawl covers it.
DEFAULT_ATSES = ("greenhouse", "ashby", "workable", "smartrecruiters")


# --- pure parsing ------------------------------------------------------------------------------


def parse_cc_urls(ndjson: str) -> list[str]:
    """Extract the ``url`` field from each line of a Common Crawl NDJSON index response."""
    urls: list[str] = []
    for line in ndjson.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        url = rec.get("url") if isinstance(rec, dict) else None
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def parse_num_pages(text: str) -> int:
    """Parse a CC index ``showNumPages=true`` response into a page count (>=1)."""
    try:
        data = json.loads(text)
    except ValueError:
        return 1
    if isinstance(data, dict):
        return max(1, int(data.get("pages", 1)))
    if isinstance(data, int):
        return max(1, data)
    return 1


def latest_crawl_api(collinfo: str) -> str | None:
    """Return the cdx-api URL of the most recent crawl from collinfo.json text."""
    try:
        crawls = json.loads(collinfo)
    except ValueError:
        return None
    if not isinstance(crawls, list) or not crawls:
        return None
    # collinfo.json is newest-first; prefer the explicit cdx-api field.
    top = crawls[0]
    return top.get("cdx-api") if isinstance(top, dict) else None


def extract_tokens(source: CCSource, urls: list[str]) -> list[str]:
    """Run one ATS's extractor over crawled URLs; return unique tokens in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        tok = source.extract(url)  # type: ignore[operator]
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def load_seed_keys(seed_path: Path = SEED) -> set[str]:
    if not seed_path.exists():
        return set()
    return set(json.loads(seed_path.read_text()).get("companies", {}))


# --- network harvest ---------------------------------------------------------------------------


async def _resolve_crawl(fetcher: AsyncFetcher, override: str | None) -> str | None:
    if override:
        return f"https://index.commoncrawl.org/{override}-index"
    try:
        return latest_crawl_api(await fetcher.get_text(_COLLINFO))
    except Exception as exc:  # noqa: BLE001
        print(f"  collinfo fetch failed: {type(exc).__name__}: {exc}")
        return None


async def _query_cc(api: str, source: CCSource, fetcher: AsyncFetcher, max_pages: int) -> list[str]:
    """Fetch crawled URLs for one ATS across ALL index pages (up to ``max_pages``).

    The CC index is internally paged: ``showNumPages=true`` reports the page count, then each
    ``page=N`` returns one block. We pull every page concurrently so a richly-crawled host like
    greenhouse yields tens of thousands of distinct board URLs instead of a single capped page.
    ``collapse=urlkey`` dedupes per-capture rows (thousands of robots.txt hits) within each page.
    """
    base = {"url": source.query, "matchType": source.match_type, "output": "json",
            "collapse": "urlkey"}
    try:
        pages = parse_num_pages(await fetcher.get_text(api, params={**base, "showNumPages": "true"}))
    except Exception as exc:  # noqa: BLE001 - CC index is flaky; report and continue
        print(f"  [{source.ats}] CC page-count failed: {type(exc).__name__}: {exc}")
        return []
    pages = min(pages, max_pages)

    results: dict[int, list[str]] = {}

    async def _page(p: int) -> None:
        try:
            results[p] = parse_cc_urls(
                await fetcher.get_text(api, params={**base, "page": str(p)})
            )
        except Exception:  # noqa: BLE001 - one bad page shouldn't sink the ATS
            results[p] = []

    async with anyio.create_task_group() as tg:
        for p in range(pages):
            tg.start_soon(_page, p)

    urls: list[str] = []
    for p in sorted(results):
        urls.extend(results[p])
    print(f"  [{source.ats}] index_pages={pages}")
    return urls


async def harvest(atses: list[str], fetcher: AsyncFetcher, limit: int, pages: int,
                  crawl: str | None) -> list[dict[str, object]]:
    api = await _resolve_crawl(fetcher, crawl)
    if not api:
        print("  no usable Common Crawl index; aborting")
        return []
    print(f"  using index: {api}")
    seed_keys = load_seed_keys()
    candidates: list[dict[str, object]] = []
    global_seen: set[str] = set()

    for name in atses:
        source = CONFIGS[name]
        urls = await _query_cc(api, source, fetcher, pages)
        tokens = extract_tokens(source, urls)
        new = [t for t in tokens if t not in seed_keys and t not in global_seen][:limit]
        for t in new:
            global_seen.add(t)
            candidates.append({"company": t, "ats": name, "token": t, "domain": None})
        print(f"  [{name}] crawled_urls={len(urls)} tokens={len(tokens)} new={len(new)}")
    return candidates


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    limit = 100000  # per-ATS token cap (safety); pagination is the real lever
    pages = 10  # max CC index pages per ATS (each page ~ a block of distinct URLs)
    crawl: str | None = None
    atses: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out_path = Path(args[i + 1]); i += 2
        elif a == "--limit":
            limit = int(args[i + 1]); i += 2
        elif a == "--pages":
            pages = int(args[i + 1]); i += 2
        elif a == "--crawl":
            crawl = args[i + 1]; i += 2
        elif a.startswith("--"):
            print(f"unknown flag: {a}"); return
        else:
            atses.append(a); i += 1

    if not atses:
        atses = list(DEFAULT_ATSES)
    unknown = [a for a in atses if a not in CONFIGS]
    if unknown:
        print(f"unknown ATS(es): {unknown}; known: {sorted(CONFIGS)}")
        return

    print(f"harvesting Common Crawl for: {atses}  (max_pages/ats={pages}, cap={limit})")
    async with AsyncFetcher(concurrency=6, per_host_rate=3, timeout=120.0) as fetcher:
        candidates = await harvest(atses, fetcher, limit, pages, crawl)

    by_ats: dict[str, int] = {}
    for c in candidates:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"\ntotal new candidates: {len(candidates)}  by_ats={by_ats}")
    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"wrote {shown}")
    print(f"\nnext: .venv/bin/python scripts/build_registry.py {shown} --dry-run")


if __name__ == "__main__":
    anyio.run(main)
