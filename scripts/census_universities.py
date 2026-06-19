"""University-cluster capture: the biggest *capturable* residual cluster (28 universities,
~6k H-1B filings) that the brand-normalization census missed.

Universities are hard for generic discovery because (1) their careers page surfaces Handshake
(a student-jobs platform) ahead of the real staff ATS, and (2) their Workday/iCIMS tenant is an
*abbreviation* (``upenn``, ``asuep``, ``osu``, ``wvumedicine``) that doesn't substring-match the
legal name, so `discover_giants._relates` rejects it. So per university we:

  1. Tavily two STAFF-careers-biased queries ("… staff careers myworkdayjobs", "… employment
     workday") — these reliably surface the real Workday/iCIMS host, not Handshake.
  2. Extract full Workday URLs (tenant|wd|site, site taken from the URL path) and careers-iCIMS
     hosts, and keep only those whose tenant matches an *abbreviation* of the university (initials,
     u+mainword, mainword) — kills on-campus-vendor noise (careers-bncollege = Barnes & Noble
     College, careers-ovg = OVG venues).
  3. Verify live + require jobs>0. Tavily-context + abbreviation-match + live jobs = high confidence
     even though Workday exposes no company display name to adjudicate on.

Usage::

    .venv/bin/python scripts/census_universities.py [--cap N] [--out scripts/candidates_uni.json]
    .venv/bin/python scripts/build_registry.py scripts/candidates_uni.json
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402
from harvest_tokens import _core  # noqa: E402

from census_successfactors import tavily  # noqa: E402  # isort: skip

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

load_builtins()
GIANTS = ROOT / "runs" / "giants.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_uni.json"
_EDU_HINT = ("university", "college", "institute of technology", "school district", "schools")

_STOP = {
    "university",
    "of",
    "the",
    "state",
    "system",
    "college",
    "regents",
    "trustees",
    "curators",
    "board",
    "at",
    "and",
    "for",
    "school",
    "district",
    "institute",
    "technology",
    "health",
    "medical",
    "center",
    "schools",
    "research",
    "foundation",
    "pc",
    "inc",
    "associates",
    "services",
    "care",
}
_WD_URL = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com(/[^\s\"'<>]*)?", re.I)
_ICIMS = re.compile(r"https?://(careers-[a-z0-9-]+|[a-z0-9-]+)\.icims\.com", re.I)


def abbrevs(name: str) -> set[str]:
    """Plausible ATS-tenant abbreviations of a university name."""
    s = re.sub(r"[^a-z ]", " ", name.lower())
    words = [w for w in s.split() if w]
    sig = [w for w in words if w not in _STOP]
    out: set[str] = set()
    if sig:
        out.add("".join(w[0] for w in words))  # all-word initials (asu, fsu, wvu, ndsu)
        out.add("".join(w[0] for w in sig))  # significant initials
        out.add("u" + sig[0])  # upenn-style
        out.add(sig[0])  # main word
        out.add("".join(sig[:2]))  # two-word run
    return {a for a in out if len(a) >= 2}


def _wd_site(path: str | None) -> str | None:
    """The Workday careersite id is the first path segment after an optional locale."""
    if not path:
        return None
    segs = [s for s in path.split("/") if s]
    if segs and re.fullmatch(r"[a-z]{2}(-[a-z]{2,4})?", segs[0], re.I):
        segs = segs[1:]  # drop locale (en-US / en-us)
    return segs[0] if segs else None


# Entity-type markers that, when present in a tenant/site but NOT in the university's own name,
# signal a DIFFERENT (legally separate) employer that merely shares the city/abbreviation:
# "cincinnatichildrens" (Cincinnati Children's Hospital) vs University of Cincinnati,
# "wvumedicine" (WVU Medicine health system) vs West Virginia University, "asuep"/"ASUFoundation"
# (ASU Enterprise Partners) vs Arizona State. Precision-first: reject these.
_DIFF_ENTITY = (
    "children",
    "hospital",
    "clinic",
    "medicine",
    "medical",
    "health",
    "foundation",
    "enterprise",
    "partners",
    "ventures",
    "physicians",
)


def _tenant_ok(tenant: str, ab: set[str], name: str) -> bool:
    t = tenant.lower()
    if not any(a in t or t in a for a in ab):
        return False
    nlow = name.lower()
    return not any(d in t and d not in nlow for d in _DIFF_ENTITY)


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
    unis = [
        g
        for g in json.loads(GIANTS.read_text())["uncovered_top"]
        if _core(g["name"]) not in seed_keys and any(h in g["name"].lower() for h in _EDU_HINT)
    ][:cap]
    print(f"university census over {len(unis)} residual universities ...", flush=True)

    candidates: list[dict] = []
    taken: set[str] = set()
    done = [0]

    async def work(g: dict, tav: AsyncFetcher, ver: AsyncFetcher) -> None:
        name = g["name"]
        ck = _core(name)
        if not ck or ck in seed_keys or ck in taken:
            done[0] += 1
            return
        ab = abbrevs(name)
        urls: list[str] = []
        for q in (f"{name} staff careers myworkdayjobs", f"{name} employment workday icims"):
            with contextlib.suppress(Exception):
                urls += await tavily(q, key, tav)
        # Collect abbreviation-matched Workday + iCIMS candidates.
        wd: list[tuple[str, str, str]] = []  # (tenant, wd, site)
        ic: list[str] = []
        nlow = name.lower()

        def _site_ok(site: str) -> bool:
            s = site.lower()
            return not any(d in s and d not in nlow for d in _DIFF_ENTITY)

        for url in urls:
            m = _WD_URL.search(url)
            if m and _tenant_ok(m.group(1), ab, name):
                site = _wd_site(m.group(3))
                if site and _site_ok(site):
                    wd.append((m.group(1).lower(), m.group(2).lower(), site))
            mi = _ICIMS.search(url)
            if mi:
                label = mi.group(1).lower().replace("careers-", "")
                if _tenant_ok(label, ab, name):
                    ic.append(mi.group(0).split("//", 1)[1].split("/")[0].lower())

        chosen: dict | None = None
        # Prefer Workday (richest), then iCIMS. Verify live; require jobs>0.
        for tenant, wdn, site in dict.fromkeys(wd):
            tok = f"{tenant}|{wdn}|{site}"
            try:
                raws = await get_provider("workday").fetch(tok, SearchQuery(limit=2), ver)
            except Exception:  # noqa: BLE001
                raws = []
            if raws:
                chosen = {
                    "company": ck,
                    "ats": "workday",
                    "domain": None,
                    "tenant": tenant,
                    "wd": wdn,
                    "site": site,
                }
                break
        if chosen is None:
            for host in dict.fromkeys(ic):
                try:
                    raws = await get_provider("icims").fetch(host, SearchQuery(limit=2), ver)
                except Exception:  # noqa: BLE001
                    raws = []
                if raws:
                    chosen = {"company": ck, "ats": "icims", "domain": None, "token": host}
                    break

        if chosen and ck not in taken:
            taken.add(ck)
            chosen["_name"] = name
            chosen["_filings"] = g.get("filings")
            candidates.append(chosen)
        done[0] += 1
        if done[0] % 10 == 0:
            print(f"  processed {done[0]}/{len(unis)} (captured: {len(candidates)})", flush=True)

    async with (
        AsyncFetcher(concurrency=8, per_host_rate=5, timeout=15.0, retries=2) as tav,
        AsyncFetcher(concurrency=10, per_host_rate=6, timeout=25.0, retries=1) as ver,
        anyio.create_task_group() as tg,
    ):
        for g in unis:
            tg.start_soon(work, g, tav, ver)

    candidates.sort(key=lambda c: -(c.get("_filings") or 0))
    for c in candidates:
        tok = c.get("token") or f"{c.get('tenant', '')}|{c.get('wd', '')}|{c.get('site', '')}"
        print(f"  + {c['_name'][:34]:34} ({c.get('_filings') or 0:>5}) {c['ats']:8} {tok}")
    out = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(out)} university candidates -> {out_path.name}")


if __name__ == "__main__":
    anyio.run(main)
