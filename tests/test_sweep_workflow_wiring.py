"""The sweep job must run with a deadline under its own timeout, or a slow shard writes nothing."""

from __future__ import annotations

from pathlib import Path

import yaml

_WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "freshness-sweep.yml"


def test_sweep_step_has_a_deadline_under_the_job_timeout() -> None:
    d = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    on = d.get("on") or d[True]
    inp = on["workflow_dispatch"]["inputs"]["deadline_minutes"]
    job = d["jobs"]["sweep"]
    step = next(s for s in job["steps"] if "scripts.freshness_sweep" in (s.get("run") or ""))
    assert "--deadline-minutes" in step["run"] and "--sr-confirm-rate" in step["run"]
    assert float(inp["default"]) < job["timeout-minutes"]
