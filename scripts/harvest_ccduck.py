"""Harvest ATS board tokens from Common Crawl's COLUMNAR index via DuckDB -> candidates.json.

This is the high-leverage cousin of ``harvest_commoncrawl.py``. Instead of the flaky, slow
HTTP CDX API, it queries Common Crawl's columnar (Parquet) index directly with DuckDB over the
public HTTPS mirror (``data.commoncrawl.org``) — no S3 credentials, no paid warehouse, free.

Why it's fast: the index is sorted by ``url_surtkey`` (reversed-host SURT), so a range filter on
that key lets DuckDB skip non-matching row groups via Parquet stats and only HTTP-range-fetch
the bytes it needs. A whole crawl (300 Parquet files) for one ATS returns in ~10s.

Pipeline (same propose -> verify seam as every other harvester):
  1. Fetch ``cc-index-table.paths.gz`` for each crawl -> the list of ``subset=warc`` Parquet files.
  2. For each ATS, DuckDB-query all files with a SURT-range + host filter -> board URLs.
  3. Extract tokens (reusing harvest_commoncrawl's per-ATS extractors), dedupe, drop seeded.
  4. ``build_registry.py`` verifies each live before merging.

Usage::

    .venv/bin/python scripts/harvest_ccduck.py [greenhouse ashby ...] [--crawls CC-MAIN-2024-51,...]
    .venv/bin/python scripts/build_registry.py scripts/candidates_ccduck.json --dry-run
"""

from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import (  # noqa: E402
    CONFIGS,
    CCSource,
    extract_tokens,
    load_seed_keys,
    recent_crawl_ids,
)

DEFAULT_OUT = ROOT / "scripts" / "candidates_ccduck.json"
_DATA = "https://data.commoncrawl.org/"
_PATHS = _DATA + "crawl-data/{crawl}/cc-index-table.paths.gz"
_COLLINFO = "https://index.commoncrawl.org/collinfo.json"

# Richly-crawled ATS hosts (greenhouse/ashby/workable/smartrecruiters) + the subdomain ATSes.
# (lever is omitted: its board paths are robots-blocked, so they're absent from CC regardless.)
DEFAULT_ATSES = ("greenhouse", "ashby", "workable", "smartrecruiters", "rippling", "pinpoint")
# Number of crawls-WITH-DATA to union by default (more snapshots = more boards; companies come and
# go and CC's per-crawl coverage varies).
DEFAULT_N_CRAWLS = 3
# Auto-resolution pulls a larger pool of recent crawls than DEFAULT_N_CRAWLS so the harvester can
# fall through the newest few (whose columnar table lags publication and returns 0 rows) to the
# most-recent crawls that actually have data.
DEFAULT_CRAWL_POOL = 8
# Static fallback ONLY for when collinfo.json is unreachable. Kept current; the live default is
# resolved dynamically from collinfo so this never silently mines a stale snapshot.
DEFAULT_CRAWL = "CC-MAIN-2026-25"


# Common Crawl asks clients to send a descriptive User-Agent; the bare urllib UA gets throttled
# (403) quickly on data.commoncrawl.org. A polite UA + a small inter-request delay keeps the
# harvester well within CC's free-access etiquette.
_UA = "ergon-tracker/0.1 (+https://github.com/konstantinosanagn/ergon-tracker) coverage-discovery"
_CRAWL_DELAY_S = 1.5


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _fetch_collinfo() -> str:
    return _http_get(_COLLINFO).decode()


def resolve_default_crawls(n: int = DEFAULT_N_CRAWLS, *, fetch=None) -> list[str]:
    """Resolve the ``n`` most-recent crawl ids from collinfo.json; fall back to ``DEFAULT_CRAWL``.

    Network + fallback are isolated here (``fetch`` is injectable for tests) so the harvester
    never mines an aged-out crawl just because a constant went stale.
    """
    fetch = fetch or _fetch_collinfo
    try:
        ids = recent_crawl_ids(fetch(), n)
    except Exception:  # noqa: BLE001 - any failure -> safe static fallback
        return [DEFAULT_CRAWL]
    return ids or [DEFAULT_CRAWL]


# --- pure SURT bounds (no network; unit-tested) -----------------------------------------------


def surt_bounds(source: CCSource) -> tuple[str, str, str, str]:
    """Return ``(lo, hi, host_col, host_val)`` for one ATS's columnar-index query.

    The columnar index is sorted by ``url_surtkey`` = reversed host + ``)`` + path, e.g.
    ``boards.greenhouse.io`` -> ``io,greenhouse,boards)/``. A half-open ``[lo, hi)`` range over
    that key bounds the scan to the ATS's rows and enables Parquet row-group skipping. The
    extra exact host filter keeps it correct.

    * host match (token in path):  lo = ``<rev-host>)/``      filter url_host_name = host
    * domain match (token in sub): lo = ``<rev-domain>,``     filter url_host_registered_domain
    """
    rev = ",".join(reversed(source.query.split(".")))
    if source.match_type == "host":
        lo = rev + ")/"
        host_col = "url_host_name"
    else:  # domain — tenant is a subdomain, so bound by the registered domain + comma separator
        lo = rev + ","
        host_col = "url_host_registered_domain"
    hi = lo[:-1] + chr(ord(lo[-1]) + 1)  # exclusive upper bound: bump the last byte
    return lo, hi, host_col, source.query


def warc_parquet_urls(paths_gz: bytes) -> list[str]:
    """Decompress a cc-index-table.paths.gz and return the ``subset=warc`` Parquet HTTPS URLs."""
    paths = gzip.decompress(paths_gz).decode().split()
    return [_DATA + p for p in paths if "/subset=warc/" in p and p.endswith(".parquet")]


# --- DuckDB query -----------------------------------------------------------------------------


def _sql_list(urls: list[str]) -> str:
    # URLs are https://… with no single quotes, so simple quoting is safe.
    return "[" + ",".join("'" + u + "'" for u in urls) + "]"


def query_ats(con, files: list[str], source: CCSource) -> list[str]:
    """Run the SURT-bounded columnar query for one ATS; return the matched board URLs."""
    lo, hi, host_col, host_val = surt_bounds(source)
    sql = (
        f"SELECT url FROM read_parquet({_sql_list(files)}) "
        f"WHERE url_surtkey >= '{lo}' AND url_surtkey < '{hi}' "
        f"AND {host_col} = '{host_val}'"
    )
    return [row[0] for row in con.execute(sql).fetchall()]


def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    crawls: list[str] | None = None  # None -> resolve the latest N from collinfo
    limit = 1_000_000
    atses: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif a == "--crawls":
            crawls = [c.strip() for c in args[i + 1].split(",") if c.strip()]
            i += 2
        elif a == "--crawl":
            crawls = [args[i + 1]]
            i += 2
        elif a == "--limit":
            limit = int(args[i + 1])
            i += 2
        elif a.startswith("--"):
            print(f"unknown flag: {a}")
            return
        else:
            atses.append(a)
            i += 1

    # Auto-resolution resolves a POOL of recent crawls (not just N), because Common Crawl's
    # columnar (Parquet) table lags the crawl by weeks: the newest 2-3 crawls have a published
    # paths.gz but an empty/unbuilt warc table (queries return 0 rows). We harvest newest-first
    # and STOP after DEFAULT_N_CRAWLS crawls that actually yield data — self-healing against the
    # publication lag instead of silently mining empty fresh crawls. A pinned --crawls/--crawl is
    # honored verbatim (no fall-through).
    auto = crawls is None
    if auto:
        crawls = resolve_default_crawls(DEFAULT_CRAWL_POOL)
        print(f"resolved recent-crawl pool from collinfo: {crawls}")

    if not atses:
        atses = list(DEFAULT_ATSES)
    unknown = [a for a in atses if a not in CONFIGS]
    if unknown:
        print(f"unknown ATS(es): {unknown}; known: {sorted(CONFIGS)}")
        return

    try:
        import duckdb
    except ModuleNotFoundError:
        print("duckdb not installed — run:  uv pip install duckdb")
        return

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET http_keep_alive=true; SET http_timeout=120000;")

    seed_keys = load_seed_keys()
    print(f"harvesting CC columnar index for {atses} over crawls {crawls}")

    candidates: list[dict[str, object]] = []
    global_seen: set[str] = set()
    # tokens accumulate across crawls per ATS, then dedupe/skip-seed once.
    tokens_by_ats: dict[str, list[str]] = {a: [] for a in atses}

    good_crawls = 0  # crawls that actually returned data (used to stop the auto pool early)
    for idx, crawl in enumerate(crawls):
        if idx:
            time.sleep(_CRAWL_DELAY_S)  # polite pacing between CC index fetches
        try:
            files = warc_parquet_urls(_http_get(_PATHS.format(crawl=crawl)))
        except Exception as exc:  # noqa: BLE001 - skip a missing/unreachable crawl
            print(f"  [{crawl}] paths.gz failed ({type(exc).__name__}); skipping")
            continue
        print(f"  [{crawl}] {len(files)} warc parquet files")
        crawl_urls = 0
        for name in atses:
            try:
                urls = query_ats(con, files, CONFIGS[name])
            except Exception as exc:  # noqa: BLE001 - one ATS/crawl shouldn't sink the run
                print(f"    [{name}] query failed: {type(exc).__name__}: {str(exc)[:60]}")
                continue
            crawl_urls += len(urls)
            toks = extract_tokens(CONFIGS[name], urls)
            tokens_by_ats[name].extend(toks)
            print(f"    [{name}] urls={len(urls)} tokens={len(toks)}")
        if crawl_urls == 0:
            # Empty table: too-fresh (unbuilt) crawl, or schema gap — don't count it.
            print(f"  [{crawl}] 0 rows (likely too-fresh/unbuilt columnar table); skipping")
            continue
        good_crawls += 1
        if auto and good_crawls >= DEFAULT_N_CRAWLS:
            print(f"  reached {DEFAULT_N_CRAWLS} crawls with data; stopping pool scan")
            break

    for name in atses:
        seen: set[str] = set()
        for t in tokens_by_ats[name]:
            if t in seen or t in seed_keys or t in global_seen:
                continue
            seen.add(t)
            global_seen.add(t)
            candidates.append({"company": t, "ats": name, "token": t, "domain": None})
            if len([c for c in candidates if c["ats"] == name]) >= limit:
                break

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
    main()
