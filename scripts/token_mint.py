"""Tier-2 offline mint shell — a browser mints a short-lived session token; we cache it.

    GATE: only for companies the ATS-Exhaustion Ladder proved exhausted AND that JS-mint a token
    (Akamai sensor cookie, ADP-RM myjobstoken, Dayforce/Paylocity JWT). The browser runs OFFLINE
    (this cron), never on the user request path. See the Tier-2 section of
    docs/superpowers/specs/2026-06-21-browser-discovery-design.md.

Same split as the rest of Stream B: the browser is a thin, swappable shell; the token-extraction +
store-write logic is pure and unit-tested.

- ``extract_token(state, cfg)`` — PURE: pull the token out of a captured browser ``state``
  ({cookies, local_storage, session_storage, xhr}) per a small ``extract`` config. Tested.
- ``mint_from_state(...)`` — PURE: extract, then write to the :class:`~ergon_tracker.token_store.TokenStore`
  with the target's TTL/refresh policy. Tested.
- ``capture_state(url)`` — the browser shell (Playwright, optional import). Loads the page, lets the
  page's JS mint the token, and reads cookies/storage/XHR headers. Not unit-tested (I/O).
- ``mint(token_ref, store)`` — orchestrates capture -> extract -> store.set (the cron entry point).

Targets live in ``scripts/tier2_mint.json`` (see ``tier2_mint.example.json``); each maps a token_ref to
``{url, extract, ttl_seconds, refresh_on}``. Token VALUES are secrets — never printed or logged here.

Usage::
    python scripts/token_mint.py fastenal                 # live-capture + mint into the store
    python scripts/token_mint.py fastenal --state cap.json # mint from a pre-captured state fixture
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.token_store import TokenStore  # noqa: E402

TARGETS_PATH = ROOT / "scripts" / "tier2_mint.json"
DEFAULT_STORE = ROOT / "runs" / "tier2_tokens.json"


class MintError(RuntimeError):
    """Raised when a target's token could not be found in the captured browser state."""


# --- pure extraction (state -> token) ------------------------------------------------------------
def extract_token(state: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Pull the token value from a captured browser ``state`` per ``cfg`` (one source). Pure.

    ``state``: ``{"cookies": {n:v}, "local_storage": {k:v}, "session_storage": {k:v},
                  "xhr": [{"url": str, "headers": {n:v}}]}``
    ``cfg``: one of ``{"cookie": name}``, ``{"local_storage": key}``, ``{"session_storage": key}``,
             ``{"xhr_header": {"url_contains": str, "header": name}}``.
    """
    if "cookie" in cfg:
        return (state.get("cookies") or {}).get(cfg["cookie"])
    if "local_storage" in cfg:
        return (state.get("local_storage") or {}).get(cfg["local_storage"])
    if "session_storage" in cfg:
        return (state.get("session_storage") or {}).get(cfg["session_storage"])
    if "xhr_header" in cfg:
        spec = cfg["xhr_header"]
        want_url, want_hdr = spec.get("url_contains", ""), spec.get("header", "").lower()
        for req in state.get("xhr") or []:
            if want_url in (req.get("url") or ""):
                for hk, hv in (req.get("headers") or {}).items():
                    if hk.lower() == want_hdr and hv:
                        return hv
    return None


def mint_from_state(
    token_ref: str, state: dict[str, Any], target: dict[str, Any], store: TokenStore
) -> str:
    """Extract the token from ``state`` and write it to ``store``. Returns the value (caller must not log)."""
    value = extract_token(state, target.get("extract") or {})
    if not value:
        raise MintError(
            f"{token_ref}: token not found in captured state via {target.get('extract')!r}"
        )
    store.set(
        token_ref,
        value,
        ttl_seconds=target.get("ttl_seconds"),
        refresh_on=tuple(target.get("refresh_on") or (401, 403)),
    )
    return value


def summarize(token_ref: str, value: str, target: dict[str, Any]) -> str:
    """A SECRETS-SAFE one-liner about a mint (never includes the token value)."""
    return (
        f"minted {token_ref}: {len(value)} chars via {list(target.get('extract') or {})[:1]} "
        f"ttl={target.get('ttl_seconds')}s refresh_on={target.get('refresh_on') or (401, 403)}"
    )


# --- browser shell (Playwright; optional import) -------------------------------------------------
async def capture_state(
    url: str, *, settle_ms: int = 4000, wait_xhr: str | None = None
) -> dict[str, Any]:
    """Load ``url`` in a headless browser, let its JS mint the token, return the captured state.

    Optional dependency: install the browser extra (``uv pip install playwright && playwright install
    chromium``). Kept off the runtime path — this only runs on the offline mint cron."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - environment-gated, mirrors the semantic extra
        raise MintError(
            "token minting needs Playwright — `uv pip install ergon-tracker[browser] && "
            "playwright install chromium`"
        ) from exc

    xhr: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # A believable, internally-consistent fingerprint beats any single stealth trick.
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        page.on("request", lambda r: xhr.append({"url": r.url, "headers": dict(r.headers)}))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_xhr:
                with __import__("contextlib").suppress(Exception):
                    await page.wait_for_request(lambda r: wait_xhr in r.url, timeout=settle_ms)
            await page.wait_for_timeout(settle_ms)
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            local_storage = await page.evaluate("() => ({...localStorage})")
            session_storage = await page.evaluate("() => ({...sessionStorage})")
        finally:
            await browser.close()
    return {
        "cookies": cookies,
        "local_storage": local_storage,
        "session_storage": session_storage,
        "xhr": xhr,
    }


async def mint(token_ref: str, store: TokenStore, targets: dict[str, Any]) -> str:
    """Cron entry point: capture state via the browser, extract the token, write it to the store."""
    target = targets.get(token_ref)
    if not target:
        raise MintError(f"no Tier-2 mint target for {token_ref!r} in {TARGETS_PATH.name}")
    state = await capture_state(
        target["url"], wait_xhr=target.get("wait_xhr"), settle_ms=int(target.get("settle_ms", 4000))
    )
    return mint_from_state(token_ref, state, target, store)


def _load_targets() -> dict[str, Any]:
    if not TARGETS_PATH.exists():
        return {}
    return json.loads(TARGETS_PATH.read_text())


def main() -> None:
    import anyio

    ap = argparse.ArgumentParser(description="Tier-2 offline token mint (browser -> TokenStore)")
    ap.add_argument("token_ref", help="target key in scripts/tier2_mint.json")
    ap.add_argument(
        "--state", help="mint from a pre-captured state.json instead of launching a browser"
    )
    ap.add_argument(
        "--store", default=str(DEFAULT_STORE), help="TokenStore path (secrets; gitignored)"
    )
    args = ap.parse_args()

    targets = _load_targets()
    target = targets.get(args.token_ref)
    if not target:
        print(f"no mint target for {args.token_ref!r} in {TARGETS_PATH}", file=sys.stderr)
        sys.exit(2)
    store = TokenStore(args.store)
    try:
        if args.state:
            state = json.loads(Path(args.state).read_text())
            value = mint_from_state(args.token_ref, state, target, store)
        else:
            value = anyio.run(mint, args.token_ref, store, targets)
    except MintError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
    print(summarize(args.token_ref, value, target))  # secrets-safe (no value)


if __name__ == "__main__":
    main()
