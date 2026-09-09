"""Career-page ATS resolver: start from the COMPANY and find its real ATS — company-first
discovery (vs the old indirect slug-guessing / inherited lists / web mining).

Pipeline per company:
1. name -> domain(s): Clearbit autocomplete (keyless), with a name-guess fallback. A
   name-plausibility guard rejects wrong-company domains (Clearbit returns mdimembrane.com for
   "Advanced Micro Devices", so we only accept a resolution whose domain/token shares a token
   with the company name).
2. domain -> careers page -> ATS board link, resolved to (ats, token) via providers' matches().
   Workday URLs resolve, so this lands the Fortune-500 crowd.

SPA handling (thorough, tiered — careers pages increasingly load the ATS link client-side):
- Tier 1: scan the fetched HTML (inline scripts/JSON included) + the final URL after redirects.
- Tier 2: if empty, fetch the page's same-origin JS bundles and scan those (tokens are often
  hardcoded in the bundle/config) — no browser.
- Tier 3: if still empty and Playwright is installed, render the page and scan the live DOM
  (catches links injected purely at runtime). Optional + lazily imported — never a hard dep.

Output is a candidates.json for ``build_registry`` (verifies live before merging).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_registry import ATS_PRIORITY  # noqa: E402
from company_resolve import core_tokens  # noqa: E402
from harvest_aggregator_apply_urls import resolve_ats_url  # noqa: E402
from harvest_tokens import company_key  # noqa: E402

from ergon.http import AsyncFetcher  # noqa: E402
from ergon.providers.base import load_builtins  # noqa: E402

DEFAULT_OUT = ROOT / "scripts" / "candidates_careers.json"
CLEARBIT = "https://autocomplete.clearbit.com/v1/companies/suggest"
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.I)
_SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)
# Shared CDN/asset hosts a provider greedily claims (e.g. cdn.phenompeople.com,
# content-us.phenompeople.com) — vendor infra, not a company's board.
_JUNK_TOKEN_MARKERS = (
    "cdn.",
    "static.",
    "assets.",
    "media.",
    "-cdn.",
    "scripts.",
    "content-us.",
    "content.",
    "img.",
)


def _is_junk(token: str) -> bool:
    t = token.lower()
    return any(m in t for m in _JUNK_TOKEN_MARKERS)


def extract_ats_links(text: str, final_url: str | None = None) -> list[tuple[str, str]]:
    """Recover every ``(ats, token)`` a provider claims from a page's final URL + the URLs in its
    content, best-ATS first, deduped, shared-CDN tokens filtered."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for url in ([final_url] if final_url else []) + _URL_RE.findall(text or ""):
        res = resolve_ats_url(url.rstrip("\"'<>),."))
        if res and res not in seen and not _is_junk(res[1]):
            seen.add(res)
            out.append(res)
    out.sort(key=lambda r: ATS_PRIORITY.get(r[0], 99))
    return out


def guess_domains(name: str) -> list[str]:
    """Name-based candidate domains (brand tokens joined + first token), .com first. Imperfect;
    the Clearbit lookup is primary, this is the offline fallback."""
    core = core_tokens(name)
    if not core:
        return []
    stems = ["".join(core)] + ([core[0]] if core[0] != "".join(core) else [])
    out: list[str] = []
    for stem in stems:
        for tld in (".com", ".io", ".co"):
            if stem + tld not in out:
                out.append(stem + tld)
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


def _plausible(name: str, domain: str, token: str = "") -> bool:
    """True if ``domain`` is plausibly the company's own — guards against attributing some other
    company's board to ``name``. Match is token-EXACT (the domain label must equal a name-derived
    stem), NOT substring: "Stripe" must reject ``stripersonline.com`` even though "stripe" is a
    substring of it, while "Exxon Mobil" still accepts ``exxonmobil.com``. (``token`` is accepted
    for signature stability but attribution hinges on the domain, not the board token.)"""
    cores = core_tokens(name)
    if not cores:
        return False
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    stems = {cores[0], "".join(cores), "".join(cores[:2])}
    return label in stems


async def company_domains(
    name: str, fetcher: AsyncFetcher, override: str | None = None
) -> list[str]:
    """Resolve a company name to candidate domains: explicit override, else Clearbit autocomplete
    (keyless, top results) followed by the offline name-guess as fallback."""
    if override:
        return [override]
    domains: list[str] = []
    try:
        data = await fetcher.get_json(CLEARBIT, params={"query": name})
        for row in (data or [])[:3]:
            d = str(row.get("domain") or "").lower()
            if d and d not in domains:
                domains.append(d)
    except Exception:  # noqa: BLE001 - autocomplete down/blocked: fall back to guessing
        pass
    for g in guess_domains(name):
        if g not in domains:
            domains.append(g)
    return domains


def _same_origin_js(html: str, base_url: str) -> list[str]:
    """Absolute URLs of same-origin <script src> bundles on the page (Tier-2 SPA scan targets)."""
    split = urlsplit(base_url)
    origin = f"{split.scheme}://{split.netloc}"
    out: list[str] = []
    for src in _SCRIPT_SRC_RE.findall(html or ""):
        if src.startswith("//"):
            url = f"{split.scheme}:{src}"
        elif src.startswith("http"):
            url = src
        elif src.startswith("/"):
            url = origin + src
        else:
            url = f"{origin}/{src}"
        if urlsplit(url).netloc == split.netloc and url not in out:
            out.append(url)
    return out


async def _render(url: str) -> str | None:
    """Tier 3: render ``url`` with Playwright and return the live DOM HTML, or None if Playwright
    isn't installed / rendering fails. Lazily imported so it's never a hard dependency."""
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001 - optional extra; absent in the default install
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=20000)
                return await page.content()
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001 - render failure never sinks the resolver
        return None


async def resolve_careers(
    name: str, fetcher: AsyncFetcher, domains: list[str] | None = None, *, render: bool = False
) -> dict[str, object] | None:
    """Resolve a company to a candidate ``(ats, token)`` by reading its careers page (tiered:
    HTML -> same-origin JS -> optional Playwright render). Returns a build_registry candidate or
    None. ``render`` enables Tier 3 (requires Playwright)."""
    # Cap fan-out: the top 2 candidate domains (Clearbit's first is usually right) x the 3
    # highest-yield careers entry points. Keeps per-company fetches ~6 (not ~18) for throughput;
    # Workday redirects live at /careers + careers.{domain}, so capture is preserved.
    domains = domains or await company_domains(name, fetcher)
    for domain in domains[:2]:
        for url in careers_urls(domain)[:3]:
            try:
                resp = await fetcher.request("GET", url)
            except Exception:  # noqa: BLE001 - dead host/blocked/timeout: next URL
                continue
            if resp.status_code >= 400:
                continue
            links = extract_ats_links(resp.text, final_url=str(resp.url))
            if not links:  # Tier 2: scan same-origin JS bundles
                for js in _same_origin_js(resp.text, str(resp.url))[:6]:
                    try:
                        jr = await fetcher.request("GET", js)
                    except Exception:  # noqa: BLE001
                        continue
                    links = extract_ats_links(jr.text)
                    if links:
                        break
            if not links and render:  # Tier 3: render the live DOM
                rendered = await _render(url)
                if rendered:
                    links = extract_ats_links(rendered)
            for ats, token in links:
                if _plausible(name, domain, token):
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
        nm, _, dom = line.partition(",")
        if nm.strip():
            out.append((nm.strip(), dom.strip() or None))
    return out


async def main() -> None:
    args = sys.argv[1:]
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print("usage: resolve_careers.py names.txt [--limit N] [--out PATH] [--render]")
        return
    names = parse_names(Path(paths[0]).read_text())
    out_path = Path(args[args.index("--out") + 1]) if "--out" in args else DEFAULT_OUT
    if "--limit" in args:
        names = names[: int(args[args.index("--limit") + 1])]
    render = "--render" in args

    load_builtins()
    results: dict[int, dict | None] = {}
    total = len(names)
    prog = {"done": 0, "hit": 0}
    print(f"resolving careers for {total} companies (render={render}) ...", flush=True)

    def tick(found: bool) -> None:
        prog["done"] += 1
        prog["hit"] += int(found)
        d = prog["done"]
        step = max(100, total // 40)  # stream ~every 2.5% so progress is visible mid-run
        if d % step == 0 or d == total:
            pct = 100 * d // total if total else 100
            print(f"  progress {d}/{total} ({pct}%)  resolved={prog['hit']}", flush=True)

    # High global concurrency is safe here: each company is a DISTINCT host, so per-host limiting
    # still protects every site; the only shared host (Clearbit) stays capped by per_host_rate.
    # Short timeout fast-fails the many guessed/dead careers URLs that would otherwise dominate.
    async with (
        AsyncFetcher(concurrency=40, per_host_rate=4, timeout=8.0, retries=1) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for i, (nm, dom) in enumerate(names):

            async def run(i: int = i, nm: str = nm, dom: str | None = dom) -> None:
                r = await resolve_careers(nm, fetcher, [dom] if dom else None, render=render)
                results[i] = r
                tick(r is not None)

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
