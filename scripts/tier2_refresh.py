"""Tier-2 token-refresh cron — re-mint session tokens before they expire (offline).

Runs on the index-build cron BEFORE the crawl, so Tier-2 replays have a fresh token. Pure decision
logic (`needs_refresh`) + a thin runner over the offline browser mint. Token values are never logged.

    GATE: only refreshes targets already in scripts/tier2_mint.json (exhaustion-proven, validated).
    The browser runs offline here, never on the request path.

Usage::
    python scripts/tier2_refresh.py            # refresh every target that's expired/near-expiry
    python scripts/tier2_refresh.py fastenal   # refresh just these
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from token_mint import DEFAULT_STORE, MintError, _load_targets, mint  # noqa: E402

from ergon_tracker.token_store import TokenStore  # noqa: E402

DEFAULT_MARGIN = 0.2  # re-mint once <20% of the TTL remains, so a token can't lapse mid-crawl


def needs_refresh(
    store: TokenStore,
    token_ref: str,
    target: dict[str, Any],
    *,
    margin_frac: float = DEFAULT_MARGIN,
) -> bool:
    """True if ``token_ref`` is missing/expired, or within ``margin_frac`` of its TTL expiring."""
    if store.get(token_ref) is None:  # absent or already expired/stale
        return True
    remaining = store.ttl_remaining(token_ref)
    ttl = target.get("ttl_seconds")
    if remaining is None or not ttl:  # no TTL policy -> only re-minted when marked stale, not here
        return False
    return remaining <= margin_frac * float(ttl)


async def refresh(
    store: TokenStore,
    targets: dict[str, Any],
    only: list[str] | None = None,
    *,
    margin_frac: float = DEFAULT_MARGIN,
    mint_fn: Callable[[str], Awaitable[str]] | None = None,
) -> dict[str, list[str]]:
    """Re-mint every (selected) target that needs it. ``mint_fn`` is injectable for tests."""
    keys = [k for k in (only or targets) if not k.startswith("_") and k in targets]
    out: dict[str, list[str]] = {"refreshed": [], "skipped": [], "failed": []}
    for ref in keys:
        if not needs_refresh(store, ref, targets[ref], margin_frac=margin_frac):
            out["skipped"].append(ref)
            continue
        try:
            await (mint_fn(ref) if mint_fn else mint(ref, store, targets))
            out["refreshed"].append(ref)
        except MintError as exc:
            out["failed"].append(f"{ref}: {exc}")
    return out


def main() -> None:
    import anyio

    ap = argparse.ArgumentParser(description="Tier-2 token refresh (offline cron)")
    ap.add_argument("targets", nargs="*", help="specific token_refs (default: all)")
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    args = ap.parse_args()

    targets = _load_targets()
    store = TokenStore(args.store)
    # Same bug as spec_health_cron.py: anyio.run(func, *args, **kw) does NOT forward kwargs to
    # `func` -- it raises TypeError for anything it doesn't own itself. Wrap in a lambda so
    # `margin_frac` actually reaches refresh().
    res = anyio.run(lambda: refresh(store, targets, args.targets or None, margin_frac=args.margin))
    print(
        f"refreshed={len(res['refreshed'])} {res['refreshed']} | skipped={len(res['skipped'])} | "
        f"failed={len(res['failed'])}"
    )
    for f in res["failed"]:
        print(f"  FAIL {f}", file=sys.stderr)
    sys.exit(1 if res["failed"] else 0)


if __name__ == "__main__":
    main()
