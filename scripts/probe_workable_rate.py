"""Bounded, safe rate-probe for the UNAUTHENTICATED Workable widget endpoint.

Workable publishes NO rate limit for ``apply.workable.com/api/v1/widget/accounts/{slug}`` (only the
authenticated ``spi/v3`` API is documented: 1 req/s account token, 5 req/s OAuth/partner, per-client
not per-IP). The widget endpoint is the one our crawler + Tier-3 drain actually use, so the ONLY way
to know its real ceiling is to measure it. This runs a stepped ramp against real board slugs, watches
for the first 429, and reports the sustained-clean knee -> a defensible ``ERGON_WORKABLE_DETAIL_RATE``.

WHY ``?details=true``: that is the exact (heavy, whole-board) payload the drain fetches, so the probe
stresses the same code path the cap governs -- not a lighter summary call that would over-report the
ceiling.

SAFETY (this is a live probe against a third-party production service):
  * BOUNDED total requests (``len(rates) * per_step``, plus a hard ``--max-requests`` backstop).
  * HARD STOP the instant a step sees a 429 (or a 5xx storm) -- never escalate past a failing rate;
    honour ``Retry-After`` before exiting.
  * Round-robins across MANY slugs so per-board load stays gentle (target_rate / num_slugs each).
  * Read-only GETs; reuses the crawler's real User-Agent so the server sees equivalent traffic.

IP SCOPING CAVEAT: the widget limit is almost certainly IP-keyed. A laptop (residential) run may
tolerate MORE than a GitHub Actions (datacenter) IP, so a local-clean result can over-estimate what
the CI drain can sustain. Treat a local run as a shape/first-signal read; confirm the exact cap from
CI (same IP class as the drain) via ``.github/workflows/probe-workable-rate.yml``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass

import anyio
import httpx

from ergon_tracker.http import DEFAULT_HEADERS

_WIDGET = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
_5XX = {500, 502, 503, 504}


@dataclass
class Result:
    slug: str
    status: int | None  # None => transport error (timeout / connection reset)
    elapsed: float
    retry_after: float | None = None
    size: int = 0


def slugs_from_index(path: str, limit: int) -> list[str]:
    """Distinct real Workable board slugs (``board_token``) from an index ``jobs`` table."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT DISTINCT board_token FROM jobs "
            "WHERE source='workable' AND board_token IS NOT NULL AND TRIM(board_token) != '' "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in rows]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def summarize(results: list[Result], target_rate: float, wall: float) -> dict:
    """Pure aggregation over one step's results -- unit-tested offline (no network)."""
    status_counts: dict[str | int, int] = {}
    for r in results:
        key: str | int = r.status if r.status is not None else "ERR"
        status_counts[key] = status_counts.get(key, 0) + 1
    ok_lat = sorted(r.elapsed for r in results if r.status == 200)

    def pct(p: float) -> float:
        if not ok_lat:
            return 0.0
        return ok_lat[min(len(ok_lat) - 1, int(p * len(ok_lat)))]

    n429 = status_counts.get(429, 0)
    n5xx = sum(c for s, c in status_counts.items() if isinstance(s, int) and s in _5XX)
    return {
        "target_rate": target_rate,
        "achieved_rate": round(len(results) / wall, 1) if wall > 0 else 0.0,
        "n": len(results),
        "ok": status_counts.get(200, 0),
        "n429": n429,
        "n5xx": n5xx,
        "errors": status_counts.get("ERR", 0),
        "status_counts": status_counts,
        "p50_ms": round(pct(0.50) * 1000),
        "p95_ms": round(pct(0.95) * 1000),
        "max_retry_after": max((r.retry_after or 0.0 for r in results), default=0.0),
    }


def step_failed(summary: dict, storm_frac: float = 0.10) -> bool:
    """A step is a failure (stop the ramp) on ANY 429, or a 5xx 'storm' (>= storm_frac of requests)."""
    if summary["n429"] > 0:
        return True
    n = summary["n"] or 1
    return summary["n5xx"] / n >= storm_frac


async def _one(
    client: httpx.AsyncClient, slug: str, out: list[Result], limiter: anyio.CapacityLimiter
) -> None:
    async with limiter:
        t0 = time.monotonic()
        try:
            resp = await client.get(_WIDGET.format(slug=slug))
            out.append(
                Result(slug, resp.status_code, time.monotonic() - t0, _retry_after(resp), len(resp.content))
            )
        except httpx.HTTPError:
            out.append(Result(slug, None, time.monotonic() - t0))


async def run_step(
    client: httpx.AsyncClient,
    slugs: list[str],
    rate: float,
    count: int,
    max_inflight: int,
) -> list[Result]:
    """Issue ``count`` GETs paced at ~``rate``/s, round-robining ``slugs``; bounded in-flight."""
    results: list[Result] = []
    limiter = anyio.CapacityLimiter(max_inflight)
    interval = 1.0 / rate if rate > 0 else 0.0
    async with anyio.create_task_group() as tg:
        for i in range(count):
            tg.start_soon(_one, client, slugs[i % len(slugs)], results, limiter)
            if interval:
                await anyio.sleep(interval)
    return results


def _print_step(s: dict) -> None:
    print(
        f"  {s['target_rate']:>4.0f}/s target | {s['achieved_rate']:>5}/s actual | "
        f"ok={s['ok']:>3} 429={s['n429']:>2} 5xx={s['n5xx']:>2} err={s['errors']:>2} | "
        f"p50={s['p50_ms']:>5}ms p95={s['p95_ms']:>6}ms",
        flush=True,
    )


async def probe(
    slugs: list[str],
    rates: list[float],
    per_step: int,
    timeout: float,
    max_inflight: int,
    cooldown: float,
    max_requests: int,
) -> tuple[float | None, list[dict]]:
    limits = httpx.Limits(max_connections=max_inflight, max_keepalive_connections=max_inflight)
    knee: float | None = None
    summaries: list[dict] = []
    issued = 0
    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout, http2=True, limits=limits
    ) as client:
        for rate in rates:
            if issued + per_step > max_requests:
                print(f"  [stop] --max-requests {max_requests} reached; not starting {rate:.0f}/s step")
                break
            t0 = time.monotonic()
            results = await run_step(client, slugs, rate, per_step, max_inflight)
            issued += len(results)
            s = summarize(results, rate, time.monotonic() - t0)
            summaries.append(s)
            _print_step(s)
            if step_failed(s):
                ra = s["max_retry_after"]
                print(
                    f"\n  [KNEE] 429/5xx first appeared at {rate:.0f}/s -> sustained-clean knee = "
                    f"{knee if knee is not None else '<below first step>'}/s"
                    + (f"; honouring Retry-After={ra:.0f}s before exit" if ra else "")
                )
                if ra:
                    await anyio.sleep(ra)
                break
            knee = rate
            await anyio.sleep(cooldown)  # let the edge breathe between steps
    return knee, summaries


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--slugs-from-index", help="index.sqlite path; pulls distinct workable board_tokens")
    src.add_argument("--slugs", help="comma-separated explicit board slugs")
    p.add_argument("--num-slugs", type=int, default=60, help="max distinct slugs to round-robin (default 60)")
    p.add_argument("--rates", default="16,24,32,48,64", help="comma-separated req/s ramp steps")
    p.add_argument("--per-step", type=int, default=120, help="requests per ramp step (default 120)")
    p.add_argument("--max-inflight", type=int, default=40, help="in-flight request cap (default 40)")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request timeout seconds (default 30)")
    p.add_argument("--cooldown", type=float, default=5.0, help="seconds between steps (default 5)")
    p.add_argument("--max-requests", type=int, default=1000, help="hard total-request backstop (default 1000)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.slugs:
        slugs = [s.strip() for s in args.slugs.split(",") if s.strip()][: args.num_slugs]
    else:
        slugs = slugs_from_index(args.slugs_from_index, args.num_slugs)
    if not slugs:
        print("no workable slugs found -- nothing to probe", file=sys.stderr)
        return 2
    rates = [float(x) for x in args.rates.split(",") if x.strip()]

    print(
        f"probing apply.workable.com/api/v1/widget/accounts/{{slug}}?details=true\n"
        f"  slugs={len(slugs)}  rates={rates}  per_step={args.per_step}  "
        f"max_requests={args.max_requests}  (per-board load ~= target_rate/{len(slugs)})\n"
    )
    knee, summaries = anyio.run(
        probe, slugs, rates, args.per_step, args.timeout, args.max_inflight, args.cooldown, args.max_requests
    )
    print("\n=== SUMMARY ===")
    for s in summaries:
        _print_step(s)
    if knee is None:
        print("\nRESULT: even the first ramp step saw 429/5xx -- current cap may already be at/over the edge.")
    elif summaries and step_failed(summaries[-1]):
        print(f"\nRESULT: sustained-clean knee = {knee:.0f}/s. Suggest ERGON_WORKABLE_DETAIL_RATE ~= {knee:.0f} "
              f"(hold a margin below the {summaries[-1]['target_rate']:.0f}/s step that first 429'd).")
    else:
        print(f"\nRESULT: clean through the whole ramp (max {knee:.0f}/s) -- no 429s observed. "
              f"Headroom to raise the cap; extend --rates higher to find the true knee.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
