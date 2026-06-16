"""Tests for the observability helpers."""

from __future__ import annotations

from jobspine.observability import (
    Timer,
    build_health,
    count_sanity_check,
    summarize,
    time_source,
)


def test_build_health_round_trips_fields() -> None:
    h = build_health("greenhouse", ok=True, count=12, elapsed_ms=34, truncated=True)
    assert h.source == "greenhouse"
    assert h.ok is True
    assert h.count == 12
    assert h.elapsed_ms == 34
    assert h.truncated is True
    assert h.error is None


def test_build_health_failure() -> None:
    h = build_health("workday", ok=False, error="boom")
    assert h.ok is False
    assert h.error == "boom"
    assert h.count == 0


def test_timer_measures_elapsed() -> None:
    with time_source() as t:
        pass
    assert isinstance(t, Timer)
    assert t.elapsed_ms >= 0


def test_count_sanity_check_fires_below_floor() -> None:
    # baseline 100, ratio 0.5 -> floor 50; count 10 is below.
    warning = count_sanity_check("remoteok", 10, baseline=100, drop_ratio=0.5)
    assert warning is not None
    assert "remoteok" in warning


def test_count_sanity_check_silent_when_healthy() -> None:
    assert count_sanity_check("remoteok", 80, baseline=100, drop_ratio=0.5) is None


def test_count_sanity_check_none_without_baseline() -> None:
    assert count_sanity_check("remoteok", 0) is None
    assert count_sanity_check("remoteok", 0, baseline=0) is None


def test_summarize_totals() -> None:
    healths = [
        build_health("greenhouse", ok=True, count=10),
        build_health("lever", ok=True, count=5),
        build_health("workday", ok=False, error="timeout"),
    ]
    summary = summarize(healths)
    assert summary["ok_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["total_jobs"] == 15
    assert summary["failed"] == ["workday"]
