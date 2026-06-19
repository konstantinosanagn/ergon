"""Greedy residual-giant census: brand-normalize the legal filing name, then re-run the proven
ATS-host discovery.

The 198 still-uncaptured giants are dominated by two patterns the first discovery pass couldn't
handle because it searched their *legal* name verbatim:

  * Boilerplate-wrapped entities — "trustees of university of pennsylvania", "leland stanford jr
    university", "curators of the university of missouri". Tavily can't find a careers page for
    "trustees of ..."; it trivially finds one for "University of Pennsylvania".
  * Brand vs. legal mismatch — "federal express" (FedEx), "bofa securities" (Bank of America),
    "cgi technologies and solutions" (CGI). The careers host is on the *brand* domain.

So per residual giant we strip legal boilerplate to a clean human query, Tavily THAT, add
brand-slug host guesses (incl. ``.edu`` for the university cluster), then reuse discover_giants'
``detect`` + verify + adjudicate pipeline unchanged. The brand query is also what we adjudicate
against (name_match on "Bank of America" beats "bofa securities").

Usage::

    .venv/bin/python scripts/census_residual.py [--cap N] [--out scripts/candidates_residual.json]
    .venv/bin/python scripts/build_registry.py scripts/candidates_residual.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from discover_giants import (  # noqa: E402
    _SUBLINK,
    EXCLUDE,
    _company_key,
    detect,
)
from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core, name_match  # noqa: E402

from census_successfactors import tavily  # noqa: E402  # isort: skip

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
GIANTS = ROOT / "runs" / "giants.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_residual.json"
HITS_CACHE = ROOT / "runs" / "residual_hits.json"  # cached detections -> cheap verify re-tuning

# Legal-entity boilerplate to strip from a filing name to recover the searchable brand. Order
# matters: longer phrases first. These are removed wherever they appear (prefix or suffix).
_BOILER = (
    "the trustees of",
    "trustees of",
    "the regents of the",
    "regents of the",
    "the regents of",
    "board of trustees of the",
    "board of trustees of",
    "board of regents of the",
    "board of regents of",
    "the curators of the",
    "curators of the",
    "curators of",
    "president and fellows of",
    "rector and visitors of",
    "the rector and visitors of",
    "leland",
)
_SUFFIX = (
    " jr university",
    " n a",
    " n.a.",
    " incorporated",
    " corporation",
    " holdings",
    " securities",
    " l p",
    " lp",
    " l l c",
    " llc",
    " inc",
    " corp",
    " co",
    " plc",
    " ag",
    " sa",
    " usa",
    " us",
)
_EDU_HINT = ("university", "college", "institute of technology", "school district", "state univ")


def brand_query(name: str) -> str:
    """A clean, human-searchable brand name from a legal filing name (keeps readable words)."""
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for b in _BOILER:
            if s.startswith(b + " "):
                s = s[len(b) + 1 :].strip()
                changed = True
            if s.endswith(" " + b):
                s = s[: -(len(b) + 1)].strip()
                changed = True
        for x in _SUFFIX:
            if s.endswith(x):
                s = s[: -len(x)].strip()
                changed = True
    return s or name.lower()


def _edu_slug(query: str) -> str:
    """For universities the brand host is the distinctive word(s) before/after 'university of'."""
    q = query.replace("university of", "").replace("university", "").strip()
    words = q.split()
    return "".join(words[:2]) if words else ""


def residual_urls(query: str, original: str, tavily_urls: list[str]) -> list[str]:
    """Tavily results plus brand-slug host guesses; adds .edu hosts for the university cluster."""
    out = [u for u in tavily_urls if u and not any(x in u.lower() for x in EXCLUDE)][:4]
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", query).split() if len(w) > 2]
    slug = words[0] if words else ""
    is_edu = any(h in original.lower() for h in _EDU_HINT)
    guesses: list[str] = []
    if len(slug) >= 3:
        guesses += [
            f"careers.{slug}.com",
            f"jobs.{slug}.com",
            f"{slug}.com/careers",
            f"www.{slug}.com/careers",
        ]
    if is_edu:
        edu = _edu_slug(query) or slug
        if len(edu) >= 3:
            guesses += [
                f"careers.{edu}.edu",
                f"jobs.{edu}.edu",
                f"{edu}.edu/careers",
                f"hr.{edu}.edu/careers",
                f"careers.{slug}.edu",
                f"jobs.{slug}.edu",
            ]
    for g in guesses:
        u = "https://" + g
        if u not in out:
            out.append(u)
    return out


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    cap = 400
    reuse = False
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--cap":
            cap = int(args[i + 1])
            i += 2
        elif (
            args[i] == "--reuse"
        ):  # skip discovery, re-verify cached detections (tune adjudication)
            reuse = True
            i += 1
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
    print(f"residual census over {len(giants)} giants (brand-normalized) ...", flush=True)

    hits: dict[int, tuple[str, str]] = {}
    brands: dict[int, str] = {}
    done = [0]

    if reuse and HITS_CACHE.exists():
        cached = json.loads(HITS_CACHE.read_text())
        hits = {int(k): tuple(v) for k, v in cached["hits"].items()}
        brands = {int(k): v for k, v in cached["brands"].items()}
        print(f"reusing {len(hits)} cached detections (skipping discovery) ...", flush=True)

    async def find(idx: int, g: dict, tav: AsyncFetcher, probe: AsyncFetcher) -> None:
        original = g["name"]
        query = brand_query(original)
        brands[idx] = query
        # Tavily the BRAND query (+ "careers" keyword for relevance) — far better recall than
        # the verbatim legal name for boilerplate-wrapped entities and universities.
        urls = await tavily(f"{query} careers", key, tav)
        for u in residual_urls(query, original, urls):
            try:
                resp = await probe.request("GET", u, timeout=12.0)
            except Exception:  # noqa: BLE001
                continue
            html = resp.text or ""
            hit = await detect(query, html, probe)
            if hit is None:
                sm = _SUBLINK.search(html)
                if sm and sm.group(1) != u:
                    try:
                        sub = await probe.request("GET", sm.group(1), timeout=12.0)
                        hit = await detect(query, sub.text or "", probe)
                    except Exception:  # noqa: BLE001
                        pass
            if hit:
                hits[idx] = hit
                break
        done[0] += 1
        if done[0] % 40 == 0:
            print(f"  scanned {done[0]}/{len(giants)} (hits: {len(hits)})", flush=True)

    if not reuse:
        async with (
            AsyncFetcher(concurrency=8, per_host_rate=4, timeout=15.0, retries=3) as tav,
            AsyncFetcher(concurrency=24, per_host_rate=10, timeout=12.0, retries=1) as probe,
            anyio.create_task_group() as tg,
        ):
            for idx, g in enumerate(giants):
                tg.start_soon(find, idx, g, tav, probe)
        HITS_CACHE.write_text(
            json.dumps(
                {"hits": {str(k): list(v) for k, v in hits.items()}, "brands": brands},
                indent=2,
            )
            + "\n"
        )

    print(
        f"detected {len(hits)} {dict(Counter(a for a, _ in hits.values()))}; verifying ...",
        flush=True,
    )

    candidates: list[dict] = []
    taken: set[str] = set()

    async def verify(idx: int, ats: str, token: str, fetcher: AsyncFetcher) -> None:
        sponsor = giants[idx]["name"]
        brand = brands.get(idx, sponsor)
        ck = _company_key(sponsor, ats, token).lower()
        if not ck or ck in seed_keys or ck in taken:
            return
        try:
            raws = await get_provider(ats).fetch(token, SearchQuery(limit=1), fetcher)
        except Exception:  # noqa: BLE001
            raws = []
        board_co = raws[0].company if raws else ""
        # Adjudication. For host-related ATSes (workday/icims/taleo/avature/phenom/SF/eightfold/
        # oracle) `detect` already vetted that the ATS *host* belongs to the sponsor via `_relates`,
        # so a board that returns jobs is trusted — its display name often differs from the filing
        # brand (e.g. "Stanford Health Care" vs "leland stanford jr university") and a second
        # name_match would wrongly kill it. Shared-host ATSes (greenhouse/lever/smartrecruiters/...)
        # let anyone register, so there we still require a name_match against the legal OR brand name.
        host_vetted = {
            "workday",
            "eightfold",
            "oracle",
            "icims",
            "taleo",
            "avature",
            "phenom",
            "successfactors",
        }
        ok = bool(raws) and (
            ats in host_vetted
            or name_match(sponsor, board_co or "")
            or name_match(brand, board_co or "")
        )
        if ok:
            taken.add(ck)
            cand: dict = {
                "company": ck,
                "ats": ats,
                "domain": None,
                "_sponsor": sponsor,
                "_brand": brand,
                "_filings": giants[idx].get("filings"),
            }
            if ats == "workday":
                tenant, wd, site = token.split("|", 2)
                cand.update({"tenant": tenant, "wd": wd, "site": site})
            else:
                cand["token"] = token
            candidates.append(cand)

    async with (
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=25.0, retries=1) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for idx, (ats, token) in hits.items():
            tg.start_soon(verify, idx, ats, token, fetcher)

    candidates.sort(key=lambda c: -(c.get("_filings") or 0))
    for c in candidates:
        tok = c.get("token") or f"{c.get('tenant', '')}|{c.get('wd', '')}|{c.get('site', '')}"
        print(f"  + {c['_sponsor'][:26]:26} ({c.get('_filings') or 0:>5}) {c['ats']:12} {tok[:40]}")
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(
        f"\nwrote {len(out)} candidates {dict(Counter(c['ats'] for c in candidates))} "
        f"-> {out_path.name}"
    )


if __name__ == "__main__":
    anyio.run(main)
