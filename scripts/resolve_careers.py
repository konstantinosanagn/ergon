"""Career-page ATS resolver: start from the COMPANY and find its real ATS — the company-first
discovery we should have had all along.

Everything before this found boards *indirectly*: inherited jobhive tenant lists, brute-force slug
guessing (``harvest_tokens``), and web/apply-URL mining. None of it asked "what ATS does *this*
company actually use?" — so it structurally missed Workday/SuccessFactors/iCIMS (the enterprise
backends most large-caps run). This resolver flips it: fetch a company's careers page, read the
ATS board link it points to (``{tenant}.wdN.myworkdayjobs.com``, ``boards.greenhouse.io/{token}``,
…), and recover ``(ats, token)`` via the providers' own ``matches()``. Critically, Workday URLs
resolve cleanly, so this is the path that lands the Fortune-500 crowd.

Output is a candidates.json for ``build_registry`` (which verifies live before merging).

Usage::

    .venv/bin/python scripts/resolve_careers.py names.txt [--limit N] [--out PATH]
    # names.txt: one company per line, optional ",domain" (domain skips the guess step)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_registry import ATS_PRIORITY  # noqa: E402
from company_resolve import core_tokens  # noqa: E402
from harvest_aggregator_apply_urls import resolve_ats_url  # noqa: E402
from harvest_tokens import company_key  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.providers.base import load_builtins  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_careers.json"
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.I)
# Tokens that are shared CDN/asset infrastructure, not a company's board — a careers page embeds
# the vendor's CDN (e.g. cdn.phenompeople.com) which matches() greedily claims. Reject them.
_JUNK_TOKEN_MARKERS = ("cdn.", "static.", "assets.", "media.", "-cdn.", "scripts.")


def _is_junk(token: str) -> bool:
    t = token.lower()
    return any(m in t for m in _JUNK_TOKEN_MARKERS)


def extract_ats_links(text: str, final_url: str | None = None) -> list[tuple[str, str]]:
    """Recover every ``(ats, token)`` an ATS provider claims from a page's final URL + the URLs in
    its HTML, best-ATS first, deduped. ``final_url`` (after redirects) catches careers pages that
    302 straight to the ATS; the HTML scan catches embedded board links/iframes. Shared CDN/asset
    hosts a provider greedily claims (e.g. cdn.phenompeople.com) are filtered out."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    candidates = ([final_url] if final_url else []) + _URL_RE.findall(text or "")
    for url in candidates:
        res = resolve_ats_url(url.rstrip("\"'<>),."))
        if res and res not in seen and not _is_junk(res[1]):
            seen.add(res)
            out.append(res)
    out.sort(key=lambda r: ATS_PRIORITY.get(r[0], 99))  # prefer a real ATS over a fallback
    return out


def guess_domains(name: str) -> list[str]:
    """Candidate domains for a company name (brand tokens joined + first token), .com first.

    Imperfect by design — works for the many companies whose domain is their name
    ("nvidia"->nvidia.com); acronym-brand cases (Advanced Micro Devices->amd.com) need a real
    name->domain source (Wikidata/search), a documented follow-up.
    """
    core = core_tokens(name)
    if not core:
        return []
    joined = "".join(core)
    first = core[0]
    stems = [joined] + ([first] if first != joined else [])
    out: list[str] = []
    for stem in stems:
        for tld in (".com", ".io", ".co"):
            d = stem + tld
            if d not in out:
                out.append(d)
    return out


def careers_urls(domain: str) -> list[str]:
    """Common careers entry points for a domain (where an ATS link/redirect tends to live)."""
    return [
        f"https://{domain}/careers",
        f"https://careers.{domain}",
        f"https://{domain}/jobs",
        f"https://jobs.{domain}",
        f"https://{domain}/careers/jobs",
        f"https://{domain}",
    ]


async def resolve_careers(
    name: str, fetcher: AsyncFetcher, domains: list[str] | None = None
) -> dict[str, object] | None:
    """Resolve a company to a candidate ``(ats, token)`` by reading its careers page. Returns a
    build_registry candidate dict, or None if no ATS board could be found."""
    for domain in domains or guess_domains(name):
        for url in careers_urls(domain):
            try:
                resp = await fetcher.request("GET", url)
            except Exception:  # noqa: BLE001 - dead host / blocked / timeout: try the next URL
                continue
            if resp.status_code >= 400:
                continue
            links = extract_ats_links(resp.text, final_url=str(resp.url))
            if links:
                ats, token = links[0]
                return {
                    "company": company_key(name),
                    "ats": ats,
                    "token": token,
                    "domain": domain,
                }
    return None


def parse_names(text: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, dom = line.partition(",")
        if name.strip():
            out.append((name.strip(), dom.strip() or None))
    return out


async def main() -> None:
    args = sys.argv[1:]
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print("usage: resolve_careers.py names.txt [--limit N] [--out PATH]")
        return
    names = parse_names(Path(paths[0]).read_text())
    out_path = DEFAULT_OUT
    if "--out" in args:
        out_path = Path(args[args.index("--out") + 1])
    if "--limit" in args:
        names = names[: int(args[args.index("--limit") + 1])]

    load_builtins()
    results: dict[int, dict | None] = {}
    print(f"resolving careers pages for {len(names)} companies ...", flush=True)
    async with (
        AsyncFetcher(concurrency=10, per_host_rate=4, timeout=15.0, retries=1) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for i, (name, dom) in enumerate(names):

            async def run(i: int = i, name: str = name, dom: str | None = dom) -> None:
                results[i] = await resolve_careers(name, fetcher, [dom] if dom else None)

            tg.start_soon(run)

    cands = [r for r in (results[i] for i in sorted(results)) if r]
    by_ats: dict[str, int] = {}
    for c in cands:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"\nresolved {len(cands)}/{len(names)} to an ATS  by_ats={by_ats}")
    out_path.write_text(json.dumps(cands, indent=2, ensure_ascii=False) + "\n")
    rel = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
    print(f"wrote {rel}")
    print(f"next: .venv/bin/python scripts/build_registry.py {rel} --gentle --onboard-empty")


if __name__ == "__main__":
    anyio.run(main)
