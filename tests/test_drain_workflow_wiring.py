"""drain-detail must run AFTER build-index publishes, never race it.

The drain can only fill JD for rows in the PUBLISHED index. On its own 09:30 cron it started
five hours after the 04:17 build — which takes five hours — and on 2026-09-10 lost that race by
seven minutes, draining yesterday's index while 379,744 freshly published rows sat unseen. Chaining
it to build-index's completion makes the ordering structural.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    doc = yaml.safe_load((_WF / name).read_text(encoding="utf-8"))
    doc["on"] = doc.get("on") or doc[True]  # PyYAML reads a bare `on:` key as boolean True
    return doc


def test_drain_is_chained_to_build_index_not_a_clock() -> None:
    d = _load("drain-detail.yml")
    assert "schedule" not in d["on"], "an independent cron is exactly the race this closes"
    run = d["on"]["workflow_run"]
    assert run["workflows"] == ["build-index"]
    assert run["types"] == ["completed"]
    assert _load("build-index.yml")["name"] == "build-index"


def test_every_job_skips_when_the_build_did_not_publish() -> None:
    """A failed build keeps the previous index; draining it again is wasted, and a skipped drain
    must not let merge run on zero shards and page ops."""
    d = _load("drain-detail.yml")
    for name, job in d["jobs"].items():
        cond = job.get("if", "")
        assert "workflow_run.conclusion == 'success'" in cond, f"{name} lacks the success guard"


def test_retry_budget_reset_is_opt_in() -> None:
    d = _load("drain-detail.yml")
    inp = d["on"]["workflow_dispatch"]["inputs"]["reset_attempts"]
    assert inp["type"] == "boolean" and inp["default"] is False
    steps = d["jobs"]["drain"]["steps"]
    reset = next(s for s in steps if "reset_detail_attempts" in (s.get("run") or ""))
    assert "reset_attempts" in reset.get("if", ""), "the reset would defeat RETRY_CAP every run"
