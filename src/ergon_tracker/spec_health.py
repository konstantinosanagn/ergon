"""Spec-health tracker for the self-healing discovery loop (Stream B).

Browser-discovered apicapture specs and Tier-2 token specs can rot silently when a site changes its
API shape or rotates a token format. This file-backed tracker records each spec's replay outcomes so a
cron can spot the rot: **N consecutive failures → the spec is stale → re-queue it for discovery**
(see docs/superpowers/specs/2026-06-21-browser-discovery-design.md, "spec health + self-healing").

Pure, offline-testable (like ``index.scheduler``): no network, no clock dependence for the core
decision (``consecutive_failures``); an optional ``now`` string is stored only for observability.
Health metrics are not secret, so the store is plain JSON (default ``runs/spec_health.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["SpecHealth", "DEFAULT_STALE_THRESHOLD"]

DEFAULT_STALE_THRESHOLD = 3  # consecutive failures before a spec is considered stale -> re-discover


def _i(value: object) -> int:
    """Best-effort int from a JSON-loaded counter (records may round-trip as int or str)."""
    return value if isinstance(value, int) else 0


class SpecHealth:
    """Per-spec replay health, keyed by spec token (e.g. an apicapture key like ``"eogresources"``)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._mem: dict[str, dict[str, object]] = self._load()

    def _load(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text())
            return loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._mem, indent=1, sort_keys=True))

    def record(self, token: str, ok: bool, *, now: str | None = None) -> None:
        """Record one replay outcome. A success resets the consecutive-failure streak."""
        r = self._mem.setdefault(
            token,
            {
                "attempts": 0,
                "oks": 0,
                "consecutive_failures": 0,
                "last_checked": None,
                "last_ok": None,
            },
        )
        r["attempts"] = _i(r["attempts"]) + 1
        if ok:
            r["oks"] = _i(r["oks"]) + 1
            r["consecutive_failures"] = 0
            r["last_ok"] = now
        else:
            r["consecutive_failures"] = _i(r["consecutive_failures"]) + 1
        r["last_checked"] = now

    def consecutive_failures(self, token: str) -> int:
        r = self._mem.get(token)
        return _i(r["consecutive_failures"]) if r else 0

    def success_rate(self, token: str) -> float | None:
        r = self._mem.get(token)
        if not r or _i(r["attempts"]) == 0:
            return None
        return _i(r["oks"]) / _i(r["attempts"])

    def is_stale(self, token: str, threshold: int = DEFAULT_STALE_THRESHOLD) -> bool:
        """True once a spec has failed ``threshold`` times in a row (→ re-discover)."""
        return self.consecutive_failures(token) >= threshold

    def stale(self, threshold: int = DEFAULT_STALE_THRESHOLD) -> list[str]:
        """All tokens currently stale — the re-discover queue."""
        return sorted(t for t in self._mem if self.is_stale(t, threshold))
