"""Recover H-1B sponsor ATS boards via Tavily name-search + adjudication -> candidates.json.

Closes the H-1B coverage gap (Option 2): we have a ranked list of sponsor *names* we don't yet
track (runs/h1b_coverage_gap.json), and we want each one's board on a SUPPORTED ATS so the
sponsor's live jobs become fetchable. Tavily name-search finds candidate boards; the hard part
is precision — raw search returns *a* related board, not necessarily the sponsor's own
(microsoft->shi, apple->applebank, cognizant->collaborative). Those false positives would
pollute the registry (cf. the amazon->personio junk entry).

So every candidate is ADJUDICATED with the strongest signal available, then live-verified:

* non-Workday: fetch the board and match the sponsor against its *displayed company name*
  (fuzzy ratio) — applebank's board reads "Apple Bank", not "Apple", so it's rejected.
* Workday: no reliable display name, so require the tenant to exactly match or cleanly prefix
  the sponsor name (google|… ok; collaborative|… for "cognizant" rejected).

Only boards that (a) return live jobs and (b) pass adjudication are written as candidates;
every decision is logged to runs/sponsor_recovery_report.json for auditability.

Usage::

    TAVILY in .env. .venv/bin/python scripts/harvest_sponsors_tavily.py [--top N] [--out PATH]
    .venv/bin/python scripts/build_registry.py scripts/candidates_sponsors.json --dry-run
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from harvest_commoncrawl import CONFIGS, load_seed_keys  # noqa: E402
from harvest_tavily import load_key  # noqa: E402

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402
from ergon_tracker.providers.icims import ICIMSProvider  # noqa: E402
from ergon_tracker.providers.personio import PersonioProvider  # noqa: E402
from ergon_tracker.providers.workday import WorkdayProvider  # noqa: E402

GAP = ROOT / "runs" / "h1b_coverage_gap.json"
DEFAULT_OUT = ROOT / "scripts" / "candidates_sponsors.json"
REPORT = ROOT / "runs" / "sponsor_recovery_report.json"
_API = "https://api.tavily.com/search"

# Supported ATS hosts to restrict Tavily results to (incl. Workday's tenant host). The enterprise
# hosts (icims/bamboohr/recruitee/personio/teamtailor) were added to open the lane where many
# Fortune-500/large-caps actually live — the 8-host set missed them entirely.
HOSTS = [
    "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com",
    "careers.smartrecruiters.com", "ats.rippling.com", "myworkdayjobs.com", "eightfold.ai",
    "icims.com", "bamboohr.com", "recruitee.com", "teamtailor.com",
    "jobs.personio.com", "jobs.personio.de",
]
_EIGHTFOLD_RE = re.compile(r"([a-z0-9][a-z0-9-]*)\.eightfold\.ai", re.IGNORECASE)
# Extractors for the path/subdomain ATSes (reused from the CC harvester) whose token == a clean
# company slug — adjudication works on these even without a display name.
_EXTRACT_ATSES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "rippling",
                  "pinpoint", "bamboohr", "recruitee", "teamtailor")
# ATSes resolved via the provider's own matches() (no CC extractor): the token is a host (icims)
# or tenant (personio); these lean on the live display-name check in adjudicate().
_MATCH_PROVIDERS = (("icims", ICIMSProvider), ("personio", PersonioProvider))


def _collapse(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _first_word(s: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
    return words[0] if words else ""


def board_of(url: str) -> tuple[str, str] | None:
    """Map a URL to (ats, token) if it's a recognised supported-ATS board, else None."""
    for ats in _EXTRACT_ATSES:
        tok = CONFIGS[ats].extract(url)  # type: ignore[operator]
        if tok:
            return ats, tok
    wt = WorkdayProvider.matches(url)
    if wt:
        return "workday", wt
    m = _EIGHTFOLD_RE.search(url)
    if m and m.group(1).lower() not in ("www", "app"):
        return "eightfold", m.group(1).lower()
    for ats, provider_cls in _MATCH_PROVIDERS:
        tok = provider_cls.matches(url)
        if tok:
            return ats, tok
    return None


# --- adjudication (pure; unit-tested) ---------------------------------------------------------


def adjudicate(sponsor: str, ats: str, token: str, board_company: str, live: bool) -> tuple[bool, str]:
    """Decide whether ``(ats, token)`` truly belongs to ``sponsor``. Returns (accept, reason).

    ``board_company`` is the board's displayed company name (use "" for Workday / unknown).
    ``live`` is whether the board returned >=1 job. Conservative: prefer rejecting over a junk
    entry. The decisive signal is the displayed company name (non-Workday) or a strict
    tenant-name match (Workday).
    """
    if not live:
        return False, "dead (no jobs)"
    sn = _collapse(sponsor)
    snf = _collapse(_first_word(sponsor))
    tenant = token.split("|")[0] if ats == "workday" else token
    tok = _collapse(tenant)
    if len(tok) < 3:
        return False, f"token {tok!r} too short"

    # Workday and Eightfold expose no reliable display name (the board "company" is just the
    # tenant slug), so the tenant slug IS the identity signal: require an exact or clean-prefix
    # match against the sponsor name. (starbucks.eightfold.ai serving "starbucks coffee" jobs is
    # decisive; an unrelated slug returning jobs is rejected.)
    if ats in ("workday", "eightfold"):
        if tok in (snf, sn):
            return True, f"tenant {tok!r} exact-matches sponsor"
        if sn.startswith(tok) and len(tok) >= 4:
            return True, f"tenant {tok!r} cleanly prefixes sponsor {sn!r}"
        return False, f"tenant {tok!r} != sponsor {sn!r}"

    bc = _collapse(board_company)
    if not bc:  # no display name: fall back to a strict token check
        if tok == snf or sn.startswith(tok):
            return True, f"token {tok!r} matches sponsor (no display name)"
        return False, f"token {tok!r} != sponsor {sn!r} (no display name)"
    # The board's OWN displayed name must be (nearly) the sponsor's name and LEADING-ALIGNED:
    # one is a prefix of the other with only a small suffix (legal form / shard number). This
    # rejects "Mr Apple"/"Apple Bank for Savings" for sponsor "apple" while keeping "Infosys"
    # and "HCL America Inc".
    if (bc.startswith(sn) or sn.startswith(bc)) and abs(len(bc) - len(sn)) <= 3:
        return True, f"board company {board_company!r} matches sponsor"
    # Allow a tiny fuzzy gap (punctuation/typo) only when lengths are close.
    if abs(len(bc) - len(sn)) <= 4 and fuzz.ratio(bc, sn) >= 88:
        return True, f"board company {board_company!r} ~{fuzz.ratio(bc, sn)} sponsor"
    return False, f"board company {board_company!r} not sponsor {sn!r}"


def to_candidate(ats: str, token: str) -> dict[str, object]:
    """Build a build_registry candidate (workday split into tenant|wd|site)."""
    if ats == "workday":
        tenant, wd, site = token.split("|")
        return {"company": tenant, "ats": "workday", "tenant": tenant, "wd": wd, "site": site,
                "domain": None}
    return {"company": token, "ats": ats, "token": token, "domain": None}


# --- network: search (sync, paced) then fetch+judge (async) -----------------------------------


async def search_candidates(sponsors: list[dict], key: str) -> list[tuple[dict, str, str]]:
    """Tavily-search supported hosts for each sponsor CONCURRENTLY; return (sponsor, ats, token).

    Concurrent (bounded by AsyncFetcher's per-host rate + retries) so a few-thousand-sponsor
    sweep finishes in minutes instead of the old sequential ~20.
    """
    out: list[tuple[dict, str, str]] = []
    headers = {"Authorization": f"Bearer {key}"}

    async def one(s: dict, fetcher: AsyncFetcher) -> None:
        body = {"query": f"{s['name']} careers jobs", "include_domains": HOSTS, "max_results": 5}
        try:
            data = await fetcher.post_json(_API, json=body, headers=headers)
        except Exception:  # noqa: BLE001
            return
        seen: set[tuple[str, str]] = set()
        for r in (data.get("results", []) if isinstance(data, dict) else []):
            b = board_of(r.get("url", ""))
            if b and b not in seen:
                seen.add(b)
                out.append((s, b[0], b[1]))

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=6, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for s in sponsors:
            tg.start_soon(one, s, fetcher)
    return out


async def fetch_and_judge(triples: list[tuple[dict, str, str]], seed_keys: set[str]) -> tuple[
    list[dict], list[dict]
]:
    """Fetch each candidate board and adjudicate; return (accepted_candidates, decision_log)."""
    load_builtins()
    decisions: dict[int, dict] = {}
    query = SearchQuery(limit=1)

    async def judge_one(i: int, sponsor: dict, ats: str, token: str, fetcher: AsyncFetcher) -> None:
        provider = get_provider(ats)
        live, board_co = False, ""
        if provider is not None:
            try:
                raws = await provider.fetch(token, query, fetcher)
                live = len(raws) > 0
                if live and ats != "workday":
                    board_co = raws[0].company or ""
            except Exception as exc:  # noqa: BLE001
                decisions[i] = {"sponsor": sponsor["name"], "ats": ats, "token": token,
                                "accept": False, "reason": f"fetch error {type(exc).__name__}"}
                return
        accept, reason = adjudicate(sponsor["name"], ats, token, board_co, live)
        decisions[i] = {"sponsor": sponsor["name"], "filings": sponsor.get("filings"),
                        "ats": ats, "token": token, "board_company": board_co,
                        "accept": accept, "reason": reason}

    async with (
        AsyncFetcher(concurrency=12, per_host_rate=8, timeout=30.0) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for i, (sponsor, ats, token) in enumerate(triples):
            tg.start_soon(judge_one, i, sponsor, ats, token, fetcher)

    log = [decisions[i] for i in sorted(decisions)]
    # One accepted candidate per sponsor (highest-filing sponsors processed first already).
    accepted: list[dict] = []
    taken: set[str] = set()
    for d in log:
        if not d["accept"]:
            continue
        cand = to_candidate(d["ats"], d["token"])
        key = str(cand["company"]).lower()
        if key in seed_keys or key in taken:
            continue
        taken.add(key)
        accepted.append(cand)
    return accepted, log


def _names_to_sponsors(text: str) -> list[dict]:
    """A plain names file (one company per line, '#' comments, optional ',domain' ignored) ->
    sponsor dicts. Lets this recover boards for ANY company list (e.g. SEC public companies),
    not just the H-1B coverage gap."""
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(",")[0].strip()
        if name:
            out.append({"name": name})
    return out


async def main() -> None:
    args = sys.argv[1:]
    top = 150
    out_path = DEFAULT_OUT
    names_file: Path | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--top":
            top = int(args[i + 1]); i += 2
        elif a == "--out":
            out_path = Path(args[i + 1]); i += 2
        elif a == "--names":
            names_file = Path(args[i + 1]); i += 2
        else:
            print(f"unknown flag: {a}"); return

    key = load_key()
    if not key:
        print("TAVILY_API_KEY not set (env or .env)."); return
    if names_file is not None:
        sponsors = _names_to_sponsors(names_file.read_text())[:top]
        print(f"recovering boards for {len(sponsors)} companies from {names_file.name} ...")
    else:
        sponsors = json.loads(GAP.read_text())["uncovered_top"][:top]
        print(f"recovering boards for top {len(sponsors)} gap sponsors ...")

    triples = await search_candidates(sponsors, key)
    print(f"  {len(triples)} candidate boards found across supported ATSes; adjudicating ...")
    accepted, log = await fetch_and_judge(triples, load_seed_keys())

    by_ats: dict[str, int] = {}
    for c in accepted:
        by_ats[str(c["ats"])] = by_ats.get(str(c["ats"]), 0) + 1
    n_acc = sum(1 for d in log if d["accept"])
    print(f"\nadjudicated {len(log)} candidates: {n_acc} accepted, {len(log) - n_acc} rejected")
    print(f"unique new sponsor boards: {len(accepted)}  by_ats={by_ats}")
    out_path.write_text(json.dumps(accepted, indent=2, ensure_ascii=False) + "\n")
    REPORT.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"wrote {shown}  (+ decision log {REPORT.relative_to(ROOT)})")
    print(f"\nnext: .venv/bin/python scripts/build_registry.py {shown} --dry-run")


if __name__ == "__main__":
    anyio.run(main)
