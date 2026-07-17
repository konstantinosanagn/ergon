"""Tests for scripts.bench.merge_votes: reduce 3-labeler votes to gold consensus + split flags.

Tests cover: unanimous agreement (gold + no split), 2-of-3 majority (gold + no split),
3-way tie (split=True, gold=None), None/null handling, and dict-valued fields
(salary with {min, max, currency}, yoe with {min, max}).
"""

from __future__ import annotations

from scripts.bench.merge_votes import merge_votes


def test_merge_votes_three_agree_sets_gold_no_split():
    """When all 3 labelers agree, gold is that value and split=False."""
    votes = {
        "level": ["senior", "senior", "senior"],
        "remote": [True, True, True],
    }
    gold, split = merge_votes(votes)
    assert gold == {"level": "senior", "remote": True}
    assert split == {"level": False, "remote": False}


def test_merge_votes_two_of_three_majority_sets_gold_no_split():
    """When 2 of 3 agree, gold is that value and split=False."""
    votes = {
        "level": ["senior", "senior", "junior"],
        "remote": [True, False, False],
    }
    gold, split = merge_votes(votes)
    assert gold == {"level": "senior", "remote": False}
    assert split == {"level": False, "remote": False}


def test_merge_votes_three_way_tie_sets_split_true_gold_none():
    """When all 3 differ (1/1/1 tie), split=True and gold=None."""
    votes = {
        "level": ["senior", "mid", "junior"],
        "sector": ["Software", "Finance", "Healthcare"],
    }
    gold, split = merge_votes(votes)
    assert gold == {"level": None, "sector": None}
    assert split == {"level": True, "sector": True}


def test_merge_votes_handles_none_votes_as_value():
    """None votes count as a value for majority determination."""
    votes = {
        "sector": [None, None, "Software"],
    }
    gold, split = merge_votes(votes)
    assert gold == {"sector": None}
    assert split == {"sector": False}


def test_merge_votes_all_none_is_unanimous():
    """When all votes are None, gold=None and split=False."""
    votes = {
        "field1": [None, None, None],
    }
    gold, split = merge_votes(votes)
    assert gold == {"field1": None}
    assert split == {"field1": False}


def test_merge_votes_handles_dict_valued_salary():
    """Dict-valued salary field with {min, max, currency} is treated as single value."""
    votes = {
        "salary": [
            {"min": 100000, "max": 150000, "currency": "USD"},
            {"min": 100000, "max": 150000, "currency": "USD"},
            {"min": 110000, "max": 160000, "currency": "USD"},
        ],
    }
    gold, split = merge_votes(votes)
    # First two agree, so that's the majority
    assert gold["salary"] == {"min": 100000, "max": 150000, "currency": "USD"}
    assert split["salary"] is False


def test_merge_votes_handles_dict_valued_yoe():
    """Dict-valued yoe field with {min, max} is treated as single value."""
    votes = {
        "yoe": [
            {"min": 5, "max": 8},
            {"min": 5, "max": 8},
            {"min": 3, "max": 6},
        ],
    }
    gold, split = merge_votes(votes)
    # First two agree
    assert gold["yoe"] == {"min": 5, "max": 8}
    assert split["yoe"] is False


def test_merge_votes_dict_three_way_tie():
    """When all 3 dict values differ, split=True and gold=None."""
    votes = {
        "salary": [
            {"min": 100000, "max": 150000, "currency": "USD"},
            {"min": 110000, "max": 160000, "currency": "USD"},
            {"min": 120000, "max": 170000, "currency": "USD"},
        ],
    }
    gold, split = merge_votes(votes)
    assert gold["salary"] is None
    assert split["salary"] is True


def test_merge_votes_mixed_types_in_same_field():
    """Handle mixed types in votes (strings, bools, None, dicts) per field."""
    votes = {
        "level": ["senior", "senior", "mid"],
        "remote": [True, True, True],
        "salary": [None, None, None],
        "yoe": [
            {"min": 5, "max": 8},
            {"min": 5, "max": 8},
            {"min": 5, "max": 8},
        ],
    }
    gold, split = merge_votes(votes)
    assert gold == {
        "level": "senior",
        "remote": True,
        "salary": None,
        "yoe": {"min": 5, "max": 8},
    }
    assert split == {
        "level": False,
        "remote": False,
        "salary": False,
        "yoe": False,
    }


def test_merge_votes_empty_dict():
    """Empty votes dict returns empty gold and split dicts."""
    votes = {}
    gold, split = merge_votes(votes)
    assert gold == {}
    assert split == {}


def test_merge_votes_ragged_counts_one_and_two_votes():
    # Real fleet coverage is ragged (a labeler misses a row / drops a file), so a field may carry
    # fewer than 3 votes. Take the strict majority of whatever is present.
    gold, split = merge_votes(
        {
            "solo": ["senior"],  # lone vote -> gold, no split
            "pair_agree": ["mid", "mid"],  # 2/2 -> gold, no split
            "pair_differ": ["mid", "senior"],  # 1/1 tie -> split, gold None
            "empty": [],  # no votes -> split, gold None
        }
    )
    assert gold["solo"] == "senior" and split["solo"] is False
    assert gold["pair_agree"] == "mid" and split["pair_agree"] is False
    assert gold["pair_differ"] is None and split["pair_differ"] is True
    assert gold["empty"] is None and split["empty"] is True
