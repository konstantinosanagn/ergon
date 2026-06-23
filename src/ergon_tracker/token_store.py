"""Tier-2 token store for the gated browser subsystem — offline minting, cheap headless replay.

Some boards mint a short-lived session token/cookie in JavaScript (Akamai sensor cookies, ADP-RM
``myjobstoken``, Dayforce/Paylocity JWTs). We cannot forge these headlessly, but we don't have to:
a browser mints one **on the offline cron**, we cache it with a TTL, and the cheap curl_cffi replay
reuses it until it expires or a request 401/403s — then we re-mint. The browser never touches the
user request path (see docs/superpowers/specs/2026-06-21-browser-discovery-design.md, Tier 2).

This module is the **pure, tested core**: persistence + TTL + single-flight refresh + refresh-on-status
+ staleness, with an **injected clock** (deterministic tests) and an **injected mint callback** (the
browser is a swappable shell — `mint_fn` is async and supplied by the offline minter, never here).

Security: tokens are session secrets. The store file is written with ``0600`` perms, lives under an
untracked path (``runs/``), and values are NEVER logged or returned in summaries — only injected into
the replay request. Keep its path out of git.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio

__all__ = ["TokenStore", "TokenRecord"]

# A persisted token: the secret value plus the policy needed to know when to drop/refresh it.
TokenRecord = dict[
    str, object
]  # {"value": str, "minted_at": float, "ttl_seconds": float|None, "refresh_on": list[int]}

_DEFAULT_REFRESH_ON = (401, 403)


def _f(value: object) -> float:
    """Best-effort float from a JSON-loaded record field (minted_at / ttl_seconds)."""
    return float(value) if isinstance(value, (int, float, str)) else 0.0


class TokenStore:
    """File-backed cache of short-lived session tokens, keyed by board token (e.g. ``"fastenal"``)."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._locks: dict[str, anyio.Lock] = {}  # per-key single-flight (lazy)
        self._mem: dict[str, TokenRecord] = self._load()

    # --- persistence (atomic write, 0600 — these are secrets) -------------------------------------
    def _load(self) -> dict[str, TokenRecord]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}  # corrupt/unreadable -> behave as empty; next mint repopulates
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            os.write(fd, json.dumps(self._mem, indent=1).encode())
            os.close(fd)
            os.chmod(tmp, 0o600)  # restrict before it lands at the final path
            os.replace(tmp, self._path)  # atomic
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- expiry / policy --------------------------------------------------------------------------
    def _expired(self, rec: TokenRecord) -> bool:
        ttl = rec.get("ttl_seconds")
        if ttl is None:  # no expiry policy => valid until explicitly marked stale
            return bool(rec.get("stale"))
        return bool(rec.get("stale")) or self._clock() >= _f(rec["minted_at"]) + _f(ttl)

    def ttl_remaining(self, key: str) -> float | None:
        """Seconds until ``key`` expires (negative if already past); ``None`` if absent, stale, or no TTL.

        The Tier-2 refresh cron uses this to re-mint *before* a token expires mid-crawl (margin)."""
        rec = self._mem.get(key)
        if rec is None or rec.get("stale"):
            return None
        ttl = rec.get("ttl_seconds")
        if ttl is None:
            return None
        return _f(rec["minted_at"]) + _f(ttl) - self._clock()

    def get(self, key: str) -> str | None:
        """The cached token value if present and still valid, else ``None`` (caller should mint)."""
        rec = self._mem.get(key)
        if rec is None or self._expired(rec):
            return None
        return str(rec["value"])

    def set(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: float | None,
        refresh_on: tuple[int, ...] = _DEFAULT_REFRESH_ON,
    ) -> None:
        self._mem[key] = {
            "value": value,
            "minted_at": self._clock(),
            "ttl_seconds": ttl_seconds,
            "refresh_on": list(refresh_on),
        }
        self._save()

    def mark_stale(self, key: str) -> None:
        """Force the next ``get``/``get_or_mint`` to treat the token as expired (call on 401/403)."""
        rec = self._mem.get(key)
        if rec is not None and not rec.get("stale"):
            rec["stale"] = True
            self._save()

    def should_refresh_on(self, key: str, status: int) -> bool:
        """Whether an HTTP ``status`` from a replay means this token must be re-minted."""
        rec = self._mem.get(key)
        refresh_on = (
            rec.get("refresh_on", list(_DEFAULT_REFRESH_ON)) if rec else _DEFAULT_REFRESH_ON
        )
        return status in refresh_on  # type: ignore[operator]

    async def get_or_mint(
        self,
        key: str,
        mint_fn: Callable[[], Awaitable[str]],
        *,
        ttl_seconds: float | None,
        refresh_on: tuple[int, ...] = _DEFAULT_REFRESH_ON,
    ) -> str:
        """Return a valid token, minting via ``mint_fn`` only if missing/expired.

        **Single-flight:** concurrent callers for the same key await ONE mint (no browser stampede)."""
        cached = self.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, anyio.Lock())
        async with lock:
            cached = self.get(key)  # re-check: another caller may have minted while we waited
            if cached is not None:
                return cached
            value = await mint_fn()  # the offline browser mint (the only expensive step)
            self.set(key, value, ttl_seconds=ttl_seconds, refresh_on=refresh_on)
            return value
