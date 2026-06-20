"""Tests for scripts/build_registry.py verification helpers.

The crux: a candidate that merely throttled (429 / timeout / circuit-open / exhausted retries)
must be classified TRANSIENT so it is re-verified, never silently merged into the permanent
"dead" set. Genuinely-empty (200 + no jobs) and gone (404/410) boards must classify as final.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_build_registry", pathlib.Path(__file__).parent.parent / "scripts" / "build_registry.py"
)
br = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(br)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "err,expected",
    [
        (None, "empty"),
        ("HTTPStatusError: 404 Not Found", "gone"),
        ("HTTPStatusError: 410 Gone", "gone"),
        ("HTTPStatusError: 403 Forbidden", "walled"),
        ("RateLimitError: 429 Too Many Requests from boards.greenhouse.io", "rate_limited"),
        ("FetchError: circuit open for workable.com (cooling down)", "circuit_open"),
        ("FetchError: exhausted retries for https://x", "exhausted"),
        ("ReadTimeout: timed out", "timeout"),
        ("ConnectError: connection refused", "transport"),
        ("JSONDecodeError: Expecting value", "parse_error"),
    ],
)
def test_classify_dead(err, expected):
    assert br.classify_dead(err) == expected


@pytest.mark.parametrize(
    "err",
    [
        "RateLimitError: 429 Too Many Requests",
        "FetchError: circuit open for workable.com (cooling down)",
        "FetchError: exhausted retries for https://x",
        "ReadTimeout: timed out",
        "ConnectError: connection refused",
        "TransientHTTPError: 503 from x",
    ],
)
def test_is_transient_true_for_throttle_and_network(err):
    assert br.is_transient(err) is True


@pytest.mark.parametrize(
    "err",
    [
        None,  # clean empty board
        "HTTPStatusError: 404 Not Found",
        "HTTPStatusError: 410 Gone",
        "HTTPStatusError: 403 Forbidden",
        "JSONDecodeError: Expecting value",
        "no provider for foo",
    ],
)
def test_is_transient_false_for_permanent(err):
    assert br.is_transient(err) is False


def test_dedupe_best_prefers_more_jobs_then_ats_priority():
    # Same company on two ATSes: more jobs wins.
    gh = {"company": "acme", "ats": "greenhouse"}
    lv = {"company": "acme", "ats": "lever"}
    best = br.dedupe_best([(gh, 3, "acme"), (lv, 9, "acme")])
    assert best["acme"][0]["ats"] == "lever"  # 9 > 3 jobs

    # Tie on job count: lower ATS_PRIORITY number wins (greenhouse 0 < lever 1).
    best2 = br.dedupe_best([(lv, 5, "acme"), (gh, 5, "acme")])
    assert best2["acme"][0]["ats"] == "greenhouse"


def test_partition_splits_live_from_dead_carrying_err():
    results = {
        0: ({"company": "a", "ats": "lever"}, 4, "a", None),
        1: ({"company": "b", "ats": "lever"}, 0, "b", "ReadTimeout: timed out"),
        2: ({"company": "c", "ats": "lever"}, 0, "c", None),
    }
    verified, dead = br.partition(results)
    assert [v[0]["company"] for v in verified] == ["a"]
    assert {d[0]["company"]: d[2] for d in dead} == {
        "b": "ReadTimeout: timed out",
        "c": None,
    }
