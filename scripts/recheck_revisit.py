"""Cheap re-check of residual giants whose boards exist but are EMPTY / mid-migration right now.

The giants effort has hit its floor: the ~150 still-uncaptured are bot-walled (TCS, American
Airlines, MathWorks, HCA), JS/auth-walled HR systems (PageUp/PeopleSoft/Cornerstone/Darwinbox/
Njoyn), over-broad-parent-only boards (rejected for entity-safety), or no-public-board staffing
shops. NONE are capturable without a runtime browser (which the SDK forbids).

The ONLY moving target is a short list of real boards that are temporarily empty or migrating.
This script probes just those — so a cron turn can run it in seconds and only act when one
repopulates. If a board returns jobs, it prints a READY line with the token to seed.

    .venv/bin/python scripts/recheck_revisit.py
"""

from __future__ import annotations

import asyncio

import httpx

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# (label, kind, probe-spec). kind drives how we count jobs.
_WATCH = [
    ("credit karma", "greenhouse", "creditkarma"),
    ("valuemomentum", "jobsoid", "valuemomentum"),
    ("university of alabama (faculty)", "peopleadmin", "facultyjobs.ua.edu"),
    ("h lee moffitt cancer center", "workday", "moffittcancercenter|wd1|External"),
    ("h lee moffitt cancer center", "workday", "moffittcancercenter|wd1|Moffitt"),
]


async def _count(kind: str, spec: str, c: httpx.AsyncClient) -> int | None:
    try:
        if kind == "greenhouse":
            r = await c.get(f"https://boards-api.greenhouse.io/v1/boards/{spec}/jobs")
            return r.json().get("meta", {}).get("total") if r.status_code == 200 else None
        if kind == "jobsoid":
            r = await c.get(f"https://{spec}.jobsoid.com/api/v1/jobs")
            return len(r.json()) if r.status_code == 200 else None
        if kind == "peopleadmin":
            host = spec if "." in spec else f"{spec}.peopleadmin.com"
            r = await c.get(f"https://{host}/postings/search.atom")
            return r.text.count("<entry>") if r.status_code == 200 else None
        if kind == "workday":
            tenant, wd, site = spec.split("|")
            r = await c.post(
                f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                json={"limit": 1, "offset": 0},
                headers={"Content-Type": "application/json"},
            )
            return r.json().get("total") if r.status_code == 200 else None
    except Exception:
        return None
    return None


async def main() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": _UA}, follow_redirects=True, timeout=12) as c:
        ready = 0
        for label, kind, spec in _WATCH:
            n = await _count(kind, spec, c)
            # >=3 jobs = genuinely repopulated (1-job "feeds" are empty-board placeholder stubs).
            is_ready = bool(n and n >= 3)
            status = f"READY ({n} jobs) -> seed {kind} {spec!r}" if is_ready else f"empty/floor (n={n})"
            if is_ready:
                ready += 1
            print(f"  {label:38s} {kind:11s} {status}")
        print(f"\n{ready} board(s) repopulated and ready to seed." if ready else "\nNo change — all still empty/floor.")


if __name__ == "__main__":
    asyncio.run(main())
