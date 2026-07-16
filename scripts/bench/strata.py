"""Stratified allocation: guarantee a floor per stratum, then fill proportionally."""

from __future__ import annotations


def allocate(available: dict[str, int], total: int, floor: int) -> dict[str, int]:
    avail = {k: v for k, v in available.items() if v > 0}
    if not avail:
        return {}
    if sum(avail.values()) <= total:
        return dict(avail)  # take everything
    # Floor pass, bounded by the running budget so the guaranteed floors can never exceed `total`
    # (matters only in the degenerate floor*strata > total regime; smallest strata first so a tight
    # budget still spreads across strata instead of being exhausted by one large one).
    out: dict[str, int] = {}
    remaining = total
    for k in sorted(avail, key=lambda k: avail[k]):
        give = min(avail[k], floor, remaining)
        out[k] = give
        remaining -= give
    # Distribute the remainder proportionally to unused availability, largest-remainder rounding.
    while remaining > 0:
        headroom = {k: avail[k] - out[k] for k in avail if avail[k] > out[k]}
        if not headroom:
            break
        pool = sum(headroom.values())
        added = 0
        for k, room in sorted(headroom.items(), key=lambda kv: -kv[1]):
            give = min(room, max(1, remaining * room // pool))
            give = min(give, remaining - added)
            out[k] += give
            added += give
            if added >= remaining:
                break
        remaining -= added
        if added == 0:
            break
    return out
