"""Adaptive crawl scheduler: decide which boards to crawl each build, and back off throttling.

Pure, offline-testable logic. The incremental builder persists one ``BoardState`` per board and
uses this module to (a) assign a freshness tier, (b) pick the boards due today, and (c) fold each
crawl outcome back into state (including throttle back-pressure). This is what shrinks a daily
build from "all 46k boards" to "only the ones likely to have changed" — the crawl-side
throttle-proofing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

__all__ = [
    "BoardState",
    "assign_tier",
    "compute_next_due",
    "due_boards",
    "apply_outcome",
    "load_state",
    "save_state",
]

# Tier thresholds (days). Tunable; chosen conservative.
HOT_DAYS = 4  # changed within this -> crawl daily
WARM_DAYS = 14  # changed within this -> crawl every WARM_INTERVAL
WARM_INTERVAL = 3
COLD_INTERVAL = 7  # cold boards re-crawled weekly
ERR_MAX = 3  # consecutive errors -> quarantine
THROTTLE_MAX = 0.5  # throttle_score (EWMA 429-rate) at/above this -> quarantine
QUARANTINE_COOLDOWN = 7  # days before a quarantined board is re-admitted


@dataclass
class BoardState:
    provider: str
    token: str
    sector: str | None = None
    last_crawled: str | None = None
    last_changed: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    consecutive_unchanged: int = 0
    consecutive_errors: int = 0
    throttle_score: float = 0.0
    tier: str = "hot"
    next_due: str | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}|{self.token}"


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def _days_between(a: str, b: str) -> int:
    return abs((_d(b) - _d(a)).days)


def assign_tier(state: BoardState, today: str) -> str:
    """Freshness tier from current state (errors/throttle dominate, then recency of change)."""
    if state.consecutive_errors >= ERR_MAX or state.throttle_score >= THROTTLE_MAX:
        return "quarantine"
    if state.last_changed is None:  # never observed a change yet (new board) -> treat as hot
        return "hot"
    age = _days_between(state.last_changed, today)
    if age <= HOT_DAYS:
        return "hot"
    if age <= WARM_DAYS:
        return "warm"
    return "cold"


def compute_next_due(tier: str, today: str) -> str:
    interval = {
        "hot": 0,
        "warm": WARM_INTERVAL,
        "cold": COLD_INTERVAL,
        "quarantine": QUARANTINE_COOLDOWN,
    }.get(tier, 0)
    return (_d(today) + timedelta(days=interval)).isoformat()


def due_boards(states: list[BoardState], today: str) -> list[str]:
    """Keys of boards to crawl today: all hot, plus any whose next_due has arrived."""
    out: list[str] = []
    for s in states:
        if s.tier == "hot":
            out.append(s.key)
        elif s.next_due is None or _d(s.next_due) <= _d(today):
            out.append(s.key)  # warm/cold due, or quarantine whose cooldown elapsed
    return out


def apply_outcome(
    state: BoardState,
    *,
    today: str,
    changed: bool,
    error: bool = False,
    http_429: int = 0,
    requests: int = 1,
) -> BoardState:
    """Fold one crawl outcome into state, then recompute tier + next_due (with back-pressure)."""
    state.last_crawled = today
    # throttle_score = EWMA of the per-build 429 rate (so a pushing-back host trends up)
    rate = (http_429 / requests) if requests else 0.0
    state.throttle_score = round(0.5 * state.throttle_score + 0.5 * rate, 4)
    if error:
        state.consecutive_errors += 1
    else:
        state.consecutive_errors = 0
    if changed:
        state.last_changed = today
        state.consecutive_unchanged = 0
    else:
        state.consecutive_unchanged += 1
    state.tier = assign_tier(state, today)
    state.next_due = compute_next_due(state.tier, today)
    return state


def load_state(path: Path | str) -> dict[str, BoardState]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, BoardState] = {}
    for rec in data.get("boards", []):
        s = BoardState(**rec)
        out[s.key] = s
    return out


def save_state(states: dict[str, BoardState], path: Path | str) -> None:
    payload = {"boards": [asdict(s) for s in states.values()]}
    Path(path).write_text(json.dumps(payload), encoding="utf-8")
