"""Crawl progress HEARTBEAT: throttled, correct payload, non-fatal, end-to-end over run_pool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio

from ergon_tracker.crawl_pool import run_pool
from ergon_tracker.crawl_progress import ProgressHeartbeat, atomic_write_json


class FakeClock:
    """Manually-advanced monotonic clock so the throttle is tested without real sleeps."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _recording_writer() -> tuple[list[dict[str, Any]], Any]:
    writes: list[dict[str, Any]] = []

    def writer(_path: Path, payload: dict[str, Any]) -> None:
        writes.append(dict(payload))

    return writes, writer


def test_throttles_not_per_board() -> None:
    """Many ticks inside one interval flush exactly ONCE (the immediate first), never per-board."""
    clock = FakeClock()
    writes, writer = _recording_writer()
    hb = ProgressHeartbeat("x.json", total=100, interval_s=90.0, clock=clock, writer=writer)

    for _ in range(50):  # all within the same interval (clock frozen)
        hb.tick()

    assert len(writes) == 1  # only the first tick flushed
    assert hb.writes == 1
    assert writes[0]["boards_done"] == 1
    assert writes[0]["boards_total"] == 100


def test_emits_again_after_interval_elapses() -> None:
    """A second flush happens only once >= interval_s has passed since the last one."""
    clock = FakeClock()
    writes, writer = _recording_writer()
    hb = ProgressHeartbeat("x.json", total=10, interval_s=90.0, clock=clock, writer=writer)

    hb.tick()  # flush #1 (immediate), boards_done=1
    clock.advance(30)
    hb.tick()  # still inside the window -> no flush
    assert len(writes) == 1
    clock.advance(70)  # now 100s since flush #1 -> over the 90s interval
    hb.tick()  # flush #2, boards_done=3
    assert len(writes) == 2
    assert writes[-1]["boards_done"] == 3


def test_serializes_expected_fields_including_extra() -> None:
    """Snapshot carries done/total/elapsed plus whatever extra() supplies (rows, host tail)."""
    clock = FakeClock()
    writes, writer = _recording_writer()

    def extra() -> dict[str, Any]:
        return {"rows_so_far": 4242, "slowest_hosts": [{"host": "join.com", "wall_s": 812.0}]}

    hb = ProgressHeartbeat(
        "x.json", total=7, interval_s=90.0, clock=clock, extra=extra, writer=writer
    )
    clock.advance(12.5)
    hb.tick()

    snap = writes[0]
    assert snap["boards_done"] == 1
    assert snap["boards_total"] == 7
    assert snap["elapsed_s"] == 12.5
    assert snap["rows_so_far"] == 4242
    assert snap["slowest_hosts"] == [{"host": "join.com", "wall_s": 812.0}]


def test_write_error_is_non_fatal_and_still_throttles() -> None:
    """A raising writer never propagates; the throttle still advances (no per-board retry storm)."""
    clock = FakeClock()
    calls = {"n": 0}

    def boom(_path: Path, _payload: dict[str, Any]) -> None:
        calls["n"] += 1
        raise OSError("disk full")

    hb = ProgressHeartbeat("x.json", total=3, interval_s=90.0, clock=clock, writer=boom)
    for _ in range(20):
        hb.tick()  # must not raise

    assert calls["n"] == 1  # only the first tick attempted a (failing) write; rest were throttled
    assert hb.writes == 0  # nothing counted as a successful write


def test_extra_error_is_swallowed_and_base_fields_still_written() -> None:
    """A raising extra() is swallowed; the base snapshot is still written."""
    clock = FakeClock()
    writes, writer = _recording_writer()

    def bad_extra() -> dict[str, Any]:
        raise RuntimeError("host accounting exploded")

    hb = ProgressHeartbeat(
        "x.json", total=2, interval_s=90.0, clock=clock, extra=bad_extra, writer=writer
    )
    hb.tick()

    assert len(writes) == 1
    assert writes[0]["boards_done"] == 1
    assert "rows_so_far" not in writes[0]


def test_emit_forces_write_bypassing_throttle() -> None:
    """emit() (seed at start / finalize at end) always writes, ignoring the interval."""
    clock = FakeClock()
    writes, writer = _recording_writer()
    hb = ProgressHeartbeat("x.json", total=5, interval_s=90.0, clock=clock, writer=writer)

    hb.emit()  # seed 0/5
    hb.emit()  # immediate again despite no interval elapsing
    assert len(writes) == 2
    assert writes[0] == {"boards_done": 0, "boards_total": 5, "elapsed_s": 0.0}


def test_atomic_write_json_roundtrips(tmp_path: Path) -> None:
    """The default writer persists valid JSON at the target path (temp+rename, no torn file)."""
    path = tmp_path / "sub" / "crawl-progress.json"  # parent created on demand
    atomic_write_json(path, {"boards_done": 3, "boards_total": 9})

    assert json.loads(path.read_text()) == {"boards_done": 3, "boards_total": 9}
    assert not list(path.parent.glob("*.tmp"))  # temp file was renamed away, none left behind


def test_end_to_end_over_run_pool(tmp_path: Path) -> None:
    """Synthetic crawl: run_pool's on_result drives tick(); the json ends at 100% with real rows.

    Uses interval_s=0 so every completion flushes, proving the on_result wiring reaches disk and
    that boards_done reflects completed items (== the pool's processed count)."""
    path = tmp_path / "crawl-progress.json"
    total = 25
    rows = {"n": 0}

    async def scenario() -> None:
        hb = ProgressHeartbeat(
            path,
            total=total,
            interval_s=0.0,  # flush on every tick for a deterministic end state
            extra=lambda: {"rows_so_far": rows["n"]},
        )
        hb.emit()  # seed 0/total

        async def handler(i: int) -> int:
            rows["n"] += i  # stand-in for "rows written by this board"
            return i

        async def on_result(_outcome: object) -> None:
            hb.tick()

        stats = await run_pool(range(total), handler, concurrency=4, on_result=on_result)
        hb.emit()
        assert stats.processed == total

    anyio.run(scenario)

    snap = json.loads(path.read_text())
    assert snap["boards_done"] == total  # every completed item ticked exactly once
    assert snap["boards_total"] == total
    assert snap["rows_so_far"] == sum(range(total))
