"""Harvest ATS tenants from crt.sh certificate-transparency logs -> candidates.json.

crt.sh indexes (almost) every TLS certificate ever issued. For ATS platforms that put the
company tenant in a **subdomain**, querying ``crt.sh?q=%.<ats-domain>&output=json`` enumerates
real, live tenants for free — no API key, no scraping, no paid service. This is the
highest-leverage way to grow the seed registry beyond hand-curated entries.

Which ATSes this works for
--------------------------
Only **subdomain-tenant** ATSes are enumerable this way, because the tenant appears in the
certificate's host name:

* ``recruitee``  -> ``{token}.recruitee.com``            (token = full subdomain label)
* ``personio``   -> ``{token}.jobs.personio.de``         (token = full subdomain label)
* ``workday``    -> ``{tenant}.wd{N}.myworkdayjobs.com``  (tenant+datacenter from host; the
  ``site`` path segment is *not* in the host, so we discover it from the careers root page)

**Path-based** ATSes (greenhouse ``boards.greenhouse.io/{token}``, lever, ashby,
smartrecruiters, workable) put the token in a URL *path*, so crt.sh cannot enumerate them —
they are intentionally excluded. Yield also depends on the provider issuing *per-tenant*
certs: a single wildcard cert (``*.recruitee.com``) hides individual tenants from crt.sh.
Workday historically issues per-tenant certs and so enumerates best.

Propose, don't dispose
----------------------
Output is a ``candidates.json`` compatible with :mod:`build_registry`, which then **verifies
every candidate live** through jobspine's own providers before merging into ``seed.json``.
This script only *proposes*; ``build_registry.py`` *disposes*. We never write ``seed.json``.

Usage::

    # harvest (defaults to recruitee + personio; add workday explicitly)
    .venv/bin/python scripts/harvest_crtsh.py recruitee personio workday --limit 200

    # then verify + merge through the real provider stack
    .venv/bin/python scripts/build_registry.py scripts/candidates_crtsh.json --dry-run
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jobspine.http import AsyncFetcher  # noqa: E402

SEED = ROOT / "src" / "jobspine" / "registry" / "data" / "seed.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_crtsh.json"

# crt.sh certificate-transparency search, JSON output. The ``q`` wildcard (``%``) is passed via
# httpx ``params`` so it is percent-encoded exactly once (no manual %25 double-encoding).
_CRTSH = "https://crt.sh/"

# Infra / framing subdomains that are never a company tenant. crt.sh returns these for the
# ATS vendor's own services; drop them so they don't become bogus candidates.
_RESERVED_LABELS = frozenset(
    {
        "www", "api", "app", "apps", "auth", "admin", "portal", "secure", "login", "sso",
        "status", "support", "help", "docs", "doc", "blog", "news", "mail", "email", "mailer",
        "smtp", "mx", "ns", "ns1", "ns2", "cdn", "assets", "static", "media", "img", "images",
        "dev", "staging", "stage", "test", "qa", "demo", "sandbox", "preview", "beta",
        "go", "info", "about", "widget", "widgets", "embed", "track", "tracking", "click",
        "events", "event", "webhook", "webhooks", "internal", "vpn", "git", "ci",
        "grafana", "metrics", "monitor", "monitoring", "data", "db", "cache",
    }
)

# A plausible tenant label: lowercase alphanumerics + hyphens, not all-numeric.
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class AtsConfig:
    """How to enumerate one subdomain-tenant ATS from crt.sh."""

    ats: str
    query: str  # crt.sh ``q`` value, e.g. "%.recruitee.com"
    host_re: re.Pattern[str]  # anchored host regex; group "tenant" (+ "wd" for workday) required


# Workday careers root embeds its real career-site segment in a cxs reference like
# ``/wday/cxs/{tenant}/{site}/jobs`` (or a redirect to ``/{locale}/{site}``). We read it from
# the page rather than brute-forcing site names.
def _workday_site_re(tenant: str) -> re.Pattern[str]:
    t = re.escape(tenant)
    return re.compile(rf"/wday/cxs/{t}/([A-Za-z0-9_]+)/", re.IGNORECASE)


CONFIGS: dict[str, AtsConfig] = {
    "recruitee": AtsConfig(
        ats="recruitee",
        query="%.recruitee.com",
        host_re=re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.recruitee\.com$", re.IGNORECASE),
    ),
    "personio": AtsConfig(
        ats="personio",
        query="%.jobs.personio.de",
        host_re=re.compile(
            r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)$", re.IGNORECASE
        ),
    ),
    "workday": AtsConfig(
        ats="workday",
        query="%.myworkdayjobs.com",
        host_re=re.compile(
            r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE
        ),
    ),
}

# Harvested by default. Workday needs an extra network step (site discovery) and so is opt-in.
DEFAULT_ATSES = ("recruitee", "personio")


# --- pure parsing (no network; unit-tested) ---------------------------------------------------


def parse_crtsh_hosts(payload: str) -> list[str]:
    """Flatten a crt.sh JSON response into a sorted list of unique lowercase host names.

    Each record's ``name_value`` may hold several newline-separated names, and entries may be
    wildcards (``*.foo.com``). We split, lowercase, strip a leading ``*.``, and dedupe. Never
    raises — a malformed payload yields ``[]``.
    """
    try:
        records = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if not isinstance(records, list):
        return []

    hosts: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name_value = rec.get("name_value") or rec.get("common_name") or ""
        for raw in str(name_value).splitlines():
            host = raw.strip().lower().lstrip("*.").strip(".")
            if host:
                hosts.add(host)
    return sorted(hosts)


def _valid_tenant(label: str) -> bool:
    """A tenant label must look like a slug, not be reserved infra, and not be all digits."""
    label = label.lower()
    return bool(_LABEL_RE.match(label)) and label not in _RESERVED_LABELS and not label.isdigit()


def extract_tenants(config: AtsConfig, hosts: list[str]) -> list[dict[str, str]]:
    """Map crt.sh hosts to ``{tenant, wd?}`` dicts for one ATS, deduped and filtered.

    For recruitee/personio each result is ``{"tenant": label}``. For workday it is
    ``{"tenant": label, "wd": "wdN"}`` (the ``site`` is discovered separately, online).
    """
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, str]] = []
    for host in hosts:
        m = config.host_re.match(host)
        if not m:
            continue
        tenant = m.group("tenant").lower()
        if not _valid_tenant(tenant):
            continue
        wd = (m.groupdict().get("wd") or "").lower()
        dedup_key = (tenant, wd)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        entry = {"tenant": tenant}
        if wd:
            entry["wd"] = wd
        out.append(entry)
    return out


def extract_workday_site(page_html: str, tenant: str) -> str | None:
    """Pull the real Workday career-site segment from a careers-root page, else ``None``."""
    m = _workday_site_re(tenant).search(page_html)
    return m.group(1) if m else None


# --- existing-registry awareness --------------------------------------------------------------


def load_existing(seed_path: Path = SEED) -> tuple[set[str], dict[str, set[str]]]:
    """Return ``(company_keys, {ats: {tokens}})`` already present in the seed registry."""
    if not seed_path.exists():
        return set(), {}
    seed = json.loads(seed_path.read_text())
    companies: dict[str, dict] = seed.get("companies", {})
    keys = set(companies)
    tokens_by_ats: dict[str, set[str]] = {}
    for entry in companies.values():
        ats = entry.get("ats")
        token = entry.get("token")
        if isinstance(ats, str) and isinstance(token, str):
            tokens_by_ats.setdefault(ats, set()).add(token)
    return keys, tokens_by_ats


# --- network harvest --------------------------------------------------------------------------


async def _fetch_crtsh(config: AtsConfig, fetcher: AsyncFetcher) -> list[str]:
    """Query crt.sh for one ATS and return parsed host names (``[]`` on any failure)."""
    try:
        payload = await fetcher.get_text(_CRTSH, params={"q": config.query, "output": "json"})
    except Exception as exc:  # noqa: BLE001 - crt.sh is flaky; report and continue
        print(f"  [{config.ats}] crt.sh fetch failed: {type(exc).__name__}: {exc}")
        return []
    return parse_crtsh_hosts(payload)


async def _discover_workday_site(tenant: str, wd: str, fetcher: AsyncFetcher) -> str | None:
    """Fetch a Workday careers root and read its career-site segment from the page."""
    root = f"https://{tenant}.{wd}.myworkdayjobs.com/"
    try:
        html = await fetcher.get_text(root)
    except Exception:
        return None
    return extract_workday_site(html, tenant)


async def harvest(
    atses: list[str], fetcher: AsyncFetcher, limit: int | None = None
) -> list[dict[str, object]]:
    """Harvest candidate ATS boards for the given ATSes, skipping ones already in the seed."""
    existing_keys, existing_tokens = load_existing()
    candidates: list[dict[str, object]] = []

    for name in atses:
        config = CONFIGS[name]
        hosts = await _fetch_crtsh(config, fetcher)
        tenants = extract_tenants(config, hosts)
        if limit is not None:
            tenants = tenants[:limit]
        seen_tokens = existing_tokens.get(name, set())
        print(f"  [{name}] crt.sh hosts={len(hosts)} tenants={len(tenants)}")

        if name == "workday":
            # Discover each tenant's real career-site segment concurrently.
            resolved: dict[int, tuple[dict[str, str], str | None]] = {}

            async def _resolve(i: int, t: dict[str, str]) -> None:
                site = await _discover_workday_site(t["tenant"], t["wd"], fetcher)
                resolved[i] = (t, site)

            async with anyio.create_task_group() as tg:
                for i, t in enumerate(tenants):
                    tg.start_soon(_resolve, i, t)

            for i in sorted(resolved):
                t, site = resolved[i]
                if not site:
                    continue
                token = f"{t['tenant']}|{t['wd']}|{site}"
                if t["tenant"] in existing_keys or token in seen_tokens:
                    continue
                candidates.append(
                    {
                        "company": t["tenant"],
                        "ats": "workday",
                        "tenant": t["tenant"],
                        "wd": t["wd"],
                        "site": site,
                        "domain": None,
                    }
                )
        else:
            for t in tenants:
                token = t["tenant"]
                if token in existing_keys or token in seen_tokens:
                    continue
                candidates.append(
                    {"company": token, "ats": name, "token": token, "domain": None}
                )

    return candidates


async def main() -> None:
    args = sys.argv[1:]
    out_path = DEFAULT_OUT
    limit: int | None = None
    atses: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--out":
            out_path = Path(args[i + 1])
            i += 2
        elif arg == "--limit":
            limit = int(args[i + 1])
            i += 2
        elif arg.startswith("--"):
            print(f"unknown flag: {arg}")
            return
        else:
            atses.append(arg)
            i += 1

    if not atses:
        atses = list(DEFAULT_ATSES)
    unknown = [a for a in atses if a not in CONFIGS]
    if unknown:
        print(f"unknown ATS(es): {unknown}; known: {sorted(CONFIGS)}")
        return

    print(f"harvesting crt.sh for: {atses}  (limit={limit})")
    async with AsyncFetcher(concurrency=12, per_host_rate=4, timeout=60.0) as fetcher:
        candidates = await harvest(atses, fetcher, limit=limit)

    by_ats: dict[str, int] = {}
    for c in candidates:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    print(f"new candidates: {len(candidates)}  by_ats={by_ats}")

    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"wrote {shown}")
    print(f"\nnext: .venv/bin/python scripts/build_registry.py {shown} --dry-run")


if __name__ == "__main__":
    anyio.run(main)
