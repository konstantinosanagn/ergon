"""Re-verify candidate ATS boards by dogfooding ergon_tracker's own providers, then merge the
live ones into the seed registry.

This is both a verification gate and a concurrency stress test: every candidate is fetched
through the real provider stack, concurrently, bounded by the shared AsyncFetcher.

A board returns 0 jobs for very different reasons. A clean 200-with-no-postings or a 404 is a
*final* dead — the board really has nothing for us. But a 429 / timeout / open-circuit /
exhausted-retries is *transient*: the board may well be live, it just got throttled while a big
sweep hammered its ATS backend. Counting those as dead is how a throttle storm silently drops
real boards. So verification runs in two phases: a main pass, then a deliberately gentle
re-verify of only the transient failures, promoting any that come alive. Network knobs are
tunable so a recovery run can pace itself well under the ATS limits.

Usage:
    .venv/bin/python scripts/build_registry.py [candidates.json] [--dry-run]
        [--concurrency N] [--per-host-rate N] [--timeout SECS] [--retries N]
        [--gentle] [--no-retry-transient] [--onboard-empty]

``--onboard-empty`` also registers confirmed-empty boards (HTTP 200, valid, 0 jobs) on trusted
JSON ATSes, so the daily build (which only crawls boards already in the registry) will pick up
their future postings. Off by default (strict >=1-job gate).
"""

from __future__ import annotations

import fcntl
import json
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.http import AsyncFetcher  # noqa: E402
from ergon_tracker.models import SearchQuery  # noqa: E402
from ergon_tracker.providers.base import get_provider, load_builtins  # noqa: E402

SEED = ROOT / "src" / "ergon_tracker" / "registry" / "data" / "seed.json"
CANDIDATES = ROOT / "scripts" / "candidates.json"
_SEED_LOCK = SEED.with_name(SEED.name + ".lock")


@contextmanager
def seed_lock():
    """Serialize the seed read-modify-write across concurrent build_registry runs.

    Verification (the network-heavy part) runs *before* this and stays fully concurrent; only
    the short read-merge-write critical section is mutually exclusive, via an advisory flock on
    a sidecar lockfile. A second run blocks here, then reads the seed *after* the first run's
    write, so additions compose instead of clobbering.
    """
    with open(_SEED_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# Lower = preferred when the same company verifies on multiple ATSes. Unknown ATSes sort last
# (via .get(..., 99)) so a new provider/candidate type can never KeyError this sweep.
ATS_PRIORITY = {
    "greenhouse": 0,
    "lever": 1,
    "ashby": 2,
    "workday": 3,
    "smartrecruiters": 4,
    "workable": 5,
    "recruitee": 6,
    "personio": 7,
    "bamboohr": 8,
    "breezy": 9,
    "teamtailor": 10,
    "join": 11,
    "rippling": 12,
    "pinpoint": 13,
    "eightfold": 14,
    "successfactors": 15,
    "oracle": 16,
    "jobdiva": 16.5,  # authoritative staffing/IT-firm candidate portal (own JSON API)
    "ripplehire": 16.6,  # IT-services career-site ATS (own XML API): Mphasis, CitiusTech
    "zwayam": 16.7,  # Indian IT-services ATS (public.zwayam.com 2-step ES API): Tavant
    "ceipal": 16.8,  # dominant US/Indian IT-staffing ATS (careerapi.ceipal.com referer-gated API)
    "radancy": 16.9,  # Radancy/TalentBrew /search-jobs server-rendered board: PwC, Carnival
    "pageup": 16.95,  # PageUp People canonical-host RSS (careers.pageuppeople.com/{id}/cw/{loc}/rss)
    "peoplesoft": 16.97,  # PeopleSoft Candidate Gateway (ICAction postback grid): universities
    "ukg": 16.98,  # UKG Pro / UltiPro Recruiting (LoadSearchResults JSON): UDR, Welltower, …
    "taleo": 17,
    "taleobe": 17.5,  # Taleo Business Edition (CwsV2 HTML)
    "icims": 18,
    "avature": 19,
    "jazzhr": 20,
    "jobvite": 21,
    "phenom": 22,
    "brassring": 23,
    "schemaorg": 24,  # generic fallback (sitemap/JSON-LD) — lowest priority vs a real ATS
    "apicapture": 25,  # captured own-domain JSON/GraphQL API (proxied giants)
    "coveo": 26,
    "peopleadmin": 27,
    "peopleclick": 27.5,  # PeopleFluent candidate portal (cookie-primed JSON; partial)  # higher-ed/public-sector Atom feed (complete)  # Coveo-for-Sitecore same-origin job proxy (SLB etc.)
    "usajobs": 28,
    "dejobs": 28.5,  # DirectEmployers federation (recruiter-direct, company-filtered)  # authoritative federal-agency board (Organization code) — gov giants only
    "themuse": 29,  # curated aggregator company board (employer-matched) — for unreachable giants
    "adzuna": 30,  # aggregator fallback for truly-proxied giants (lowest priority)
}


def token_for(entry: dict) -> str:
    if entry["ats"] == "workday":
        return f"{entry['tenant']}|{entry['wd']}|{entry['site']}"
    return entry["token"]


def is_demo_board(ats: str | None, token: str | None) -> bool:
    """True for vendor demo/training boards that carry FAKE postings (e.g. Lever's 'leverdemo*'
    accounts list 'Truck Driver'/'Computer Builder' with nonsense salaries). These pollute the
    index, so they're denied at registry-build time and purged on every run."""
    t = str(token or "").lower()
    return ats == "lever" and t.startswith("leverdemo")


def purge_demo_boards(companies: dict) -> int:
    """Drop any demo/test boards already in the registry (self-healing if one slips back in)."""
    dead = [k for k, e in companies.items() if is_demo_board(e.get("ats"), e.get("token"))]
    for k in dead:
        del companies[k]
    return len(dead)


async def verify_one(
    entry: dict, fetcher: AsyncFetcher, query: SearchQuery
) -> tuple[dict, int, str, str | None]:
    provider = get_provider(entry["ats"])
    token = token_for(entry)
    if provider is None:
        return entry, 0, token, f"no provider for {entry['ats']}"
    try:
        raws = await provider.fetch(token, query, fetcher)
        return entry, len(raws), token, None
    except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
        return entry, 0, token, f"{type(exc).__name__}: {exc}"[:120]


# --- dead-reason classification ----------------------------------------------------------------
# A board returning 0 jobs is only *finally* dead for permanent reasons; throttle/network errors
# are transient and earn a gentle re-verify. ``err`` is the ``"{ExcType}: {msg}"`` from
# verify_one (or None for a clean empty board).
_TRANSIENT_CATEGORIES = frozenset(
    {"rate_limited", "circuit_open", "exhausted", "timeout", "transport"}
)


def classify_dead(err: str | None) -> str:
    """Bucket a dead candidate's error. ``None`` == clean 200 with no postings ('empty')."""
    if err is None:
        return "empty"
    e = err.lower()
    if "404" in e or "410" in e:
        return "gone"
    if "403" in e or "401" in e:
        return "walled"
    if "429" in e or "ratelimit" in e or "too many" in e:
        return "rate_limited"
    if "circuit" in e:
        return "circuit_open"
    if "exhausted" in e:
        return "exhausted"
    if "timeout" in e or "timedout" in e:
        return "timeout"
    # TransientHTTPError (5xx) and connection/transport faults are retryable network failures.
    if "transient" in e or "transport" in e or "connect" in e or "connection" in e:
        return "transport"
    if "json" in e or "decode" in e or "expecting value" in e:
        return "parse_error"
    return "other"


def is_transient(err: str | None) -> bool:
    """True when a 0-result is a throttle/network failure (re-verify), not a final dead."""
    return classify_dead(err) in _TRANSIENT_CATEGORIES


# Providers that fetch a pure JSON API: a 200 with an empty job list unambiguously means "this
# board exists and simply has no openings right now" (a non-existent token 404s instead). For
# these we can safely ONBOARD an empty board so the daily build picks up its FUTURE postings
# (the daily crawl only visits boards already in the registry — it never discovers new ones).
# HTML/feed scrapers (personio, join, jazzhr — all get_text) are excluded: an "empty" parse
# there can mean a changed page, not a confirmed-empty board.
TRUSTED_EMPTY_PROVIDERS = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "workable",
        "recruitee",
        "bamboohr",
        "breezy",
        "teamtailor",
        "rippling",
        "pinpoint",
    }
)


def trusted_empties(
    dead: list[tuple[dict, str, str | None]], trusted: frozenset[str]
) -> list[tuple[dict, str]]:
    """From the dead set, the confirmed-empty boards on trusted JSON ATSes — onboardable as
    zero-job registry entries (returns (entry, token))."""
    return [
        (entry, token)
        for entry, token, err in dead
        if classify_dead(err) == "empty" and entry["ats"] in trusted
    ]


def partition(
    results: dict[int, tuple[dict, int, str, str | None]],
) -> tuple[list[tuple[dict, int, str]], list[tuple[dict, str, str | None]]]:
    """Split verify results into (verified [count>0], dead [(entry, token, err)])."""
    verified: list[tuple[dict, int, str]] = []
    dead: list[tuple[dict, str, str | None]] = []
    for i in sorted(results):
        entry, count, token, err = results[i]
        if count > 0:
            verified.append((entry, count, token))
        else:
            dead.append((entry, token, err))
    return verified, dead


def dedupe_best(
    verified: list[tuple[dict, int, str]],
) -> dict[str, tuple[dict, int, str]]:
    """Dedup by company key; on conflict keep the most jobs, then the best ATS priority."""
    best: dict[str, tuple[dict, int, str]] = {}
    for entry, count, token in verified:
        key = entry["company"]
        cur = best.get(key)
        if cur is None or (count, -ATS_PRIORITY.get(entry["ats"], 99)) > (
            cur[1],
            -ATS_PRIORITY.get(cur[0]["ats"], 99),
        ):
            best[key] = (entry, count, token)
    return best


async def verify_all(
    candidates: list[dict],
    *,
    concurrency: int,
    per_host_rate: int,
    timeout: float,
    retries: int,
    query: SearchQuery,
    label: str = "verifying",
) -> dict[int, tuple[dict, int, str, str | None]]:
    """Verify every candidate concurrently under the given fetcher pacing. Streams progress."""
    results: dict[int, tuple[dict, int, str, str | None]] = {}
    total = len(candidates)
    prog = {"done": 0, "live": 0}

    def tick(is_live: bool) -> None:
        prog["done"] += 1
        prog["live"] += int(is_live)
        d = prog["done"]
        step = max(100, total // 50)  # stream every ~2% (min 100) so long sweeps aren't silent
        if d % step == 0 or d == total:
            pct = 100 * d // total if total else 100
            print(
                f"  {label} {d}/{total} ({pct}%)  live={prog['live']} dead={d - prog['live']}",
                flush=True,
            )

    print(
        f"{label}: {total} candidates "
        f"(conc={concurrency} rate={per_host_rate}/s timeout={timeout}s retries={retries})",
        flush=True,
    )
    async with (
        AsyncFetcher(
            concurrency=concurrency,
            per_host_rate=per_host_rate,
            timeout=timeout,
            retries=retries,
        ) as fetcher,
        anyio.create_task_group() as tg,
    ):
        for i, entry in enumerate(candidates):

            async def run(i: int = i, entry: dict = entry) -> None:
                res = await verify_one(entry, fetcher, query)
                results[i] = res
                tick(res[1] > 0)

            tg.start_soon(run)
    return results


def _int_flag(name: str, default: int) -> int:
    if name in sys.argv:
        return int(sys.argv[sys.argv.index(name) + 1])
    return default


def _float_flag(name: str, default: float) -> float:
    if name in sys.argv:
        return float(sys.argv[sys.argv.index(name) + 1])
    return default


# Phase 2: a deliberately gentle re-verify of only the transient failures. Low concurrency and a
# low per-host rate keep us well under each ATS backend's tolerance so a board that was merely
# throttled in phase 1 gets a fair, unhurried second look.
_PHASE2 = {"concurrency": 4, "per_host_rate": 2, "timeout": 45.0, "retries": 5}


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    gentle = "--gentle" in sys.argv
    retry_transient = "--no-retry-transient" not in sys.argv
    onboard_empty = "--onboard-empty" in sys.argv
    # Defaults: the historical pacing (conc=12, rate=8). `--gentle` paces phase 1 like phase 2's
    # recovery profile (best for a re-verify of an already-throttled set). Explicit flags win.
    base = (
        {"concurrency": 6, "per_host_rate": 3, "timeout": 45.0, "retries": 5}
        if gentle
        else {
            "concurrency": 12,
            "per_host_rate": 8,
            "timeout": 30.0,
            "retries": 3,
        }
    )
    concurrency = _int_flag("--concurrency", base["concurrency"])
    per_host_rate = _int_flag("--per-host-rate", base["per_host_rate"])
    timeout = _float_flag("--timeout", base["timeout"])
    retries = _int_flag("--retries", base["retries"])

    # Positional candidates path: skip flags and the values consumed by value-taking flags.
    _VALUE_FLAGS = {"--concurrency", "--per-host-rate", "--timeout", "--retries"}
    paths: list[str] = []
    skip = False
    for tok in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if tok in _VALUE_FLAGS:
            skip = True
            continue
        if tok.startswith("--"):
            continue
        paths.append(tok)
    cand_path = Path(paths[0]) if paths else CANDIDATES
    load_builtins()
    candidates: list[dict] = json.loads(cand_path.read_text())
    # Verification only needs to confirm a board returns >=1 job — fetching every page (up to a
    # provider's MAX_PAGES) just to gate-check is pure waste and lets one huge board stall the
    # whole sweep. Cap to the first page; the dedup tiebreaker only needs a live signal, not an
    # exact count.
    query = SearchQuery(limit=5)

    # Phase 1: main verification pass.
    results = await verify_all(
        candidates,
        concurrency=concurrency,
        per_host_rate=per_host_rate,
        timeout=timeout,
        retries=retries,
        query=query,
        label="verifying",
    )
    verified, dead = partition(results)

    cats = Counter(classify_dead(err) for _e, _t, err in dead)
    print(f"\nphase-1: verified={len(verified)} dead={len(dead)}  dead-by-reason={dict(cats)}")

    # Phase 2: gentle re-verify of the transient failures only (throttle false-deads).
    recovered = 0
    if retry_transient:
        transient = [{**entry} for entry, _token, err in dead if is_transient(err)]
        if transient:
            print(f"\nretrying {len(transient)} transient (likely-throttled) candidates gently ...")
            r2 = await verify_all(transient, query=query, label="retry-transient", **_PHASE2)
            v2, d2 = partition(r2)
            recovered = len(v2)
            verified += v2
            # Rebuild the dead list: keep finals + whatever stayed dead in phase 2.
            finals = [(e, t, err) for e, t, err in dead if not is_transient(err)]
            dead = finals + d2
            cats = Counter(classify_dead(err) for _e, _t, err in dead)
            print(
                f"phase-2: recovered={recovered} still-dead={len(d2)}  "
                f"final dead-by-reason={dict(cats)}"
            )

    # Pool the live boards with, optionally, confirmed-empty boards on trusted JSON ATSes
    # (count=0). dedupe_best keeps a live board over an empty one for the same company, and lets
    # ATS priority break ties among empties.
    pool = list(verified)
    empties: list[tuple[dict, str]] = []
    if onboard_empty:
        empties = trusted_empties(dead, TRUSTED_EMPTY_PROVIDERS)
        pool += [(entry, 0, token) for entry, token in empties]
        print(f"\nonboard-empty: {len(empties)} confirmed-empty boards on trusted JSON ATSes")
    best = dedupe_best(pool)

    # Read-merge-write under the lock so concurrent runs compose instead of clobbering.
    with seed_lock():
        seed = json.loads(SEED.read_text())
        companies: dict[str, dict] = seed["companies"]
        added = 0
        added_empty = 0
        purged = purge_demo_boards(companies)  # self-heal: drop any demo board already present
        for key, (entry, count, token) in sorted(best.items()):
            # Registry keys are always lowercase; the token keeps its case (some ATSes, e.g.
            # SmartRecruiters, are case-sensitive on the token but not the company key).
            lk = key.lower()
            if lk in companies or is_demo_board(entry["ats"], token):
                continue
            companies[lk] = {
                "ats": entry["ats"],
                "token": token,
                "domain": entry.get("domain"),
            }
            added += 1
            if count == 0:
                added_empty += 1

        seed["_meta"]["version"] = 2
        seed["_meta"]["updated"] = "2026-06-16"

        print(
            f"\ncandidates={len(candidates)}  verified={len(verified)} "
            f"(recovered_in_phase2={recovered})  dead={len(dead)}"
        )
        by_ats: dict[str, int] = {}
        for entry, _c, _t in best.values():
            by_ats[entry["ats"]] = by_ats.get(entry["ats"], 0) + 1
        print(f"unique verified by ats: {by_ats}")
        print(
            f"added={added} (of which empty-onboarded={added_empty})  "
            f"purged_demo={purged}  registry_total={len(companies)}"
        )
        if dead:
            print("\nDEAD (first 20):")
            for entry, _token, err in dead[:20]:
                print(f"  {entry['ats']:10s} {entry['company']:25s} {err}")

        if dry_run:
            print("\n--dry-run: seed.json NOT written")
            return
        # indent=1 matches the committed seed.json format (the curated file uses 1-space
        # indent); writing indent=2 here would re-indent all ~49k lines on every merge.
        SEED.write_text(json.dumps(seed, indent=1, ensure_ascii=False) + "\n")
        print(f"\nwrote {SEED.relative_to(ROOT)}")


if __name__ == "__main__":
    anyio.run(main)
