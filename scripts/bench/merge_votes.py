"""Reduce 3-labeler votes to gold consensus + split flags.

merge_votes() takes field -> list of 3 votes from blind labelers and returns
(gold, split) dicts. Gold is the majority value; split=True when there's no
majority (3-way tie), in which case gold=None. Handle None as a value and
unhashable dict votes by value comparison.
"""

from __future__ import annotations

from typing import Any


def _normalize_vote(vote: Any) -> Any:
    """Normalize a vote for comparison. Dicts are normalized by sorted items."""
    if isinstance(vote, dict):
        # For dicts, convert to a sorted tuple of items for hashability
        return ("__dict__", tuple(sorted(vote.items())))
    return vote


def _denormalize_vote(normalized: Any) -> Any:
    """Convert normalized vote back to original form."""
    if isinstance(normalized, tuple) and len(normalized) == 2 and normalized[0] == "__dict__":
        return dict(normalized[1])
    return normalized


def merge_votes(votes: dict[str, list[Any]]) -> tuple[dict[str, Any], dict[str, bool]]:
    """Merge 3 labeler votes into gold consensus and split flags.

    Args:
        votes: Maps each field to a list of 3 votes from blind labelers.
               Values may be str, bool, None, or dicts like {min,max,currency}.

    Returns:
        (gold, split) tuple where:
        - gold[field] = the majority value (None if no majority)
        - split[field] = True if there's a 3-way tie (all different), False otherwise
    """
    gold: dict[str, Any] = {}
    split: dict[str, bool] = {}

    for field, field_votes in votes.items():
        # Real fleet coverage is ragged: a labeler can miss a row or drop a file, so a field may
        # carry 1-3 votes (not always 3). Take the STRICT majority of whatever is present; a value
        # is gold only when it holds > half the votes. n=1 -> that lone vote (majority of 1);
        # n=2 agree -> gold, differ -> split; n=3 -> 2-or-3 agree -> gold, all-differ -> split.
        n = len(field_votes)
        if n == 0:
            gold[field] = None
            split[field] = True
            continue

        # Normalize all votes for comparison
        normalized = [_normalize_vote(v) for v in field_votes]

        # Count occurrences of each normalized vote
        vote_counts: dict[Any, int] = {}
        for nv in normalized:
            vote_counts[nv] = vote_counts.get(nv, 0) + 1

        majority_vote = None
        majority_count = 0
        for nv, count in vote_counts.items():
            if count > majority_count:
                majority_vote = nv
                majority_count = count

        if majority_count * 2 > n:
            # A strict majority (> half the present votes) agree.
            gold[field] = _denormalize_vote(majority_vote)
            split[field] = False
        else:
            # No strict majority (e.g. 1/1/1 or a 1/1 two-way tie).
            gold[field] = None
            split[field] = True

    return gold, split
